"""
Tests for Tier-1 Instagram video support.

These tests lock in three pieces of behaviour that make the rest of the
pipeline safe even when yt-dlp isn't installed, cookies aren't provided,
or Instagram returns nothing:

1. `_extract_with_yt_dlp` composes the right command line for Instagram
   profile URLs, including the cookies file and a bounded --playlist-end.
2. The auto-mode Instagram branch in `_extract_page_image_urls` falls
   through to yt-dlp when instaloader / gallery-dl are unavailable or
   return empty.
3. `_download_ingestion_url` adds an instagram.com Referer header when
   the target host is an Instagram / Facebook CDN. Without that Referer,
   the IG CDN 403s even valid direct-media URLs — this is the single
   biggest cause of "yt-dlp found videos but we downloaded zero".
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace
from unittest import mock

import django
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")
# views.py imports django.contrib.auth models at import time, so Django apps
# must be loaded before we can `from djangoscrap import views`.
django.setup()


# --------------------------------------------------------------------------- #
#  1. yt-dlp command shape for Instagram                                      #
# --------------------------------------------------------------------------- #


def _fake_completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_ytdlp_instagram_command_includes_cookies_when_configured(tmp_path):
    from djangoscrap import views

    cookies = tmp_path / "ig_cookies.txt"
    cookies.write_text("# Netscape cookie stub\n", encoding="utf-8")

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_completed(stdout="https://cdn.example/video.mp4\n")

    with (
        mock.patch.object(views.py_shutil, "which", return_value="/usr/bin/yt-dlp"),
        mock.patch.object(views.settings, "INSTAGRAM_COOKIES_FILE", str(cookies), create=True),
        mock.patch.object(views.subprocess, "run", side_effect=_fake_run),
    ):
        urls = views._extract_with_yt_dlp("https://www.instagram.com/asmr__slice/", max_urls=50)

    assert urls == ["https://cdn.example/video.mp4"]
    cmd = captured["cmd"]
    assert cmd[0] == "yt-dlp"
    assert "--cookies" in cmd
    assert str(cookies) in cmd
    assert "--playlist-end" in cmd


def test_ytdlp_instagram_omits_cookies_when_file_missing(tmp_path):
    from djangoscrap import views

    missing_path = tmp_path / "does_not_exist.txt"

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_completed(stdout="")

    with (
        mock.patch.object(views.py_shutil, "which", return_value="/usr/bin/yt-dlp"),
        mock.patch.object(views.settings, "INSTAGRAM_COOKIES_FILE", str(missing_path), create=True),
        mock.patch.object(views.subprocess, "run", side_effect=_fake_run),
    ):
        views._extract_with_yt_dlp("https://www.instagram.com/asmr__slice/", max_urls=50)

    cmd = captured["cmd"]
    assert "--cookies" not in cmd, "Cookies flag should be skipped when file does not exist"


def test_ytdlp_non_instagram_url_skips_ig_specific_flags():
    from djangoscrap import views

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_completed(stdout="")

    with (
        mock.patch.object(views.py_shutil, "which", return_value="/usr/bin/yt-dlp"),
        mock.patch.object(views.subprocess, "run", side_effect=_fake_run),
    ):
        views._extract_with_yt_dlp("https://www.youtube.com/watch?v=foo", max_urls=50)

    cmd = captured["cmd"]
    assert "--cookies" not in cmd
    assert "--playlist-end" not in cmd


def test_ytdlp_returns_empty_when_binary_missing():
    from djangoscrap import views

    with mock.patch.object(views.py_shutil, "which", return_value=None):
        out = views._extract_with_yt_dlp("https://www.instagram.com/asmr__slice/")
    assert out == []


# --------------------------------------------------------------------------- #
#  2. Auto-mode Instagram branch uses yt-dlp as last-resort                   #
# --------------------------------------------------------------------------- #


def test_ytdlp_adapter_claims_instagram_hosts():
    """Regression: YtDlpAdapter._KNOWN_HOSTS must include instagram.com so
    archive-mode cycles can route IG /p/ and /reel/ URLs through yt-dlp
    even when instagrapi credentials aren't configured."""
    from djangoscrap.ingestion_adapters.ytdlp import YtDlpAdapter

    ad = YtDlpAdapter(binary="/usr/bin/yt-dlp")
    assert ad.can_handle("https://www.instagram.com/asmr__slice/")
    assert ad.can_handle("https://instagram.com/p/SOMECODE/")
    assert ad.can_handle("https://www.instagram.com/reel/abc123/")


