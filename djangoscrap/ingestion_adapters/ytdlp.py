"""
YtDlpAdapter — generic long-tail adapter powered by ``yt-dlp``.

Covers ~1800+ sites yt-dlp knows about that we never hand-wrote a DOM
extractor for: Reddit video posts, Pixiv video, DeviantArt video posts,
Bilibili, Douyin/TikTok, Xiaohongshu, Pornhub (if tagged appropriately
in a future ignore-rule), and hundreds of smaller platforms.

Design choices
==============
* **Priority 30** — low. Adapter runs only if no higher-priority
  specialized adapter (Pinterest, Instagram, Pixiv, per-site DOM, …)
  matches the URL first. This matches the plan's "generic fallback"
  positioning.
* **Can-handle gate** — matches on a known set of host suffixes so we
  don't pay the ``yt-dlp`` process-spawn cost on every random URL.
  The list is intentionally conservative; expand it as new sites prove
  useful.
* **Shells out to yt-dlp** — reuses the existing dependency rather than
  importing the Python API. The ``-g`` (``--get-url``) flag prints
  direct media URLs without downloading, which is exactly what we need.
* **Fails soft** — if yt-dlp isn't on PATH, ``can_handle`` returns False
  and the adapter silently defers to the legacy chain.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from urllib.parse import urlparse

from djangoscrap.ingestion_adapters.base import Adapter, AdapterResult

_LOGGER = logging.getLogger("djangoscrap.adapters.ytdlp")


# Host suffixes where yt-dlp reliably beats the legacy chain. Keep this
# list conservative — random HTML blogs should fall through to the
# normal DOM scraper, not get a ~1-2 s yt-dlp spawn per page.
_KNOWN_HOSTS: tuple[str, ...] = (
    "reddit.com",
    "redd.it",
    "bilibili.com",
    "b23.tv",
    "douyin.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "pinterest.com",
    "youtube.com",
    "youtu.be",
    # Instagram: yt-dlp still handles single posts and reels even when
    # the profile-feed extractor is broken upstream. Without this entry
    # the adapter silently refused every IG URL, so archive-mode cycles
    # locked to an IG feed fell all the way through to the playwright
    # logged-out-HTML scraper and returned UI sprite junk.
    "instagram.com",
    "pixiv.net",
    "niconico.jp",
    "vimeo.com",
    "xiaohongshu.com",
    "xhs.cn",
    "facebook.com",
    "weibo.com",
    "deviantart.com",
    "imgur.com",
)


def _host_matches(netloc: str) -> bool:
    netloc = (netloc or "").lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    for suffix in _KNOWN_HOSTS:
        if netloc == suffix or netloc.endswith("." + suffix):
            return True
    return False


def _parse_urls_from_stdout(blob: str, max_urls: int) -> list[str]:
    """``yt-dlp -g`` prints one URL per line. Filter to plausible http(s) URLs."""
    out: list[str] = []
    for raw in (blob or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if not (raw.startswith("http://") or raw.startswith("https://")):
            continue
        out.append(raw)
        if len(out) >= max_urls:
            break
    return out


class YtDlpAdapter:
    name: str = "yt-dlp"
    priority: int = 30

    _UNSET = object()

    def __init__(self, *, timeout_s: int = 35, binary=_UNSET):
        self._timeout = int(timeout_s)
        # ``binary`` is a sentinel so the test can pass None to mean
        # "pretend yt-dlp is missing" without tripping the shutil.which
        # fallback. Real callers omit the kwarg.
        if binary is self._UNSET:
            self._binary = shutil.which("yt-dlp")
        else:
            self._binary = binary or None

    def can_handle(self, url: str) -> bool:
        if not self._binary:
            return False
        if not url:
            return False
        try:
            netloc = urlparse(url).netloc
        except Exception:
            return False
        return _host_matches(netloc)

    def extract(self, url: str, *, max_urls: int, state: dict) -> AdapterResult:
        if not self._binary:
            return AdapterResult(error="yt-dlp binary not found")
        try:
            proc = subprocess.run(
                [
                    self._binary,
                    "-g",
                    "--no-warnings",
                    "--flat-playlist",
                    "--no-call-home",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            return AdapterResult(error="yt-dlp timeout")
        except Exception as exc:
            _LOGGER.warning("yt-dlp subprocess failed for %s: %r", url, exc)
            return AdapterResult(error=f"yt-dlp subprocess error: {exc!r}")

        if proc.returncode != 0:
            return AdapterResult(
                error=f"yt-dlp exit={proc.returncode}",
                diagnostics={"stderr_head": (proc.stderr or "")[:200]},
            )
        urls = _parse_urls_from_stdout(proc.stdout, max_urls=max_urls)
        if not urls:
            return AdapterResult(error="yt-dlp produced 0 urls")
        return AdapterResult(
            urls=urls,
            next_state=dict(state or {}),
            diagnostics={"emitted": str(len(urls))},
        )


# Module-level singleton convenient for registration at app startup.
_SINGLETON: YtDlpAdapter | None = None


def get_singleton() -> YtDlpAdapter:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = YtDlpAdapter()
    return _SINGLETON
