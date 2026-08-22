"""
Data models for data extracted from Amazon product pages.

These dataclasses are deliberately plain (no network, no I/O) so they can be
unit-tested in isolation and serialised to JSON for SQLite storage.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Offer:
    """A single structured bank / card offer.

    Amazon surfaces offers in many shapes (instant discount, cashback, EMI ...).
    We normalise what we can confidently read into structured fields and keep
    the raw ``description`` for everything else.
    """

    bank: Optional[str] = None
    offer_type: str = "unknown"
    # offer_type ∈ {instant_discount, cashback, no_cost_emi, emi, gift, unknown}
    discount_percent: Optional[float] = None
    discount_amount: Optional[float] = None
    maximum_discount: Optional[float] = None
    minimum_purchase: Optional[float] = None
    card_type: Optional[str] = None        # credit / debit / prepaid ...
    card_network: Optional[str] = None     # Visa / Mastercard / RuPay ...
    description: str = ""
    expiry: Optional[str] = None

    # ----- helpers -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Offer":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def signature(self) -> tuple:
        """A stable tuple identifying the *meaningful* content of the offer.

        Two offers with the same signature are considered equal for
        change-detection purposes (ignoring cosmetic description wording).
        """
        return (
            (self.bank or "").strip().lower(),
            (self.offer_type or "").strip().lower(),
            round(self.discount_percent, 2) if self.discount_percent is not None else None,
            round(self.discount_amount, 2) if self.discount_amount is not None else None,
            round(self.maximum_discount, 2) if self.maximum_discount is not None else None,
            round(self.minimum_purchase, 2) if self.minimum_purchase is not None else None,
            (self.card_type or "").strip().lower(),
            (self.card_network or "").strip().lower(),
        )

    def hash(self) -> str:
        """Short stable hash used as the offer_history.offer_hash."""
        return hashlib.sha1(
            json.dumps(self.to_dict(), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]

    def headline(self) -> str:
        """One-line human summary used in notifications."""
        parts: list[str] = []
        if self.bank:
            parts.append(self.bank)
        if self.card_type:
            parts.append(self.card_type.title() + " Card")
        who = " ".join(parts) if parts else "Bank Offer"

        if self.offer_type == "instant_discount" and self.discount_percent:
            desc = f"{int(self.discount_percent) if self.discount_percent.is_integer() else self.discount_percent}% instant discount"
        elif self.offer_type == "cashback" and self.discount_percent:
            desc = f"{int(self.discount_percent) if self.discount_percent.is_integer() else self.discount_percent}% cashback"
        elif self.discount_amount:
            desc = f"₹{int(self.discount_amount)} off"
        elif self.offer_type == "no_cost_emi":
            desc = "No Cost EMI"
        elif self.offer_type == "emi":
            desc = "EMI offer"
        else:
            desc = self.description or "offer"

        tail = []
        if self.maximum_discount:
            tail.append(f"Max ₹{int(self.maximum_discount)}")
        if self.minimum_purchase:
            tail.append(f"Min purchase ₹{int(self.minimum_purchase)}")
        tail_str = f" ({', '.join(tail)})" if tail else ""
        return f"{who}: {desc}{tail_str}"


@dataclass
class PriceInfo:
    """Pricing fields extracted for a single observation."""

    selling_price: Optional[float] = None
    mrp: Optional[float] = None
    deal_price: Optional[float] = None
    coupon_amount: Optional[float] = None
    coupon_percent: Optional[float] = None
    subscribe_save_price: Optional[float] = None

    def effective_price(self) -> Optional[float]:
        """Effective price after an *immediately applicable* coupon only.

        We deliberately do NOT subtract bank discounts / cashback here because
        those are card-conditional. Effective price = selling - coupon.
        Returns None if there is no known selling price.
        """
        if self.selling_price is None:
            return None
        eff = self.selling_price
        if self.coupon_amount:
            eff -= self.coupon_amount
        elif self.coupon_percent:
            eff -= self.selling_price * (self.coupon_percent / 100.0)
        return round(max(eff, 0.0), 2)

    def effective_price_with_offer(self, offer: Optional[Offer]) -> Optional[float]:
        """Effective price if a specific bank offer applies.

        Used to show e.g. "HDFC effective price" without replacing the headline
        price. Only subtracts clearly quantifiable instant discounts / cashback.
        """
        base = self.effective_price()
        if base is None:
            return None
        discount = 0.0
        if offer is None:
            return base
        if offer.discount_percent and offer.offer_type in ("instant_discount", "cashback"):
            discount = base * (offer.discount_percent / 100.0)
            if offer.maximum_discount:
                discount = min(discount, offer.maximum_discount)
        elif offer.discount_amount:
            discount = offer.discount_amount
            if offer.maximum_discount:
                discount = min(discount, offer.maximum_discount)
        return round(max(base - discount, 0.0), 2)

    def discount_amount(self) -> Optional[float]:
        if self.mrp is not None and self.selling_price is not None:
            return round(self.mrp - self.selling_price, 2)
        return None

    def discount_percent(self) -> Optional[float]:
        if self.mrp and self.selling_price and self.mrp > self.selling_price:
            return round((self.mrp - self.selling_price) / self.mrp * 100.0, 2)
        return None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["effective_price"] = self.effective_price()
        d["discount_amount"] = self.discount_amount()
        d["discount_percent"] = self.discount_percent()
        return d


@dataclass
class AvailabilityInfo:
    in_stock: bool = True
    status_text: str = "In Stock"
    seller: Optional[str] = None
    fulfilled_by_amazon: bool = False
    is_amazon_seller: bool = False
    delivery: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProductObservation:
    """The complete extracted snapshot of a product at one moment in time."""

    asin: str
    url: str
    title: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    variant: Optional[str] = None
    price: PriceInfo = field(default_factory=PriceInfo)
    availability: AvailabilityInfo = field(default_factory=AvailabilityInfo)
    offers: list[Offer] = field(default_factory=list)
    currency: str = "INR"
    parsed_at: str = ""
    # Whether the parser is confident the *price* is real (not a 0 / placeholder).
    price_confident: bool = False
    # Whether the offers block was confidently read (vs "could not detect").
    offers_confident: bool = False
    # Free-form extraction notes / warnings (not surfaced to users as failures).
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asin": self.asin,
            "url": self.url,
            "title": self.title,
            "brand": self.brand,
            "category": self.category,
            "image_url": self.image_url,
            "rating": self.rating,
            "review_count": self.review_count,
            "variant": self.variant,
            "price": self.price.to_dict(),
            "availability": self.availability.to_dict(),
            "offers": [o.to_dict() for o in self.offers],
            "currency": self.currency,
            "parsed_at": self.parsed_at,
            "price_confident": self.price_confident,
            "offers_confident": self.offers_confident,
            "notes": self.notes,
        }


@dataclass
class ScrapeResult:
    """Outcome of a single scraping attempt."""

    observation: Optional[ProductObservation] = None
    error: Optional[str] = None
    status_code: Optional[int] = None
    blocked: bool = False            # True if CAPTCHA / bot-detection page
    response_time: Optional[float] = None
    used_fallback: bool = False      # True if a fallback parser/layer was used
