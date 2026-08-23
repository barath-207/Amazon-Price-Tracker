"""
Amazon scraping layer.

Amazon aggressively CAPTCHAs automated requests from datacenter IPs (GitHub
Actions runners included). Raw ``requests`` is therefore blocked ~100% of the
time from such IPs, while a real headless browser (Playwright/Chromium) passes
most bot checks because it has a genuine browser fingerprint and executes JS.

This module layers fetch strategies and tries them in order until one returns a
usable (non-CAPTCHA) page:

  Strategy ``requests`` : session-warmed HTTP only (fast, usually blocked on CI).
  Strategy ``playwright``: headless Chromium first, HTTP fallback (CI default).
  Strategy ``auto``      : HTTP first, Playwright on CAPTCHA (best of both).

It also:
  * resolves ``amzn.in``/``a.co`` short links to a canonical ``/dp/ASIN`` URL,
  * warms a cookie session against the homepage,
  * falls back to the mobile product endpoint,
  * optionally routes everything through a residential/datacenter proxy.

This is the ONLY module that talks to Amazon. Swapping it for an official
PA-API client later leaves history/statistics/notifications untouched.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Optional
from urllib.parse import urlparse

import requests

from .models import ScrapeResult
from .parser import extract_asin_from_url, normalize_url, parse_page
from .validators import looks_like_captcha, looks_like_not_found

log = logging.getLogger("tracker.scraper")


# Rotating desktop user-agents (Amazon blocks obvious bots / defaults).
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

ACCEPT_LANG_BY_DOMAIN = {
    "www.amazon.in": "en-IN,en;q=0.9,hi;q=0.8",
    "amazon.in": "en-IN,en;q=0.9,hi;q=0.8",
    "www.amazon.com": "en-US,en;q=0.9",
    "www.amazon.co.uk": "en-GB,en;q=0.9",
}

# Hosts whose URLs need redirect-resolution to reach the real product URL.
SHORT_HOSTS = ("amzn.in", "amzn.com", "amzn.to", "a.co", "amazon.ae", "amzn.eu")


class AmazonScraper:
    """Fetches and parses Amazon product pages using layered strategies."""

    def __init__(
        self,
        domain: str = "www.amazon.in",
        timeout: int = 20,
        max_retries: int = 3,
        backoff_base: int = 2,
        user_agents: Optional[list[str]] = None,
        fetch_strategy: str = "auto",          # requests | playwright | auto
        use_mobile_fallback: bool = True,
        warm_session: bool = True,
        proxy: Optional[str] = None,
    ) -> None:
        self.domain = domain
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_base = backoff_base
        self.user_agents = user_agents or USER_AGENTS
        self.fetch_strategy = fetch_strategy
        self.use_mobile_fallback = use_mobile_fallback
        self.warm_session = warm_session
        self.proxy = proxy
        self._session = requests.Session()
        self._warmed = False
        self._playwright = None        # sync_playwright() handle
        self._browser = None           # cached Chromium browser

    # ------------------------------------------------------------------
    # URL / header helpers
    # ------------------------------------------------------------------
    def _is_short_link(self, url: str) -> bool:
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return False
        return any(h in host for h in SHORT_HOSTS) or "/d/" in url

    def _resolve_url(self, url: str, expected_asin: Optional[str]) -> tuple[str, Optional[str]]:
        """Return (canonical_url, asin). Resolves short links via redirect."""
        asin = expected_asin or extract_asin_from_url(url)
        if asin:
            return normalize_url(url, self.domain), asin
        # No ASIN found -> try following the redirect of a short link.
        if self._is_short_link(url):
            try:
                # stream + close: we only need the final URL, not the body.
                resp = self._session.get(
                    url, headers=self._headers(), timeout=self.timeout,
                    allow_redirects=True, stream=True,
                )
                final = resp.url
                resp.close()
                asin = extract_asin_from_url(final)
                if asin:
                    return normalize_url(final, self.domain), asin
                return final, None
            except requests.RequestException as exc:
                log.debug("short-link resolve failed for %s: %s", url, exc)
        return url, None

    def _headers(self) -> dict[str, str]:
        ua = random.choice(self.user_agents)
        return {
            "User-Agent": ua,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": ACCEPT_LANG_BY_DOMAIN.get(self.domain, "en-US,en;q=0.9"),
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }

    def _proxies(self) -> Optional[dict[str, str]]:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def _mobile_url(self, url: str, asin: Optional[str]) -> Optional[str]:
        asin = asin or extract_asin_from_url(url)
        if not asin:
            return None
        return f"https://{self.domain}/gp/aw/d/{asin}"

    # ------------------------------------------------------------------
    # Session warming (populates cookies, looks more "human")
    # ------------------------------------------------------------------
    def _warm(self) -> None:
        if self._warmed:
            return
        try:
            self._session.get(
                f"https://{self.domain}/", headers=self._headers(),
                timeout=self.timeout, allow_redirects=True,
            )
        except requests.RequestException:
            pass
        self._warmed = True

    # ------------------------------------------------------------------
    # Fetch layer 1+2: HTTP
    # ------------------------------------------------------------------
    def _requests_get(self, url: str) -> tuple[Optional[str], Optional[int], Optional[str], float]:
        last_err: Optional[str] = None
        last_status: Optional[int] = None
        for attempt in range(1, self.max_retries + 1):
            t0 = time.monotonic()
            try:
                resp = self._session.get(
                    url, headers=self._headers(), timeout=self.timeout,
                    allow_redirects=True, proxies=self._proxies(),
                )
                elapsed = time.monotonic() - t0
                last_status = resp.status_code
                if resp.status_code == 200 and resp.text:
                    return resp.text, 200, None, elapsed
                if resp.status_code in (404, 410):
                    return None, resp.status_code, f"HTTP {resp.status_code} (not found)", elapsed
                if resp.text and looks_like_captcha(resp.text):
                    return resp.text, resp.status_code, "CAPTCHA/bot-detection page", elapsed
                last_err = f"HTTP {resp.status_code}"
            except requests.RequestException as exc:
                elapsed = time.monotonic() - t0
                last_err = f"request error: {exc}"
            if attempt < self.max_retries:
                time.sleep((self.backoff_base ** attempt) + random.uniform(0, 1.0))
        return None, last_status, last_err or "unknown error", 0.0

    # ------------------------------------------------------------------
    # Fetch layer 5: Playwright (lazy, optional dependency)
    # ------------------------------------------------------------------
    def _get_browser(self):
        if self._browser is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise ImportError(f"playwright not installed: {exc}")
            self._playwright = sync_playwright().start()
            launch_kwargs: dict = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            }
            if self.proxy:
                launch_kwargs["proxy"] = {"server": self.proxy}
            self._browser = self._playwright.chromium.launch(**launch_kwargs)
        return self._browser

    def _playwright_get(self, url: str) -> tuple[Optional[str], Optional[str]]:
        try:
            browser = self._get_browser()
        except ImportError as exc:
            return None, str(exc)
        try:
            ctx = browser.new_context(
                user_agent=random.choice(self.user_agents),
                locale="en-IN",
                viewport={"width": 1366, "height": 768},
                extra_http_headers={"Accept-Language": ACCEPT_LANG_BY_DOMAIN.get(self.domain, "en-US,en;q=0.9")},
            )
            page = ctx.new_page()
            page.goto(url, timeout=self.timeout * 1000 * 3, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            html = page.content()
            ctx.close()
            return html, None
        except Exception as exc:  # noqa: BLE001
            return None, f"playwright error: {exc}"

    def close(self) -> None:
        """Release the cached browser. Call once at the end of a run."""
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None

    # ------------------------------------------------------------------
    # Strategy orchestration
    # ------------------------------------------------------------------
    def _candidate_urls(self, canonical: str, asin: Optional[str]) -> list[str]:
        urls = [canonical]
        if self.use_mobile_fallback:
            mob = self._mobile_url(canonical, asin)
            if mob and mob not in urls:
                urls.append(mob)
        return urls

    def _fetch_html(self, canonical: str, asin: Optional[str]) -> tuple[Optional[str], Optional[str], bool]:
        """Return (html, error, used_fallback).

        Tries the configured strategy across candidate URLs. Returns the first
        non-empty, non-CAPTCHA, non-not-found HTML found.
        """
        candidates = self._candidate_urls(canonical, asin)
        err: Optional[str] = None
        used_fallback = False

        def try_http() -> tuple[Optional[str], Optional[str]]:
            nonlocal err
            for u in candidates:
                html, status, e, _ = self._requests_get(u)
                if html and not looks_like_captcha(html) and not looks_like_not_found(html):
                    return html, None
                if html and looks_like_captcha(html):
                    err = e or "CAPTCHA"
                elif e:
                    err = e
            return None, err

        def try_playwright() -> tuple[Optional[str], Optional[str]]:
            nonlocal err
            for u in candidates:
                html, e = self._playwright_get(u)
                if html and not looks_like_captcha(html) and not looks_like_not_found(html):
                    return html, None
                if e:
                    err = e
                elif html and looks_like_captcha(html):
                    err = "CAPTCHA (playwright)"
            return None, err

        if self.fetch_strategy == "requests":
            html, _ = try_http()
            return html, _, html is not None and candidates.index(canonical) != 0

        if self.fetch_strategy == "playwright":
            html, e = try_playwright()
            if html:
                return html, None, True
            # fall back to HTTP as a last resort.
            html, e = try_http()
            return html, e, True

        # auto: HTTP first, then Playwright if still blocked.
        html, _ = try_http()
        if html:
            return html, None, False
        log.info("HTTP blocked; trying Playwright fallback")
        html, e = try_playwright()
        return html, e, True

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def scrape(self, url: str, expected_asin: Optional[str] = None) -> ScrapeResult:
        """Scrape one product URL. Never raises - returns a ScrapeResult."""
        if self.warm_session:
            self._warm()

        canonical, asin = self._resolve_url(url, expected_asin)
        t0 = time.monotonic()
        html, err, used_fallback = self._fetch_html(canonical, asin)
        elapsed = time.monotonic() - t0

        if not html:
            return ScrapeResult(error=err or "no content", response_time=elapsed)

        if looks_like_captcha(html):
            return ScrapeResult(error="CAPTCHA/bot-detection page", blocked=True, response_time=elapsed)
        if looks_like_not_found(html):
            return ScrapeResult(error="product not found / removed", status_code=404, response_time=elapsed)

        try:
            obs = parse_page(html, url=canonical, expected_asin=asin, domain=self.domain)
        except Exception as exc:  # noqa: BLE001
            return ScrapeResult(error=f"parse error: {exc}", response_time=elapsed)

        return ScrapeResult(observation=obs, status_code=200, response_time=elapsed, used_fallback=used_fallback)
