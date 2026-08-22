"""
Configuration loading.

Reads:
  * ``config/settings.yaml``  - non-secret defaults (safe to commit)
  * ``config/products.yaml``  - product list (safe to commit)

Secrets (ntfy token etc.) come ONLY from environment variables / GitHub
Actions secrets - never from the YAML files.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from amazon.parser import extract_asin_from_url, normalize_url
from .models import HistoryConfig, ProductConfig

log = logging.getLogger("tracker.config")


@dataclass
class NtfyConfig:
    server: Optional[str] = None
    topic: Optional[str] = None
    token: Optional[str] = None  # never logged
    priority: str = "default"

    @property
    def configured(self) -> bool:
        return bool(self.server and self.topic)


@dataclass
class NotificationToggles:
    price_changes: bool = True
    offer_changes: bool = True
    coupon_changes: bool = True
    availability_changes: bool = True
    seller_changes: bool = True
    target_price: bool = True
    new_tracked_low: bool = True
    new_tracked_high: bool = True
    on_check_failure: bool = False


@dataclass
class Settings:
    tracker_name: str = "Amazon Price Tracker"
    request_delay_min: int = 3
    request_delay_max: int = 8
    request_timeout: int = 20
    max_retries: int = 3
    backoff_base: int = 2
    store_every_check: bool = True
    notifications: NotificationToggles = field(default_factory=NotificationToggles)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    ntfy_priorities: dict[str, str] = field(default_factory=dict)
    domain: str = "www.amazon.in"
    use_playwright: bool = False
    config_dir: Path = field(default_factory=lambda: Path("config"))

    def priority_for(self, change_type: str) -> str:
        return self.ntfy_priorities.get(change_type, "default")


def _read_env_file(path: Path) -> dict[str, str]:
    """Load a .env file (KEY=value) into a dict. Optional convenience."""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def load_settings(config_dir: str | Path = "config") -> Settings:
    """Load settings from YAML, then overlay environment variables for secrets."""
    config_dir = Path(config_dir)
    settings_path = config_dir / "settings.yaml"

    raw: dict[str, Any] = {}
    if settings_path.exists():
        with settings_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    # .env for local dev (optional). Environment / GitHub secrets take priority.
    env = {**_read_env_file(Path(".env")), **os.environ}

    s = Settings(config_dir=config_dir)

    tracker = raw.get("tracker", {}) or {}
    s.tracker_name = tracker.get("name", s.tracker_name)
    s.request_delay_min = int(tracker.get("request_delay_min", s.request_delay_min))
    s.request_delay_max = int(tracker.get("request_delay_max", s.request_delay_max))
    s.request_timeout = int(tracker.get("request_timeout", s.request_timeout))
    s.max_retries = int(tracker.get("max_retries", s.max_retries))
    s.backoff_base = int(tracker.get("backoff_base", s.backoff_base))
    s.store_every_check = bool(tracker.get("store_every_check", s.store_every_check))
    s.domain = tracker.get("domain", s.domain)
    s.use_playwright = bool(tracker.get("use_playwright", s.use_playwright))

    notif = raw.get("notifications", {}) or {}
    s.notifications = NotificationToggles(
        price_changes=bool(notif.get("price_changes", True)),
        offer_changes=bool(notif.get("offer_changes", True)),
        coupon_changes=bool(notif.get("coupon_changes", True)),
        availability_changes=bool(notif.get("availability_changes", True)),
        seller_changes=bool(notif.get("seller_changes", True)),
        target_price=bool(notif.get("target_price", True)),
        new_tracked_low=bool(notif.get("new_tracked_low", True)),
        new_tracked_high=bool(notif.get("new_tracked_high", True)),
        on_check_failure=bool(notif.get("on_check_failure", False)),
    )

    hist = raw.get("history", {}) or {}
    s.history = HistoryConfig(
        classification_enabled=bool(hist.get("classification_enabled", True)),
        minimum_observations=int(hist.get("minimum_observations", 10)),
        range_thresholds={**s.history.range_thresholds, **(hist.get("range_thresholds") or {})},
    )

    s.ntfy_priorities = raw.get("ntfy_priorities", {}) or {}

    # --- Secrets from environment only ---------------------------------
    s.ntfy = NtfyConfig(
        server=env.get("NTFY_SERVER"),
        topic=env.get("NTFY_TOPIC"),
        token=env.get("NTFY_TOKEN"),
        priority=str(env.get("NTFY_PRIORITY", "default")),
    )

    if not s.ntfy.configured:
        log.warning("ntfy not configured (NTFY_SERVER / NTFY_TOPIC missing) - notifications disabled")

    return s


def load_products(config_dir: str | Path = "config", domain: str = "www.amazon.in") -> list[ProductConfig]:
    """Load products.yaml and normalise / de-duplicate by ASIN."""
    config_dir = Path(config_dir)
    path = config_dir / "products.yaml"
    if not path.exists():
        log.warning("no products.yaml found at %s", path)
        return []

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    items = raw.get("products", []) or []
    products: list[ProductConfig] = []
    seen_asins: dict[str, ProductConfig] = {}

    for item in items:
        url = item.get("url")
        if not url:
            log.warning("skipping product with no url: %s", item)
            continue
        asin = extract_asin_from_url(url)
        canonical = normalize_url(url, domain)

        # De-duplicate by ASIN (same product supplied via different URLs).
        if asin and asin in seen_asins:
            log.info("skipping duplicate ASIN %s (%s)", asin, url)
            continue

        pid = item.get("id") or (asin and f"asin-{asin.lower()}") or "unknown"
        pc = ProductConfig(
            url=url,
            id=str(pid),
            name=item.get("name"),
            enabled=bool(item.get("enabled", True)),
            target_price=item.get("target_price"),
            notify_on_any_price_change=item.get("notify_on_any_price_change"),
            notify_on_offer_change=item.get("notify_on_offer_change"),
            notify_on_coupon_change=item.get("notify_on_coupon_change"),
            min_drop_percent=item.get("min_drop_percent"),
            asin=asin,
            canonical_url=canonical,
        )
        products.append(pc)
        if asin:
            seen_asins[asin] = pc

    return products


def save_products(products: list[ProductConfig], config_dir: str | Path = "config") -> None:
    """Write products back to products.yaml (used by ``tracker add``)."""
    config_dir = Path(config_dir)
    path = config_dir / "products.yaml"
    config_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        "# ===========================================================================",
        "# Products tracked by Amazon Price Tracker.",
        "# Edit freely - any Amazon URL format is accepted; ASINs are normalised.",
        "# ===========================================================================",
        "",
        "products:",
    ]
    for p in products:
        lines.append(f"  - id: {p.id}")
        if p.name:
            lines.append(f'    name: "{p.name}"')
        lines.append(f'    url: "{p.url}"')
        lines.append(f"    enabled: {str(p.enabled).lower()}")
        if p.target_price is not None:
            lines.append(f"    target_price: {p.target_price}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
