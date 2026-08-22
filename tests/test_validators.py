"""Tests for validators and data integrity guards."""
from __future__ import annotations

from amazon.models import AvailabilityInfo, PriceInfo, ProductObservation
from amazon.validators import (
    is_plausible_price,
    is_valid_asin,
    looks_like_not_found,
    validate_observation,
)


def test_is_valid_asin():
    assert is_valid_asin("B0BDHWDR12") is True
    assert is_valid_asin("b0bdhwdr12") is True
    assert is_valid_asin("TOOSHORT") is False
    assert is_valid_asin("") is False
    assert is_valid_asin(None) is False


def test_is_plausible_price():
    assert is_plausible_price(100) is True
    assert is_plausible_price(0) is False
    assert is_plausible_price(-5) is False
    assert is_plausible_price(None) is False
    assert is_plausible_price(float("nan")) is False
    assert is_plausible_price(1_000_000_000) is False  # above guard


def test_validate_rejects_zero_price():
    obs = ProductObservation(
        asin="B0BDHWDR12", url="https://www.amazon.in/dp/B0BDHWDR12",
        title="Some Product", price=PriceInfo(selling_price=0.0),
        availability=AvailabilityInfo(),
    )
    ok, reasons = validate_observation(obs)
    assert ok is False
    assert any("implausible" in r for r in reasons)


def test_validate_rejects_mrp_below_selling():
    obs = ProductObservation(
        asin="B0BDHWDR12", url="https://www.amazon.in/dp/B0BDHWDR12",
        title="Some Product",
        price=PriceInfo(selling_price=10000.0, mrp=5000.0),
        availability=AvailabilityInfo(),
    )
    ok, reasons = validate_observation(obs)
    assert ok is False
    assert any("MRP" in r for r in reasons)


def test_validate_rejects_asin_mismatch():
    obs = ProductObservation(
        asin="B0BDHWDR12", url="https://www.amazon.in/dp/B0BDHWDR12",
        title="Some Product", price=PriceInfo(selling_price=10000.0),
        availability=AvailabilityInfo(),
    )
    ok, reasons = validate_observation(obs, expected_asin="OTHERASIN0")
    assert ok is False
    assert any("ASIN mismatch" in r for r in reasons)


def test_validate_accepts_good_observation():
    obs = ProductObservation(
        asin="B0BDHWDR12", url="https://www.amazon.in/dp/B0BDHWDR12",
        title="Samsung Monitor", price=PriceInfo(selling_price=17499.0, mrp=21999.0),
        availability=AvailabilityInfo(),
    )
    ok, reasons = validate_observation(obs)
    assert ok is True
    assert obs.price_confident is True


def test_validate_rejects_missing_title():
    obs = ProductObservation(
        asin="B0BDHWDR12", url="https://www.amazon.in/dp/B0BDHWDR12",
        title="", price=PriceInfo(selling_price=17499.0),
        availability=AvailabilityInfo(),
    )
    ok, reasons = validate_observation(obs)
    assert ok is False


def test_looks_like_not_found():
    assert looks_like_not_found("No results for your search") is True
    assert looks_like_not_found("productTitle content") is False
