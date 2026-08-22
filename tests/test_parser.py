"""Tests for Amazon HTML parsing, ASIN extraction, price/offer/coupon logic."""
from __future__ import annotations

from amazon.parser import (
    extract_asin_from_url,
    normalize_url,
    parse_page,
    parse_price_text,
    parse_rating_text,
    parse_review_count,
)
from amazon.validators import looks_like_captcha, validate_observation


# ---------------------------------------------------------------------------
# Price text parsing
# ---------------------------------------------------------------------------
def test_parse_price_indian_format():
    assert parse_price_text("₹17,499") == 17499.0
    assert parse_price_text("Rs. 17499") == 17499.0
    assert parse_price_text("17,499.00") == 17499.0
    assert parse_price_text("INR 17,499/-") == 17499.0
    assert parse_price_text("₹500 off") == 500.0


def test_parse_price_invalid():
    assert parse_price_text(None) is None
    assert parse_price_text("") is None
    assert parse_price_text("no price here") is None


def test_parse_rating():
    assert parse_rating_text("4.3 out of 5 stars") == 4.3
    assert parse_rating_text("5") == 5.0
    assert parse_rating_text("not a rating") is None


def test_parse_review_count():
    assert parse_review_count("1,245 ratings") == 1245
    assert parse_review_count("8,901") == 8901


# ---------------------------------------------------------------------------
# ASIN / URL handling
# ---------------------------------------------------------------------------
def test_extract_asin_dp():
    assert extract_asin_from_url("https://www.amazon.in/dp/B0BDHWDR12") == "B0BDHWDR12"


def test_extract_asin_gp_product():
    assert extract_asin_from_url(
        "https://www.amazon.in/gp/product/B0BDHWDR12/ref=xxx"
    ) == "B0BDHWDR12"


def test_extract_asin_with_query_params():
    assert extract_asin_from_url(
        "https://www.amazon.in/dp/B0BDHWDR12?tag=affinity&th=1&encoding=UTF8"
    ) == "B0BDHWDR12"


def test_extract_asin_invalid():
    assert extract_asin_from_url("https://example.com/no-asin") is None


def test_normalize_url_strips_params():
    url = normalize_url("https://www.amazon.in/dp/B0BDHWDR12?tag=affinity&th=1")
    assert url == "https://www.amazon.in/dp/B0BDHWDR12"


def test_normalize_url_gp_product():
    url = normalize_url("https://www.amazon.in/gp/product/B0BDHWDR12/ref=xxx")
    assert url == "https://www.amazon.in/dp/B0BDHWDR12"


def test_normalize_url_other_domain():
    url = normalize_url("https://www.amazon.com/dp/B0BDHWDR12", domain="www.amazon.com")
    assert url == "https://www.amazon.com/dp/B0BDHWDR12"


# ---------------------------------------------------------------------------
# Full page parsing
# ---------------------------------------------------------------------------
def test_parse_in_stock_product(fx):
    html = fx("product_in_stock.html")
    obs = parse_page(html, url="https://www.amazon.in/dp/B0BDHWDR12", expected_asin="B0BDHWDR12")
    assert obs.asin == "B0BDHWDR12"
    assert "Odyssey" in (obs.title or "")
    assert obs.brand == "Samsung"
    assert obs.price.selling_price == 17499.0
    assert obs.price.mrp == 21999.0
    assert obs.price.coupon_amount == 500.0
    assert obs.availability.in_stock is True
    assert obs.availability.status_text == "In Stock"
    assert obs.availability.fulfilled_by_amazon is True
    assert obs.variant == "27-inch"
    assert obs.rating == 4.3
    assert obs.review_count == 1245
    assert obs.image_url == "https://m.media-amazon.com/images/I/61abc.jpg"


def test_parse_in_stock_validation_passes(fx):
    html = fx("product_in_stock.html")
    obs = parse_page(html, url="https://www.amazon.in/dp/B0BDHWDR12")
    ok, reasons = validate_observation(obs, expected_asin="B0BDHWDR12")
    assert ok, reasons


def test_parse_offers(fx):
    html = fx("product_in_stock.html")
    obs = parse_page(html, url="https://www.amazon.in/dp/B0BDHWDR12")
    # Two distinct bank offers expected.
    banks = {o.bank for o in obs.offers}
    assert "HDFC Bank" in banks
    assert "ICICI Bank" in banks
    hdfc = next(o for o in obs.offers if o.bank == "HDFC Bank")
    assert hdfc.discount_percent == 10.0
    assert hdfc.maximum_discount == 1500.0
    assert hdfc.minimum_purchase == 10000.0
    assert obs.offers_confident is True


def test_parse_out_of_stock(fx):
    html = fx("product_out_of_stock.html")
    obs = parse_page(html, url="https://www.amazon.in/dp/B0XM4SONY00", expected_asin="B0XM4SONY00")
    assert obs.availability.in_stock is False
    assert obs.price.selling_price == 29990.0


def test_parse_jsonld_fallback(fx):
    """A page whose price is only in JSON-LD still parses."""
    html = fx("product_jsonld_only.html")
    obs = parse_page(html, url="https://www.amazon.in/dp/B07L68LMNL", expected_asin="B07L68LMNL")
    assert obs.title.startswith("Logitech")
    assert obs.price.selling_price == 2499.0
    assert obs.rating == 4.5


def test_effective_price_after_coupon(fx):
    html = fx("product_in_stock.html")
    obs = parse_page(html, url="https://www.amazon.in/dp/B0BDHWDR12")
    eff = obs.price.effective_price()
    assert eff == 16999.0  # 17499 - 500 coupon


def test_effective_price_with_bank_offer(fx):
    from amazon.models import Offer
    html = fx("product_in_stock.html")
    obs = parse_page(html, url="https://www.amazon.in/dp/B0BDHWDR12")
    hdfc = Offer(bank="HDFC Bank", offer_type="instant_discount", discount_percent=10.0,
                 maximum_discount=1500.0)
    eff = obs.price.effective_price_with_offer(hdfc)
    # effective base 16999, 10% = 1699.9 capped at 1500 -> 16999-1500 = 15499
    assert eff == 15499.0


def test_captcha_detection(fx):
    html = fx("captcha.html")
    assert looks_like_captcha(html) is True
