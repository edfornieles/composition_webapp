"""
Polite HTTP helpers for the ingestion pipeline.

This module owns the two pieces of "download etiquette" that used to be
missing from :func:`djangoscrap.views._download_ingestion_url`:

1. **Per-host throttle** — a semaphore + minimum-delay gate keyed by
   hostname. Before this existed, a single campaign cycle could fire
   30+ back-to-back requests at ``i.pinimg.com``; after the thumbnail→
   ``/originals/`` rewriter went in, that traffic concentrated onto
   a smaller number of CDN paths and started attracting 403 / 429
   responses. :class:`HostThrottle` keeps concurrency per host bounded
   (``max_concurrency=2``) and guarantees at least ``min_delay_s``
   (default 0.4 s) between consecutive requests to the same host.

2. **HEAD-before-GET probing** — a short HEAD request that asks the
   server for ``Content-Type`` and ``Content-Length`` without pulling
   the body. This lets us drop:

   * broken URLs (404 / 410 / DNS fail) before they create tombstones
     on disk,
   * tiny thumbnails/icons masquerading as content (e.g. a 2 KB
     sprite), and
   * non-image / non-video responses (error pages, redirects to
     captchas, ``text/html`` from dead CDNs).

   Many CDNs either don't implement HEAD or respond ``405`` — those
   cases fall through to the caller's normal GET path rather than
   causing false rejections (see :class:`HeadProbe.retry_with_get`).

Both pieces are tolerant by design: if the throttle can't acquire its
semaphore within a generous timeout, or if HEAD fails in a way we don't
understand, we let the caller proceed with its GET. The goal is
fewer wasted downloads, not a stricter pipeline that starves on
edge cases.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


# --------------------------------------------------------------------------- #
#  Config                                                                     #
# --------------------------------------------------------------------------- #

# Default user agent used for HEAD and GET. Kept consistent with the
# existing downloader so hosts that whitelist our UA still work.
DEFAULT_USER_AGENT = "CompositionWebappIngestion/1.0"

# Per-host throttle defaults. Tuned conservatively; most sites are fine with
# ~2 concurrent requests and a half-second gap. If a specific host turns out
# to need tighter politeness it can be raised via ``HostThrottle.set_host(...)``.
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_MIN_DELAY_S = 0.4

# Per-host concurrency/delay overrides for sites that need heavier politeness
# or are known to tolerate more parallelism. Add sparingly — most of the
# value comes from the defaults.
_PER_HOST_OVERRIDES: dict[str, tuple[int, float]] = {
    # Pinterest CDN: after the /originals/ rewriter hit we briefly saw some
    # 429s. 1 concurrent req + 0.7s gap keeps it quiet.
    "i.pinimg.com": (1, 0.7),
    # Reddit has strict per-IP rate limits.
    "i.redd.it": (1, 1.0),
    "preview.redd.it": (1, 1.0),
}

# HEAD-probe defaults. 15 KB minimum catches most "this is a UI sprite, not
# a photo" cases without blocking legitimate small icons that happen to be
# the desired content (e.g. some illustration ingestions). Adjust at the
# call site if needed.
DEFAULT_HEAD_TIMEOUT_S = 6
DEFAULT_MIN_BYTES_IMAGE = 15 * 1024
DEFAULT_MIN_BYTES_VIDEO = 100 * 1024


# --------------------------------------------------------------------------- #
#  Host throttle                                                              #
# --------------------------------------------------------------------------- #


class _HostState:
    """Per-host lock + last-request timestamp, tracked inside ``HostThrottle``."""

    __slots__ = ("semaphore", "last_ts", "lock")

    def __init__(self, max_concurrency: int) -> None:
        self.semaphore = threading.Semaphore(max_concurrency)
        self.last_ts = 0.0
        self.lock = threading.Lock()


class HostThrottle:
    """
    Process-wide per-host politeness gate. Acquire it as a context manager:

        with HOST_THROTTLE.hold("i.pinimg.com"):
            # do one HTTP request here; throttle enforces:
            #   - at most N concurrent requests to this host, AND
            #   - at least min_delay_s since the last request to this host.
            ...

    Non-throttled callers bypass this with no setup; the throttle is optional.
    """

    def __init__(
        self,
        default_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        default_min_delay_s: float = DEFAULT_MIN_DELAY_S,
    ) -> None:
        self._default_concurrency = default_concurrency
        self._default_min_delay_s = default_min_delay_s
        self._state: dict[str, _HostState] = {}
        self._overrides: dict[str, tuple[int, float]] = dict(_PER_HOST_OVERRIDES)
        self._creation_lock = threading.Lock()

    def set_host(self, host: str, *, max_concurrency: int, min_delay_s: float) -> None:
        """Override concurrency / delay for a single host. Takes effect for new holds."""
        self._overrides[host.lower()] = (max_concurrency, min_delay_s)
        # Reset existing state so the new concurrency applies next acquisition.
        self._state.pop(host.lower(), None)

    def _get_state(self, host: str) -> tuple[_HostState, float]:
        host_key = host.lower()
        concurrency, min_delay = self._overrides.get(
            host_key, (self._default_concurrency, self._default_min_delay_s)
        )
        state = self._state.get(host_key)
        if state is None:
            # Double-checked creation: cheap fast path when state exists, and
            # a single global lock only when we need to allocate.
            with self._creation_lock:
                state = self._state.get(host_key)
                if state is None:
                    state = _HostState(max_concurrency=concurrency)
                    self._state[host_key] = state
        return state, min_delay

    class _Hold:
        def __init__(self, throttle: "HostThrottle", host: str, timeout: float) -> None:
            self._throttle = throttle
            self._host = host
            self._timeout = timeout
            self._state: Optional[_HostState] = None
            self._acquired = False

        def __enter__(self) -> "HostThrottle._Hold":
            state, min_delay = self._throttle._get_state(self._host)
            self._state = state
            # Respect concurrency. Block up to ``timeout`` seconds — if we
            # can't get in, fall through unthrottled rather than fail the
            # whole download. The whole point is politeness, not correctness.
            self._acquired = state.semaphore.acquire(timeout=self._timeout)
            if not self._acquired:
                return self
            # Respect minimum inter-request delay. We hold state.lock only
            # long enough to read + update the last-request timestamp.
            now = time.monotonic()
            with state.lock:
                gap = now - state.last_ts
                sleep_for = min_delay - gap
                # Schedule the "next" last_ts now so concurrent waiters
                # that enter the lock in succession stagger themselves
                # without all seeing the same stale timestamp.
                if sleep_for > 0:
                    state.last_ts = now + sleep_for
                else:
                    state.last_ts = now
            if sleep_for > 0:
                time.sleep(sleep_for)
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            if self._acquired and self._state is not None:
                self._state.semaphore.release()

    def hold(self, host: str, *, timeout: float = 15.0) -> "HostThrottle._Hold":
        return HostThrottle._Hold(self, host, timeout)


# Process-wide singleton. Safe to import from anywhere.
HOST_THROTTLE = HostThrottle()


# --------------------------------------------------------------------------- #
#  HEAD probe                                                                 #
# --------------------------------------------------------------------------- #


@dataclass
class HeadProbe:
    """
    Result of a HEAD probe. ``ok`` is True when the response looks usable;
    ``reason`` is a short tag meant for telemetry (e.g. ``too_small``,
    ``wrong_type``, ``http_404``). ``retry_with_get`` is True when HEAD
    itself failed in a way that's inconclusive (e.g. the server returned
    405 Method Not Allowed) — the caller should fall through to its normal
    GET path rather than treat the URL as broken.
    """

    ok: bool
    reason: str
    status: int = 0
    content_length: int = 0
    content_type: str = ""
    retry_with_get: bool = False


def head_probe(
    url: str,
    *,
    timeout: float = DEFAULT_HEAD_TIMEOUT_S,
    min_bytes_image: int = DEFAULT_MIN_BYTES_IMAGE,
    min_bytes_video: int = DEFAULT_MIN_BYTES_VIDEO,
    user_agent: str = DEFAULT_USER_AGENT,
    throttle: Optional[HostThrottle] = HOST_THROTTLE,
) -> HeadProbe:
    """
    Issue a HEAD request and decide whether to proceed with a GET.

    Returns a :class:`HeadProbe`. Callers should:

    * skip the URL entirely when ``probe.ok is False and probe.retry_with_get is False``
      (known bad — 404, wrong content type, too small);
    * fall back to a normal GET when ``probe.retry_with_get`` is True
      (HEAD unsupported or transient error — we just don't know yet);
    * proceed to GET when ``probe.ok`` is True.

    Known-tricky cases we intentionally treat as ``retry_with_get``:

    * ``Content-Length`` absent (many CDNs skip it on HEAD).
    * ``Content-Type`` absent (some shared hosts).
    * Redirects returning a 3xx without body metadata (most clients would
      have followed it anyway).
    * SSL handshake errors / connection resets (often flaky on HEAD but
      succeed on GET with the browser-ish UA we send).
    """
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return HeadProbe(ok=False, reason="bad_url")

    req = Request(url, headers={"User-Agent": user_agent}, method="HEAD")
    throttle_cm = throttle.hold(host) if throttle else _NULL_CM
    try:
        with throttle_cm:
            try:
                resp = urlopen(req, timeout=timeout)
            except HTTPError as exc:
                # 405 Method Not Allowed / 501 Not Implemented → server doesn't
                # support HEAD. Fall through to GET.
                if exc.code in (405, 501):
                    return HeadProbe(
                        ok=False,
                        reason=f"head_unsupported_{exc.code}",
                        status=exc.code,
                        retry_with_get=True,
                    )
                # 403 can be HEAD-specific (some CDNs forbid HEAD). Retry
                # with GET and let the caller decide.
                if exc.code == 403:
                    return HeadProbe(
                        ok=False,
                        reason="head_forbidden",
                        status=403,
                        retry_with_get=True,
                    )
                return HeadProbe(
                    ok=False,
                    reason=f"http_{exc.code}",
                    status=exc.code,
                )
            except URLError:
                return HeadProbe(ok=False, reason="network_error", retry_with_get=True)
            except TimeoutError:
                return HeadProbe(ok=False, reason="head_timeout", retry_with_get=True)
            except Exception:
                return HeadProbe(ok=False, reason="head_exception", retry_with_get=True)

            with resp:
                status = getattr(resp, "status", 200) or 200
                headers = resp.headers
                content_type = (headers.get("Content-Type") or "").lower().strip()
                content_length_raw = headers.get("Content-Length")
                try:
                    content_length = int(content_length_raw) if content_length_raw else 0
                except (ValueError, TypeError):
                    content_length = 0
    finally:
        pass

    is_image = "image" in content_type
    is_video = "video" in content_type

    if not content_type:
        # Missing type — don't call it broken, just let the caller GET it.
        return HeadProbe(
            ok=False,
            reason="missing_content_type",
            status=status,
            content_length=content_length,
            content_type="",
            retry_with_get=True,
        )

    if not (is_image or is_video):
        return HeadProbe(
            ok=False,
            reason="wrong_type",
            status=status,
            content_length=content_length,
            content_type=content_type,
        )

    if content_length:
        min_bytes = min_bytes_video if is_video else min_bytes_image
        if content_length < min_bytes:
            return HeadProbe(
                ok=False,
                reason="too_small",
                status=status,
                content_length=content_length,
                content_type=content_type,
            )

    return HeadProbe(
        ok=True,
        reason="ok",
        status=status,
        content_length=content_length,
        content_type=content_type,
    )


class _NullCM:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_NULL_CM = _NullCM()


# --------------------------------------------------------------------------- #
#  Diagnostic counters                                                        #
# --------------------------------------------------------------------------- #


@dataclass
class DownloadStats:
    """
    Accumulator for download-path telemetry. One instance per campaign cycle;
    pass it into :func:`djangoscrap.views._download_ingestion_url` and the
    cycle's ``campaign_last_report`` picks up a summary line like:

        download: probed=176 head_blocked=19 (too_small:12,wrong_type:5,http_404:2)
                  get_ok=145 get_failed=12 parallel=8 wall_ms=12345
    """

    probed: int = 0
    head_blocked: int = 0
    head_unsupported: int = 0
    get_ok: int = 0
    get_failed: int = 0
    # reason-code histogram so we can see *why* things got blocked.
    reasons: dict = None
    # Phase 1B: record the parallel fan-out config and the wall clock elapsed
    # for the download fan-out so we can prove the speedup in
    # ``campaign_last_report``. Populated by ``_run_campaign_cycle`` after the
    # fan-out completes.
    parallel_workers: int = 0
    wall_ms: int = 0

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = {}

    def bump_reason(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_summary(self) -> str:
        if not self.probed and not self.get_ok and not self.get_failed:
            return "download: (no attempts)"
        parts = [
            f"probed={self.probed}",
            f"head_blocked={self.head_blocked}",
        ]
        if self.head_unsupported:
            parts.append(f"head_unsup={self.head_unsupported}")
        parts.append(f"get_ok={self.get_ok}")
        parts.append(f"get_failed={self.get_failed}")
        if self.reasons:
            top = sorted(self.reasons.items(), key=lambda kv: -kv[1])[:5]
            parts.append("reasons=" + ",".join(f"{k}:{v}" for k, v in top))
        if self.parallel_workers:
            parts.append(f"parallel={self.parallel_workers}")
        if self.wall_ms:
            parts.append(f"wall_ms={self.wall_ms}")
        return "download: " + " ".join(parts)
