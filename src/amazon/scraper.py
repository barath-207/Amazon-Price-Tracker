"""
Amazon scraping layer.

Layered fetching strategy:

  Layer 1 - plain HTTP (requests) with realistic headers + retries.
  Layer 2 - retry with a rotated user-agent on a transient failure.
  Layer 3 - optional Playwright render, enabled only when configured, for
            pages whose price is dynamically injected.

This module is the ONLY place that talks to Amazon. Swapping it for an
official PA-API client later leaves history/statistics/notifications untouched.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Optional

import requests

from .models import ScrapeResult
from .parser import parse_page
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


class AmazonScraper:
    """Fetches and parses Amazon product pages."""

    def __init__(
        self,
        domain: str = "www.amazon.in",
        timeout: int = 20,
        max_retries: int = 3,
        backoff_base: int = 2,
        user_agents: Optional[list[str]] = None,
        use_playwright: bool = False,
    ) -> None:
        self.domain = domain
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_base = backoff_base
        self.user_agents = user_agents or USER_AGENTS
        self.use_playwright = use_playwright
        self._session = requests.Session()

    # ------------------------------------------------------------------
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
        }

    # ------------------------------------------------------------------
    def _fetch_html(self, url: str) -> tuple[Optional[str], Optional[int], Optional[str], float]:
        """Fetch HTML with retries + exponential backoff.

        Returns (html, status_code, error, elapsed_seconds).
        """
        last_err: Optional[str] = None
        last_status: Optional[int] = None
        for attempt in range(1, self.max_retries + 1):
            t0 = time.monotonic()
            try:
                resp = self._session.get(
                    url, headers=self._headers(), timeout=self.timeout, allow_redirects=True
                )
                elapsed = time.monotonic() - t0
                last_status = resp.status_code
                if resp.status_code == 200 and resp.text:
                    return resp.text, 200, None, elapsed
                # 404 / gone -> not worth retrying.
                if resp.status_code in (404, 410):
                    return None, resp.status_code, f"HTTP {resp.status_code} (not found)", elapsed
                # Captcha page (often 200 or 503) -> stop retrying HTTP.
                if resp.text and looks_like_captcha(resp.text):
                    return resp.text, resp.status_code, "CAPTCHA/bot-detection page", elapsed
                last_err = f"HTTP {resp.status_code}"
            except requests.RequestException as exc:  # network / timeout
                elapsed = time.monotonic() - t0
                last_err = f"request error: {exc}"

            if attempt < self.max_retries:
                sleep_for = (self.backoff_base ** attempt) + random.uniform(0, 1.0)
                log.debug("retry %d/%d for %s after %.1fs (%s)", attempt, self.max_retries, url, sleep_for, last_err)
                time.sleep(sleep_for)
        return None, last_status, last_err or "unknown error", 0.0

    # ------------------------------------------------------------------
    def _playwright_fetch(self, url: str) -> tuple[Optional[str], Optional[str]]:
        """Render a page with Playwright. Imported lazily (optional dep).

        Returns (html, error).
        """
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            return None, f"playwright not installed: {exc}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(user_agent=random.choice(self.user_agents))
                page = ctx.new_page()
                page.goto(url, timeout=self.timeout * 1000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                html = page.content()
                browser.close()
                return html, None
        except Exception as exc:  # noqa: BLE001
            return None, f"playwright error: {exc}"

    # ------------------------------------------------------------------
    def scrape(self, url: str, expected_asin: Optional[str] = None) -> ScrapeResult:
        """Scrape one product URL. Never raises - returns a ScrapeResult."""
        html, status, err, elapsed = self._fetch_html(url)
        used_fallback = False

        # Captcha / block -> try Playwright if enabled.
        if html and looks_like_captcha(html) and self.use_playwright:
            log.info("CAPTCHA detected; trying Playwright fallback")
            pw_html, pw_err = self._playwright_fetch(url)
            if pw_html and not looks_like_captcha(pw_html):
                html, err, used_fallback = pw_html, None, True
            elif pw_err:
                log.debug("playwright fallback failed: %s", pw_err)

        # No usable HTML at all -> try Playwright once if enabled.
        if (not html) and self.use_playwright:
            log.info("HTTP fetch empty; trying Playwright fallback")
            pw_html, pw_err = self._playwright_fetch(url)
            if pw_html:
                html, err, used_fallback = pw_html, None, True
            elif pw_err:
                err = err or pw_err

        if not html:
            return ScrapeResult(error=err or "no content", status_code=status, response_time=elapsed)

        if looks_like_captcha(html):
            return ScrapeResult(
                error="CAPTCHA/bot-detection page", status_code=status,
                blocked=True, response_time=elapsed,
            )
        if looks_like_not_found(html):
            return ScrapeResult(
                error="product not found / removed", status_code=status or 404,
                response_time=elapsed,
            )

        try:
            obs = parse_page(html, url=url, expected_asin=expected_asin, domain=self.domain)
        except Exception as exc:  # noqa: BLE001
            return ScrapeResult(error=f"parse error: {exc}", status_code=status, response_time=elapsed)

        return ScrapeResult(
            observation=obs, status_code=status, response_time=elapsed, used_fallback=used_fallback
        )
