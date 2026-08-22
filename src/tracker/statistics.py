"""
Historical statistics + Low/Normal/High classification.

All computations are based ONLY on data collected by this tracker. We never
claim knowledge of Amazon's price history before tracking started.
"""
from __future__ import annotations

import statistics as pystats
from datetime import datetime, timedelta, timezone
from typing import Optional

from .database import Database
from .models import Classification, HistoryConfig, PriceStats


def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _median(values: list[float]) -> Optional[float]:
    return pystats.median(values) if values else None


def _window_minmax(rows, days: int) -> tuple[Optional[float], Optional[float]]:
    """Min/max selling price within the last ``days`` days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    window: list[float] = []
    for r in rows:
        ts = _parse_ts(r.timestamp)
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff and r.selling_price is not None:
            window.append(r.selling_price)
    if not window:
        return None, None
    return min(window), max(window)


def compute_stats(db: Database, product_id: str) -> PriceStats:
    """Compute the full PriceStats for a product from its stored history."""
    rows = db.price_history(product_id)
    stats = PriceStats(num_observations=len(rows))

    priced = [r.selling_price for r in rows if r.selling_price is not None]
    if not priced:
        # No usable prices yet (e.g. only failed checks recorded elsewhere).
        return stats

    stats.first = priced[0]
    stats.current = priced[-1]
    if len(priced) >= 2:
        stats.previous = priced[-2]

    stats.min = min(priced)
    stats.max = max(priced)
    stats.average = round(sum(priced) / len(priced), 2)
    stats.median = round(_median(priced), 2) if _median(priced) is not None else None

    # Timestamps of min/max (first occurrence).
    for r in rows:
        if r.selling_price == stats.min and stats.min_timestamp is None:
            stats.min_timestamp = r.timestamp
        if r.selling_price == stats.max and stats.max_timestamp is None:
            stats.max_timestamp = r.timestamp

    # Rolling windows.
    stats.min_7d, stats.max_7d = _window_minmax(rows, 7)
    stats.min_30d, stats.max_30d = _window_minmax(rows, 30)
    stats.min_90d, stats.max_90d = _window_minmax(rows, 90)

    # 30-day average.
    cutoff30 = datetime.now(timezone.utc) - timedelta(days=30)
    w30: list[float] = []
    for r in rows:
        ts = _parse_ts(r.timestamp)
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff30 and r.selling_price is not None:
            w30.append(r.selling_price)
    if w30:
        stats.avg_30d = round(sum(w30) / len(w30), 2)

    # Changes.
    num_changes = num_drops = num_increases = 0
    for i in range(1, len(priced)):
        if priced[i] != priced[i - 1]:
            num_changes += 1
            if priced[i] < priced[i - 1]:
                num_drops += 1
            else:
                num_increases += 1
    stats.num_changes = num_changes
    stats.num_drops = num_drops
    stats.num_increases = num_increases

    # Delta vs previous.
    if stats.previous is not None and stats.current is not None:
        stats.change_from_previous = round(stats.current - stats.previous, 2)
        if stats.previous:
            stats.pct_change_from_previous = round(
                (stats.current - stats.previous) / stats.previous * 100.0, 2
            )
    # Delta vs first.
    if stats.first and stats.current is not None:
        stats.pct_change_from_first = round(
            (stats.current - stats.first) / stats.first * 100.0, 2
        )
    # Distance from low/high.
    if stats.min and stats.current is not None and stats.min != stats.max:
        rng = stats.max - stats.min
        stats.pct_from_low = round((stats.current - stats.min) / rng * 100.0, 2)
        stats.pct_from_high = round((stats.max - stats.current) / rng * 100.0, 2)

    # Percentile rank of the current price.
    sorted_p = sorted(priced)
    if len(sorted_p) > 1 and stats.current is not None:
        below = sum(1 for p in sorted_p if p < stats.current)
        stats.percentile = round(below / (len(sorted_p) - 1) * 100.0, 1)

    # Days tracked.
    first_ts = _parse_ts(rows[0].timestamp) if rows else None
    last_ts = _parse_ts(rows[-1].timestamp) if rows else None
    if first_ts and last_ts:
        if first_ts.tzinfo is None:
            first_ts = first_ts.replace(tzinfo=timezone.utc)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        stats.days_tracked = max(1, (last_ts - first_ts).days + 1)
        stats.first_seen = rows[0].timestamp

    return stats


def classify(stats: PriceStats, cfg: HistoryConfig) -> Classification:
    """Classify current price into LOW / NORMAL / HIGH bands.

    Requires a minimum number of observations AND a meaningful spread
    (min != max). Otherwise reports INSUFFICIENT.
    """
    if (
        not cfg.classification_enabled
        or stats.num_observations < cfg.minimum_observations
        or stats.current is None
        or stats.min is None
        or stats.max is None
        or stats.min == stats.max
    ):
        return Classification(
            label="INSUFFICIENT",
            reason=(
                f"Only {stats.num_observations} observation(s). Need "
                f"{cfg.minimum_observations} for a meaningful classification."
            ),
        )

    rng = stats.max - stats.min
    pos = (stats.current - stats.min) / rng if rng > 0 else 0.5
    t = cfg.range_thresholds

    if pos <= t["very_low"]:
        label = "VERY_LOW"
    elif pos <= t["low"]:
        label = "LOW"
    elif pos < t["high"]:
        label = "NORMAL"
    elif pos < t["very_high"]:
        label = "HIGH"
    else:
        label = "VERY_HIGH"

    # Percentile-based secondary label.
    pct_label: Optional[str] = None
    if stats.percentile is not None:
        if stats.percentile <= 10:
            pct_label = "VERY_LOW"
        elif stats.percentile <= 30:
            pct_label = "LOW"
        elif stats.percentile < 70:
            pct_label = "NORMAL"
        elif stats.percentile < 90:
            pct_label = "HIGH"
        else:
            pct_label = "VERY_HIGH"

    return Classification(
        label=label,
        method_range=f"{pos*100:.0f}% of range",
        method_percentile=pct_label,
        percentile_rank=stats.percentile,
        reason=f"Range position {pos*100:.0f}% (percentile {stats.percentile}{'th' if stats.percentile is not None else ''}).",
    )


def format_price(value: Optional[float], currency: str = "₹") -> str:
    """Format a price with Indian-style thousands separators."""
    if value is None:
        return "N/A"
    if abs(value - round(value)) < 1e-9:
        return f"{currency}{int(round(value)):,}"
    return f"{currency}{value:,.2f}"


def format_date(ts: Optional[str]) -> str:
    parsed = _parse_ts(ts) if ts else None
    if not parsed:
        return "N/A"
    return parsed.strftime("%d %b %Y")
