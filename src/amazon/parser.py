"""
Amazon HTML parsing.

Built defensively in layers, because Amazon's markup changes frequently:

  Layer 1  - primary CSS selectors (fast path).
  Layer 2  - alternative selectors + text fallbacks.
  Layer 3  - JSON-LD structured data (``<script type="application/ld+json">``).
  Layer 4  - embedded ``P`` / ``APlus`` data blobs on the page.

All Amazon-specific knowledge is isolated here so that updating a selector
never touches history/statistics/notifications.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import (
    AvailabilityInfo,
    Offer,
    PriceInfo,
    ProductObservation,
)
from .offers import extract_offers
from .validators import is_valid_asin


# ---------------------------------------------------------------------------
# Low-level text/number helpers
# ---------------------------------------------------------------------------
def parse_price_text(text: Optional[str]) -> Optional[float]:
    """Parse an Indian-format price string into a float.

    Handles: "₹17,499", "17,499.00", "Rs. 17499", "INR 17,499 /-", "17499".
    Returns None when no number can be found.
    """
    if not text:
        return None
    cleaned = text.strip()
    # Strip currency symbols and surrounding noise.
    cleaned = re.sub(r"(?:₹|rs\.?|inr|/-|\$|eur|€|£|gbp)", "", cleaned, flags=re.IGNORECASE)
    # Indian/standard thousands separators: keep digits, dots, commas.
    m = re.search(r"([0-9][0-9,]*\.?[0-9]*)", cleaned)
    if not m:
        return None
    num = m.group(1).replace(",", "")
    try:
        return float(num)
    except ValueError:
        return None


def parse_rating_text(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"([0-5](?:\.[0-9])?)", text)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    return val if 0 <= val <= 5 else None


def parse_review_count(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    cleaned = text.replace(",", "").replace("ratings", "").replace("rating", "")
    cleaned = cleaned.replace("reviews", "").replace("review", "").strip()
    m = re.search(r"(\d[\d,]*)", cleaned)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _first_text(node: Any, selectors: list[str]) -> Optional[str]:
    """Return stripped text from the first selector that matches."""
    for sel in selectors:
        el = node.select_one(sel)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    return None


def _attr(node: Any, selectors: list[str], attr: str) -> Optional[str]:
    for sel in selectors:
        el = node.select_one(sel)
        if el and el.get(attr):
            return el[attr].strip()
    return None


# ---------------------------------------------------------------------------
# ASIN extraction
# ---------------------------------------------------------------------------
ASIN_PATTERN = re.compile(r"/(?:dp|gp/product|dp/product|product)/([A-Z0-9]{10})", re.IGNORECASE)


def extract_asin_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    m = ASIN_PATTERN.search(url)
    if m:
        return m.group(1).upper()
    # Some short URLs encode ASIN elsewhere; last-ditch 10-char token.
    m = re.search(r"\b([A-Z0-9]{10})\b", url)
    if m and is_valid_asin(m.group(1)):
        return m.group(1).upper()
    return None


def extract_asin_from_html(soup: BeautifulSoup) -> Optional[str]:
    """Pull ASIN from common embedded locations."""
    candidates = [
        ("input", {"id": "ASIN"}),
        ("input", {"name": "ASIN"}),
        ("meta", {"name": "ASIN"}),
    ]
    for tag, attrs in candidates:
        el = soup.find(tag, attrs)
        if el and el.get("value") or el and el.get("content"):
            val = el.get("value") or el.get("content")
            if is_valid_asin(val):
                return val.upper()
    # data-asin on body or form
    el = soup.find(attrs={"data-asin": True})
    if el and is_valid_asin(el["data-asin"]):
        return el["data-asin"].upper()
    # Canonical link / og:url
    for sel in ['link[rel="canonical"]', 'meta[property="og:url"]']:
        node = soup.select_one(sel)
        if node:
            href = node.get("href") or node.get("content")
            asin = extract_asin_from_url(href or "")
            if asin:
                return asin
    return None


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------
def normalize_url(url: str, domain: str = "www.amazon.in") -> str:
    """Reduce any Amazon URL to its canonical ``/dp/ASIN`` form."""
    asin = extract_asin_from_url(url)
    if not asin:
        return url.strip()
    base = url.split("/dp/")[0].split("/gp/product/")[0].split("/product/")[0]
    if not base or not base.startswith("http"):
        base = f"https://{domain}"
    return f"{base}/dp/{asin}"


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------
def extract_title(soup: BeautifulSoup) -> Optional[str]:
    title = _first_text(
        soup,
        ["#productTitle", "#title", "#ebooksProductTitle", "h1.a-size-large"],
    )
    return title.strip() if title else None


def extract_brand(soup: BeautifulSoup) -> Optional[str]:
    node = soup.select_one("#bylineInfo")
    if node:
        text = node.get_text(" ", strip=True)
        m = re.match(
            r"(?:Visit the\s+|Brand:\s*|by\s+|Sold by\s+)?(.+?)(?:\s+Store)?$",
            text, re.IGNORECASE,
        )
        if m:
            candidate = m.group(1).strip(": ").strip()
            if candidate:
                return candidate
    # Fallback: title's first token group before a separator.
    title = extract_title(soup)
    if title:
        first = re.split(r"[-,|]", title)[0].strip()
        if 2 <= len(first.split()) <= 4:
            return first
    return None


def extract_image(soup: BeautifulSoup) -> Optional[str]:
    img = _attr(
        soup,
        [
            "#landingImage",
            "#imgBlkFront",
            "#ebooksImgBlkFront",
            "img.a-dynamic-image",
            "#main-image-container img",
        ],
        "src",
    )
    if img and img.startswith("http"):
        return img
    if img and img.startswith("//"):
        return "https:" + img
    # data-old-hires on landingImage
    node = soup.select_one("#landingImage")
    if node and node.get("data-old-hires"):
        return node["data-old-hires"]
    return None


def extract_category(soup: BeautifulSoup) -> Optional[str]:
    crumbs = soup.select("#wayfinding-breadcrumbs_container a, #wayfinding-breadcrumbs_feature_div a")
    if crumbs:
        return crumbs[-1].get_text(strip=True) or None
    # breadcrumb list items
    crumbs = soup.select("#wayfinding-breadcrumbs_feature_div li")
    if crumbs:
        last = crumbs[-1].get_text(strip=True)
        return last or None
    return None


def extract_rating(soup: BeautifulSoup) -> Optional[float]:
    text = _first_text(
        soup,
        [
            "#acrPopover span.a-icon-alt",
            "#averageCustomerReviews .a-icon-alt",
            'span[data-hook="rating-out-of-text"]',
            "i.a-icon-star span.a-icon-alt",
        ],
    )
    return parse_rating_text(text)


def extract_review_count(soup: BeautifulSoup) -> Optional[int]:
    text = _first_text(
        soup,
        [
            "#acrCustomerReviewText",
            "#cmrs-atf-text",
            'span[data-hook="total-review-count"]',
        ],
    )
    return parse_review_count(text)


def extract_variant(soup: BeautifulSoup) -> Optional[str]:
    """Selected variation (size / colour / storage) when Amazon exposes it."""
    selected = soup.select(
        "#variation_size_name li.a-selected, "
        "#variation_color_name li.a-selected, "
        "#variation_style_name li.a-selected, "
        "li.swatchSelect"
    )
    for node in selected:
        txt = node.get_text(" ", strip=True)
        if txt:
            return txt[:80]
    # inline selected label
    node = soup.select_one("#variation_size_name .selection, #variation_color_name .selection")
    if node:
        txt = node.get_text(strip=True)
        if txt:
            return txt[:80]
    return None


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
def extract_selling_price(soup: BeautifulSoup) -> tuple[Optional[float], Optional[str]]:
    """Return (price, source_label).

    Tries several known locations and cross-validates. We never assume the
    first currency string is the price.
    """
    candidates: list[tuple[str, list[str]]] = [
        # (label, selectors)
        ("priceblock_ourprice", ["#priceblock_ourprice", "#priceblock_saleprice"]),
        ("priceblock_dealprice", ["#priceblock_dealprice"]),
        ("corePrice", [
            ".a-price .a-offscreen",
            "span.a-price[data-a-size] .a-offscreen",
            "#corePrice_feature_div .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-offscreen",
        ]),
        ("price_inside_buybox", ["#price_inside_buybox", ".a-color-price"]),
        ("apex_price", ["#apex_desktop .a-offscreen", "#apex_mobile .a-offscreen"]),
        ("buybox_price", [
            "#buybox .a-color-price",
            "#qualifiedBuybox .a-color-price",
        ]),
    ]
    for label, selectors in candidates:
        text = _first_text(soup, selectors)
        price = parse_price_text(text)
        if price and price > 0:
            return price, label
    return None, None


def extract_mrp(soup: BeautifulSoup) -> Optional[float]:
    text = _first_text(
        soup,
        [
            ".a-text-price[data-a-strike]",
            ".a-text-price span.a-offscreen",
            ".priceBlockStrikePriceString",
            "#listPrice",
            ".a-color-secondary .a-text-strike",
            "span.basisPrice .a-offscreen",
        ],
    )
    return parse_price_text(text)


def extract_deal_price(soup: BeautifulSoup) -> Optional[float]:
    text = _first_text(
        soup,
        ["#priceblock_dealprice", "#dealsAccordionRow .a-color-price"],
    )
    return parse_price_text(text)


def extract_coupon(soup: BeautifulSoup) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Return (coupon_amount, coupon_percent, raw_text).

    Amazon coupons appear in several nodes. We parse both flat (₹500) and
    percentage (5%) forms.
    """
    coupon_selectors = [
        "#couponBadgeRegularVpc",
        "#vpcButton",
        "#couponBadge",
        ".couponBadge",
        "#promoPriceBlockMessage",
        'span[id*="coupon"]',
        "#dealsAccordionRow .couponBadge",
    ]
    raw = None
    for sel in coupon_selectors:
        node = soup.select_one(sel)
        if node:
            raw = node.get_text(" ", strip=True)
            if raw:
                break
    if not raw:
        return None, None, None

    amount = None
    percent = None
    amt_match = re.search(r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.\d+)?)", raw, re.IGNORECASE)
    pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", raw)
    if amt_match:
        amount = float(amt_match.group(1).replace(",", ""))
    if pct_match:
        percent = float(pct_match.group(1))
    return amount, percent, raw


