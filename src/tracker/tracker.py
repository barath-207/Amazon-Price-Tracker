"""
Core orchestration: process every configured product, detect changes,
fire notifications, and produce a run summary.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from amazon.models import Offer
from amazon.offers import offer_sets_equal
from amazon.scraper import AmazonScraper
from amazon.validators import validate_observation

from .config import Settings, load_products, load_settings
from .database import Database
from .history import (
    record_failed_check,
    record_successful_check,
    store_observation,
)
from .models import (
    ChangeEvent,
    ChangeType,
    Classification,
    PriceStats,
    ProductConfig,
)
from .notifications import NtfySender, send_event
from .statistics import classify, compute_stats

log = logging.getLogger("tracker")


@dataclass
class ProductResult:
    product: ProductConfig
    success: bool
    error: Optional[str] = None
    observation = None  # set after init in tracker
    obs: Optional[object] = None
    events: list[ChangeEvent] = field(default_factory=list)
    changed: bool = False


@dataclass
class RunSummary:
    checked: int = 0
    successful: int = 0
    failed: int = 0
    price_drops: int = 0
    price_increases: int = 0
    offer_changes: int = 0
    coupon_changes: int = 0
    availability_changes: int = 0
    seller_changes: int = 0
    target_reached: int = 0
    new_lows: int = 0
    new_highs: int = 0
    results: list[ProductResult] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return self.failed


class Tracker:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        db: Optional[Database] = None,
        scraper: Optional[AmazonScraper] = None,
        sender: Optional[NtfySender] = None,
        products: Optional[list[ProductConfig]] = None,
        config_dir: str = "config",
        db_path: str = "data/amazon_tracker.db",
    ) -> None:
        self.settings = settings or load_settings(config_dir)
        self.db = db or Database(db_path)
        self.scraper = scraper or AmazonScraper(
            domain=self.settings.domain,
            timeout=self.settings.request_timeout,
            max_retries=self.settings.max_retries,
            backoff_base=self.settings.backoff_base,
            use_playwright=self.settings.use_playwright,
        )
        self.sender = sender or NtfySender(self.settings)
        self.products = products if products is not None else load_products(config_dir, self.settings.domain)

    # ------------------------------------------------------------------
    def check_all(self, only_product: Optional[str] = None) -> RunSummary:
        summary = RunSummary()
        products = [p for p in self.products if p.enabled]
        if only_product:
            products = [p for p in products if p.id == only_product]
        log.info("Checking %d product(s)", len(products))

        for i, product in enumerate(products):
            if i > 0:
                delay = random.uniform(self.settings.request_delay_min, self.settings.request_delay_max)
                log.debug("sleeping %.1fs before next product", delay)
                time.sleep(delay)
            try:
                result = self.check_one(product)
            except Exception as exc:  # never abort the whole run
                log.exception("unexpected error checking %s: %s", product.id, exc)
                result = ProductResult(product=product, success=False, error=f"unexpected: {exc}")
            summary.results.append(result)
            summary.checked += 1
            if result.success:
                summary.successful += 1
            else:
                summary.failed += 1
            for ev in result.events:
                self._bump_summary(summary, ev.change_type)

        return summary

    # ------------------------------------------------------------------
    def check_one(self, product: ProductConfig) -> ProductResult:
        result = ProductResult(product=product, success=False)
        log.info("Checking %s (%s)", product.id, product.url)

        res = self.scraper.scrape(product.url, expected_asin=product.asin)
        if res.error and not res.observation:
            log.warning("%s: scrape failed - %s", product.id, res.error)
            record_failed_check(self.db, product.id, res.error or "unknown")
            self._maybe_notify_failure(product, res.error)
            result.error = res.error
            return result

        obs = res.observation
        result.obs = obs

        # Validate before trusting any price.
        ok, reasons = validate_observation(obs, expected_asin=product.asin)
        if not ok:
            msg = "; ".join(reasons)
            log.warning("%s: invalid observation (%s) - not updating price", product.id, msg)
            record_failed_check(self.db, product.id, f"invalid parse: {msg}")
            result.error = msg
            return result

        result.success = True

        # Capture previous state for change detection.
        prev_row = self.db.last_price_row(product.id)
        prev_offers = self.db.last_offers(product.id)
        prev_min = self._historical_min(product.id)
        prev_max = self._historical_max(product.id)

        # Stats BEFORE storing give us "previous" values for low/high detection.
        stats_before = compute_stats(self.db, product.id)

        # Store the observation.
        price_changed, _, prev_price_info, prev_offers = store_observation(
            self.db, product.id, obs, store_every_check=self.settings.store_every_check
        )

        # Stats AFTER storing reflect the current state.
        stats = compute_stats(self.db, product.id)
        classification = classify(stats, self.settings.history)

        result.changed = price_changed or not offer_sets_equal(prev_offers, obs.offers)

        # --- Build change events --------------------------------------
        events: list[ChangeEvent] = []

        # Price change (only when we had a prior price to compare).
        if price_changed and prev_row is not None and prev_row.selling_price is not None:
            old = prev_row.selling_price
            new = obs.price.selling_price
            if new is not None and new != old:
                if self._should_notify_price(product, old, new):
                    ct = ChangeType.PRICE_DROP if new < old else ChangeType.PRICE_INCREASE
                    # Build a pseudo previous observation for message formatting.
                    from amazon.models import ProductObservation as PO
                    prev_obs_proxy = PO(asin=obs.asin, url=obs.url)
                    prev_obs_proxy.price.selling_price = old
                    events.append(ChangeEvent(
                        change_type=ct, product=product, observation=obs,
                        previous_observation=prev_obs_proxy, stats=stats,
                        classification=classification,
                    ))

        # Coupon change.
        if self.settings.notifications.coupon_changes:
            coupon_events = self._coupon_changes(product, obs, prev_row)
            events.extend(coupon_events)

        # Bank offer changes.
        if self.settings.notifications.offer_changes:
            offer_events = self._offer_changes(product, obs, prev_offers, stats, classification)
            events.extend(offer_events)

        # Availability + seller.
        if prev_row is not None:
            if self.settings.notifications.availability_changes:
                if bool(prev_row.in_stock) and not obs.availability.in_stock:
                    events.append(ChangeEvent(
                        change_type=ChangeType.OUT_OF_STOCK, product=product,
                        observation=obs, stats=stats, classification=classification,
                    ))
                elif (not bool(prev_row.in_stock)) and obs.availability.in_stock:
                    events.append(ChangeEvent(
                        change_type=ChangeType.BACK_IN_STOCK, product=product,
                        observation=obs, stats=stats, classification=classification,
                    ))
            if self.settings.notifications.seller_changes:
                if (prev_row.seller or "") != (obs.availability.seller or "") and obs.availability.seller:
                    events.append(ChangeEvent(
                        change_type=ChangeType.SELLER_CHANGED, product=product,
                        observation=obs, stats=stats, classification=classification,
                        detail={"prev_seller": prev_row.seller or "Unknown"},
                    ))

        # Target price (crossing detection).
        if self.settings.notifications.target_price and product.target_price:
            events.extend(self._target_events(product, obs, prev_row, stats))

        # New tracked low / high.
        if self.settings.notifications.new_tracked_low and prev_min is not None:
            cur = obs.price.selling_price
            if cur is not None and cur < prev_min:
                events.append(ChangeEvent(
                    change_type=ChangeType.NEW_TRACKED_LOW, product=product,
                    observation=obs, stats=stats, classification=classification,
                    detail={"prev_low": prev_min},
                ))
        if self.settings.notifications.new_tracked_high and prev_max is not None:
            cur = obs.price.selling_price
            if cur is not None and cur > prev_max:
                events.append(ChangeEvent(
                    change_type=ChangeType.NEW_TRACKED_HIGH, product=product,
                    observation=obs, stats=stats, classification=classification,
                    detail={"prev_high": prev_max},
                ))

        # --- Send notifications --------------------------------------
        for ev in events:
            log.info("%s: sending notification %s", product.id, ev.change_type.value)
            send_event(self.sender, ev, self.settings)

        record_successful_check(self.db, product.id, result.changed, res.response_time)
        result.events = events

        price_str = f"₹{obs.price.selling_price:,.0f}" if obs.price.selling_price else "N/A"
        log.info("%s: %s (%s)", product.id, price_str, obs.availability.status_text)
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _should_notify_price(self, product: ProductConfig, old: float, new: float) -> bool:
        # Per-product notify toggle.
        if product.notify_on_any_price_change is False:
            return False
        # Minimum drop percentage applies to drops only.
        if product.min_drop_percent is not None and new < old and old > 0:
            drop_pct = (old - new) / old * 100.0
            return drop_pct >= product.min_drop_percent
        return True

    def _coupon_changes(self, product, obs, prev_row):
        events = []
        prev_coupon = prev_row.coupon_amount if prev_row else None
        cur_coupon = obs.price.coupon_amount or obs.price.coupon_percent
        has_cur = obs.price.coupon_amount or obs.price.coupon_percent
        if prev_coupon is None and has_cur:
            events.append(ChangeEvent(
                change_type=ChangeType.COUPON_ADDED, product=product, observation=obs))
        elif prev_coupon is not None and not has_cur:
            events.append(ChangeEvent(
                change_type=ChangeType.COUPON_REMOVED, product=product, observation=obs,
                detail={"prev_coupon": prev_coupon}))
        elif prev_coupon is not None and has_cur and prev_coupon != (obs.price.coupon_amount or obs.price.coupon_percent):
            events.append(ChangeEvent(
                change_type=ChangeType.COUPON_CHANGED, product=product, observation=obs,
                detail={"prev_coupon": prev_coupon}))
        return events

    def _offer_changes(self, product, obs, prev_offers, stats, classification):
        events = []
        cur = obs.offers
        if offer_sets_equal(prev_offers, cur):
            return events
        prev_hashes = {o.hash() for o in prev_offers}
        cur_hashes = {o.hash() for o in cur}
        added = [o for o in cur if o.hash() not in prev_hashes]
        removed = [o for o in prev_offers if o.hash() not in cur_hashes]

        if prev_offers and not cur and not obs.offers_confident:
            # Offers vanished AND detection is uncertain -> do not claim removal.
            return events
        if not prev_offers and not cur:
            return events

        if removed and not added:
            events.append(ChangeEvent(
                change_type=ChangeType.BANK_OFFER_REMOVED, product=product,
                observation=obs, stats=stats, classification=classification,
                detail={"removed_offers": removed}))
        elif added and not removed:
            events.append(ChangeEvent(
                change_type=ChangeType.BANK_OFFER_ADDED, product=product,
                observation=obs, stats=stats, classification=classification))
        else:
            events.append(ChangeEvent(
                change_type=ChangeType.BANK_OFFER_CHANGED, product=product,
                observation=obs, stats=stats, classification=classification))
        return events

    def _target_events(self, product, obs, prev_row, stats):
        events = []
        target = product.target_price
        cur = obs.price.selling_price
        if cur is None or target is None:
            return events
        key = "below_target"
        was_below = self.db.get_state(product.id, key) == "1"
        is_below = cur <= target
        if is_below and not was_below:
            events.append(ChangeEvent(
                change_type=ChangeType.TARGET_PRICE_REACHED, product=product,
                observation=obs, stats=stats))
        self.db.set_state(product.id, key, "1" if is_below else "0")
        return events

    def _historical_min(self, product_id: str) -> Optional[float]:
        rows = self.db.price_history(product_id)
        vals = [r.selling_price for r in rows if r.selling_price is not None]
        return min(vals) if vals else None

    def _historical_max(self, product_id: str) -> Optional[float]:
        rows = self.db.price_history(product_id)
        vals = [r.selling_price for r in rows if r.selling_price is not None]
        return max(vals) if vals else None

    def _maybe_notify_failure(self, product: ProductConfig, error: Optional[str]) -> None:
        if not self.settings.notifications.on_check_failure:
            return
        ev = ChangeEvent(
            change_type=ChangeType.CHECK_FAILED, product=product,
            detail={"error": error or "unknown"},
        )
        send_event(self.sender, ev, self.settings)

    @staticmethod
    def _bump_summary(summary: RunSummary, ct: ChangeType) -> None:
        if ct == ChangeType.PRICE_DROP:
            summary.price_drops += 1
        elif ct == ChangeType.PRICE_INCREASE:
            summary.price_increases += 1
        elif ct in (ChangeType.BANK_OFFER_ADDED, ChangeType.BANK_OFFER_REMOVED, ChangeType.BANK_OFFER_CHANGED):
            summary.offer_changes += 1
        elif ct in (ChangeType.COUPON_ADDED, ChangeType.COUPON_REMOVED, ChangeType.COUPON_CHANGED):
            summary.coupon_changes += 1
        elif ct in (ChangeType.BACK_IN_STOCK, ChangeType.OUT_OF_STOCK):
            summary.availability_changes += 1
        elif ct == ChangeType.SELLER_CHANGED:
            summary.seller_changes += 1
        elif ct == ChangeType.TARGET_PRICE_REACHED:
            summary.target_reached += 1
        elif ct == ChangeType.NEW_TRACKED_LOW:
            summary.new_lows += 1
        elif ct == ChangeType.NEW_TRACKED_HIGH:
            summary.new_highs += 1