def test_ig_branch_short_circuits_without_falling_through_to_playwright():
    """Regression: when the IG-specific extractors all return empty, the
    auto-mode dispatcher must stop on the IG branch (engine=
    ``instagram-blocked``) instead of falling through to playwright-html,
    which on logged-out IG pages returns 6-8 UI sprite URLs that the HEAD
    probe then rejects as too-small. That fall-through was the entire
    cause of the "imported=0, failed=7" report the user saw on their
    first archive-mode cycle."""
    from djangoscrap import views

    with (
        mock.patch.object(views, "_extract_with_instaloader", return_value=[]),
        mock.patch.object(views, "_extract_with_gallery_dl", return_value=[]),
        mock.patch.object(views, "_extract_with_yt_dlp", return_value=[]),
        mock.patch.object(
            views,
            "_extract_image_urls_from_page",
            side_effect=AssertionError("playwright-html must not run on IG"),
        ),
    ):
        urls, engine = views._extract_candidates_from_page(
            "https://www.instagram.com/asmr__slice/",
            max_urls=50,
        )

    assert urls == []
    assert engine == "instagram-blocked"


def test_auto_mode_ig_falls_through_to_ytdlp_when_other_engines_empty():
    from djangoscrap import views

    calls: list[str] = []

    def _empty(engine):
        def _inner(page_url, max_urls=200):
            calls.append(engine)
            return []
        return _inner

    def _ytdlp(page_url, max_urls=200):
        calls.append("yt-dlp")
        return ["https://scontent.cdninstagram.com/a.mp4"]

    with (
        mock.patch.object(views, "_extract_with_instaloader", side_effect=_empty("instaloader")),
        mock.patch.object(views, "_extract_with_gallery_dl", side_effect=_empty("gallery-dl")),
        mock.patch.object(views, "_extract_with_yt_dlp", side_effect=_ytdlp),
    ):
        urls, engine = views._extract_candidates_from_page(
            "https://www.instagram.com/asmr__slice/",
            max_urls=100,
        )

    assert engine == "yt-dlp"
    # The URL may be rewritten by the candidate-URL rewriter; assert the
    # engine attribution and that we got something, not exact equality.
    assert urls, "yt-dlp fallback should produce at least one URL"
    # Order matters: instaloader -> gallery-dl -> yt-dlp.
    assert calls == ["instaloader", "gallery-dl", "yt-dlp"]


# --------------------------------------------------------------------------- #
#  3. _download_ingestion_url sends Referer on Instagram CDNs                 #
# --------------------------------------------------------------------------- #


class _FakeResp:
    def __init__(self, content_type: str, payload: bytes = b"x" * 200_000):
        self.headers = {"Content-Type": content_type}
        self._payload = payload
        self._read = False

    def read(self, *_a, **_kw):
        if self._read:
            return b""
        self._read = True
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(
    "host",
    [
        "scontent.cdninstagram.com",
        "video.fna.fbcdn.net",
    ],
)
def test_download_sends_instagram_referer_on_cdn_hosts(host, tmp_path):
    from djangoscrap import views

    url = f"https://{host}/path/video.mp4"

    class _Batch:
        id = 99_999

    probe = SimpleNamespace(ok=True, retry_with_get=False, reason="", content_type="video/mp4")
    captured: dict = {}

    def _fake_urlopen(req, timeout=12):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        return _FakeResp("video/mp4")

    with (
        mock.patch.object(views, "head_probe", return_value=probe),
        mock.patch("djangoscrap.views.urlopen", side_effect=_fake_urlopen),
        mock.patch.object(views, "INGESTION_MEDIA_ROOT", tmp_path),
        mock.patch.object(views, "_sha256_file", return_value="deadbeef"),
        mock.patch.object(views, "_average_hash_hex", return_value=""),
        mock.patch.object(views, "_image_metadata", return_value={"width": 0, "height": 0}),
    ):
        rel_path, sha, phash, meta = views._download_ingestion_url(_Batch(), url)

    assert rel_path, "expected a downloaded file path"
    assert sha == "deadbeef"
    headers_ci = {k.lower(): v for k, v in captured["headers"].items()}
    assert "referer" in headers_ci, "Referer must be set for IG CDN hosts"
    assert headers_ci["referer"] == "https://www.instagram.com/"


def test_download_omits_referer_for_unrelated_hosts(tmp_path):
    from djangoscrap import views

    url = "https://i.pinimg.com/originals/ab/cd/image.jpg"

    class _Batch:
        id = 99_998

    probe = SimpleNamespace(ok=True, retry_with_get=False, reason="", content_type="image/jpeg")
    captured: dict = {}

    def _fake_urlopen(req, timeout=12):
        captured["headers"] = dict(req.header_items())
        return _FakeResp("image/jpeg")

    with (
        mock.patch.object(views, "head_probe", return_value=probe),
        mock.patch("djangoscrap.views.urlopen", side_effect=_fake_urlopen),
        mock.patch.object(views, "INGESTION_MEDIA_ROOT", tmp_path),
        mock.patch.object(views, "_sha256_file", return_value="deadbeef"),
        mock.patch.object(views, "_average_hash_hex", return_value=""),
        mock.patch.object(views, "_image_metadata", return_value={"width": 0, "height": 0}),
    ):
        views._download_ingestion_url(_Batch(), url)

    headers_ci = {k.lower(): v for k, v in captured["headers"].items()}
    assert "referer" not in headers_ci, "Referer must not leak to non-IG hosts"
