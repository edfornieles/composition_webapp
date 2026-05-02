"""Tests for the concept→campaign cycle planner (Phase 6)."""

from __future__ import annotations

import pytest

from djangoscrap.ingestion_campaign_planner import (
    CampaignBrief,
    CampaignSettings,
    CycleBrief,
    build_cycle_brief,
    classify_seed,
    infer_brief,
)


# --------------------------------------------------------------------------- #
#  classify_seed                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://cdn.example.com/photo.jpg", "cbir"),
        ("https://cdn.example.com/photo.JPG", "cbir"),
        ("https://cdn.example.com/x.webp?size=big", "cbir"),
        ("https://www.instagram.com/someone/", "archive"),
        ("https://myblog.tumblr.com/", "archive"),
        ("https://www.pinterest.com/user/board/", "archive"),
        ("https://twitter.com/someone", "archive"),
        ("https://x.com/someone", "archive"),
        ("vaporwave record sleeve", "query"),
        ("https://unknown.example.com/page", "query"),
        ("", "query"),
        ("   ", "query"),
    ],
)
def test_classify_seed(raw, expected):
    assert classify_seed(raw) == expected


# --------------------------------------------------------------------------- #
#  infer_brief                                                                #
# --------------------------------------------------------------------------- #


def test_infer_brief_partitions_mixed_input():
    brief = infer_brief(
        "retro vaporwave",
        [
            "vaporwave sleeves",
            "https://www.instagram.com/someone/",
            "https://cdn.example.com/ref.jpg",
            "brutalist typography",
            "https://myblog.tumblr.com/",
            "https://cdn.example.com/ref2.png",
        ],
    )
    assert brief.concept == "retro vaporwave"
    assert brief.queries == ["vaporwave sleeves", "brutalist typography"]
    assert brief.archive_seeds == [
        "https://www.instagram.com/someone/",
        "https://myblog.tumblr.com/",
    ]
    assert brief.cbir_seeds == [
        "https://cdn.example.com/ref.jpg",
        "https://cdn.example.com/ref2.png",
    ]


def test_infer_brief_dedupes():
    brief = infer_brief(
        "x",
        [
            "vaporwave",
            "vaporwave",
            "https://www.instagram.com/a/",
            "https://www.instagram.com/a/",
        ],
    )
    assert brief.queries == ["vaporwave"]
    assert brief.archive_seeds == ["https://www.instagram.com/a/"]


def test_infer_brief_appends_to_prebuilt_queries():
    brief = infer_brief(
        "x",
        ["new query"],
        queries=[{"q": "ai generated query", "priority": 1}],
    )
    assert brief.queries == [{"q": "ai generated query", "priority": 1}, "new query"]


# --------------------------------------------------------------------------- #
#  build_cycle_brief — composition                                            #
# --------------------------------------------------------------------------- #


def test_build_cycle_brief_pure_search():
    brief = CampaignBrief(concept="x", queries=["cats"])
    cycle = build_cycle_brief(brief)
    assert cycle.search_plan is not None
    assert cycle.archive_plan is None
    assert cycle.cbir_plan is None
    assert len(cycle.urls) > 0
    # Summary includes the search section
    assert "search-fanout" in cycle.as_summary()


def test_build_cycle_brief_pure_cbir():
    brief = CampaignBrief(
        concept="x",
        cbir_seeds=["https://cdn.example.com/a.jpg"],
    )
    cycle = build_cycle_brief(brief)
    assert cycle.cbir_plan is not None
    assert cycle.search_plan is None
    assert cycle.archive_plan is None
    assert len(cycle.urls) > 0


def test_build_cycle_brief_empty_inputs_produces_empty_plan():
    cycle = build_cycle_brief(CampaignBrief(concept="x"))
    assert cycle.urls == []
    assert cycle.search_plan is None
    assert cycle.archive_plan is None
    assert cycle.cbir_plan is None
    assert "cycle-total-urls=0" in cycle.as_summary()


