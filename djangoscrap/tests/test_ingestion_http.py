"""
Tests for djangoscrap.ingestion_http.

Covers:
* HEAD probe decision matrix (ok / too_small / wrong_type / 404 / 405 / 403 /
  missing_content_type).
* HostThrottle enforces per-host concurrency and minimum inter-request delay.
* DownloadStats summary line format.

Uses ``unittest.mock.patch`` on ``urlopen`` — the real network is never
touched. This keeps the suite fast (<1 s) and deterministic.
"""

from __future__ import annotations

import io
import threading
import time
from unittest import mock

import pytest

from djangoscrap import ingestion_http as ih


# --------------------------------------------------------------------------- #
#  Helpers                                                                    #
# --------------------------------------------------------------------------- #


class _FakeResp:
    """Minimal duck-type for urllib.request response objects used by head_probe."""

    def __init__(self, *, status=200, content_type="image/jpeg", content_length=200_000):
        self.status = status
        headers = {"Content-Type": content_type}
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --------------------------------------------------------------------------- #
#  HEAD probe decision matrix                                                 #
# --------------------------------------------------------------------------- #


def test_head_probe_ok_large_image():
    with mock.patch.object(ih, "urlopen", return_value=_FakeResp(content_length=500_000)):
        p = ih.head_probe("https://cdn.example.com/big.jpg", throttle=None)
    assert p.ok is True
    assert p.reason == "ok"
    assert p.content_length == 500_000
    assert "image" in p.content_type


def test_head_probe_too_small_image_is_blocked():
    with mock.patch.object(ih, "urlopen", return_value=_FakeResp(content_length=5_000)):
        p = ih.head_probe("https://cdn.example.com/icon.jpg", throttle=None)
    assert p.ok is False
    assert p.reason == "too_small"
    assert p.retry_with_get is False


def test_head_probe_video_min_bytes_higher_than_image():
    """Video minimum is 100 KB, not 15 KB, so a 50 KB video counts as too_small."""
    with mock.patch.object(
        ih,
        "urlopen",
        return_value=_FakeResp(content_type="video/mp4", content_length=50_000),
    ):
        p = ih.head_probe("https://cdn.example.com/tiny.mp4", throttle=None)
    assert p.ok is False
    assert p.reason == "too_small"


def test_head_probe_wrong_content_type_blocked():
    with mock.patch.object(
        ih,
        "urlopen",
        return_value=_FakeResp(content_type="text/html; charset=utf-8", content_length=50_000),
    ):
        p = ih.head_probe("https://cdn.example.com/page", throttle=None)
    assert p.ok is False
    assert p.reason == "wrong_type"
    assert p.retry_with_get is False


def test_head_probe_missing_content_type_defers_to_get():
    resp = _FakeResp(content_type="", content_length=500_000)
    # Strip the header entirely so content_type evaluates to "".
    resp.headers = {"Content-Length": "500000"}
    with mock.patch.object(ih, "urlopen", return_value=resp):
        p = ih.head_probe("https://cdn.example.com/mystery", throttle=None)
    assert p.ok is False
    assert p.reason == "missing_content_type"
    assert p.retry_with_get is True


def test_head_probe_404_is_blocked_not_retried():
    from urllib.error import HTTPError
    err = HTTPError("https://x/y.jpg", 404, "Not Found", hdrs=None, fp=io.BytesIO(b""))
    with mock.patch.object(ih, "urlopen", side_effect=err):
        p = ih.head_probe("https://x/y.jpg", throttle=None)
    assert p.ok is False
    assert p.reason == "http_404"
    assert p.retry_with_get is False


def test_head_probe_405_method_not_allowed_retries_with_get():
    from urllib.error import HTTPError
    err = HTTPError("https://x/y.jpg", 405, "Method Not Allowed", hdrs=None, fp=io.BytesIO(b""))
    with mock.patch.object(ih, "urlopen", side_effect=err):
        p = ih.head_probe("https://x/y.jpg", throttle=None)
    assert p.ok is False
    assert p.reason == "head_unsupported_405"
    assert p.retry_with_get is True


def test_head_probe_403_retries_with_get():
    from urllib.error import HTTPError
    err = HTTPError("https://x/y.jpg", 403, "Forbidden", hdrs=None, fp=io.BytesIO(b""))
    with mock.patch.object(ih, "urlopen", side_effect=err):
        p = ih.head_probe("https://x/y.jpg", throttle=None)
    assert p.reason == "head_forbidden"
    assert p.retry_with_get is True


def test_head_probe_url_error_is_retried():
    from urllib.error import URLError
    with mock.patch.object(ih, "urlopen", side_effect=URLError("timeout")):
        p = ih.head_probe("https://x/y.jpg", throttle=None)
    assert p.reason == "network_error"
    assert p.retry_with_get is True


# --------------------------------------------------------------------------- #
#  HostThrottle                                                               #
# --------------------------------------------------------------------------- #


def test_host_throttle_enforces_min_delay():
    """Two consecutive holds for the same host must be at least min_delay_s apart."""
    throttle = ih.HostThrottle(default_concurrency=4, default_min_delay_s=0.2)
    start = time.monotonic()
    with throttle.hold("host-a.example.com"):
        pass
    with throttle.hold("host-a.example.com"):
        pass
    elapsed = time.monotonic() - start
    # Two holds with a 0.2s min gap → total elapsed ≥ 0.2s (first has no delay,
    # second inherits the gap).
    assert elapsed >= 0.18, f"expected ≥ 0.18s, got {elapsed:.3f}s"


def test_host_throttle_does_not_delay_across_hosts():
    """Different hosts are independent — no inter-host serialization."""
    throttle = ih.HostThrottle(default_concurrency=4, default_min_delay_s=0.3)
    start = time.monotonic()
    with throttle.hold("host-a.example.com"):
        pass
    with throttle.hold("host-b.example.com"):
        pass
    elapsed = time.monotonic() - start
    # No delay expected since the second hold is on a different host.
    assert elapsed < 0.15, f"expected fast crossover, got {elapsed:.3f}s"


def test_host_throttle_bounds_concurrency():
    """max_concurrency=1 means exactly one in-flight hold at a time for that host."""
    throttle = ih.HostThrottle(default_concurrency=1, default_min_delay_s=0.0)
    active = 0
    peak = 0
    peak_lock = threading.Lock()

    def worker():
        nonlocal active, peak
        with throttle.hold("busy.example.com"):
            with peak_lock:
                nonlocal_active_inc = active + 1
                active = nonlocal_active_inc
                if active > peak:
                    peak = active
            time.sleep(0.05)
            with peak_lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert peak == 1, f"per-host concurrency exceeded: peak={peak}"


# --------------------------------------------------------------------------- #
#  DownloadStats                                                              #
# --------------------------------------------------------------------------- #


def test_download_stats_empty_summary():
    s = ih.DownloadStats()
    assert s.as_summary() == "download: (no attempts)"


def test_download_stats_summary_contents():
    s = ih.DownloadStats()
    s.probed = 100
    s.head_blocked = 12
    s.head_unsupported = 3
    s.get_ok = 80
    s.get_failed = 5
    s.bump_reason("too_small")
    s.bump_reason("too_small")
    s.bump_reason("http_404")
    summary = s.as_summary()
    assert summary.startswith("download:")
    assert "probed=100" in summary
    assert "head_blocked=12" in summary
    assert "head_unsup=3" in summary
    assert "get_ok=80" in summary
    assert "get_failed=5" in summary
    assert "too_small:2" in summary
    assert "http_404:1" in summary
