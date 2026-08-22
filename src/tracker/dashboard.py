"""
Static HTML dashboard generator.

Reads everything from SQLite and writes a self-contained ``docs/index.html``
(no backend, safe to publish via GitHub Pages). Inline styles + inline SVG
sparklines keep it viewable in the sandbox preview.
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import Settings
from .database import Database
from .models import ProductConfig
from .statistics import classify, compute_stats, format_date, format_price


def _sparkline(values: list[float], width: int = 160, height: int = 36) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1.0
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = (i / (n - 1)) * width if n > 1 else width / 2
        y = height - ((v - lo) / rng) * height
        pts.append(f"{x:.1f},{y:.1f}")
    last_x = (width) if n > 1 else width / 2
    last_y = height - ((values[-1] - lo) / rng) * height
    path = " ".join(pts)
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<polyline fill="none" stroke="#2563eb" stroke-width="1.6" points="{path}"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.4" fill="#dc2626"/>'
        f'</svg>'
    )


_STATUS_COLORS = {
    "VERY_LOW": "#16a34a", "LOW": "#22c55e", "NORMAL": "#64748b",
    "HIGH": "#f59e0b", "VERY_HIGH": "#dc2626", "INSUFFICIENT": "#94a3b8",
}


def _status_badge(label: str) -> str:
    color = _STATUS_COLORS.get(label, "#64748b")
    name = label.replace("_", " ")
    return f'<span class="badge" style="background:{color}">{html.escape(name)}</span>'


def build_dashboard(db: Database, products: list[ProductConfig],
                    settings: Settings, out_dir: str = "docs") -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows_html: list[str] = []
    for p in products:
        stats = compute_stats(db, p.id)
        cls = classify(stats, settings.history)
        priced = [r.selling_price for r in db.price_history(p.id) if r.selling_price is not None]
        spark = _sparkline(priced[-40:]) if priced else ""
        name = html.escape(p.name or p.id)
        url = html.escape(p.canonical_url or p.url)
        prev_change = ""
        if stats.change_from_previous is not None:
            sign = "▲" if stats.change_from_previous > 0 else ("▼" if stats.change_from_previous < 0 else "—")
            prev_change = f"{sign} {format_price(abs(stats.change_from_previous))}"

        last_offers = db.last_offers(p.id)
        offer_text = "<br>".join(html.escape(o.headline()) for o in last_offers) or "—"
        last_row = db.last_price_row(p.id)
        coupon = format_price(last_row.coupon_amount) if last_row and last_row.coupon_amount else "—"

        rows_html.append(f"""
        <tr>
          <td><a href="{url}" target="_blank" rel="noopener">{name}</a><div class="muted">{html.escape(p.asin or '')}</div></td>
          <td class="num">{format_price(stats.current)}</td>
          <td class="num">{prev_change}</td>
          <td class="num">{format_price(stats.min)}</td>
          <td class="num">{format_price(stats.max)}</td>
          <td class="num">{format_price(stats.average)}</td>
          <td>{_status_badge(cls.label)}</td>
          <td class="spark">{spark}</td>
          <td>{html.escape((last_row.availability if last_row else '') or '')}</td>
          <td>{offer_text}</td>
          <td class="num">{coupon}</td>
          <td class="muted">{format_date(last_row.timestamp) if last_row else ''}</td>
        </tr>""")

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Amazon Price Tracker Dashboard</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
          margin: 0; padding: 24px; background:#0f172a; color:#e2e8f0; }}
  h1 {{ font-size: 1.4rem; }}
  .muted {{ color:#94a3b8; font-size:.8rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size:.85rem; }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid #1e293b; text-align: left; vertical-align: top; }}
  th {{ background:#1e293b; position: sticky; top: 0; color:#cbd5e1; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .spark {{ text-align:center; }}
  .badge {{ color:#fff; padding:2px 8px; border-radius:10px; font-size:.72rem; white-space:nowrap; }}
  a {{ color:#60a5fa; text-decoration:none; }}
  a:hover {{ text-decoration: underline; }}
  .meta {{ color:#94a3b8; margin-bottom:16px; font-size:.85rem;}}
</style></head><body>
<h1>🛒 Amazon Price Tracker</h1>
<div class="meta">Generated {html.escape(now)} · {len(products)} products ·
  Comparison based only on data collected by this tracker.</div>
<table>
  <thead><tr>
    <th>Product</th><th>Price</th><th>Change</th><th>Low</th><th>High</th>
    <th>Avg</th><th>Status</th><th>Trend</th><th>Availability</th>
    <th>Bank offers</th><th>Coupon</th><th>Last checked</th>
  </tr></thead>
  <tbody>{''.join(rows_html)}
  </tbody>
</table>
<p class="muted">Low/High/Average are computed solely from observations recorded
since each product was first added to this tracker.</p>
</body></html>"""

    out = out_dir / "index.html"
    out.write_text(page, encoding="utf-8")
    return out