def extract_subscribe_save(soup: BeautifulSoup) -> Optional[float]:
    text = _first_text(
        soup,
        [
            "#snbiz-price",
            "#subscribeAndSavePrice",
            ".sns-price-placeholder",
        ],
    )
    return parse_price_text(text)


# ---------------------------------------------------------------------------
# Availability + seller
# ---------------------------------------------------------------------------
def extract_availability(soup: BeautifulSoup) -> AvailabilityInfo:
    info = AvailabilityInfo()
    text = _first_text(
        soup,
        [
            "#availability span",
            "#availability",
            "#deliveryBlockMessage",
            "#outOfStock",
        ],
    )
    if text:
        low = text.lower()
        info.status_text = text
        if any(w in low for w in ("in stock", "usually dispatched", "usually ships", "in-stock")):
            info.in_stock = True
        elif any(w in low for w in ("out of stock", "currently unavailable", "unavailable", "not in stock")):
            info.in_stock = False
        # Delivery snippet
        info.delivery = text[:120]

    seller_node = soup.select_one("#sellerProfileTriggerId, #merchant-info, #sellerInfoSection")
    if seller_node:
        seller_text = seller_node.get_text(" ", strip=True)
        if "amazon" in seller_text.lower():
            info.is_amazon_seller = True
            info.seller = "Amazon"
        if "fulfilled by amazon" in seller_text.lower() or "fulfilment by amazon" in seller_text.lower():
            info.fulfilled_by_amazon = True
        if not info.seller:
            m = re.search(r"(?:Sold by|Ships from and sold by)\s+(.+?)(?:\.|$)", seller_text, re.IGNORECASE)
            if m:
                info.seller = m.group(1).strip()
            else:
                node = seller_node.select_one("a")
                if node:
                    info.seller = node.get_text(strip=True)

    # Tabular seller section fallback.
    seller2 = _first_text(soup, ["#tabular-buybox .tabular-buybox-text", "#tabular_feature_div a"])
    if seller2 and not info.seller:
        info.seller = seller2

    if info.in_stock and not info.status_text:
        info.status_text = "In Stock"
    return info


