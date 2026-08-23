"""
Validation helpers that guard the historical database from bad observations.

Amazon pages can change, break, or return CAPTCHAs. Before any price is
written to history it must pass these checks so a parse failure is never
misinterpreted as a real (e.g. ₹0) price change.
"""
from __future__ import annotations

import re
from typing import Optional

from .models import ProductObservation


# --- ASIN ----------------------------------------------------------------
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def is_valid_asin(asin: Optional[str]) -> bool:
    """An ASIN is a 10-char alphanumeric string. Books use ISBN-10 (may contain
    digits/X); products use uppercase alphanumerics."""
    if not asin:
        return False
    return bool(_ASIN_RE.match(asin.strip().upper()))


# --- Price plausibility --------------------------------------------------
# Sanity bounds (INR). We never store a price of 0 or an absurd outlier.
ABSOLUTE_MIN_PRICE = 1.0
ABSOLUTE_MAX_PRICE = 50_000_000.0  # 50 lakh - generous upper guard


def is_plausible_price(price: Optional[float]) -> bool:
    """A price is plausible if it is a positive, finite number in a sane range."""
    if price is None:
        return False
    try:
        p = float(price)
    except (TypeError, ValueError):
        return False
    if p != p:  # NaN
        return False
    return ABSOLUTE_MIN_PRICE <= p <= ABSOLUTE_MAX_PRICE


# --- Observation validation ----------------------------------------------
def validate_observation(obs: ProductObservation, expected_asin: Optional[str] = None) -> tuple[bool, list[str]]:
    """Validate an observation before it is allowed to touch the history.

    Returns ``(ok, reasons)``. When ``ok`` is False the caller must NOT record
    a price change - the observation is treated as a failed parse.
    """
    reasons: list[str] = []

    if not is_valid_asin(obs.asin):
        reasons.append(f"invalid ASIN: {obs.asin!r}")

    if expected_asin and obs.asin.upper() != expected_asin.upper():
        # The returned page is for a different product - reject.
        reasons.append(f"ASIN mismatch: expected {expected_asin}, got {obs.asin}")

    sp = obs.price.selling_price
    if sp is None:
        reasons.append("no selling price found")
    elif not is_plausible_price(sp):
        reasons.append(f"implausible selling price: {sp}")
        obs.price_confident = False

    if not obs.title or len(obs.title.strip()) < 5:
        reasons.append("missing or too-short product title")

    # If MRP exists it must be >= selling price (allow tiny epsilon for rounding).
    if (
        is_plausible_price(sp)
        and obs.price.mrp is not None
        and is_plausible_price(obs.price.mrp)
        and obs.price.mrp + 0.01 < sp
    ):
        # MRP below selling price is almost always a parse error.
        reasons.append(
            f"MRP {obs.price.mrp} below selling price {sp} - likely parse error"
        )
        obs.price_confident = False

    # Guard against duplicated/glued MRP values (e.g. 4499+4499 -> 44994499).
    # A genuine MRP is rarely more than ~50x the selling price.
    if (
        is_plausible_price(sp)
        and obs.price.mrp is not None
        and obs.price.mrp > sp * 50
    ):
        if hasattr(obs, "notes"):
            obs.notes.append(f"dropping implausible MRP {obs.price.mrp} (vs price {sp})")
        obs.price.mrp = None

    ok = len(reasons) == 0
    if ok:
        obs.price_confident = True
    return ok, reasons


def looks_like_captcha(html: str) -> bool:
    """Heuristic detection of Amazon bot-detection / CAPTCHA pages."""
    if not html:
        return False
    low = html.lower()
    needles = (
        "captcha",
        "robot check",
        "automated access",
        "api-services-support@amazon",
        "enter the characters you see below",
        "sorry, we just need to make sure you're not a bot",
    )
    return any(n in low for n in needles)


def looks_like_not_found(html: str) -> bool:
    """Heuristic detection of a missing / removed product page."""
    if not html:
        return True
    low = html.lower()
    needles = (
        "no results for",
        "page not found",
        "we're sorry. the web address you entered is not",
        "looking for something?",
        "dogsofamazon",
    )
    # A real product page always has a title block; absence is suspicious.
    has_title = "producttitle" in low or "ppd" in low
    return any(n in low for n in needles) and not has_title
