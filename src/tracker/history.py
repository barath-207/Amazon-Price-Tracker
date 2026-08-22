"""
History bookkeeping.

Bridges the scraper output (``ProductObservation``) with the database, decides
whether to record a new observation, and surfaces the previous observation so
the tracker can detect changes.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from amazon.models import Offer, PriceInfo, ProductObservation
from amazon.offers import offer_sets_equal

from .database import Database, utc_now

log = logging.getLogger("tracker.history")


def price_effective(price: PriceInfo) -> Optional[float]:
    return price.effective_price()


def last_offers(db: Database, product_id: str) -> list[Offer]:
    return db.last_offers(product_id)


def store_observation(
    db: Database,
    product_id: str,
    obs: ProductObservation,
    store_every_check: bool = True,
) -> tuple[bool, Optional[ProductObservation], Optional[PriceInfo], list[Offer]]:
    """Persist a valid observation, returning whether it represents a change.

    Returns ``(price_changed, prev_obs_proxy, prev_price_info, prev_offers)``.
    """
    ts = utc_now()
    prev_row = db.last_price_row(product_id)
    prev_offers = db.last_offers(product_id)

    # Decide whether something materially changed vs the last stored check.
    price_changed = True
    if prev_row is not None:
        if (
            prev_row.selling_price == obs.price.selling_price
            and prev_row.coupon_amount == obs.price.coupon_amount
            and prev_row.availability == obs.availability.status_text
            and bool(prev_row.in_stock) == obs.availability.in_stock
            and (prev_row.seller or "") == (obs.availability.seller or "")
        ):
            price_changed = False

    offers_changed = not offer_sets_equal(prev_offers, obs.offers)
    anything_changed = price_changed or offers_changed

    if (not anything_changed) and (not store_every_check):
        log.info("%s: no change; not recording (store_every_check=false)", product_id)
        return False, None, None, prev_offers

    # Record the observation.
    row_id = db.add_price_observation(
        product_id=product_id,
        asin=obs.asin,
        timestamp=ts,
        selling_price=obs.price.selling_price,
        mrp=obs.price.mrp,
        deal_price=obs.price.deal_price,
        coupon_amount=obs.price.coupon_amount,
        coupon_percent=obs.price.coupon_percent,
        effective_price=price_effective(obs.price),
        availability=obs.availability.status_text,
        in_stock=obs.availability.in_stock,
        seller=obs.availability.seller,
        fulfilled_by_amazon=obs.availability.fulfilled_by_amazon,
        variant=obs.variant,
        raw_json=json.dumps(obs.to_dict(), default=str),
    )

    # Offers are stored on every change set, linked to their price row.
    if obs.offers_confident and anything_changed:
        db.add_offers(product_id, ts, obs.offers, price_history_id=row_id)

    # Build a lightweight "previous observation" proxy from the prior row for
    # change detection in the tracker layer.
    prev_price_info: Optional[PriceInfo] = None
    if prev_row is not None:
        prev_price_info = PriceInfo(
            selling_price=prev_row.selling_price,
            coupon_amount=prev_row.coupon_amount,
        )

    return price_changed, None, prev_price_info, prev_offers


def record_failed_check(db: Database, product_id: str, error: str) -> None:
    db.add_check(product_id, utc_now(), "failed", error, None, changed=False)


def record_successful_check(db: Database, product_id: str, changed: bool,
                            response_time: Optional[float]) -> None:
    db.add_check(product_id, utc_now(), "success", None, response_time, changed=changed)
    db.set_last_checked(product_id)
