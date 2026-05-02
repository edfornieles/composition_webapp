"""
Tests for YtDlpAdapter.

No live yt-dlp invocation — the subprocess.run call is mocked so these
tests run in < 1 s without any external dependencies.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from djangoscrap.ingestion_adapters.ytdlp import YtDlpAdapter, _host_matches


# --------------------------------------------------------------------------- #
#  Host matching                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "netloc,expected",
    [
        ("www.reddit.com", True),
        ("old.reddit.com", True),
        ("bilibili.com", True),
        ("video.bilibili.com", True),
        ("v.redd.it", True),
        ("www.pixiv.net", True),
        ("random-blog.example.com", False),
        ("notareal.site", False),
        ("", False),
    ],
)
def test_host_matches(netloc, expected):
    assert _host_matches(netloc) is expected


# --------------------------------------------------------------------------- #
#  can_handle                                                                 #
# --------------------------------------------------------------------------- #


def test_can_handle_requires_yt_dlp_binary():
    adapter = YtDlpAdapter(binary=None)
    assert adapter.can_handle("https://www.reddit.com/r/x/comments/y/z/") is False


def test_can_handle_accepts_known_host():
    adapter = YtDlpAdapter(binary="/usr/local/bin/yt-dlp")
    assert adapter.can_handle("https://www.reddit.com/r/x/comments/y/z/") is True


def test_can_handle_rejects_unknown_host():
    adapter = YtDlpAdapter(binary="/usr/local/bin/yt-dlp")
    assert adapter.can_handle("https://random.example.com/page") is False


# --------------------------------------------------------------------------- #
#  extract                                                                    #
# --------------------------------------------------------------------------- #


class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_extract_parses_stdout_urls():
    adapter = YtDlpAdapter(binary="/usr/local/bin/yt-dlp")
    stdout = (
        "https://v.redd.it/abc123/DASH_720.mp4\n"
        "https://v.redd.it/abc123/audio.m4a\n"
        "\n"  # blank line
        "oops-not-a-url\n"
        "https://v.redd.it/abc123/thumb.jpg\n"
    )
    with mock.patch(
        "djangoscrap.ingestion_adapters.ytdlp.subprocess.run",
        return_value=_FakeProc(stdout=stdout, returncode=0),
    ):
        result = adapter.extract(
            "https://www.reddit.com/r/x/comments/y/z/", max_urls=10, state={}
        )
    assert result.ok is True
    assert result.urls == [
        "https://v.redd.it/abc123/DASH_720.mp4",
        "https://v.redd.it/abc123/audio.m4a",
        "https://v.redd.it/abc123/thumb.jpg",
    ]
    assert result.diagnostics["emitted"] == "3"


def test_extract_empty_stdout_is_error():
    adapter = YtDlpAdapter(binary="/usr/local/bin/yt-dlp")
    with mock.patch(
        "djangoscrap.ingestion_adapters.ytdlp.subprocess.run",
        return_value=_FakeProc(stdout="", returncode=0),
    ):
        result = adapter.extract(
            "https://www.reddit.com/r/x/", max_urls=10, state={}
        )
    assert result.ok is False
    assert result.error == "yt-dlp produced 0 urls"


def test_extract_nonzero_exit_is_error():
    adapter = YtDlpAdapter(binary="/usr/local/bin/yt-dlp")
    with mock.patch(
        "djangoscrap.ingestion_adapters.ytdlp.subprocess.run",
        return_value=_FakeProc(stderr="ERROR: Unable to download", returncode=1),
    ):
        result = adapter.extract(
            "https://www.reddit.com/r/x/", max_urls=10, state={}
        )
    assert result.ok is False
    assert "yt-dlp exit=1" in result.error


def test_extract_timeout_is_error():
    adapter = YtDlpAdapter(binary="/usr/local/bin/yt-dlp", timeout_s=1)
    with mock.patch(
        "djangoscrap.ingestion_adapters.ytdlp.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="yt-dlp", timeout=1),
    ):
        result = adapter.extract(
            "https://www.reddit.com/r/x/", max_urls=10, state={}
        )
    assert result.ok is False
    assert result.error == "yt-dlp timeout"


def test_extract_respects_max_urls_cap():
    adapter = YtDlpAdapter(binary="/usr/local/bin/yt-dlp")
    stdout = "\n".join(f"https://v.redd.it/x/{i}.jpg" for i in range(50))
    with mock.patch(
        "djangoscrap.ingestion_adapters.ytdlp.subprocess.run",
        return_value=_FakeProc(stdout=stdout, returncode=0),
    ):
        result = adapter.extract(
            "https://www.reddit.com/r/x/", max_urls=7, state={}
        )
    assert len(result.urls) == 7
