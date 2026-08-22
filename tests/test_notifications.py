"""Tests for notification message building (without hitting the network)."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from amazon.models import AvailabilityInfo, Offer, PriceInfo, ProductObservation

from tracker.config import Settings
from tracker.models import ChangeEvent, ChangeType, ProductConfig
from tracker.notifications import (
    build_message,
    NtfySender,
    PRIORITY_MAP,
    _priority_value,
)


def _product():
    return ProductConfig(id="p1", name="Samsung Monitor", url="https://www.amazon.in/dp/B0BDHWDR12",
                         canonical_url="https://www.amazon.in/dp/B0BDHWDR12", asin="B0BDHWDR12")


def _obs(price=17499.0):
    return ProductObservation(
        asin="B0BDHWDR12", url="https://www.amazon.in/dp/B0BDHWDR12",
        title="Samsung Monitor",
        price=PriceInfo(selling_price=price, coupon_amount=500.0, mrp=21999.0),
        availability=AvailabilityInfo(in_stock=True, status_text="In Stock", seller="Amazon"),
    )


def _prev(price=18999.0):
    p = ProductObservation(asin="B0BDHWDR12", url="https://www.amazon.in/dp/B0BDHWDR12")
    p.price.selling_price = price
    return p


def test_price_drop_message():
    event = ChangeEvent(
        change_type=ChangeType.PRICE_DROP, product=_product(),
        observation=_obs(17499.0), previous_observation=_prev(18999.0),
    )
    title, message, priority, tags, click = build_message(event)
    assert "Price Drop" in title
    assert "₹18,999" in message
    assert "₹17,499" in message
    assert "₹500 OFF" in message
    assert click == "https://www.amazon.in/dp/B0BDHWDR12"


def test_price_increase_message():
    event = ChangeEvent(
        change_type=ChangeType.PRICE_INCREASE, product=_product(),
        observation=_obs(18999.0), previous_observation=_prev(17499.0),
    )
    title, message, priority, tags, click = build_message(event)
    assert "Price Increase" in title
    assert "₹17,499" in message


def test_target_price_message():
    product = _product()
    product.target_price = 15000
    event = ChangeEvent(
        change_type=ChangeType.TARGET_PRICE_REACHED, product=product,
        observation=_obs(14999.0),
    )
    title, message, priority, tags, click = build_message(event)
    assert "Target Price" in title
    assert "₹15,000" in message
    assert priority == "urgent"


def test_back_in_stock_message():
    event = ChangeEvent(
        change_type=ChangeType.BACK_IN_STOCK, product=_product(),
        observation=_obs(),
    )
    title, message, priority, tags, click = build_message(event)
    assert "Back in Stock" in title


def test_offer_added_message():
    obs = _obs()
    obs.offers = [Offer(bank="HDFC Bank", offer_type="instant_discount",
                        discount_percent=10.0, maximum_discount=1500.0)]
    event = ChangeEvent(
        change_type=ChangeType.BANK_OFFER_ADDED, product=_product(), observation=obs,
    )
    title, message, priority, tags, click = build_message(event)
    assert "Offer" in title
    assert "HDFC Bank" in message


def test_coupon_added_message():
    event = ChangeEvent(
        change_type=ChangeType.COUPON_ADDED, product=_product(), observation=_obs(),
    )
    title, message, priority, tags, click = build_message(event)
    assert "Coupon" in title
    assert "₹500 OFF" in message


def test_new_tracked_low_message():
    event = ChangeEvent(
        change_type=ChangeType.NEW_TRACKED_LOW, product=_product(),
        observation=_obs(16499.0), detail={"prev_low": 16999.0},
    )
    title, message, priority, tags, click = build_message(event)
    assert "NEW TRACKED LOW" in title
    assert "lowest price recorded by this tracker" in message
    assert "Not necessarily Amazon's all-time low" in message


def test_sender_no_config_skips():
    settings = Settings()  # no ntfy configured
    sender = NtfySender(settings)
    sent = sender.send(title="t", message="m")
    assert sent is False


def test_check_failed_message():
    event = ChangeEvent(
        change_type=ChangeType.CHECK_FAILED, product=_product(),
        detail={"error": "CAPTCHA detected"},
    )
    title, message, priority, tags, click = build_message(event)
    assert "Check Failed" in title
    assert "CAPTCHA" in message
    assert "No price has been updated" in message
    assert "previous price is retained" in message


# ---------------------------------------------------------------------------
# Regression: emoji / non-ASCII must NOT crash ntfy sending.
# Previously `send()` put the raw emoji title into an HTTP header, and Python's
# requests/urllib3 raised UnicodeEncodeError (latin-1 codec). Now we publish as
# JSON, so emoji + rupee signs survive intact.
# ---------------------------------------------------------------------------
def test_priority_value_mapping():
    assert _priority_value("urgent") == 5
    assert _priority_value("max") == 5
    assert _priority_value("high") == 4
    assert _priority_value("default") == 3
    assert _priority_value("low") == 2
    assert _priority_value("min") == 1
    assert _priority_value("unknown-label") == 3  # falls back to default


def test_send_uses_json_publishing_with_emoji_title():
    """The crash repro: emoji in the title must not raise, and the request must
    carry a JSON body to the ntfy ROOT url with the emoji preserved."""
    settings = Settings()
    settings.ntfy.server = "https://ntfy.sh"
    settings.ntfy.topic = "test-topic"
    sender = NtfySender(settings)

    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch("tracker.notifications.requests.post", side_effect=fake_post):
        ok = sender.send(
            title="✅ Amazon Tracker test notification",
            message="Emoji + ₹1,499 work",
            priority="high",
            tags=["white_check_mark"],
            click="https://example.com",
            icon="https://example.com/i.png",
        )

    assert ok is True
    # Posted to ROOT url, not the topic url.
    assert captured["url"].endswith("/")
    assert "/test-topic" not in captured["url"]
    assert captured["headers"]["Content-Type"] == "application/json"

    # The JSON body preserves the emoji + rupee losslessly.
    import json
    payload = json.loads(captured["data"])
    assert payload["title"] == "✅ Amazon Tracker test notification"
    assert "₹1,499" in payload["message"]
    assert payload["topic"] == "test-topic"
    assert payload["priority"] == 4
    assert payload["tags"] == ["white_check_mark"]
    assert payload["click"] == "https://example.com"
    assert payload["icon"] == "https://example.com/i.png"


def test_send_priority_map_covers_config_labels():
    # Every default label used in settings.yaml must map cleanly.
    for label in ("default", "low", "high", "urgent"):
        assert _priority_value(label) in PRIORITY_MAP.values()
