"""Tests for the history/database layer and change detection."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from amazon.models import AvailabilityInfo, Offer, PriceInfo, ProductObservation
from amazon.offers import offer_sets_equal

from tracker.database import utc_now
from tracker.history import store_observation


def _obs(price=17499.0, coupon=None, offers=None, in_stock=True, seller="Amazon"):
    return ProductObservation(
        asin="B0BDHWDR12", url="https://www.amazon.in/dp/B0BDHWDR12",
        title="Samsung Monitor",
        price=PriceInfo(selling_price=price, coupon_amount=coupon),
        availability=AvailabilityInfo(in_stock=in_stock, status_text="In Stock" if in_stock else "Out of Stock", seller=seller),
        offers=offers or [],
        offers_confident=True,
    )


def test_store_first_observation(tmp_db):
    obs = _obs()
    price_changed, _, _, prev_offers = store_observation(tmp_db, "p1", obs, store_every_check=True)
    # First observation counts as "changed" (no prior).
    assert price_changed is True
    assert prev_offers == []
    assert tmp_db.observation_count("p1") == 1


def test_store_no_change_with_heartbeat(tmp_db):
    obs = _obs()
    store_observation(tmp_db, "p1", obs, store_every_check=True)
    price_changed, _, _, _ = store_observation(tmp_db, "p1", obs, store_every_check=True)
    assert price_changed is False
    # store_every_check=True still records the row.
    assert tmp_db.observation_count("p1") == 2


def test_store_only_on_change(tmp_db):
    obs = _obs()
    store_observation(tmp_db, "p1", obs, store_every_check=False)
    price_changed, _, _, _ = store_observation(tmp_db, "p1", obs, store_every_check=False)
    assert price_changed is False
    # No new row because nothing changed.
    assert tmp_db.observation_count("p1") == 1


def test_price_change_detected(tmp_db):
    store_observation(tmp_db, "p1", _obs(17499.0), store_every_check=False)
    price_changed, _, _, _ = store_observation(tmp_db, "p1", _obs(16999.0), store_every_check=False)
    assert price_changed is True
    assert tmp_db.observation_count("p1") == 2


def test_offer_change_detected(tmp_db):
    o1 = [Offer(bank="HDFC Bank", offer_type="instant_discount", discount_percent=10.0, maximum_discount=1500.0)]
    store_observation(tmp_db, "p1", _obs(offers=o1), store_every_check=True)
    # Different offer set.
    o2 = [Offer(bank="ICICI Bank", offer_type="instant_discount", discount_percent=10.0, maximum_discount=1500.0)]
    price_changed, _, _, prev_offers = store_observation(tmp_db, "p1", _obs(offers=o2), store_every_check=True)
    assert offer_sets_equal(prev_offers, o1) is True
    assert tmp_db.last_offers("p1")[0].bank == "ICICI Bank"


def test_target_price_state_persistence(tmp_db):
    tmp_db.set_state("p1", "below_target", "0")
    assert tmp_db.get_state("p1", "below_target") == "0"
    tmp_db.set_state("p1", "below_target", "1")
    assert tmp_db.get_state("p1", "below_target") == "1"


def test_check_recording(tmp_db):
    tmp_db.add_check("p1", utc_now(), "success", None, 1.23, changed=True)
    tmp_db.add_check("p1", utc_now(), "failed", "timeout", None, changed=False)
    rows = tmp_db.price_history("p1")
    assert rows == []