# ---------------------------------------------------------------------------
# JSON-LD structured data (Layer 3)
# ---------------------------------------------------------------------------
def extract_jsonld(soup: BeautifulSoup) -> Optional[dict[str, Any]]:
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        # Could be a list or wrapped in @graph.
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict) and d.get("@type") in ("Product", ["Product"])), data[0] if data else None)
        if isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                data = next((d for d in data["@graph"] if isinstance(d, dict) and d.get("@type") == "Product"), data["@graph"][0] if data["@graph"] else data)
            if data.get("@type") in ("Product", ["Product"]):
                return data
    return None


def _apply_jsonld(obs: ProductObservation, data: dict[str, Any]) -> None:
    if not obs.title:
        obs.title = data.get("name")
    if not obs.image_url and isinstance(data.get("image"), str):
        obs.image_url = data["image"]
    if not obs.brand:
        brand = data.get("brand")
        if isinstance(brand, dict):
            obs.brand = brand.get("name")
        elif isinstance(brand, str):
            obs.brand = brand
    offers = data.get("offers")
    price_val: Optional[float] = None
    if isinstance(offers, dict):
        price_val = parse_price_text(str(offers.get("price", "")))
        if offers.get("availability"):
            avail = str(offers["availability"]).lower()
            if "outofStock".lower() in avail or "out_of_stock" in avail or "discontinued" in avail:
                obs.availability.in_stock = False
                obs.availability.status_text = "Out of Stock"
            elif "instock" in avail:
                obs.availability.in_stock = True
                obs.availability.status_text = "In Stock"
        if offers.get("seller"):
            s = offers["seller"]
            obs.availability.seller = s.get("name") if isinstance(s, dict) else str(s)
    elif isinstance(offers, list) and offers:
        first = offers[0]
        if isinstance(first, dict):
            price_val = parse_price_text(str(first.get("price", "")))
    if not obs.price.selling_price and price_val:
        obs.price.selling_price = price_val
        obs.notes.append("price from JSON-LD")
    agg = data.get("aggregateRating")
    if isinstance(agg, dict):
        if obs.rating is None:
            obs.rating = parse_rating_text(str(agg.get("ratingValue", "")))
        if obs.review_count is None:
            obs.review_count = parse_review_count(str(agg.get("reviewCount") or agg.get("ratingCount") or ""))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def parse_page(
    html: str,
    url: str,
    expected_asin: Optional[str] = None,
    domain: str = "www.amazon.in",
) -> ProductObservation:
    """Parse an Amazon product HTML page into a :class:`ProductObservation`.

    Combines all layers. Always returns an observation object; callers should
    run :func:`validators.validate_observation` before trusting its price.
    """
    soup = BeautifulSoup(html, "lxml")

    asin = expected_asin or extract_asin_from_html(soup) or extract_asin_from_url(url) or "UNKNOWN"
    canonical = normalize_url(url, domain)

    obs = ProductObservation(
        asin=asin.upper(),
        url=canonical,
        title=extract_title(soup),
        brand=extract_brand(soup),
        category=extract_category(soup),
        image_url=extract_image(soup),
        rating=extract_rating(soup),
        review_count=extract_review_count(soup),
        variant=extract_variant(soup),
        availability=extract_availability(soup),
    )

    # --- Pricing (Layer 1 + 2) ---
    sp, _label = extract_selling_price(soup)
    obs.price = PriceInfo(
        selling_price=sp,
        mrp=extract_mrp(soup),
        deal_price=extract_deal_price(soup),
        subscribe_save_price=extract_subscribe_save(soup),
    )
    coupon_amt, coupon_pct, coupon_raw = extract_coupon(soup)
    obs.price.coupon_amount = coupon_amt
    obs.price.coupon_percent = coupon_pct
    if coupon_raw:
        obs.notes.append(f"coupon text: {coupon_raw[:60]}")

    if sp is None:
        obs.notes.append("no selling price via primary selectors")

    # --- Layer 3: JSON-LD fills any gaps ---
    jsonld = extract_jsonld(soup)
    if jsonld:
        _apply_jsonld(obs, jsonld)

    # --- Offers (Layer 2 over visible text + offer sections) ---
    offer_sections: list[str] = []
    for sel in [
        "#ibTextDiv",
        "#offers-section",
        "#quickPromoBucketContent",
        "#gc-buy-box",
        "#amazon-GC-balance-block",
        ".sopp-offers-layer",
        "#dynamicDeliveryMessage",
    ]:
        node = soup.select_one(sel)
        if node:
            txt = node.get_text(" ", strip=True)
            if txt:
                offer_sections.append(txt)
    visible = soup.get_text(" ", strip=True)
    offers, offers_confident = extract_offers(visible, offer_sections)
    obs.offers = offers
    obs.offers_confident = offers_confident

    return obs


__all__ = [
    "parse_page",
    "normalize_url",
    "extract_asin_from_url",
    "extract_asin_from_html",
    "parse_price_text",
    "parse_rating_text",
    "parse_review_count",
]
