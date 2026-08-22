"""
Bank / card offer extraction.

Amazon surfaces offers inconsistently - sometimes in structured sections,
sometimes only in rendered text, and sometimes they are personalized /
dynamically loaded. We take a layered, heuristic approach:

  1. Scan recognised bank names and offer keywords in the visible text.
  2. For each match, extract structured numbers (discount %, max discount,
     min purchase) from the surrounding text with targeted regexes.
  3. Normalise into :class:`Offer` records.

Crucially we return a ``confidence`` flag. When the offers section cannot be
located at all we report ``offers_confident=False`` rather than claiming "no
offer exists" - because the absence may simply be a parsing/region artefact.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .models import Offer


# Known Indian bank names (lowercased, matched as substrings).
BANKS = [
    "hdfc", "icici", "sbi", "axis", "kotak", "citibank", "citi bank",
    "rbl", "idfc", "idbi", "yes bank", "indusind", "indusind", "federal",
    "bob", "bank of baroda", "pnb", "punjab national", "canara",
    "american express", "amex", "onecard", "one card", "au small",
    "standard chartered", "stan chart", "hsbc", "dbs",
]

CARD_NETWORKS = ["visa", "mastercard", "rupay", "amex", "diners", "maestro"]
CARD_TYPES = ["credit", "debit", "prepaid", "emi"]


def _rupee_number(text: str) -> Optional[float]:
    """Extract the first ₹/Rs/-prefixed number from text (Indian comma format)."""
    if not text:
        return None
    m = re.search(r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*\.?[0-9]*)", text, re.IGNORECASE)
    if m:
        return _to_float(m.group(1))
    # bare number
    m = re.search(r"([0-9][0-9,]*\.?[0-9]*)", text)
    if m:
        return _to_float(m.group(1))
    return None


def _to_float(numstr: str) -> Optional[float]:
    try:
        return float(numstr.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _percent(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) if m else None


def _classify_offer_type(text: str) -> str:
    low = text.lower()
    if "no cost emi" in low or "no-cost emi" in low:
        return "no_cost_emi"
    if "instant discount" in low or "instantdiscount" in low:
        return "instant_discount"
    if "cashback" in low or "cash back" in low:
        return "cashback"
    if "emi" in low:
        return "emi"
    if "discount" in low:
        return "instant_discount"
    return "unknown"


def _find_bank(text: str) -> Optional[str]:
    low = text.lower()
    for b in BANKS:
        if b in low:
            # Title-case the matched bank for nicer display.
            return _title_bank(b)
    return None


def _title_bank(key: str) -> str:
    mapping = {
        "hdfc": "HDFC Bank",
        "icici": "ICICI Bank",
        "sbi": "SBI Bank",
        "axis": "Axis Bank",
        "kotak": "Kotak Bank",
        "citibank": "CitiBank",
        "citi bank": "CitiBank",
        "rbl": "RBL Bank",
        "idfc": "IDFC First Bank",
        "idbi": "IDBI Bank",
        "yes bank": "Yes Bank",
        "indusind": "IndusInd Bank",
        "federal": "Federal Bank",
        "bob": "Bank of Baroda",
        "bank of baroda": "Bank of Baroda",
        "pnb": "Punjab National Bank",
        "punjab national": "Punjab National Bank",
        "canara": "Canara Bank",
        "american express": "American Express",
        "amex": "American Express",
        "onecard": "OneCard",
        "one card": "OneCard",
        "au small": "AU Small Finance Bank",
        "standard chartered": "Standard Chartered",
        "stan chart": "Standard Chartered",
        "hsbc": "HSBC",
        "dbs": "DBS Bank",
    }
    return mapping.get(key, key.upper())


def _find_card_network(text: str) -> Optional[str]:
    low = text.lower()
    for n in CARD_NETWORKS:
        if n in low:
            return n.capitalize()
    return None


def _find_card_type(text: str) -> Optional[str]:
    low = text.lower()
    for t in CARD_TYPES:
        if re.search(rf"\b{re.escape(t)}\b", low):
            return t
    return None


def extract_offer_from_block(block: str) -> Optional[Offer]:
    """Build an Offer from a single offer text block.

    Returns None if the block does not look like a real offer.
    """
    block = block.strip()
    if len(block) < 6:
        return None

    offer_type = _classify_offer_type(block)
    bank = _find_bank(block)

    # Must reference a bank/card or an explicit offer keyword to count.
    has_keyword = any(
        k in block.lower()
        for k in (
            "instant discount", "cashback", "cash back", "no cost emi",
            "emi", "off on", "discount", "% off", "save",
        )
    )
    if bank is None and not has_keyword:
        return None

    offer = Offer(
        bank=bank,
        offer_type=offer_type,
        discount_percent=_percent(block),
        discount_amount=_rupee_number(block) if (_percent(block) is None) else None,
        card_type=_find_card_type(block),
        card_network=_find_card_network(block),
        description=re.sub(r"\s+", " ", block)[:300],
    )

    # Pull "up to ₹X" / "maximum ₹X" style caps.
    max_match = re.search(
        r"(?:up to|max(?:imum)?|maximum discount of)\s*(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*)",
        block, re.IGNORECASE,
    )
    if max_match:
        offer.maximum_discount = _to_float(max_match.group(1))

    min_match = re.search(
        r"(?:min(?:imum)?(?:\s*order|\s*purchase|\s*transaction)?(?:\s*value)?\s*(?:of)?|on\s*(?:orders?\s*)?(?:above|over))\s*(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*)",
        block, re.IGNORECASE,
    )
    if min_match:
        offer.minimum_purchase = _to_float(min_match.group(1))

    # Prefer the explicit "maximum" figure as a cleaner discount amount fallback.
    if offer.discount_amount is None and offer.maximum_discount is not None:
        pass  # keep discount_amount None; max is separate

    return offer


def extract_offers(
    visible_text: str,
    offer_sections: Optional[list[str]] = None,
) -> tuple[list[Offer], bool]:
    """Extract offers from page text.

    Parameters
    ----------
    visible_text:
        The full visible (non-script) text of the page.
    offer_sections:
        Optional list of specific offer-section text blocks (e.g. pulled from
        known offer container nodes). When present these are scanned first and
        given priority.

    Returns
    -------
    (offers, offers_confident)
        ``offers_confident`` is True when at least one clearly-parseable offer
        block was found OR when an offers section was present but genuinely
        empty. It is False when we simply could not locate any offers area,
        meaning "we don't know whether there is an offer".
    """
    if not visible_text:
        return [], False

    raw_blocks: list[str] = []
    section_seen = False

    if offer_sections:
        section_seen = True
        for s in offer_sections:
            # Split a section into sub-blocks on common delimiters.
            for piece in re.split(r"(?:\n|•|\u2022|;|(?<=[.])\s{2,})", s):
                piece = piece.strip()
                if piece:
                    raw_blocks.append(piece)

    # Heuristic global scan: find lines mentioning offer keywords + a bank.
    keyword_re = re.compile(
        r"(instant\s*discount|cashback|cash\s*back|no[\s-]*cost\s*emi|emi\s+(?:option|available))",
        re.IGNORECASE,
    )
    for line in visible_text.splitlines():
        line = line.strip()
        if keyword_re.search(line) or _find_bank(line):
            if line not in raw_blocks:
                raw_blocks.append(line)

    # If we found a dedicated offers area or any bank/keyword, we can be
    # reasonably confident about our detection result (positive OR negative).
    offers_confident = section_seen or bool(raw_blocks)

    # De-duplicate and build Offer objects.
    seen_sigs: set[tuple] = set()
    offers: list[Offer] = []
    for block in raw_blocks:
        offer = extract_offer_from_block(block)
        if offer is None:
            continue
        sig = offer.signature()
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        offers.append(offer)

    # Drop offers that carry no usable numeric/keyword signal at all.
    offers = [
        o for o in offers
        if o.discount_percent is not None
        or o.discount_amount is not None
        or o.maximum_discount is not None
        or o.offer_type in ("no_cost_emi",)
    ]

    return offers, offers_confident


def offer_sets_equal(prev: list[Offer], curr: list[Offer]) -> bool:
    """True when the two offer sets are identical (order-independent)."""
    return {o.hash() for o in prev} == {o.hash() for o in curr}
