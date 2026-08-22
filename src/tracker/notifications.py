"""
ntfy notification system.

Sends notifications through the ntfy.sh protocol (works with the public server
or any self-hosted instance). All connection details come from environment /
GitHub secrets via :class:`Settings`.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import requests

from amazon.models import Offer

from .config import Settings
from .models import ChangeEvent, ChangeType, Classification, PriceStats
from .statistics import format_date, format_price

log = logging.getLogger("tracker.notifications")


# String priority labels (used in config) -> ntfy numeric priority.
# ntfy JSON publishing uses ints: 1=min, 2=low, 3=default, 4=high, 5=max.
PRIORITY_MAP: dict[str, int] = {
    "min": 1, "minimum": 1,
    "low": 2,
    "default": 3, "normal": 3,
    "high": 4,
    "max": 5, "urgent": 5, "emergency": 5,
}


def _priority_value(priority: str) -> int:
    return PRIORITY_MAP.get(str(priority).strip().lower(), 3)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
class NtfySender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(
        self,
        title: str,
        message: str,
        priority: str = "default",
        tags: Optional[list[str]] = None,
        click: Optional[str] = None,
        icon: Optional[str] = None,
    ) -> bool:
        cfg = self.settings.ntfy
        if not cfg.configured:
            log.warning("ntfy not configured - skipping notification: %s", title)
            return False

        # Publish as JSON to the ntfy ROOT URL (not the topic URL).
        # This keeps the whole payload as UTF-8 (emoji/rupee safe) and avoids
        # HTTP-header Latin-1 restrictions that crash on emoji titles.
        base = (cfg.server or "").rstrip("/")
        url = f"{base}/"
        payload: dict[str, object] = {
            "topic": cfg.topic,
            "title": title[:250],
            "message": message,
            "priority": _priority_value(priority),
        }
        if tags:
            payload["tags"] = [t for t in tags if t]
        if click:
            payload["click"] = click
        if icon:
            payload["icon"] = icon

        headers = {"Content-Type": "application/json"}
        if cfg.token:
            headers["Authorization"] = f"Bearer {cfg.token}"

        try:
            resp = requests.post(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                timeout=15,
            )
            if resp.status_code in (200, 201, 202):
                log.info("ntfy sent: %s", title)
                return True
            log.error("ntfy error %s: %s", resp.status_code, resp.text[:200])
            return False
        except requests.RequestException as exc:
            log.error("ntfy request failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------
def _history_block(stats: Optional[PriceStats], tracked_since: Optional[str]) -> list[str]:
    if not stats:
        return []
    lines = [
        "Historical:",
        f"  Low: {format_price(stats.min)}",
        f"  High: {format_price(stats.max)}",
        f"  Average: {format_price(stats.average)}",
    ]
    if stats.avg_30d:
        lines.append(f"  30-day avg: {format_price(stats.avg_30d)}")
    if tracked_since:
        lines.append(f"Tracked since: {format_date(tracked_since)}")
    return lines


def _status_line(stats: Optional[PriceStats], classification: Optional[Classification]) -> str:
    if not stats or stats.num_observations < 2:
        return "Status: Insufficient historical data"
    if classification and classification.label != "INSUFFICIENT":
        return f"Status: {classification.label.replace('_', ' ')}"
    return "Status: Insufficient historical data"


def _price_section(obs, label: str = "Current") -> list[str]:
    lines: list[str] = []
    sp = obs.price.selling_price
    if sp is not None:
        lines.append(f"{label}:\n  {format_price(sp)}")
    if obs.price.coupon_amount:
        eff = obs.price.effective_price()
        lines.append(f"Coupon: {format_price(obs.price.coupon_amount)} OFF")
        if eff is not None:
            lines.append(f"Effective price: {format_price(eff)}")
    elif obs.price.coupon_percent:
        eff = obs.price.effective_price()
        lines.append(f"Coupon: {obs.price.coupon_percent:.0f}% OFF")
        if eff is not None:
            lines.append(f"Effective price: {format_price(eff)}")
    return lines


def _offer_lines(offers: list[Offer]) -> list[str]:
    if not offers:
        return ["No bank offer detected"]
    return [o.headline() for o in offers]


def build_message(event: ChangeEvent) -> tuple[str, str, str, list[str], Optional[str]]:
    """Build (title, message, priority, tags, click_url) for a ChangeEvent."""
    ct = event.change_type
    p = event.product
    obs = event.observation
    prev = event.previous_observation
    stats = event.stats
    cls = event.classification
    name = p.name or p.id or "Product"
    url = obs.url if obs else (p.canonical_url or p.url)

    title = ""
    tags: list[str] = []
    body: list[str] = [name, ""]

    if ct == ChangeType.PRICE_DROP:
        title = "📉 Amazon Price Drop"
        tags = ["chart_with_downwards_trend", "money_with_wings"]
        old, new = prev.price.selling_price if prev else None, obs.price.selling_price
        delta = (new - old) if (old is not None and new is not None) else None
        pct = (delta / old * 100.0) if (delta is not None and old) else None
        lines = []
        if old is not None and new is not None:
            lines.append(f"{format_price(old)} → {format_price(new)}")
            if delta is not None:
                lines.append(f"Drop: {format_price(abs(delta))} ({abs(pct):.2f}%)")
        body += lines + [""] + _price_section(obs) + [""] + _history_block(stats, stats.first_seen if stats else None)
        body.append(_status_line(stats, cls))

    elif ct == ChangeType.PRICE_INCREASE:
        title = "📈 Amazon Price Increase"
        tags = ["chart_with_upwards_trend"]
        old, new = prev.price.selling_price if prev else None, obs.price.selling_price
        delta = (new - old) if (old is not None and new is not None) else None
        pct = (delta / old * 100.0) if (delta is not None and old) else None
        lines = []
        if old is not None and new is not None:
            lines.append(f"{format_price(old)} → {format_price(new)}")
            if delta is not None:
                lines.append(f"Increase: {format_price(delta)} ({pct:.2f}%)")
        body += lines + [""] + _price_section(obs) + [""] + _history_block(stats, stats.first_seen if stats else None)
        body.append(_status_line(stats, cls))

    elif ct in (ChangeType.BANK_OFFER_ADDED, ChangeType.BANK_OFFER_CHANGED):
        title = "🏦 Amazon Offer Updated" if ct == ChangeType.BANK_OFFER_CHANGED else "🏦 Amazon Offer Added"
        tags = ["bank", "credit_card"]
        if ct == ChangeType.BANK_OFFER_ADDED:
            body.append("NEW OFFER")
        body += [""] + _offer_lines(obs.offers) + [""]
        if not obs.offers_confident:
            body.append("⚠️ Bank offer could not be reliably detected during this check.")
        body += [""] + _price_section(obs)
        body.append(_status_line(stats, cls))

    elif ct == ChangeType.BANK_OFFER_REMOVED:
        title = "🏦 Amazon Offer Removed"
        tags = ["bank"]
        removed = event.detail.get("removed_offers", [])
        for line in _offer_lines(removed):
            body.append(line)
        body += ["", "is no longer detected.", ""]
        if not obs.offers_confident:
            body.append("⚠️ Offer detection is uncertain - verify on Amazon.")
        body += _price_section(obs)

    elif ct == ChangeType.COUPON_ADDED:
        title = "🎟️ Amazon Coupon Added"
        tags = ["tickets"]
        body += ["Coupon added:", _coupon_line(obs)] + ["", "Price:"] + _price_section(obs, "Price")

    elif ct == ChangeType.COUPON_REMOVED:
        title = "🎟️ Amazon Coupon Removed"
        tags = ["tickets"]
        body += ["Coupon removed.", ""] + _price_section(obs, "Price")

    elif ct == ChangeType.COUPON_CHANGED:
        title = "🎟️ Amazon Coupon Changed"
        tags = ["tickets"]
        old_c = event.detail.get("prev_coupon")
        body += [f"Coupon changed: {old_c} → {_coupon_line(obs)}"] + [""] + _price_section(obs, "Price")

    elif ct == ChangeType.BACK_IN_STOCK:
        title = "🟢 Back in Stock"
        tags = ["package", "white_check_mark"]
        body += _price_section(obs)
        body.append(_status_line(stats, cls))

    elif ct == ChangeType.OUT_OF_STOCK:
        title = "🔴 Out of Stock"
        tags = ["x"]
        body += ["Product is no longer in stock."]

    elif ct == ChangeType.TARGET_PRICE_REACHED:
        title = "🎯 Target Price Reached"
        tags = ["dart", "tada"]
        body += [
            f"Target: {format_price(p.target_price)}",
            f"Current: {format_price(obs.price.selling_price)}",
            f"Historical low: {format_price(stats.min) if stats else 'N/A'}",
            "",
            "This product has reached your target price.",
        ]

    elif ct == ChangeType.SELLER_CHANGED:
        title = "🛒 Seller Changed"
        tags = ["shopping_cart"]
        prev_seller = event.detail.get("prev_seller", "?")
        new_seller = obs.availability.seller or "?"
        body += [f"{prev_seller} → {new_seller}"]

    elif ct == ChangeType.NEW_TRACKED_LOW:
        title = "🔥 NEW TRACKED LOW"
        tags = ["fire", "chart_with_downwards_trend"]
        prev_low = event.detail.get("prev_low")
        body += [
            f"Current: {format_price(obs.price.selling_price)}",
            "",
            f"Previous tracked low: {format_price(prev_low)}",
            f"New low by: {format_price((prev_low or 0) - (obs.price.selling_price or 0))}",
            "",
            "This is the lowest price recorded by this tracker"
            + (f" since {format_date(stats.first_seen)}." if stats and stats.first_seen else "."),
            "(Not necessarily Amazon's all-time low.)",
        ]

    elif ct == ChangeType.NEW_TRACKED_HIGH:
        title = "📈 NEW TRACKED HIGH"
        tags = ["chart_with_upwards_trend"]
        prev_high = event.detail.get("prev_high")
        body += [
            f"Current: {format_price(obs.price.selling_price)}",
            "",
            f"Previous tracked high: {format_price(prev_high)}",
            "",
            "Highest price recorded since tracking began"
            + (f" ({format_date(stats.first_seen)})." if stats and stats.first_seen else "."),
        ]

    elif ct == ChangeType.CHECK_FAILED:
        title = "⚠️ Amazon Check Failed"
        tags = ["warning"]
        body += ["The latest check could not extract reliable product data.",
                 f"Product: {name}", f"URL: {url}",
                 "", "No price has been updated. The previous price is retained."]
        if event.detail.get("error"):
            body.append(f"Reason: {event.detail['error']}")

    else:
        title = f"ℹ️ {name}"
        body += ["Update"]

    # Footer link.
    body += ["", f"Amazon: {url}"]

    priority = event.detail.get(
        "priority", (stats and _priority_for(cls)) or "default"
    )
    priority = "default"  # let the tracker set real priority; default here
    priority = event.detail.get("priority") or _event_default_priority(ct)

    message = "\n".join(str(b) for b in body if b is not None)
    return title, message, priority, tags, url


def _coupon_line(obs) -> str:
    if obs.price.coupon_amount:
        return f"{format_price(obs.price.coupon_amount)} OFF"
    if obs.price.coupon_percent:
        return f"{obs.price.coupon_percent:.0f}% OFF"
    return "No coupon"


def _priority_for(cls: Optional[Classification]) -> str:
    if not cls:
        return "default"
    if cls.label in ("VERY_LOW", "LOW"):
        return "high"
    return "default"


def _event_default_priority(ct: ChangeType) -> str:
    return {
        ChangeType.PRICE_DROP: "high",
        ChangeType.PRICE_INCREASE: "default",
        ChangeType.BANK_OFFER_ADDED: "high",
        ChangeType.BANK_OFFER_REMOVED: "default",
        ChangeType.BANK_OFFER_CHANGED: "default",
        ChangeType.COUPON_ADDED: "default",
        ChangeType.COUPON_REMOVED: "low",
        ChangeType.COUPON_CHANGED: "low",
        ChangeType.BACK_IN_STOCK: "high",
        ChangeType.OUT_OF_STOCK: "urgent",
        ChangeType.TARGET_PRICE_REACHED: "urgent",
        ChangeType.SELLER_CHANGED: "low",
        ChangeType.NEW_TRACKED_LOW: "high",
        ChangeType.NEW_TRACKED_HIGH: "default",
        ChangeType.CHECK_FAILED: "low",
    }.get(ct, "default")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def send_event(sender: NtfySender, event: ChangeEvent, settings: Settings) -> bool:
    """Build + send a single event notification."""
    title, message, default_prio, tags, click = build_message(event)
    priority = settings.priority_for(event.change_type.value) or default_prio
    icon = event.observation.image_url if event.observation else None
    return sender.send(
        title=title,
        message=message,
        priority=priority,
        tags=tags,
        click=click,
        icon=icon,
    )
