"""
GitHub Actions job summary + console summary generation.

Writes Markdown to ``$GITHUB_STEP_SUMMARY`` when running in Actions, and also
saves a copy to ``docs/summary.md`` for the static dashboard.
"""
from __future__ import annotations

import os
from typing import Optional

from .statistics import format_price
from .tracker import RunSummary


def _status_label(result) -> str:
    if not result.obs or result.obs.price.selling_price is None:
        return "—"
    # lightweight label without full stats
    return result.obs.availability.status_text or "—"


def build_markdown_summary(summary: RunSummary) -> str:
    lines: list[str] = []
    lines.append("## 🛒 Amazon Tracker")
    lines.append("")
    lines.append(f"**Checked:** {summary.checked} products")
    lines.append("")
    lines.append(f"| Metric | Count |")
    lines.append(f"| --- | ---: |")
    lines.append(f"| 📉 Price Drops | {summary.price_drops} |")
    lines.append(f"| 📈 Price Increases | {summary.price_increases} |")
    lines.append(f"| 🏦 Offer Changes | {summary.offer_changes} |")
    lines.append(f"| 🎟️ Coupon Changes | {summary.coupon_changes} |")
    lines.append(f"| 🟢 Back in Stock | {sum(1 for r in summary.results for e in r.events if e.change_type.value == 'BACK_IN_STOCK')} |")
    lines.append(f"| 🔴 Out of Stock | {sum(1 for r in summary.results for e in r.events if e.change_type.value == 'OUT_OF_STOCK')} |")
    lines.append(f"| ⚠️ Errors | {summary.errors} |")
    lines.append("")

    # Per-product table.
    lines.append("### Products")
    lines.append("")
    lines.append("| Product | Price | Status | OK? |")
    lines.append("| --- | ---: | --- | :---: |")
    for r in summary.results:
        name = r.product.name or r.product.id
        price = "—"
        if r.obs and r.obs.price.selling_price is not None:
            price = format_price(r.obs.price.selling_price)
        status = _status_label(r)
        ok = "✅" if r.success else "❌"
        lines.append(f"| {name} | {price} | {status} | {ok} |")
    lines.append("")
    return "\n".join(lines)


def write_summary(summary: RunSummary, path: Optional[str] = None) -> str:
    md = build_markdown_summary(summary)
    # GitHub Actions step summary.
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with open(step_summary, "a", encoding="utf-8") as fh:
                fh.write(md + "\n")
        except OSError:
            pass
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(md)
    return md


def console_summary(summary: RunSummary) -> str:
    lines = [
        "Amazon Tracker Summary",
        "======================",
        f"Products checked : {summary.checked}",
        f"Successful       : {summary.successful}",
        f"Failed           : {summary.failed}",
        "",
        f"Price drops      : {summary.price_drops}",
        f"Price increases  : {summary.price_increases}",
        f"Offer changes    : {summary.offer_changes}",
        f"Coupon changes   : {summary.coupon_changes}",
        f"Availability     : {summary.availability_changes}",
        f"Seller changes   : {summary.seller_changes}",
        f"Target reached   : {summary.target_reached}",
        f"New tracked lows : {summary.new_lows}",
        f"New tracked highs: {summary.new_highs}",
    ]
    return "\n".join(lines)
