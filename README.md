# 🛒 Amazon Price Tracker

A **production-quality Amazon product price tracker** that runs entirely on **GitHub Actions** — no laptop, phone, VPS, or Raspberry Pi needs to stay on. It tracks multiple products, stores permanent price history in SQLite, detects price / offer / coupon / availability changes, classifies prices as low/normal/high, and pushes instant notifications via **[ntfy](https://ntfy.sh)**.

> Built for **Amazon India (`amazon.in`)** first, with the Amazon-specific code isolated in `src/amazon/` so new domains can be added without touching history, statistics, or notifications.

---

## ✨ Features

- **Multiple products** checked in a single run (one product failing never aborts the rest).
- **Permanent price history** in SQLite, committed back to the repo so it survives across ephemeral GitHub runners.
- **Everything-is-a-change detection**: price drops & increases, bank/card offers added/removed/changed, coupons, availability, and seller changes.
- **Low / Normal / High classification** using both *range* and *percentile* methods, with configurable thresholds.
- **Honest history**: never claims "all-time Amazon low" — only *"lowest recorded by this tracker since you added it"*. Insufficient-history products are clearly marked.
- **Target-price alerts** that fire on threshold crossing (not repeatedly).
- **Defensive scraping**: layered parsers, retries with backoff, CAPTCHA/bot-detection detection, and data-integrity validation that **never** lets a parse failure be recorded as a fake price (e.g. ₹0).
- **ntfy notifications** with titles, priority, tags, click-through URLs and product icons.
- **Static dashboard** + **CLI** + **price charts**.
- **No secrets in the repo** — ntfy credentials live in GitHub Actions Secrets.

---

## 🏗️ Architecture

```
amazon-price-tracker/
├── .github/workflows/amazon-tracker.yml   # schedule + auto-commit workflow
├── config/
│   ├── settings.yaml                       # non-secret defaults (safe to commit)
│   └── products.yaml                       # your product list
├── data/
│   ├── amazon_tracker.db                   # SQLite history (committed!)
│   └── charts/                             # generated PNG charts
├── docs/
│   └── index.html                          # static dashboard (GitHub Pages ready)
├── src/
│   ├── amazon/                             # ← ALL Amazon-specific parsing lives here
│   │   ├── scraper.py                      #   HTTP fetching + retries + Playwright fallback
│   │   ├── parser.py                       #   layered HTML → structured data
│   │   ├── offers.py                       #   bank/card offer extraction
│   │   ├── validators.py                   #   data-integrity guards
│   │   └── models.py                       #   ProductObservation / PriceInfo / Offer
│   └── tracker/                            #   Amazon-agnostic application logic
│       ├── cli.py                          #   `python -m tracker ...`
│       ├── config.py                       #   settings + products loading
│       ├── database.py                     #   SQLite schema + CRUD
│       ├── history.py                      #   observation storage + change detection
│       ├── statistics.py                   #   stats + classification
│       ├── notifications.py                #   ntfy message building & sending
│       ├── dashboard.py                    #   static HTML report
│       ├── summary.py                      #   GitHub Actions job summary
│       └── tracker.py                      #   orchestration
├── tests/                                  # unit tests with mocked Amazon HTML
├── requirements.txt
└── pyproject.toml
```

**Design principle:** the `src/amazon/` package is the *only* module that talks to Amazon. Swapping it for an official Amazon Product Advertising API client later leaves history, statistics, notifications, and the GitHub Actions workflow completely untouched.

---

## 🚀 Quick Start

### 1. Use this project

```bash
git clone <your-fork-url> amazon-price-tracker
cd amazon-price-tracker
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # core + dev tools
```

### 2. Add products

Edit `config/products.yaml`:

```yaml
products:
  - id: samsung-monitor
    name: "Samsung Monitor"
    url: "https://www.amazon.in/dp/B0BDHWDR12"
    enabled: true
    target_price: 17000          # optional
```

…or use the CLI:

```bash
python -m tracker add "https://www.amazon.in/dp/B0BDHWDR12" --name "Samsung Monitor"
```

Any Amazon URL format works (`/dp/ASIN`, `/gp/product/ASIN`, URLs with tracking params). The ASIN is extracted and used as the canonical id, so adding the same product via two different URLs **never** creates a duplicate.

### 3. Configure ntfy

1. Install the free **ntfy** app (or self-host a server).
2. Create a topic, e.g. `my-amazon-alerts`.
3. *(Optional)* create an access token for a protected topic.

### 4. Add GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Required | Example |
| --- | --- | --- |
| `NTFY_SERVER` | ✅ | `https://ntfy.sh` |
| `NTFY_TOPIC` | ✅ | `my-amazon-alerts` |
| `NTFY_TOKEN` | only if topic is protected | `tk_abc123…` |

### 5. Enable GitHub Actions

The workflow in `.github/workflows/amazon-tracker.yml` runs on a schedule **and** on manual dispatch. GitHub only runs scheduled workflows from your **default branch**, so push everything to `main`.

Test it immediately: **Actions tab → "Amazon Price Tracker" → Run workflow → Run workflow**.

---

## ⏱️ Changing the Check Interval

Open `.github/workflows/amazon-tracker.yml` and edit the `cron:` line. The interval is **not** hardcoded in the app:

```yaml
on:
  schedule:
    - cron: "0 */2 * * *"   # ← change me
```

Common options:

| Frequency | cron |
| --- | --- |
| Every 30 min | `*/30 * * * *` |
| Every hour | `0 * * * *` |
| Every 2 hours | `0 */2 * * *` |
| Daily 09:30 IST | `30 4 * * *` (UTC) |

> GitHub may delay scheduled runs ~15 min during peak load. Be polite to Amazon — don't set aggressive intervals. See **Legal / Safety** below.

---

## 🔔 ntfy Setup

**Public server (simplest):**
- Server: `https://ntfy.sh`
- Pick any unique topic name (treat it like a password — anyone who knows it can read your notifications).

**Self-hosted:**
- Set `NTFY_SERVER` to your instance URL.
- Create a token and set `NTFY_TOKEN` if authentication is required.

**Test it locally** (set the env vars first):

```bash
export NTFY_SERVER=https://ntfy.sh
export NTFY_TOPIC=my-amazon-alerts
python -m tracker test-notification
```

You can also override per-event priority in `config/settings.yaml` under `ntfy_priorities`.

---

## 💾 How Persistence Works

GitHub Actions runners are ephemeral — nothing survives between runs by default. This tracker:

1. Updates `data/amazon_tracker.db` (SQLite) every successful check.
2. Commits the changed database (and dashboard) back to `main` using a `github-actions[bot]` identity:

   ```
   🤖 Update Amazon price history
   ```

3. Uses a **concurrency lock** (`concurrency.group: amazon-tracker`) so two scheduled runs can never edit the database simultaneously.
4. Commits **only when something changed** (avoids empty commits).

> The database is intentionally **committed to the repo** (it is *not* in `.gitignore`). If it ever grows very large you can periodically prune old rows, but for typical usage it stays small.

---

## 📈 How Price History & Classification Work

History is stored in SQLite tables: `products`, `price_history`, `offers_history`, `checks`, and `product_state`. **Every successful observation is recorded; history is never overwritten.**

For each product the tracker computes: first/current/min/max/average/median price, 7/30/90-day windows, change vs previous, % change vs first, distance from low/high, number of price changes/drops/increases, and days tracked.

**Classification** is based **only** on data collected by this tracker (it never pretends to know Amazon's pre-tracking history):

```
Current price: ₹16,979
Historical low:  ₹16,909
Historical high: ₹17,519
Range position:  11%   →  LOW
Percentile:      10th
```

Thresholds (editable in `config/settings.yaml`):

| Range position | Label |
| --- | --- |
| 0 – 10 % | VERY LOW |
| 10 – 30 % | LOW |
| 30 – 70 % | NORMAL |
| 70 – 90 % | HIGH |
| 90 – 100 % | VERY HIGH |

A second **percentile** method guards against a single extreme outlier distorting the range; both are reported where useful.

### First-day / insufficient history

A product freshly added is **not** reported as "all-time low". Until it reaches `history.minimum_observations` (default 10), its status reads **"Insufficient historical data"**, and notifications note that comparison is based only on data collected since the add date.

---

## 🏦 How Bank Offers Are Detected

Amazon shows bank/card offers inconsistently — sometimes in structured sections, sometimes only as text, and sometimes they are **personalized / dynamically loaded**. The extractor (`src/amazon/offers.py`):

1. Scans known bank names (HDFC, ICICI, SBI, Axis, Kotak, …) and offer keywords.
2. Pulls structured numbers (discount %, max discount, min purchase, card type/network) via targeted regexes.
3. Normalises each offer into a structured record, e.g.:

   ```json
   { "bank": "HDFC Bank", "offer_type": "instant_discount",
     "discount_percent": 10, "maximum_discount": 1500, "minimum_purchase": 10000 }
   ```

**Important distinction:** the extractor reports whether detection was **confident**. When it simply can't locate an offers area, the notification says:

> ⚠️ Bank offer could not be reliably detected during this check.

rather than falsely claiming "no offer exists". A disappearance is only reported as a *removal* when detection was confident.

---

## 🖥️ Local CLI

```bash
python -m tracker check                       # check all products now
python -m tracker check --product samsung-monitor
python -m tracker add "https://www.amazon.in/dp/XXXXXXXX" --name "My Product"
python -m tracker list                         # list tracked products
python -m tracker history samsung-monitor      # show price history
python -m tracker stats samsung-monitor        # full statistics
python -m tracker chart samsung-monitor        # → data/charts/samsung-monitor.png
python -m tracker dashboard                    # → docs/index.html
python -m tracker test-notification            # send an ntfy test
```

> Global flags (`--config`, `--db`, `-v`) go **before** the subcommand, e.g. `python -m tracker --db path.db stats X`.

---

## 📊 Static Dashboard

```bash
python -m tracker dashboard            # writes docs/index.html
```

A single self-contained HTML file (inline CSS + inline SVG sparklines) showing every product's current/previous price, change, low/high/average, status badge, availability, current offers, coupon and a trend sparkline. Enable **GitHub Pages** on the `docs/` folder (Settings → Pages) to publish it, or just open the file locally.

---

## 🔔 Notification Types

| Event | Default priority | Example title |
| --- | --- | --- |
| Price drop | high | 📉 Amazon Price Drop |
| Price increase | default | 📈 Amazon Price Increase |
| Bank offer added | high | 🏦 Amazon Offer Added |
| Bank offer removed | default | 🏦 Amazon Offer Removed |
| Bank offer changed | default | 🏦 Amazon Offer Updated |
| Coupon added/removed/changed | default/low | 🎟️ Amazon Coupon Added |
| Back in stock | high | 🟢 Back in Stock |
| Out of stock | urgent | 🔴 Out of Stock |
| Target price reached | urgent | 🎯 Target Price Reached |
| Seller changed | low | 🛒 Seller Changed |
| New tracked low | high | 🔥 NEW TRACKED LOW |
| New tracked high | default | 📈 NEW TRACKED HIGH |
| Check failed *(opt-in)* | low | ⚠️ Amazon Check Failed |

"New tracked low/high" is always phrased as **"lowest/highest price recorded by this tracker"** — never as Amazon's all-time low.

---

## 🧪 Running Tests

```bash
pip install -e ".[dev]"
pytest                      # 51 tests, all mocked - no real Amazon requests
```

Tests cover: price/ASIN/coupon/offer extraction, price-change & offer-change detection, historical low/high, percentages, target price, notification building, database operations, invalid HTML, missing price, CAPTCHA pages, and out-of-stock products.

---

## 🛠️ Adding Another Amazon Domain

1. Set `tracker.domain` in `config/settings.yaml` (e.g. `www.amazon.com`).
2. The parser's selectors are already multi-region; if a region needs tweaks, edit only `src/amazon/parser.py`.
3. `user_agents` and `Accept-Language` headers are mapped in `src/amazon/scraper.py`.

No other code changes are required.

---

## ⚠️ Known Amazon Scraping Limitations

Amazon actively blocks automated access. **The single biggest issue on GitHub
Actions is that runners use datacenter (Azure) IP ranges, and Amazon serves a
CAPTCHA/"bot check" page to raw HTTP requests from those IPs essentially 100%
of the time.** This shows up as `scrape failed - CAPTCHA/bot-detection page`
for every product.

### The fix: Playwright (already wired up)

This project ships with a **Playwright (headless Chromium) fetch strategy** that
emulates a real browser. Because a real browser has a genuine fingerprint and
executes JavaScript, it bypasses most of Amazon's data checks that block
`requests`. The GitHub Actions workflow installs Chromium and sets
`FETCH_STRATEGY=playwright` by default, so once you push the updated workflow
your checks should succeed.

If you need a real browser locally too:

```bash
pip install playwright
python -m playwright install chromium      # add --with-deps on Linux
FETCH_STRATEGY=playwright python -m tracker check
```

Strategies (set via `tracker.fetch_strategy` in `config/settings.yaml` or the
`FETCH_STRATEGY` env var):

| Strategy | What it does | When to use |
| --- | --- | --- |
| `requests` | HTTP only (fast) | Local machine with a residential IP |
| `playwright` | Chromium first, HTTP fallback | **GitHub Actions / any datacenter IP** |
| `auto` | HTTP first, Chromium on CAPTCHA | Mixed |

### Other mitigations

- **Slow down** — raise `request_delay_min`/`request_delay_max` and reduce cron frequency.
- **Add a residential proxy** — set an `AMAZON_PROXY` GitHub secret
  (e.g. `http://user:pass@host:port`) and it is used for both HTTP and Playwright.
  This is the most reliable fix for datacenter IPs.
- **Partial success is fine** — a price tracker runs every couple of hours; even
  if some checks are blocked, the ones that succeed build up a useful history.

### Important caveats

- **Bank offers / coupons** may be personalized, A/B-tested, or loaded via JS.
  The extractor reports a *confidence* flag and won't claim an offer vanished
  unless it's sure.
- **Variants**: the tracker records the selected variant Amazon exposes, so two
  variants are never merged into one history.
- HTML changes frequently. The parser is layered (primary selectors → fallbacks
  → JSON-LD → embedded data) so a single broken selector doesn't break extraction.
- If blocking is persistent even with Playwright, consider the **official Amazon
  Product Advertising API** — the scraping layer is isolated precisely so an API
  client can replace it without rewriting anything else.

---

## 🔐 Secrets Reference

| Secret | Purpose |
| --- | --- |
| `NTFY_SERVER` | ntfy base URL (public or self-hosted) |
| `NTFY_TOPIC` | ntfy topic to publish to |
| `NTFY_TOKEN` | ntfy access token (only for protected topics) |

Optional, for an official Amazon PA-API integration (not wired up by default):

| Secret | Purpose |
| --- | --- |
| `AMAZON_ACCESS_KEY` | PA-API access key |
| `AMAZON_SECRET_KEY` | PA-API secret key |
| `AMAZON_PARTNER_TAG` | PA-API associate tag |

Secrets are **never** written to source, YAML, SQLite, or logs.

---

## ⚖️ Legal / Operational Safety

- The scraper does **not** bypass CAPTCHA, authentication, or any access control.
- It prefers official APIs where practical and is designed so an API client can replace the scraper cleanly.
- Keep request frequency reasonable and respect Amazon's applicable terms.
- This project is for **personal price monitoring**. You are responsible for complying with Amazon's Terms of Service in your jurisdiction.

---

## 📜 License

MIT — see `LICENSE`.
