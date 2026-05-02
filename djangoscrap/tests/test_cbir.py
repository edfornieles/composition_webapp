"""Tests for the reverse-image-search fan-out planner."""

from __future__ import annotations

from djangoscrap.ingestion_cbir import (
    ENGINES,
    CbirPlan,
    ReverseEngine,
    SeedImage,
    plan_cbir_fanout,
)


def _lookup(name: str) -> ReverseEngine:
    for e in ENGINES:
        if e.name == name:
            return e
    raise KeyError(name)


# --------------------------------------------------------------------------- #
#  Engine URL builders                                                        #
# --------------------------------------------------------------------------- #


def test_yandex_reverse_url_has_rpt_imageview_and_encoded_url():
    url = _lookup("yandex-rev").build_url("https://cdn.example.com/seed.jpg")
    assert url.startswith("https://yandex.com/images/search?")
    assert "rpt=imageview" in url
    # The input URL must be URL-encoded inside the query string
    assert "url=https%3A%2F%2Fcdn.example.com%2Fseed.jpg" in url


def test_google_reverse_url_uses_image_url_param():
    url = _lookup("google-rev").build_url("https://cdn.example.com/seed.jpg")
    assert "google.com/searchbyimage" in url
    assert "image_url=https%3A%2F%2Fcdn.example.com%2Fseed.jpg" in url


def test_bing_reverse_url_uses_mediaurl_param():
    url = _lookup("bing-rev").build_url("https://cdn.example.com/seed.jpg")
    assert "bing.com/images/search" in url
    assert "view=detailv2" in url
    assert "mediaurl=https%3A%2F%2Fcdn.example.com%2Fseed.jpg" in url


def test_tineye_reverse_url_uses_url_param():
    url = _lookup("tineye-rev").build_url("https://cdn.example.com/seed.jpg")
    assert url.startswith("https://tineye.com/search?url=")


# --------------------------------------------------------------------------- #
#  plan_cbir_fanout                                                           #
# --------------------------------------------------------------------------- #


def test_plan_cbir_fans_across_all_engines():
    plan = plan_cbir_fanout(["https://cdn.example.com/seed.jpg"])
    assert len(plan.urls) == len(ENGINES)
    assert set(plan.per_engine.keys()) == {e.name for e in ENGINES}


def test_plan_cbir_handles_multiple_seeds():
    seeds = [
        "https://cdn.example.com/a.jpg",
        "https://cdn.example.com/b.jpg",
    ]
    plan = plan_cbir_fanout(seeds)
    assert len(plan.urls) == len(ENGINES) * 2
    assert plan.per_seed["https://cdn.example.com/a.jpg"] == len(ENGINES)
    assert plan.per_seed["https://cdn.example.com/b.jpg"] == len(ENGINES)


def test_plan_cbir_dedupes_repeated_seeds():
    plan = plan_cbir_fanout(
        [
            "https://cdn.example.com/a.jpg",
            "https://cdn.example.com/a.jpg",
        ]
    )
    assert len(plan.urls) == len(ENGINES)


def test_plan_cbir_accepts_seed_image_dataclass():
    plan = plan_cbir_fanout(
        [SeedImage(url="https://cdn.example.com/a.jpg", weight=2.0, note="centroid")]
    )
    assert len(plan.urls) == len(ENGINES)
    assert "https://cdn.example.com/a.jpg" in plan.per_seed


def test_plan_cbir_accepts_dict_seeds():
    plan = plan_cbir_fanout(
        [{"url": "https://cdn.example.com/a.jpg", "weight": 1.5, "note": "boost"}]
    )
    assert len(plan.urls) == len(ENGINES)


def test_plan_cbir_skips_empty_and_malformed():
    plan = plan_cbir_fanout(
        [
            "",
            None,
            {"url": ""},
            {"no_url": "x"},
            SeedImage(url=""),
            "https://cdn.example.com/good.jpg",
            42,  # not a url-ish type
        ]
    )
    assert list(plan.per_seed.keys()) == ["https://cdn.example.com/good.jpg"]


def test_plan_cbir_enabled_engines_filter():
    plan = plan_cbir_fanout(
        ["https://cdn.example.com/a.jpg"],
        enabled_engines={"yandex-rev", "tineye-rev"},
    )
    assert set(plan.per_engine.keys()) == {"yandex-rev", "tineye-rev"}


def test_plan_cbir_empty_enabled_set_produces_empty_plan():
    plan = plan_cbir_fanout(
        ["https://cdn.example.com/a.jpg"], enabled_engines=set()
    )
    assert plan.urls == []
    assert plan.per_seed == {"https://cdn.example.com/a.jpg": 0}


def test_plan_cbir_respects_engine_enabled_field():
    disabled = ReverseEngine("off", False, lambda u: f"off://{u}")
    on = ReverseEngine("on", True, lambda u: f"on://{u}")
    plan = plan_cbir_fanout(
        ["https://cdn.example.com/a.jpg"], engines=(disabled, on)
    )
    assert plan.per_engine == {"on": 1}


def test_plan_cbir_builder_exceptions_swallowed():
    def _boom(_):
        raise RuntimeError("nope")

    broken = ReverseEngine("broken", True, _boom)
    ok = ReverseEngine("ok", True, lambda u: f"ok://{u}")
    plan = plan_cbir_fanout(
        ["https://cdn.example.com/a.jpg"], engines=(broken, ok)
    )
    assert plan.per_engine == {"ok": 1}


def test_plan_cbir_summary_format():
    plan = plan_cbir_fanout(
        ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"]
    )
    summary = plan.as_summary()
    assert summary.startswith("cbir:")
    assert "seeds=2" in summary
    assert f"urls={len(ENGINES) * 2}" in summary
