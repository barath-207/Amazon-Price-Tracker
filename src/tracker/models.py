"""
Domain models for the tracker layer (configuration, change events, stats).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional

from amazon.models import Offer, PriceInfo, ProductObservation


@dataclass
class HistoryConfig:
    """Classification thresholds - shared by config + statistics modules."""

    classification_enabled: bool = True
    minimum_observations: int = 10
    range_thresholds: dict[str, float] = field(
        default_factory=lambda: {
            "very_low": 0.10,
            "low": 0.30,
            "high": 0.70,
            "very_high": 0.90,
        }
    )


class ChangeType(str, Enum):
    PRICE_DROP = "PRICE_DROP"
    PRICE_INCREASE = "PRICE_INCREASE"
    BANK_OFFER_ADDED = "BANK_OFFER_ADDED"
    BANK_OFFER_REMOVED = "BANK_OFFER_REMOVED"
    BANK_OFFER_CHANGED = "BANK_OFFER_CHANGED"
    COUPON_ADDED = "COUPON_ADDED"
    COUPON_REMOVED = "COUPON_REMOVED"
    COUPON_CHANGED = "COUPON_CHANGED"
    BACK_IN_STOCK = "BACK_IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    TARGET_PRICE_REACHED = "TARGET_PRICE_REACHED"
    SELLER_CHANGED = "SELLER_CHANGED"
    NEW_TRACKED_LOW = "NEW_TRACKED_LOW"
    NEW_TRACKED_HIGH = "NEW_TRACKED_HIGH"
    CHECK_FAILED = "CHECK_FAILED"


@dataclass
class ProductConfig:
    """A product as declared in ``config/products.yaml``."""

    url: str
    id: Optional[str] = None
    name: Optional[str] = None
    enabled: bool = True
    target_price: Optional[float] = None
    notify_on_any_price_change: Optional[bool] = None
    notify_on_offer_change: Optional[bool] = None
    notify_on_coupon_change: Optional[bool] = None
    min_drop_percent: Optional[float] = None
    # resolved at load time
    asin: Optional[str] = None
    canonical_url: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PriceRow:
    """A row from the price_history table."""

    product_id: str
    timestamp: str
    selling_price: Optional[float]
    mrp: Optional[float]
    effective_price: Optional[float]
    coupon_amount: Optional[float]
    availability: str
    in_stock: int
    seller: Optional[str]
    fulfilled_by_amazon: int
    variant: Optional[str]


@dataclass
class PriceStats:
    """Historical statistics for a single product."""

    current: Optional[float] = None
    previous: Optional[float] = None
    first: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None
    average: Optional[float] = None
    median: Optional[float] = None
    min_7d: Optional[float] = None
    max_7d: Optional[float] = None
    min_30d: Optional[float] = None
    max_30d: Optional[float] = None
    min_90d: Optional[float] = None
    max_90d: Optional[float] = None
    avg_30d: Optional[float] = None
    min_timestamp: Optional[str] = None
    max_timestamp: Optional[str] = None
    change_from_previous: Optional[float] = None
    pct_change_from_previous: Optional[float] = None
    pct_change_from_first: Optional[float] = None
    pct_from_low: Optional[float] = None
    pct_from_high: Optional[float] = None
    num_changes: int = 0
    num_drops: int = 0
    num_increases: int = 0
    num_observations: int = 0
    days_tracked: int = 0
    percentile: Optional[float] = None
    first_seen: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Classification:
    """LOW / HIGH / etc classification plus context."""

    label: str  # VERY_LOW / LOW / NORMAL / HIGH / VERY_HIGH / INSUFFICIENT
    method_range: Optional[str] = None
    method_percentile: Optional[str] = None
    percentile_rank: Optional[float] = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChangeEvent:
    """A detected change to be turned into a notification."""

    change_type: ChangeType
    product: ProductConfig
    observation: Optional[ProductObservation] = None
    previous_observation: Optional[ProductObservation] = None
    stats: Optional[PriceStats] = None
    classification: Optional[Classification] = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_type": self.change_type.value,
            "product": self.product.to_dict(),
            "detail": self.detail,
        }
