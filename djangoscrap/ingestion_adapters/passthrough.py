"""
PassthroughAdapter — reference implementation of the Adapter protocol.

Used as:
  1. A sanity test of the Adapter framework (included in registry tests).
  2. A fallback that simply treats the input URL as an already-extracted
     image URL. This is what happens when the user pastes a direct CDN
     link: no scraping needed, the URL is the output.

Also a living reference for anyone writing a new adapter: copy this
file, change the name / can_handle / extract and you have a working
adapter in ~30 lines.
"""

from __future__ import annotations

from djangoscrap.ingestion_adapters.base import Adapter, AdapterResult


_IMAGE_EXT_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")
_VIDEO_EXT_SUFFIXES = (".mp4", ".webm", ".mov", ".m4v")


class PassthroughAdapter:
    """Treat any direct CDN image/video URL as a single-URL extraction result."""

    name: str = "passthrough"
    # Last-resort priority: only runs when nothing more specific matched.
    priority: int = 10

    def can_handle(self, url: str) -> bool:
        if not url:
            return False
        low = url.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            return False
        # Strip any query string before extension check so ``photo.jpg?s=1`` counts.
        without_query = low.split("?", 1)[0]
        return without_query.endswith(_IMAGE_EXT_SUFFIXES + _VIDEO_EXT_SUFFIXES)

    def extract(self, url: str, *, max_urls: int, state: dict) -> AdapterResult:
        # The input URL IS the extraction result. No scrape needed.
        return AdapterResult(
            urls=[url],
            next_state=dict(state or {}),  # unchanged
            diagnostics={"source": "direct_link"},
        )
