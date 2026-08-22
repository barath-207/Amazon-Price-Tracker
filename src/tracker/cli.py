"""
Command-line interface.

    python -m tracker check                       # check all products
    python -m tracker check --product <id>        # check one product
    python -m tracker add "<amazon url>"          # add a product
    python -m tracker list                        # list tracked products
    python -m tracker history <id>                # show price history
    python -m tracker stats <id>                  # show statistics
    python -m tracker chart <id>                  # render a price chart
    python -m tracker test-notification           # send an ntfy test
    python -m tracker dashboard                   # build static dashboard
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from amazon.parser import extract_asin_from_url, normalize_url

from .config import load_products, load_settings
from .database import Database
from .dashboard import build_dashboard
from .models import ProductConfig
from .notifications import NtfySender
from .statistics import classify, compute_stats, format_date, format_price
from .summary import console_summary, write_summary
from .tracker import Tracker


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ---------------------------------------------------------------------------
def cmd_check(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    tracker = Tracker(settings=settings, config_dir=args.config, db_path=args.db)
    summary = tracker.check_all(only_product=args.product)
    print(console_summary(summary))
    write_summary(summary, path=str(Path(args.config).parent.parent / "docs" / "summary.md"))
    return 0 if summary.failed == 0 else 1


def cmd_add(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    url = args.url
    asin = extract_asin_from_url(url)
    if not asin:
        print(f"ERROR: could not extract an ASIN from {url}", file=sys.stderr)
        return 2
    products = load_products(args.config, settings.domain)
    # De-dupe by ASIN.
    if any(p.asin == asin for p in products):
        print(f"Product with ASIN {asin} is already tracked.")
        return 0
    pc = ProductConfig(
        url=url,
        id=args.id or f"asin-{asin.lower()}",
        name=args.name,
        asin=asin,
        canonical_url=normalize_url(url, settings.domain),
    )
    products.append(pc)
    from .config import save_products
    save_products(products, args.config)
    print(f"Added product {pc.id} (ASIN {asin}) -> {pc.canonical_url}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    products = load_products(args.config)
    if not products:
        print("No products configured. Add one with: python -m tracker add <url>")
        return 0
    print(f"{'ID':<20} {'ASIN':<12} {'Enabled':<8} Name")
    print("-" * 70)
    for p in products:
        print(f"{(p.id or ''):<20} {(p.asin or '?'):<12} {str(p.enabled):<8} {p.name or ''}")
        print(f"    {p.canonical_url or p.url}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    db = Database(args.db)
    rows = db.price_history(args.product)
    if not rows:
        print(f"No history for {args.product}")
        return 0
    print(f"History for {args.product} ({len(rows)} observations)")
    print(f"{'Timestamp':<22} {'Price':>12} {'MRP':>12} {'Coupon':>10} {'Avail':<20} Seller")
    print("-" * 90)
    for r in rows:
        print(
            f"{r.timestamp:<22} "
            f"{format_price(r.selling_price):>12} "
            f"{format_price(r.mrp):>12} "
            f"{format_price(r.coupon_amount):>10} "
            f"{(r.availability or ''):<20} "
            f"{r.seller or ''}"
        )
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    db = Database(args.db)
    stats = compute_stats(db, args.product)
    classification = classify(stats, settings.history)

    print(f"Statistics for {args.product}")
    print("=" * 50)
    print(f"Observations    : {stats.num_observations}")
    print(f"Days tracked    : {stats.days_tracked}")
    print(f"Tracked since   : {format_date(stats.first_seen)}")
    print(f"Current price   : {format_price(stats.current)}")
    print(f"Previous price  : {format_price(stats.previous)}")
    print(f"First price     : {format_price(stats.first)}")
    print(f"Low (all time)  : {format_price(stats.min)}  @ {format_date(stats.min_timestamp)}")
    print(f"High (all time) : {format_price(stats.max)}  @ {format_date(stats.max_timestamp)}")
    print(f"Average         : {format_price(stats.average)}")
    print(f"Median          : {format_price(stats.median)}")
    print(f"7-day range     : {format_price(stats.min_7d)} - {format_price(stats.max_7d)}")
    print(f"30-day range    : {format_price(stats.min_30d)} - {format_price(stats.max_30d)}")
    print(f"30-day average  : {format_price(stats.avg_30d)}")
    print(f"90-day range    : {format_price(stats.min_90d)} - {format_price(stats.max_90d)}")
    if stats.change_from_previous is not None:
        print(f"Last change     : {format_price(stats.change_from_previous)} "
              f"({stats.pct_change_from_previous}%)")
    print(f"Price changes   : {stats.num_changes} (drops {stats.num_drops}, rises {stats.num_increases})")
    if stats.percentile is not None:
        print(f"Current percentile: {stats.percentile}")
    print(f"Classification  : {classification.label.replace('_', ' ')} ({classification.reason})")
    return 0


def cmd_chart(args: argparse.Namespace) -> int:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        print("matplotlib is required for charts. Install with: pip install matplotlib")
        return 2

    from datetime import datetime
    settings = load_settings(args.config)
    db = Database(args.db)
    rows = db.price_history(args.product)
    if not rows:
        print(f"No history for {args.product}")
        return 0
    rows = [r for r in rows if r.selling_price is not None]
    times = [datetime.strptime(r.timestamp[:19], "%Y-%m-%d %H:%M:%S") for r in rows]
    prices = [r.selling_price for r in rows]

    stats = compute_stats(db, args.product)
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(times, prices, marker="o", linewidth=1.6, label="Selling price")
    if stats.min is not None:
        ax.axhline(stats.min, color="green", linestyle="--", alpha=0.7, label=f"Low {format_price(stats.min)}")
    if stats.max is not None:
        ax.axhline(stats.max, color="red", linestyle="--", alpha=0.7, label=f"High {format_price(stats.max)}")
    product = next((p for p in load_products(args.config) if p.id == args.product), None)
    if product and product.target_price:
        ax.axhline(product.target_price, color="orange", linestyle=":", alpha=0.8, label=f"Target {format_price(product.target_price)}")
    if prices:
        ax.axhline(prices[-1], color="blue", linestyle="-", alpha=0.3, label=f"Current {format_price(prices[-1])}")

    ax.set_title(f"Price history: {product.name if product else args.product}")
    ax.set_ylabel("Price (INR)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()

    out = Path(args.out) if args.out else Path("data/charts") / f"{args.product}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"Chart written to {out}")
    return 0


def cmd_test_notification(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    sender = NtfySender(settings)
    if not settings.ntfy.configured:
        print("ntfy is not configured. Set NTFY_SERVER, NTFY_TOPIC (and optionally NTFY_TOKEN).")
        return 2
    ok = sender.send(
        title="✅ Amazon Tracker test notification",
        message="If you can read this, your ntfy configuration is working correctly.",
        priority="default",
        tags=["white_check_mark", "test_tube"],
        click="https://github.com",
    )
    print("Notification sent." if ok else "Notification FAILED - check your ntfy settings/logs.")
    return 0 if ok else 1


def cmd_dashboard(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    db = Database(args.db)
    products = load_products(args.config)
    out = build_dashboard(db, products, settings, out_dir=args.out)
    print(f"Dashboard written to {out}")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tracker", description="Amazon price tracker")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--config", default="config", help="config directory")
    p.add_argument("--db", default="data/amazon_tracker.db", help="database path")
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("check", help="check configured products")
    pc.add_argument("--product", help="check only this product id")
    pc.set_defaults(func=cmd_check)

    pa = sub.add_parser("add", help="add a product URL")
    pa.add_argument("url", help="Amazon product URL")
    pa.add_argument("--id", help="friendly product id")
    pa.add_argument("--name", help="display name")
    pa.set_defaults(func=cmd_add)

    pl = sub.add_parser("list", help="list configured products")
    pl.set_defaults(func=cmd_list)

    ph = sub.add_parser("history", help="show price history")
    ph.add_argument("product", help="product id")
    ph.set_defaults(func=cmd_history)

    ps = sub.add_parser("stats", help="show statistics")
    ps.add_argument("product", help="product id")
    ps.set_defaults(func=cmd_stats)

    pc2 = sub.add_parser("chart", help="render a price chart PNG")
    pc2.add_argument("product", help="product id")
    pc2.add_argument("--out", help="output file path")
    pc2.set_defaults(func=cmd_chart)

    pn = sub.add_parser("test-notification", help="send an ntfy test notification")
    pn.set_defaults(func=cmd_test_notification)

    pd = sub.add_parser("dashboard", help="build a static dashboard")
    pd.add_argument("--out", default="docs", help="output directory")
    pd.set_defaults(func=cmd_dashboard)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
