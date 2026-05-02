"""
Parallel download orchestration for the ingestion pipeline.

Motivation
==========
Campaign cycles spent most of their wall-clock time inside
:func:`djangoscrap.views._download_ingestion_url`, which does exactly one
synchronous HTTP round-trip per URL. With ``candidate_urls`` budgets of
~180 per cycle, a cycle would spend 60–120 seconds just waiting on the
network even when most URLs were duplicates or blocked by HEAD-probe.

Fanning out the HTTP waits across a small thread pool converts that from
``sum(per-url latency)`` to ``max(per-url latency)`` across each batch of
``max_workers``. The HOST_THROTTLE already guarantees per-host politeness
is preserved even under concurrent dispatch — within a single host we
stay ≤ N concurrent + ≥ delay_s gaps; across hosts we get free speedup.

Design
======
This module deliberately does NOT know about Django models or the rest
of the ingestion pipeline. It takes:

* a list of URLs,
* a ``downloader`` callable that does the per-URL work,

and returns an iterator of ``(url, result)`` pairs in completion order.
The consumer is responsible for dedupe / budget / DB writes.

Tested against a mocked downloader that sleeps for deterministic
intervals so the speedup assertion is stable across CI machines.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterator, Optional

_LOGGER = logging.getLogger("djangoscrap.ingestion_download")

# Default worker pool size. 8 matches the rule of thumb for I/O-bound
# Python work: enough parallelism to hide network latency, small enough
# that the shared SQLite writer / CLIP encoder doesn't get starved by
# the consumer side.
DEFAULT_MAX_WORKERS = 8


def parallel_download_urls(
    urls: list[str],
    downloader: Callable[[str], Any],
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    stop_when: Optional[Callable[[], bool]] = None,
) -> Iterator[tuple[str, Any]]:
    """
    Fan out ``downloader(url)`` calls across a thread pool and yield
    ``(url, result)`` pairs as each future completes.

    Ordering is NOT guaranteed — the consumer in ``_run_campaign_cycle``
    de-dupes on ``sha256`` / ``original_url`` regardless of arrival order,
    so completion-order iteration is a pure throughput win.

    ``stop_when`` (optional) is consulted:
      * before submitting each new task — so budget-hit on the consumer
        side stops us from launching further work, and
      * before yielding each completed result — so a consumer that hits
        its budget mid-drain can stop iterating without blocking on the
        pool's teardown.

    On any exception inside ``downloader``, the yielded tuple is
    ``(url, None)`` so the caller can treat that as a failure and
    continue with the next URL. Exceptions are logged at WARN level with
    the URL for diagnostics.
    """
    if not urls:
        return
    workers = max(1, min(max_workers, len(urls)))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="ingest-dl"
    ) as pool:
        futures: dict = {}
        for url in urls:
            if stop_when is not None and stop_when():
                break
            futures[pool.submit(_safe_call, downloader, url)] = url
        for fut in as_completed(futures):
            if stop_when is not None and stop_when():
                break
            url = futures[fut]
            try:
                yield url, fut.result()
            except Exception as exc:  # pragma: no cover — _safe_call caught it
                _LOGGER.warning("ingest-dl unexpected future.result() error for %s: %r", url, exc)
                yield url, None


def _safe_call(downloader: Callable[[str], Any], url: str) -> Any:
    """Invoke ``downloader(url)`` returning None on any exception."""
    try:
        return downloader(url)
    except Exception as exc:
        _LOGGER.warning("ingest-dl downloader raised for %s: %r", url, exc)
        return None


# --------------------------------------------------------------------------- #
#  Lightweight counter for thread-safe stats bumps                            #
# --------------------------------------------------------------------------- #


class CounterLock:
    """
    Small context-manager wrapper around an object + a lock. Useful when the
    consumer wants to bump a ``DownloadStats`` counter from a worker thread
    without racing the main-thread reads.

    Usage:
        cl = CounterLock(stats)
        with cl:
            stats.probed += 1
    """

    def __init__(self, target: Any) -> None:
        self._target = target
        self._lock = threading.Lock()

    def __enter__(self):
        self._lock.acquire()
        return self._target

    def __exit__(self, *exc):
        self._lock.release()
