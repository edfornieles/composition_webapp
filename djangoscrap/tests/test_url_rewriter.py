"""
Tests for djangoscrap.ingestion_url_rewriter.

Covers:
* Every built-in per-site rewrite rule gets at least one positive-case test
  plus one non-matching path that proves we don't false-upgrade.
* The ignore list correctly drops known watermark hosts.
* The JSON overlay loader's schema (ignore list, upgrade entries with
  ``apply_to_query``, invalid entries).
* ``RewriteStats`` counters + summary line.
* Post-rewrite dedupe (two thumbnails upgrading to the same URL collapse).
"""

from __future__ import annotations

import json

import pytest

from djangoscrap import ingestion_url_rewriter as rw


# --------------------------------------------------------------------------- #
#  Built-in per-site rules                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "input_url,expected",
    [
        # Pinterest thumbnails → /originals/
        (
            "https://i.pinimg.com/236x/ab/cd/ef/abcdef1234.jpg",
            "https://i.pinimg.com/originals/ab/cd/ef/abcdef1234.jpg",
        ),
        (
            "https://i.pinimg.com/474x/11/22/33/deadbeef.jpg",
            "https://i.pinimg.com/originals/11/22/33/deadbeef.jpg",
        ),
        # Tumblr size segment → s2048x3072
        (
            "https://64.media.tumblr.com/abc/s540x810/hash123.jpg",
            "https://64.media.tumblr.com/abc/s2048x3072/hash123.jpg",
        ),
        # Flickr static farm → _b (large)
        (
            "https://live.staticflickr.com/65535/51234_abcdef_m.jpg",
            "https://live.staticflickr.com/65535/51234_abcdef_b.jpg",
        ),
        # Etsy il_340x270 → il_fullxfull
        (
            "https://i.etsystatic.com/12345678/r/il/abcdef/9876/il_340x270.12345_xyz.jpg",
            "https://i.etsystatic.com/12345678/r/il/abcdef/9876/il_fullxfull.12345_xyz.jpg",
        ),
        # Dribbble _1x → _4x
        (
            "https://cdn.dribbble.com/userupload/123/shot_1x.png",
            "https://cdn.dribbble.com/userupload/123/shot_4x.png",
        ),
        # Wikimedia /thumb/ → full path
        (
            "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Sunset.jpg/320px-Sunset.jpg",
            "https://upload.wikimedia.org/wikipedia/commons/a/ab/Sunset.jpg",
        ),
        # Reddit preview → i.redd.it, no query
        (
            "https://preview.redd.it/abc123.jpg?auto=webp&s=deadbeef",
            "https://i.redd.it/abc123.jpg",
        ),
        # Squarespace ?format=… → strip query
        (
            "https://images.squarespace-cdn.com/content/v1/abc/def.jpg?format=500w",
            "https://images.squarespace-cdn.com/content/v1/abc/def.jpg",
        ),
        # WP Photon: drop query
        (
            "https://i0.wp.com/example.com/foo.jpg?resize=300,200",
            "https://i0.wp.com/example.com/foo.jpg",
        ),
        # Unsplash: rewrite query
        (
            "https://images.unsplash.com/photo-12345?w=400&q=60",
            "https://images.unsplash.com/photo-12345?auto=format&q=80&w=2400",
        ),
        # 500px: rewrite size params inside query
        (
            "https://drscdn.500px.org/photo/12345/v2?webp=1&w=440&h=660",
            "https://drscdn.500px.org/photo/12345/v2?webp=1&w=2048&h=2048",
        ),
    ],
)
def test_builtin_upgrades(clean_url_rewriter, input_url, expected):
    """Each builtin rewrite rule must produce the expected upgraded URL."""
    got = rw.rewrite_candidate_url(input_url)
    assert got == expected, f"{input_url} → {got!r} (expected {expected!r})"


@pytest.mark.parametrize(
    "already_fullres_url",
    [
        "https://i.pinimg.com/originals/ab/cd/ef/x.jpg",
        "https://live.staticflickr.com/65535/51234_abcdef_b.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/a/ab/Sunset.jpg",
    ],
)
def test_already_fullres_unchanged(clean_url_rewriter, already_fullres_url):
    """URLs that are already at full resolution must pass through unmodified."""
    assert rw.rewrite_candidate_url(already_fullres_url) == already_fullres_url


@pytest.mark.parametrize(
    "ignored_url",
    [
        "https://www.shutterstock.com/image-photo/foo",
        "https://www.gettyimages.com/detail/photo/bar",
        "https://www.freepik.com/free-photo/whatever",
        "https://stock.adobe.com/images/xyz",  # ftcdn.net also; both covered
        "https://ftcdn.net/v2/jpg/01/23/45.jpg",
        "https://www.dreamstime.com/some-photo",
    ],
)
def test_ignore_list_drops_url(clean_url_rewriter, ignored_url):
    """URLs from ignore-listed hosts must return None (dropped)."""
    assert rw.rewrite_candidate_url(ignored_url) is None