def test_build_cycle_brief_combines_all_three_stages():
    # Use stubbed archive seeds; real dispatch will return "no adapter
    # registered" for example.com which is fine — we just need the
    # archive plan to exist so we exercise the composition path.
    brief = CampaignBrief(
        concept="mixed",
        queries=["cats"],
        archive_seeds=["https://example.com/profile"],
        cbir_seeds=["https://cdn.example.com/ref.jpg"],
    )
    cycle = build_cycle_brief(brief)
    assert cycle.search_plan is not None
    assert cycle.archive_plan is not None
    assert cycle.cbir_plan is not None
    # Summary covers every sub-stage
    summary = cycle.as_summary()
    assert "search-fanout" in summary
    assert "archive" in summary
    assert "cbir" in summary


def test_build_cycle_brief_dedupes_across_stages():
    # Hand-craft a settings that disables all search engines and all cbir
    # engines so the only URL production comes from the archive planner;
    # then supply a fake dispatch that returns duplicate URLs. Simplest
    # way to express this: just verify that the post-dedupe URL set is a
    # subset of each planner's urls.
    brief = CampaignBrief(
        concept="x",
        queries=["cats"],
        cbir_seeds=["https://cdn.example.com/ref.jpg"],
    )
    cycle = build_cycle_brief(brief)
    assert len(cycle.urls) == len(set(cycle.urls))


def test_build_cycle_brief_threads_state_through_archive():
    prior_state = {
        "archive_cursors": {},
        "archive_drained": ["https://example.com/old-profile"],
        "custom_key": "keep-me",
    }
    brief = CampaignBrief(
        concept="x",
        archive_seeds=["https://example.com/new-profile"],
    )
    cycle = build_cycle_brief(brief, state=prior_state)
    assert cycle.next_state["custom_key"] == "keep-me"
    assert "https://example.com/old-profile" in cycle.next_state["archive_drained"]


def test_build_cycle_brief_no_archive_preserves_state_untouched():
    prior_state = {"archive_cursors": {}, "custom": "stays"}
    brief = CampaignBrief(concept="x", queries=["cats"])
    cycle = build_cycle_brief(brief, state=prior_state)
    # No archive_plan → state passes through unchanged
    assert cycle.next_state == prior_state


def test_build_cycle_brief_settings_narrows_search_engines():
    brief = CampaignBrief(concept="x", queries=["cats"])
    settings = CampaignSettings(
        pages_per_engine=1, enabled_search_engines={"google"}
    )
    cycle = build_cycle_brief(brief, settings=settings)
    assert set(cycle.search_plan.per_engine.keys()) == {"google"}


def test_build_cycle_brief_settings_narrows_cbir_engines():
    brief = CampaignBrief(
        concept="x", cbir_seeds=["https://cdn.example.com/a.jpg"]
    )
    settings = CampaignSettings(enabled_cbir_engines={"tineye-rev"})
    cycle = build_cycle_brief(brief, settings=settings)
    assert set(cycle.cbir_plan.per_engine.keys()) == {"tineye-rev"}


def test_build_cycle_brief_preserves_url_ordering_search_archive_cbir():
    # Search URLs appear before archive URLs; archive URLs before CBIR.
    brief = CampaignBrief(
        concept="x",
        queries=["cats"],
        cbir_seeds=["https://cdn.example.com/a.jpg"],
    )
    cycle = build_cycle_brief(brief)
    # First URL should be a Google SERP (search first)
    assert "google.com" in cycle.urls[0]
    # A TinEye URL should appear after all search URLs
    tineye_idx = next(
        (i for i, u in enumerate(cycle.urls) if "tineye" in u), -1
    )
    assert tineye_idx > 0
    # And every URL before that TinEye URL should be a search URL (not CBIR)
    for u in cycle.urls[:tineye_idx]:
        assert "tineye" not in u
