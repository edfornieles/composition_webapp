"""
Adapter protocol + result dataclass.

Every adapter implements the following contract:

    name: str                         # "pinterest_board", "instagrapi", ...
    priority: int = 50                # higher wins when multiple adapters match
                                      # a URL (100 = explicit specialization,
                                      # 50 = generic engine, 10 = last-resort)
    def can_handle(self, url: str) -> bool: ...
    def extract(self, url: str, *, max_urls: int,
                state: dict) -> AdapterResult: ...

Adapters must be **stateless across calls** — any resumable-sync state
(Instagram last-post cursor, Pinterest board scroll offset) is carried on
the ``IngestionBatch.campaign_state`` JSON, passed in via ``state`` and
returned in ``AdapterResult.next_state``. That way:

  * A single adapter instance can serve many batches concurrently,
  * A batch can migrate between adapters (e.g. instagrapi → yt-dlp
    fallback) without data loss,
  * State is always visible for debugging in the Django admin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class AdapterResult:
    """
    Return value from ``Adapter.extract()``. URLs are the primary payload;
    everything else is diagnostic or state.

    ``diagnostics`` is merged into ``campaign_last_report`` under the
    adapter's name (e.g. ``adapter=pinterest_board:page_count=4``). Keep
    keys short and values stringifiable — they'll end up concatenated
    into a ~4000 char report field.

    ``next_state`` is stored back onto ``IngestionBatch.campaign_state``
    under ``adapter_state[adapter.name]``. Use it for resumable sync
    markers (last post id, next page cursor, etc).
    """

    urls: list[str] = field(default_factory=list)
    next_state: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    # Optional free-form error string for when the adapter wanted to
    # return nothing but still have something to log. Non-None means
    # "adapter ran but didn't produce usable URLs". Downstream code
    # tries the next registered adapter; see registry.dispatch().
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.urls)


@runtime_checkable
class Adapter(Protocol):
    """Pluggable URL extractor. Implemented by each new sourcing surface."""

    name: str
    priority: int

    def can_handle(self, url: str) -> bool:
        """True if this adapter knows how to extract URLs from ``url``."""
        ...

    def extract(self, url: str, *, max_urls: int, state: dict) -> AdapterResult:
        """
        Extract up to ``max_urls`` candidate URLs from ``url``.

        ``state`` is the adapter's per-batch resumable cursor (empty dict
        on first call). Returned ``AdapterResult.next_state`` replaces it.
        """
        ...
