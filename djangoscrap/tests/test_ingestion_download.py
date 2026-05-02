"""
Tests for djangoscrap.ingestion_download.parallel_download_urls.

Uses a mocked downloader that sleeps so we can assert both:
  (1) correctness — every URL gets yielded exactly once, with its result,
  (2) wall-clock speedup — N URLs × 100 ms each complete in well under
      N × 100 ms when max_workers > 1.

The assertions have generous margins so flaky CI machines don't flake.
"""

from __future__ import annotations

import time

import pytest

from djangoscrap import ingestion_download as idl


def _slow_downloader(delay_s: float = 0.08):
    """Return a downloader that sleeps ``delay_s`` seconds then returns ``("OK", url)``."""
    def _dl(url: str):
        time.sleep(delay_s)
        return ("OK", url)
    return _dl


def test_parallel_download_empty_input_yields_nothing():
    got = list(idl.parallel_download_urls([], _slow_downloader()))
    assert got == []


def test_parallel_download_all_urls_yielded_once():
    urls = [f"https://h{i}.example.com/{i}.jpg" for i in range(12)]
    got = list(idl.parallel_download_urls(urls, _slow_downloader(0.01), max_workers=4))
    assert len(got) == 12
    yielded_urls = {u for u, _ in got}
    assert yielded_urls == set(urls)
    for url, result in got:
        assert result is not None
        status, returned_url = result
        assert status == "OK"
        assert returned_url == url


def test_parallel_download_speedup_over_sequential():
    """8 tasks × 0.1 s each with 4 workers should complete in ≤ 0.5 s, not 0.8 s."""
    urls = [f"https://h{i}.example.com/{i}.jpg" for i in range(8)]
    t0 = time.monotonic()
    got = list(idl.parallel_download_urls(urls, _slow_downloader(0.1), max_workers=4))
    elapsed = time.monotonic() - t0
    assert len(got) == 8
    # Sequential would take 0.8s. With 4 workers we expect ~0.2s + overhead.
    # Guard with a generous 0.6s upper bound so CI CPU thrash doesn't flake.
    assert elapsed < 0.6, f"expected parallel speedup, got {elapsed:.3f}s"


def test_parallel_download_exceptions_become_none_results():
    def _sometimes_fails(url: str):
        if "bad" in url:
            raise RuntimeError("boom")
        return ("OK", url)

    urls = [
        "https://ok.example.com/1.jpg",
        "https://bad.example.com/2.jpg",
        "https://ok.example.com/3.jpg",
    ]
    got = dict(idl.parallel_download_urls(urls, _sometimes_fails, max_workers=3))
    assert got["https://ok.example.com/1.jpg"] == ("OK", "https://ok.example.com/1.jpg")
    assert got["https://bad.example.com/2.jpg"] is None
    assert got["https://ok.example.com/3.jpg"] == ("OK", "https://ok.example.com/3.jpg")


def test_parallel_download_stop_when_immediate_halts_submission():
    """stop_when returning True from the first call means zero tasks submitted."""
    call_count = {"n": 0}

    def _dl(url: str):
        call_count["n"] += 1
        return ("OK", url)

    urls = [f"https://h{i}.example.com/{i}.jpg" for i in range(10)]

    def stop_when():
        return True

    got = list(idl.parallel_download_urls(urls, _dl, max_workers=4, stop_when=stop_when))
    assert got == []
    assert call_count["n"] == 0


def test_parallel_download_stop_when_halts_during_drain():
    """stop_when flipping to True mid-drain stops iteration before all yield."""
    import threading

    lock = threading.Lock()
    state = {"completed": 0, "stopped": False}

    def _dl(url: str):
        with lock:
            state["completed"] += 1
        return ("OK", url)

    urls = [f"https://h{i}.example.com/{i}.jpg" for i in range(20)]

    def stop_when():
        # Trigger stop once we've seen at least 3 completions ready to drain.
        with lock:
            if state["completed"] >= 5:
                state["stopped"] = True
            return state["stopped"]

    # Use a single worker so submits don't all race ahead.
    got = list(idl.parallel_download_urls(urls, _dl, max_workers=2, stop_when=stop_when))
    # We should have yielded fewer than the full 20 items.
    assert len(got) < 20


def test_parallel_download_single_worker_is_sequential_and_correct():
    urls = ["https://h.example.com/1.jpg", "https://h.example.com/2.jpg"]
    got = list(idl.parallel_download_urls(urls, _slow_downloader(0.001), max_workers=1))
    assert len(got) == 2
    assert {u for u, _ in got} == set(urls)
