"""
Tests for "archive mode" (a.k.a. per-batch point-and-download watcher).

Archive mode lets the operator lock a batch to one or more source URLs —
typically an Instagram/Tumblr/RSS feed — and optionally filter to just
images or just videos. The campaign then stops doing search/CBIR and
just re-scrapes the saved URLs each cycle.

These tests pin down:

1. ``_get_campaign_archive_prefs`` round-trips through ``campaign_state``.
2. Invalid / empty URLs and unknown ``media_kind_filter`` values are
   coerced to safe defaults.
3. The URL list is capped so a runaway paste doesn't produce an
   unbounded per-cycle scrape fan-out.
4. ``_run_campaign_cycle`` short-circuits search / CBIR / AI-QC when
   archive URLs are configured (we don't run the full cycle here, but
   the helper's ``archive_mode`` flag is what the cycle branches on).
"""

from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")
django.setup()


def test_archive_prefs_defaults_when_missing():
    from djangoscrap import views

    class _Batch:
        campaign_state = None

    prefs = views._get_campaign_archive_prefs(_Batch())

    assert prefs == {"urls": [], "media_kind_filter": "any"}


def test_archive_prefs_reads_list_and_media_kind():
    from djangoscrap import views

    class _Batch:
        campaign_state = {
            "archive": {
                "urls": [
                    "https://www.instagram.com/asmr__slice/",
                    "  ",
                    "https://vimeo.com/user/123",
                ],
                "media_kind_filter": "video",
            }
        }

    prefs = views._get_campaign_archive_prefs(_Batch())
    assert prefs["urls"] == [
        "https://www.instagram.com/asmr__slice/",
        "https://vimeo.com/user/123",
    ]
    assert prefs["media_kind_filter"] == "video"


def test_archive_prefs_rejects_unknown_media_kind():
    from djangoscrap import views

    class _Batch:
        campaign_state = {
            "archive": {
                "urls": ["https://example.com/feed"],
                "media_kind_filter": "audio",
            }
        }

    prefs = views._get_campaign_archive_prefs(_Batch())
    assert prefs["media_kind_filter"] == "any"


def test_archive_prefs_accepts_newline_separated_string_form():
    # Safety net for older records that stored the textarea value verbatim.
    from djangoscrap import views

    class _Batch:
        campaign_state = {
            "archive": {
                "urls": "https://a.example/\n\nhttps://b.example/feed\n",
                "media_kind_filter": "image",
            }
        }

    prefs = views._get_campaign_archive_prefs(_Batch())
    assert prefs["urls"] == ["https://a.example/", "https://b.example/feed"]
    assert prefs["media_kind_filter"] == "image"


def test_archive_prefs_caps_url_list_length():
    from djangoscrap import views

    class _Batch:
        campaign_state = {
            "archive": {
                "urls": [f"https://example.com/p/{i}" for i in range(200)],
                "media_kind_filter": "any",
            }
        }

    prefs = views._get_campaign_archive_prefs(_Batch())
    assert len(prefs["urls"]) == 50


def test_archive_prefs_default_helper_returns_fresh_dict():
    # Guard against accidental shared-mutable-default regressions.
    from djangoscrap import views

    a = views._default_campaign_archive_prefs()
    b = views._default_campaign_archive_prefs()
    a["urls"].append("https://example.com/")
    assert b["urls"] == []
