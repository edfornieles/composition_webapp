"""
Tests for the multi-engine search fan-out planner.

All engine URLs are constructed from the query directly — no network —
so the tests are fast and deterministic.
"""

from __future__ import annotations

from djangoscrap.ingestion_search_fanout import (
    ENGINES,
    SearchEngine,
    plan_search_fanout,
)


# --------------------------------------------------------------------------- #
#  URL builders                                                               #
# --------------------------------------------------------------------------- #


def _lookup(name: str) -> SearchEngine:
    for e in ENGINES:
        if e.name == name:
            return e
    raise KeyError(name)


def test_google_images_page_0_no_start_param():
    url = _lookup("google").build_url("vaporwave sleeves", 0)
    assert url.startswith("https://www.google.com/search?")
    assert "tbm=isch" in url
    assert "q=vaporwave+sleeves" in url
    assert "start=" not in url


def test_google_images_page_2_has_start_40():
    url = _lookup("google").build_url("vaporwave", 2)
    assert "start=40" in url


def test_bing_page_0_first_is_1():
    url = _lookup("bing").build_url("cats", 0)
    assert "first=1" in url


def test_bing_page_2_first_offset():
    url = _lookup("bing").build_url("cats", 2)
    assert "first=71" in url


def test_yandex_uses_p_param():
    url = _lookup("yandex").build_url("brutalist architecture", 3)
    assert "text=brutalist+architecture" in url
    assert "p=3" in url


def test_pinterest_uses_pins_endpoint():
    url = _lookup("pinterest").build_url("eames chair", 1)
    assert "pinterest.com/search/pins/?q=eames+chair" in url


def test_special_characters_are_url_encoded():
    url = _lookup("google").build_url("space & time", 0)
    # '&' inside the query must be escaped so it doesn't become a new param
    assert "q=space+%26+time" in url


# --------------------------------------------------------------------------- #
#  plan_search_fanout                                                         #
# --------------------------------------------------------------------------- #


def test_plan_search_fanout_fans_across_all_engines():
    plan = plan_search_fanout(["cats"], pages_per_engine=1)
    # 5 engines × 1 page × 1 query = 5 URLs
    assert len(plan.urls) == len(ENGINES)
    assert set(plan.per_engine.keys()) == {e.name for e in ENGINES}
    assert all(v == 1 for v in plan.per_engine.values())


def test_plan_search_fanout_handles_multiple_pages():
    plan = plan_search_fanout(["cats"], pages_per_engine=3)
    # Google/Bing/Yandex encode page in URL → 3 URLs each = 9.
    # DuckDuckGo & Pinterest don't paginate via URL (JS-driven), so
    # requesting more pages just dedupes to 1 per engine = 2. Total = 11.
    assert plan.per_engine["google"] == 3
    assert plan.per_engine["bing"] == 3
    assert plan.per_engine["yandex"] == 3
    assert plan.per_engine["duckduckgo"] == 1
    assert plan.per_engine["pinterest"] == 1
    assert len(plan.urls) == 11


def test_plan_search_fanout_multiple_queries():
    plan = plan_search_fanout(["cats", "dogs"], pages_per_engine=1)
    assert len(plan.urls) == len(ENGINES) * 2
    assert plan.per_query["cats"] == len(ENGINES)
    assert plan.per_query["dogs"] == len(ENGINES)


def test_plan_search_fanout_accepts_dict_queries():
    # AI query pack shape: [{"q": "…", ...}, ...]
    plan = plan_search_fanout(
        [{"q": "vaporwave", "priority": 1}, {"query": "art deco"}],
        pages_per_engine=1,
    )
    assert plan.per_query["vaporwave"] == len(ENGINES)
    assert plan.per_query["art deco"] == len(ENGINES)


def test_plan_search_fanout_dedupes_identical_queries():
    plan = plan_search_fanout(["cats", "cats", "cats"], pages_per_engine=1)
    # Each engine URL is identical across the duplicates, so total URLs == engines count
    assert len(plan.urls) == len(ENGINES)


def test_plan_search_fanout_skips_empty_queries():
    plan = plan_search_fanout(["cats", "", None, {"q": ""}, "  "], pages_per_engine=1)
    assert len(plan.per_query) == 1
    assert "cats" in plan.per_query


def test_plan_search_fanout_enabled_engines_flag():
    plan = plan_search_fanout(
        ["cats"], pages_per_engine=2, enabled_engines={"google", "bing"}
    )
    assert set(plan.per_engine.keys()) == {"google", "bing"}
    assert len(plan.urls) == 4  # 2 engines × 2 pages


def test_plan_search_fanout_empty_enabled_set_produces_no_urls():
    plan = plan_search_fanout(["cats"], enabled_engines=set())
    assert plan.urls == []
    assert plan.per_engine == {}
    # We still record that the query was processed, just with 0 hits
    assert plan.per_query["cats"] == 0


def test_plan_search_fanout_respects_engine_enabled_field():
    disabled_bing = SearchEngine("bing", False, lambda q, p: f"bing://{q}")
    google = SearchEngine("google", True, lambda q, p: f"google://{q}")
    plan = plan_search_fanout(
        ["cats"], pages_per_engine=1, engines=(google, disabled_bing)
    )
    assert plan.per_engine == {"google": 1}


def test_plan_search_fanout_summary_format():
    plan = plan_search_fanout(["cats", "dogs"], pages_per_engine=1)
    summary = plan.as_summary()
    assert summary.startswith("search-fanout:")
    assert "queries=2" in summary
    assert f"urls={len(ENGINES) * 2}" in summary


def test_plan_search_fanout_pages_clamped_to_minimum_1():
    plan = plan_search_fanout(["cats"], pages_per_engine=0)
    assert len(plan.urls) == len(ENGINES)  # 0 → 1 page


def test_plan_search_fanout_builder_exceptions_are_swallowed():
    def _boom(q, p):
        raise RuntimeError("nope")

    broken = SearchEngine("broken", True, _boom)
    ok = SearchEngine("ok", True, lambda q, p: f"ok://{q}?p={p}")
    plan = plan_search_fanout(["cats"], pages_per_engine=2, engines=(broken, ok))
    # Only the healthy engine contributes URLs
    assert plan.per_engine == {"ok": 2}
    assert len(plan.urls) == 2
