"""Desuarchive (FoolFuuka) client for historical /fit/ scraping.

Desuarchive archives /fit/ back to ~2015 and exposes a JSON index API at
https://desuarchive.org/_/api/chan/index/?board=fit&page=N

Each page returns ~15 threads keyed by OP num. Each thread carries an `op`
record and a small `posts` map (last few replies); the full thread is at
https://desuarchive.org/_/api/chan/thread/?board=fit&num=<thread_num>

The image and thumbnail file URLs are exposed as `media_link` and
`thumb_link` on each post.

This client is deliberately simple, single-threaded, and rate-limited.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Iterator

DEFAULT_HOST = "https://desuarchive.org"
DEFAULT_UA = "fit-research-scraper/0.1 (educational; contact: local)"

_LAST_REQ = 0.0
_RPS = 0.7  # ~1 every 1.4s


def set_rate(rps: float) -> None:
    global _RPS
    _RPS = max(0.05, float(rps))


def _throttle() -> None:
    global _LAST_REQ
    now = time.time()
    min_gap = 1.0 / _RPS
    delta = now - _LAST_REQ
    if delta < min_gap:
        time.sleep(min_gap - delta)
    _LAST_REQ = time.time()


def _get(url: str, *, timeout: float = 30.0) -> bytes:
    _throttle()
    req = urllib.request.Request(url, headers={
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json,text/plain,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_index(board: str, page: int, *, host: str = DEFAULT_HOST) -> dict:
    url = f"{host}/_/api/chan/index/?{urllib.parse.urlencode({'board': board, 'page': page})}"
    return json.loads(_get(url).decode("utf-8"))


def fetch_thread(board: str, thread_num: int, *, host: str = DEFAULT_HOST) -> dict:
    url = f"{host}/_/api/chan/thread/?{urllib.parse.urlencode({'board': board, 'num': thread_num})}"
    return json.loads(_get(url).decode("utf-8"))


def fetch_bytes(url: str, *, timeout: float = 60.0) -> bytes:
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def iter_thread_nums(index: dict) -> Iterator[int]:
    """Yield thread OP nums in the order returned by the index page."""
    for key in index.keys():
        try:
            yield int(key)
        except (TypeError, ValueError):
            continue


def iter_posts_with_media(thread: dict) -> Iterator[dict]:
    """Yield posts that have media (op + replies).

    Each yielded dict has: num, timestamp, comment, media_link, thumb_link,
    media_orig, preview_orig, ext.
    """
    if not thread:
        return
    for outer_key, payload in thread.items():
        op = payload.get("op") or {}
        posts = payload.get("posts") or {}
        for post in [op] + list(posts.values()):
            if not post:
                continue
            media = post.get("media") or {}
            media_link = media.get("media_link") or post.get("media_link")
            thumb_link = media.get("thumb_link") or post.get("thumb_link")
            if not (media_link or thumb_link):
                continue
            yield {
                "num": post.get("num"),
                "thread_num": post.get("thread_num") or outer_key,
                "timestamp": post.get("timestamp"),
                "comment": post.get("comment") or "",
                "media_link": media_link,
                "thumb_link": thumb_link,
                "media_orig": media.get("media_orig"),
                "preview_orig": media.get("preview_orig"),
                "ext": (media.get("media_orig") or "").rsplit(".", 1)[-1] if media.get("media_orig") else "",
            }
