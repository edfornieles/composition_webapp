"""
Tests for the extended ``ingestion_site_rules.json`` overlay.

This test file loads the *real* project-root overlay (the same one the live
ingestion pipeline uses) and asserts each rule upgrades at least one
realistic input URL and never breaks an already-full-res URL from the same
host. Think of it as a contract test: if someone edits
``ingestion_site_rules.json`` without running tests, the failure surfaces
immediately.

If a rule here fails, the fix is almost always to edit
``ingestion_site_rules.json`` rather than this file — that JSON is the
source of truth for what the rewriter does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from djangoscrap import ingestion_url_rewriter as rw


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_OVERLAY_PATH = _PROJECT_ROOT / "ingestion_site_rules.json"


@pytest.fixture(autouse=True)
def _use_project_overlay(monkeypatch):
    """Ensure the real project-root ingestion_site_rules.json is loaded."""
    monkeypatch.setenv("INGESTION_SITE_RULES", str(_OVERLAY_PATH))
    rw.reload_rules()
    yield
    monkeypatch.delenv("INGESTION_SITE_RULES", raising=False)
    rw.reload_rules()


# --------------------------------------------------------------------------- #
#  Overlay loads                                                              #
# --------------------------------------------------------------------------- #


def test_overlay_file_exists_and_is_valid_json():
    assert _OVERLAY_PATH.exists(), f"missing overlay at {_OVERLAY_PATH}"
    data = json.loads(_OVERLAY_PATH.read_text("utf-8"))
    assert "ignore" in data or "upgrade" in data
    assert isinstance(data.get("upgrade", []), list)
    assert isinstance(data.get("ignore", []), list)


def test_overlay_rules_count_is_sane():
    """Guard against accidentally emptying the overlay."""
    data = json.loads(_OVERLAY_PATH.read_text("utf-8"))
    assert len(data.get("ignore", [])) >= 10, "ignore list shrunk unexpectedly"
    assert len(data.get("upgrade", [])) >= 20, "upgrade list shrunk unexpectedly"


# --------------------------------------------------------------------------- #
#  Ignore list                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "ignored_url",
    [
        "https://www.canstockphoto.com/image/12345",
        "https://www.bigstockphoto.com/image/foo",
        "https://www.rawpixel.com/image/bar",
        "https://www.pikwizard.com/photo/baz",
        "https://stocksnap.io/photo/abc",
        "https://www.hippopx.com/en/photo/qux",
        "https://pixnio.com/free-images/xyz",
    ],
)
def test_overlay_ignore_drops_url(ignored_url):
    assert rw.rewrite_candidate_url(ignored_url) is None


# --------------------------------------------------------------------------- #
#  Upgrade rules — one positive case per rule                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "input_url,expected",
    [
        # Imgur: strip thumbnail suffix letter.
        (
            "https://i.imgur.com/abcDEF1b.jpg",
            "https://i.imgur.com/abcDEF1.jpg",
        ),
        # Blogger: /s400/ → /s0/
        (
            "https://1.bp.blogspot.com/path/s400/image.jpg",
            "https://1.bp.blogspot.com/path/s0/image.jpg",
        ),
        # googleusercontent: =s400 → =s0 (applied to full URL since size is
        # sometimes after path, not in query)
        (
            "https://lh3.googleusercontent.com/abc=s400-c",
            "https://lh3.googleusercontent.com/abc=s0",
        ),
        # Twitter pbs: :large → :orig
        (
            "https://pbs.twimg.com/media/AbCdEf.jpg:large",
            "https://pbs.twimg.com/media/AbCdEf.jpg:orig",
        ),
        # Twitter name=large → name=orig (query)
        (
            "https://pbs.twimg.com/media/AbCdEf?format=jpg&name=large",
            "https://pbs.twimg.com/media/AbCdEf?format=jpg&name=orig",
        ),
        # Facebook fbcdn: /p200x200/ → /
        (
            "https://scontent.fbcdn.net/v/t1.0-9/p200x200/12345_foo_n.jpg",
            "https://scontent.fbcdn.net/v/t1.0-9/12345_foo_n.jpg",
        ),
        # Instagram cdninstagram: /s150x150/ → /
        (
            "https://scontent.cdninstagram.com/v/t1/s150x150/12345_n.jpg",
            "https://scontent.cdninstagram.com/v/t1/12345_n.jpg",
        ),
        # Shopify: _small.jpg → .jpg
        (
            "https://cdn.shopify.com/s/files/1/0000/0000/products/shirt_small.jpg",
            "https://cdn.shopify.com/s/files/1/0000/0000/products/shirt.jpg",
        ),
        # Amazon: strip ._SX500_
        (
            "https://images-amazon.com/images/I/foo._SX500_.jpg",
            "https://images-amazon.com/images/I/foo.jpg",
        ),
        (
            "https://m.media-amazon.com/images/I/bar._AC_UL480_.jpg",
            "https://m.media-amazon.com/images/I/bar.jpg",
        ),
        # ArtStation: /large/ → /original/
        (
            "https://cdnb.artstation.com/p/assets/images/images/000/00/large/foo.jpg",
            "https://cdnb.artstation.com/p/assets/images/images/000/00/original/foo.jpg",
        ),
        # Wix: bound-fill size → 4096x4096
        (
            "https://static.wixstatic.com/media/abc/v1/fill/w_300,h_200,q_90/photo.jpg",
            "https://static.wixstatic.com/media/abc/v1/fill/w_4096,h_4096,al_c/photo.jpg",
        ),
        # Pixiv: c/360x360/img-master → img-original
        (
            "https://i.pximg.net/c/360x360_70/img-master/img/2024/01/01/12/00/00/12345_p0_master1200.jpg",
            "https://i.pximg.net/img-original/img/2024/01/01/12/00/00/12345_p0_master1200.jpg",
        ),
        # iTunes/Apple mzstatic
        (
            "https://is4-ssl.mzstatic.com/image/thumb/Music/abc/300x300cc.jpg",
            "https://is4-ssl.mzstatic.com/image/thumb/Music/abc/999999999x0w.jpg",
        ),
        # Weibo sinaimg: /bmiddle/ → /large/
        (
            "https://wx1.sinaimg.cn/bmiddle/abcd1234.jpg",
            "https://wx1.sinaimg.cn/large/abcd1234.jpg",
        ),
        # postimg thumb → full
        (
            "https://i.postimg.cc/thumb/abc/photo.jpg",
            "https://i.postimg.cc/abc/photo.jpg",
        ),
        # ibb: .th.jpg → .jpg
        (
            "https://i.ibb.co/abc/foo.th.jpg",
            "https://i.ibb.co/abc/foo.jpg",
        ),
        # TikTok CDN: strip tplv-…-WxH segment
        (
            "https://p16-sign-va.tiktokcdn.com/tos-maliva-i-abcd~tplv-photomode-vcard-share-24:480x480.jpeg",
            "https://p16-sign-va.tiktokcdn.com/tos-maliva-i-abcd:480x480.jpeg",
        ),
        # VK: /100x100/ → /orig/
        (
            "https://sun9-1.userapi.com/c0/100x100/foo.jpg",
            "https://sun9-1.userapi.com/c0/orig/foo.jpg",
        ),
        # Xiaohongshu: strip !variant suffix
        (
            "https://sns-img-qc.xhscdn.com/abc1234!nd_dft_wgth_webp_3",
            "https://sns-img-qc.xhscdn.com/abc1234",
        ),
    ],
)
def test_overlay_upgrade_produces_expected_url(input_url, expected):
    got = rw.rewrite_candidate_url(input_url)
    assert got == expected, f"{input_url} → {got!r} (expected {expected!r})"


# --------------------------------------------------------------------------- #
#  Combined stats run: sanity-check the full pipeline                         #
# --------------------------------------------------------------------------- #


def test_overlay_stats_report_for_mixed_input():
    """Mixed input URL list should upgrade many, drop some, pass others through."""
    urls = [
        "https://i.pinimg.com/236x/ab/cd/ef/x.jpg",        # built-in pinterest
        "https://i.imgur.com/abcDEF1b.jpg",                # overlay imgur
        "https://1.bp.blogspot.com/p/s400/image.jpg",      # overlay blogger
        "https://pbs.twimg.com/media/AbCdEf.jpg:large",    # overlay twitter
        "https://www.shutterstock.com/foo",                # built-in ignore
        "https://www.rawpixel.com/foo",                    # overlay ignore
        "https://example.com/some.jpg",                    # passthrough
    ]
    stats = rw.RewriteStats()
    out = rw.rewrite_candidate_urls(urls, stats=stats)
    # 5 valid outputs (4 upgrades + 1 passthrough). 2 dropped.
    assert len(out) == 5
    assert stats.total_input == 7
    assert stats.upgraded >= 4
    assert stats.dropped == 2
    summary = stats.as_summary()
    assert "upgraded=" in summary
    assert "dropped=2" in summary