def test_unknown_host_passes_through(clean_url_rewriter):
    url = "https://random-blog.example.com/photo.jpg"
    assert rw.rewrite_candidate_url(url) == url


def test_empty_and_invalid_inputs(clean_url_rewriter):
    assert rw.rewrite_candidate_url("") == ""
    # data: / ftp: are not http/https → passthrough.
    assert rw.rewrite_candidate_url("data:image/png;base64,AAA") == "data:image/png;base64,AAA"


# --------------------------------------------------------------------------- #
#  Dedup on post-rewrite value                                                #
# --------------------------------------------------------------------------- #


def test_rewrite_candidate_urls_dedupes_post_rewrite(clean_url_rewriter):
    """Two distinct thumbnails that upgrade to the same /originals/ must collapse."""
    input_urls = [
        "https://i.pinimg.com/236x/ab/cd/ef/abcdef1234.jpg",
        "https://i.pinimg.com/474x/ab/cd/ef/abcdef1234.jpg",   # same hash, diff size
        "https://i.pinimg.com/236x/11/22/33/deadbeef.jpg",     # different image
    ]
    stats = rw.RewriteStats()
    out = rw.rewrite_candidate_urls(input_urls, stats=stats)
    assert len(out) == 2
    assert out[0] == "https://i.pinimg.com/originals/ab/cd/ef/abcdef1234.jpg"
    assert out[1] == "https://i.pinimg.com/originals/11/22/33/deadbeef.jpg"
    assert stats.total_input == 3
    assert stats.upgraded >= 2
    assert stats.dropped == 0


def test_rewrite_stats_summary_format(clean_url_rewriter):
    stats = rw.RewriteStats()
    rw.rewrite_candidate_urls(
        [
            "https://i.pinimg.com/236x/ab/cd/ef/abcdef1234.jpg",
            "https://www.shutterstock.com/foo",
            "https://random.example.com/bar.jpg",
        ],
        stats=stats,
    )
    summary = stats.as_summary()
    assert summary.startswith("url-rewriter:")
    assert "in=3" in summary
    assert "upgraded=1" in summary
    assert "dropped=1" in summary


# --------------------------------------------------------------------------- #
#  JSON overlay                                                               #
# --------------------------------------------------------------------------- #


def test_json_overlay_adds_ignore_and_upgrade(tmp_path, monkeypatch):
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({
        "ignore": ["evilcdn.example.com"],
        "upgrade": [
            {
                "name": "mycdn-small-to-big",
                "host_suffix": "cdn.example.com",
                "pattern": "/thumb/(\\d+)/",
                "replacement": "/original/",
            },
        ],
    }))
    monkeypatch.setenv("INGESTION_SITE_RULES", str(overlay))
    n = rw.reload_rules()
    try:
        assert n == 2
        # Ignore overlay fires
        assert rw.rewrite_candidate_url("https://evilcdn.example.com/foo.jpg") is None
        # Upgrade overlay fires
        assert (
            rw.rewrite_candidate_url("https://cdn.example.com/thumb/42/photo.jpg")
            == "https://cdn.example.com/original/photo.jpg"
        )
        # Non-matching path on the same host falls through unchanged.
        assert (
            rw.rewrite_candidate_url("https://cdn.example.com/full/photo.jpg")
            == "https://cdn.example.com/full/photo.jpg"
        )
    finally:
        monkeypatch.delenv("INGESTION_SITE_RULES", raising=False)
        rw.reload_rules()


def test_json_overlay_bad_regex_is_skipped(tmp_path, monkeypatch):
    overlay = tmp_path / "bad.json"
    overlay.write_text(json.dumps({
        "upgrade": [
            {"name": "broken", "host_suffix": "a.example.com", "pattern": "(unclosed", "replacement": "/x/"},
            {"name": "ok",     "host_suffix": "b.example.com", "pattern": "/x/", "replacement": "/y/"},
        ],
    }))
    monkeypatch.setenv("INGESTION_SITE_RULES", str(overlay))
    try:
        n = rw.reload_rules()
        assert n == 1  # only the well-formed rule survives
        assert (
            rw.rewrite_candidate_url("https://b.example.com/x/z.jpg")
            == "https://b.example.com/y/z.jpg"
        )
    finally:
        monkeypatch.delenv("INGESTION_SITE_RULES", raising=False)
        rw.reload_rules()


def test_json_overlay_malformed_json_is_tolerated(tmp_path, monkeypatch):
    overlay = tmp_path / "bad.json"
    overlay.write_text("{not json")
    monkeypatch.setenv("INGESTION_SITE_RULES", str(overlay))
    try:
        # Must not raise, must not break later rewrites.
        n = rw.reload_rules()
        assert n == 0
        assert (
            rw.rewrite_candidate_url("https://i.pinimg.com/236x/a/b/c/d.jpg")
            == "https://i.pinimg.com/originals/a/b/c/d.jpg"
        )
    finally:
        monkeypatch.delenv("INGESTION_SITE_RULES", raising=False)
        rw.reload_rules()
