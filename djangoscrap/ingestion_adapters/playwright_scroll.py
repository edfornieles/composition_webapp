"""
Playwright-based scroll-harvester for infinite-scroll surfaces
(Pinterest boards, Tumblr tag pages, Pixiv search, etc).

How it works
============
* Launch headless Chromium (via the existing ``playwright`` install),
* Navigate to ``url``,
* Repeatedly scroll to the bottom of the viewport,
* Between scrolls, pull ``<img src>`` and ``srcset`` attributes out of
  the DOM and accumulate a set,
* Stop when either (a) ``max_urls`` images collected, (b) no new images
  appeared after ``max_idle_passes`` consecutive scrolls, or
  (c) ``timeout_s`` wall-clock elapsed.

Selectors are per-host so the harvester only pulls image nodes that
actually matter (avoids picking up avatars, ad previews, footer icons).

Registered adapters
===================
* :class:`PinterestBoardAdapter` — priority 100, Pinterest board/search/ideas.
* :class:`TumblrTagAdapter`      — priority 100, Tumblr tag + blog pages.

Both share the same scroll engine and differ only in selector + URL
classifier. Adding a new site is a ~10 line change.

Testing
=======
Unit tests exercise the HTML-parsing / image-extraction logic with
fixture strings, not a live browser. Live integration is gated on
``RUN_BROWSER_TESTS=1`` so CI boxes without Playwright don't hang.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional
from urllib.parse import urlparse

from djangoscrap.ingestion_adapters.base import Adapter, AdapterResult

_LOGGER = logging.getLogger("djangoscrap.adapters.playwright_scroll")


_IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif|avif)(?:$|\?)", re.IGNORECASE)


def extract_image_urls_from_html(html: str, host_filter: Optional[str] = None) -> list[str]:
    """
    Pull image URLs out of a snapshot of ``document.documentElement.outerHTML``.

    Extraction rules:
      * Matches ``<img src="..."`` and ``srcset="..."`` attributes.
      * For srcset, splits on commas and takes the URL portion (not the
        width / pixel-density descriptor).
      * Filters to URLs whose path ends in a known image extension.
      * If ``host_filter`` is provided (e.g. ``pinimg.com``), only URLs on
        that host are returned.

    This function is pure-text — no browser required — so it's cheap to
    unit-test with representative HTML blobs.
    """
    found: list[str] = []
    seen: set[str] = set()

    # <img src="…">
    for match in re.finditer(r'<img\b[^>]*\bsrc="([^"]+)"', html, re.IGNORECASE):
        url = match.group(1).strip()
        _maybe_add(found, seen, url, host_filter)

    # <img srcset="url1 640w, url2 1280w, …">
    for match in re.finditer(r'\bsrcset="([^"]+)"', html, re.IGNORECASE):
        for piece in match.group(1).split(","):
            # "https://cdn/example.jpg 1280w" → first whitespace-separated token.
            url = piece.strip().split(None, 1)[0] if piece.strip() else ""
            _maybe_add(found, seen, url, host_filter)

    # Also pick up raw image URLs inside data-src / data-original attrs (common
    # lazy-load pattern used by both Pinterest and Tumblr).
    for match in re.finditer(r'\bdata-(?:src|original)="([^"]+)"', html, re.IGNORECASE):
        _maybe_add(found, seen, match.group(1).strip(), host_filter)

    return found


def _maybe_add(out: list[str], seen: set[str], url: str, host_filter: Optional[str]) -> None:
    if not url:
        return
    if not (url.startswith("http://") or url.startswith("https://")):
        return
    if not _IMG_EXT_RE.search(url):
        return
    if host_filter:
        host = (urlparse(url).netloc or "").lower()
        if host_filter.lower() not in host:
            return
    if url in seen:
        return
    seen.add(url)
    out.append(url)


# --------------------------------------------------------------------------- #
#  Scroll engine                                                              #
# --------------------------------------------------------------------------- #


class _PlaywrightScroller:
    """
    Thin wrapper around ``playwright.sync_api``. Split out so the adapter
    glue code can be tested without a browser.
    """

    def __init__(self, *, timeout_s: int = 60, max_idle_passes: int = 5):
        self._timeout = timeout_s
        self._max_idle = max_idle_passes

    def collect(
        self,
        url: str,
        *,
        max_urls: int,
        host_filter: Optional[str],
    ) -> list[str]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            _LOGGER.info("playwright not importable; scroll-harvester unavailable")
            return []
        collected: list[str] = []
        seen: set[str] = set()
        started = time.monotonic()
        idle_passes = 0
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1440, "height": 900},
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                # Give JS-heavy pages (Pinterest) time to render initial results
                # before we start scraping and scrolling.
                page.wait_for_timeout(2500)
                while True:
                    if time.monotonic() - started > self._timeout:
                        break
                    html = page.content()
                    before = len(collected)
                    for extracted in extract_image_urls_from_html(html, host_filter):
                        if extracted in seen:
                            continue
                        seen.add(extracted)
                        collected.append(extracted)
                        if len(collected) >= max_urls:
                            break
                    if len(collected) >= max_urls:
                        break
                    if len(collected) == before:
                        idle_passes += 1
                        if idle_passes >= self._max_idle:
                            break
                    else:
                        idle_passes = 0
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(800)
                context.close()
                browser.close()
        except Exception as exc:
            _LOGGER.warning("playwright scroll failed for %s: %r", url, exc)
        return collected[:max_urls]


# --------------------------------------------------------------------------- #
#  Adapters                                                                   #
# --------------------------------------------------------------------------- #


class _ScrollAdapterBase:
    """Common scaffold — subclasses supply name, hosts, selector hint."""

    name: str = "scroll-base"
    priority: int = 100
    host_suffixes: tuple[str, ...] = ()
    cdn_host_filter: Optional[str] = None

    def __init__(self, *, scroller: Optional[_PlaywrightScroller] = None):
        self._scroller = scroller or _PlaywrightScroller()

    def can_handle(self, url: str) -> bool:
        if not url:
            return False
        try:
            host = (urlparse(url).netloc or "").lower().removeprefix("www.")
        except Exception:
            return False
        return any(host == h or host.endswith("." + h) for h in self.host_suffixes)

    def extract(self, url: str, *, max_urls: int, state: dict) -> AdapterResult:
        urls = self._scroller.collect(
            url, max_urls=max_urls, host_filter=self.cdn_host_filter
        )
        if not urls:
            return AdapterResult(error=f"{self.name}: 0 urls collected")
        return AdapterResult(
            urls=urls,
            next_state=dict(state or {}),
            diagnostics={"collected": str(len(urls))},
        )


class PinterestBoardAdapter(_ScrollAdapterBase):
    name = "pinterest-board"
    priority = 100
    host_suffixes = ("pinterest.com", "pinterest.ca", "pinterest.co.uk")
    cdn_host_filter = "pinimg.com"


class TumblrTagAdapter(_ScrollAdapterBase):
    name = "tumblr-tag"
    priority = 100
    host_suffixes = ("tumblr.com",)
    cdn_host_filter = "media.tumblr.com"
