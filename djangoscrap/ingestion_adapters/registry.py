"""
Adapter registry + dispatcher.

Adapters register themselves at import time via :func:`register`. The
dispatcher picks the highest-priority adapter whose ``can_handle(url)``
returns True, falling through on empty results so generic fallbacks
(yt-dlp, Yandex HTML) still run when specialized adapters miss.

Priority convention
===================
* **100** — one-site specialization (``PinterestBoardAdapter``,
  ``InstagrapiAdapter``, ``PixivAdapter``). Only matches that specific
  site's URL shape; never runs on other URLs.
* **50** — generic multi-site engines (yt-dlp, gallery-dl). Match a wide
  surface but defer to specialized adapters first.
* **10** — last-resort fallbacks (raw HTML scrape, Yandex HTML extractor).

Tied priorities resolve by registration order (first-wins). Adapters can
override this by calling ``register(adapter, priority_override=…)``.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable, Optional

from djangoscrap.ingestion_adapters.base import Adapter, AdapterResult

_LOGGER = logging.getLogger("djangoscrap.adapters.registry")

_REGISTRY: list[Adapter] = []
_REGISTRY_LOCK = threading.Lock()


def register(adapter: Adapter, *, priority_override: Optional[int] = None) -> None:
    """
    Add ``adapter`` to the registry. If ``priority_override`` is given,
    it overrides the adapter's class-level ``priority`` for this registration.
    Re-registering an adapter with the same name replaces the previous
    entry, which makes hot-reloading in tests straightforward.
    """
    if priority_override is not None:
        try:
            adapter.priority = int(priority_override)  # type: ignore[assignment]
        except Exception:
            pass
    with _REGISTRY_LOCK:
        _REGISTRY[:] = [a for a in _REGISTRY if a.name != adapter.name]
        _REGISTRY.append(adapter)
        _REGISTRY.sort(key=lambda a: (-int(getattr(a, "priority", 50)), a.name))
    _LOGGER.info("adapter registered: %s priority=%s", adapter.name, adapter.priority)


def unregister(name: str) -> bool:
    """Remove the adapter with ``name`` from the registry. Returns True if found."""
    with _REGISTRY_LOCK:
        before = len(_REGISTRY)
        _REGISTRY[:] = [a for a in _REGISTRY if a.name != name]
        return len(_REGISTRY) != before


def list_registered() -> list[Adapter]:
    """Return the current registry snapshot (highest priority first)."""
    with _REGISTRY_LOCK:
        return list(_REGISTRY)


def pick_for(url: str) -> list[Adapter]:
    """
    Return every adapter whose ``can_handle(url)`` returns True, ordered
    from highest priority to lowest. The dispatcher iterates this list
    and stops on the first adapter whose ``extract()`` returns ``ok``
    (non-empty URLs and no error).
    """
    matches: list[Adapter] = []
    with _REGISTRY_LOCK:
        snapshot = list(_REGISTRY)
    for adapter in snapshot:
        try:
            if adapter.can_handle(url):
                matches.append(adapter)
        except Exception as exc:
            _LOGGER.warning(
                "adapter %s raised in can_handle(%s): %r", adapter.name, url, exc
            )
    return matches


def dispatch(
    url: str,
    *,
    max_urls: int,
    state: dict,
    enabled_adapters: Optional[Iterable[str]] = None,
) -> tuple[Optional[Adapter], Optional[AdapterResult]]:
    """
    Run the highest-priority matching adapter that isn't disabled. Fall
    through to the next one if the chosen adapter returns an empty /
    error result.

    ``enabled_adapters`` (optional) whitelists adapter names — anything
    not in the set is skipped. This is how ``campaign_state.adapters``
    feature-flags new engines: default-off until the per-adapter smoke
    test passes on a real batch.

    Returns ``(adapter, result)`` for the adapter that produced URLs, or
    ``(None, None)`` if no adapter matched (caller falls back to
    pre-adapter scraping behavior).
    """
    enabled_set = set(enabled_adapters) if enabled_adapters is not None else None
    for adapter in pick_for(url):
        if enabled_set is not None and adapter.name not in enabled_set:
            continue
        try:
            result = adapter.extract(url, max_urls=max_urls, state=state)
        except Exception as exc:
            _LOGGER.warning("adapter %s raised in extract: %r", adapter.name, exc)
            continue
        if result is None:
            continue
        if not result.ok:
            _LOGGER.info(
                "adapter %s returned empty/error for %s: %s",
                adapter.name,
                url,
                result.error or "no urls",
            )
            continue
        return adapter, result
    return None, None
