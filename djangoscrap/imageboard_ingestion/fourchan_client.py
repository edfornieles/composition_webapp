"""Rate-limited 4chan JSON API client.

API docs: https://github.com/4chan/4chan-API
- Catalog:  https://a.4cdn.org/{board}/catalog.json
- Thread:   https://a.4cdn.org/{board}/thread/{no}.json
- Image:    https://i.4cdn.org/{board}/{tim}{ext}
- Thumb:    https://i.4cdn.org/{board}/{tim}s.jpg

Rules: max 1 req/sec global, no per-thread refresh < 10s, set User-Agent.
"""
from __future__ import annotations

import time
import threading
import urllib.request
import urllib.error
import json
from typing import Optional, Tuple

API_HOST = "https://a.4cdn.org"
IMG_HOST = "https://i.4cdn.org"

USER_AGENT = "edfornieles-aggregation-research/0.1 (artwork corpus; contact via github)"

_global_lock = threading.Lock()
_last_request_at: float = 0.0
_MIN_INTERVAL_S = 1.05  # slightly over 1s to stay safe


class FourChanError(Exception):
    pass


class FourChanNotModified(Exception):
    """Raised on HTTP 304."""


def _throttle() -> None:
    global _last_request_at
    with _global_lock:
        now = time.monotonic()
        delta = now - _last_request_at
        if delta < _MIN_INTERVAL_S:
            time.sleep(_MIN_INTERVAL_S - delta)
        _last_request_at = time.monotonic()


def _request(url: str, *, if_modified_since: Optional[str] = None, timeout: float = 30.0) -> Tuple[bytes, dict]:
    _throttle()
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json, image/*;q=0.8, */*;q=0.5")
    if if_modified_since:
        req.add_header("If-Modified-Since", if_modified_since)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return data, headers
    except urllib.error.HTTPError as e:
        if e.code == 304:
            raise FourChanNotModified()
        if e.code == 404:
            raise FourChanError(f"404 not found: {url}")
        raise FourChanError(f"HTTP {e.code} for {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise FourChanError(f"network error for {url}: {e.reason}") from e


def fetch_catalog(board: str = "fit", *, if_modified_since: Optional[str] = None) -> Tuple[list, dict]:
    """Return (catalog_pages, headers). catalog_pages is a list of page dicts."""
    data, headers = _request(f"{API_HOST}/{board}/catalog.json", if_modified_since=if_modified_since)
    return json.loads(data.decode("utf-8")), headers


def fetch_thread(board: str, thread_no: int, *, if_modified_since: Optional[str] = None) -> Tuple[dict, dict]:
    """Return (thread_dict, headers). thread_dict has key 'posts'."""
    data, headers = _request(
        f"{API_HOST}/{board}/thread/{thread_no}.json",
        if_modified_since=if_modified_since,
    )
    return json.loads(data.decode("utf-8")), headers


def fetch_image(board: str, tim: int, ext: str, *, thumbnail: bool = False) -> bytes:
    """Fetch an image attachment by its tim+ext. ext includes leading dot, e.g. '.jpg'."""
    suffix = "s.jpg" if thumbnail else f"{ext}"
    if thumbnail:
        url = f"{IMG_HOST}/{board}/{tim}s.jpg"
    else:
        url = f"{IMG_HOST}/{board}/{tim}{ext}"
    data, _ = _request(url)
    return data


def iter_catalog_threads(catalog_pages: list):
    """Yield (page_index, thread_dict) for every thread in a catalog response."""
    for page in catalog_pages:
        for t in page.get("threads", []) or []:
            yield page.get("page", -1), t
