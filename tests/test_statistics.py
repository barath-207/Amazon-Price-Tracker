"""Tests for statistics computation and price classification."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tracker.config import HistoryConfig
from tracker.database import utc_now
from tracker.models import PriceStats
from tracker.statistics import classify, compute_stats, format_price


def _seed_prices(tmp_db, product_id, prices):
    """Insert a sequence of prices spaced ~1 day apart."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, p in enumerate(prices):
        ts = (base + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
        tmp_db.add_price_observation(product_id, "ASIN000001", ts, selling_price=p)


def test_compute_stats_basic(tmp_db):
    _seed_prices(tmp_db, "p1", [1000, 1200, 900, 1100, 950])
    stats = compute_stats(tmp_db, "p1")
    assert stats.num_observations == 5
    assert stats.min == 900
    assert stats.max == 1200
    assert stats.current == 950
    assert stats.previous == 1100
    assert stats.first == 1000
    assert stats.average == 1030.0
    assert stats.change_from_previous == -150.0
    assert stats.num_changes == 4
    assert stats.num_drops == 2
    assert stats.num_increases == 2


def test_percentile_calculation(tmp_db):
    # current is the lowest -> percentile should be 0.
    _seed_prices(tmp_db, "p1", [100, 200, 300, 400, 50])
    stats = compute_stats(tmp_db, "p1")
    assert stats.percentile == 0.0


def test_format_price():
    assert format_price(17499.0) == "₹17,499"
    assert format_price(16999.5) == "₹16,999.50"
    assert format_price(None) == "N/A"


def test_classify_insufficient_history(tmp_db):
    _seed_prices(tmp_db, "p1", [1000])  # only 1 observation
    stats = compute_stats(tmp_db, "p1")
    cfg = HistoryConfig(minimum_observations=10)
    cls = classify(stats, cfg)
    assert cls.label == "INSUFFICIENT"


def test_classify_low(tmp_db):
    prices = [100, 100, 100, 100, 100, 100, 100, 100, 100, 90]  # current near low
    _seed_prices(tmp_db, "p1", prices)
    stats = compute_stats(tmp_db, "p1")
    cfg = HistoryConfig(minimum_observations=5)
    cls = classify(stats, cfg)
    assert cls.label == "VERY_LOW"


def test_classify_high(tmp_db):
    prices = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100]
    prices[-1] = 200  # current at the top
    _seed_prices(tmp_db, "p1", prices)
    stats = compute_stats(tmp_db, "p1")
    cfg = HistoryConfig(minimum_observations=5)
    cls = classify(stats, cfg)
    assert cls.label in ("VERY_HIGH", "HIGH")


def test_classify_constant_prices(tmp_db):
    # All equal -> range is 0 -> INSUFFICIENT (no spread to classify on).
    prices = [100] * 12
    _seed_prices(tmp_db, "p1", prices)
    stats = compute_stats(tmp_db, "p1")
    cfg = HistoryConfig(minimum_observations=5)
    cls = classify(stats, cfg)
    assert cls.label == "INSUFFICIENT"


def test_days_tracked(tmp_db):
    _seed_prices(tmp_db, "p1", [100, 110, 120])
    stats = compute_stats(tmp_db, "p1")
    assert stats.days_tracked == 3
