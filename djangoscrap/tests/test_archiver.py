"""
Tests for the account-archiver planner.

The planner is pure Python — no DB, no network — so every test here runs
offline with a fake dispatch function.
"""

from __future__ import annotations

from typing import Optional

from djangoscrap.ingestion_adapters.base import Adapter, AdapterResult
from djangoscrap.ingestion_archiver import (
    ArchivePlan,
    SeedResult,
    plan_archive_cycle,
)


# --------------------------------------------------------------------------- #
#  Fake adapter + dispatch                                                    #
# --------------------------------------------------------------------------- #


class _FakeAdapter:
    """Minimal duck-typed Adapter good enough for the planner."""

    def __init__(self, name: str, priority: int = 100):
        self.name = name
        self.priority = priority

    def can_handle(self, url: str) -> bool:  # pragma: no cover - unused
        return True

    def extract(self, url, *, max_urls, state):  # pragma: no cover - unused
        return AdapterResult()


def _dispatch_factory(responses: dict[str, tuple[Optional[_FakeAdapter], Optional[AdapterResult]]]):
    """Build a dispatch function whose return is a simple dict lookup.

    Keys are seed URLs. If a seed isn't in the dict we return (None, None)
    so the planner treats it as "no adapter registered".
    """
    calls: list[tuple[str, dict, int, Optional[set[str]]]] = []

    def _fake(seed, cursor, max_urls, enabled):
        calls.append((seed, dict(cursor), max_urls, set(enabled) if enabled else None))
        return responses.get(seed, (None, None))

    _fake.calls = calls  # type: ignore[attr-defined]
    return _fake


# --------------------------------------------------------------------------- #
#  Basic URL collection                                                       #
# --------------------------------------------------------------------------- #


def test_plan_archive_collects_urls_in_seed_order():
    ada = _FakeAdapter("instagrapi")
    dispatch = _dispatch_factory({
        "https://instagram.com/alice": (ada, AdapterResult(urls=["u1", "u2"])),
        "https://instagram.com/bob": (ada, AdapterResult(urls=["u3"])),
    })
    plan = plan_archive_cycle(
        ["https://instagram.com/alice", "https://instagram.com/bob"],
        dispatch=dispatch,
    )
    assert plan.urls == ["u1", "u2", "u3"]
    assert [s.seed_url for s in plan.per_seed] == [
        "https://instagram.com/alice",
        "https://instagram.com/bob",
    ]
    assert all(s.ok and not s.drained for s in plan.per_seed)


def test_plan_archive_dedupes_urls_across_seeds():
    ada = _FakeAdapter("instagrapi")
    dispatch = _dispatch_factory({
        "https://instagram.com/alice": (ada, AdapterResult(urls=["shared.jpg", "a.jpg"])),
        "https://instagram.com/bob": (ada, AdapterResult(urls=["shared.jpg", "b.jpg"])),
    })
    plan = plan_archive_cycle(
        ["https://instagram.com/alice", "https://instagram.com/bob"],
        dispatch=dispatch,
    )
    assert plan.urls == ["shared.jpg", "a.jpg", "b.jpg"]
    assert plan.per_seed[1].urls == ["b.jpg"]


def test_plan_archive_dedupes_input_seeds():
    ada = _FakeAdapter("instagrapi")
    dispatch = _dispatch_factory({
        "https://instagram.com/alice": (ada, AdapterResult(urls=["u1"])),
    })
    plan = plan_archive_cycle(
        ["https://instagram.com/alice", "https://instagram.com/alice", ""],
        dispatch=dispatch,
    )
    assert len(plan.per_seed) == 1
    assert plan.urls == ["u1"]


# --------------------------------------------------------------------------- #
#  Cursors & resumability                                                     #
# --------------------------------------------------------------------------- #


def test_plan_archive_threads_prior_cursor_into_dispatch():
    ada = _FakeAdapter("instagrapi")
    dispatch = _dispatch_factory({
        "https://instagram.com/alice": (
            ada,
            AdapterResult(urls=["u1"], next_state={"last_post_pk": "42"}),
        ),
    })
    prior_state = {"archive_cursors": {"https://instagram.com/alice": {"last_post_pk": "10"}}}
    plan = plan_archive_cycle(
        ["https://instagram.com/alice"], state=prior_state, dispatch=dispatch
    )
    # Dispatch was called with the prior cursor
    assert dispatch.calls[0][1] == {"last_post_pk": "10"}
    # Next state records the fresh cursor the adapter returned
    assert plan.next_state["archive_cursors"]["https://instagram.com/alice"] == {
        "last_post_pk": "42"
    }


def test_plan_archive_clears_cursor_when_adapter_returns_empty_state():
    ada = _FakeAdapter("instagrapi")
    dispatch = _dispatch_factory({
        "https://instagram.com/alice": (
            ada,
            AdapterResult(urls=["u1"], next_state={}),
        ),
    })
    prior_state = {"archive_cursors": {"https://instagram.com/alice": {"last_post_pk": "10"}}}
    plan = plan_archive_cycle(
        ["https://instagram.com/alice"], state=prior_state, dispatch=dispatch
    )
    # Stale cursor is dropped rather than being preserved forever
    assert "https://instagram.com/alice" not in plan.next_state["archive_cursors"]


def test_plan_archive_preserves_unrelated_state_keys():
    ada = _FakeAdapter("instagrapi")
    dispatch = _dispatch_factory({
        "https://instagram.com/alice": (ada, AdapterResult(urls=["u1"])),
    })
    prior_state = {
        "archive_cursors": {},
        "search_plan": {"q": "vaporwave"},
        "clip_centroid": [0.1, 0.2],
    }
    plan = plan_archive_cycle(
        ["https://instagram.com/alice"], state=prior_state, dispatch=dispatch
    )
    assert plan.next_state["search_plan"] == {"q": "vaporwave"}
    assert plan.next_state["clip_centroid"] == [0.1, 0.2]


# --------------------------------------------------------------------------- #
#  Drain logic                                                                #
# --------------------------------------------------------------------------- #


def test_plan_archive_marks_seed_drained_when_adapter_returns_zero_urls():
    ada = _FakeAdapter("instagrapi")
    dispatch = _dispatch_factory({
        "https://instagram.com/alice": (ada, AdapterResult(urls=[])),
    })
    # AdapterResult.ok is False for empty, but planner still runs.
    # The planner needs to drive even on .error="…" returns too; we test
    # that scenario separately. Here we simulate a successful-but-empty
    # response by hand since AdapterResult normally treats empty as error.
    # We patch the planner via a small custom AdapterResult subclass.

    class _NoErrResult(AdapterResult):
        error: Optional[str] = None

    dispatch = _dispatch_factory({
        "https://instagram.com/alice": (ada, _NoErrResult(urls=[])),
    })

    plan = plan_archive_cycle(["https://instagram.com/alice"], dispatch=dispatch)
    assert plan.per_seed[0].drained is True
    assert plan.next_state["archive_drained"] == ["https://instagram.com/alice"]


def test_plan_archive_skips_previously_drained_seeds():
    ada = _FakeAdapter("instagrapi")
    calls: list[str] = []

    def _dispatch(seed, cursor, max_urls, enabled):
        calls.append(seed)
        return ada, AdapterResult(urls=["u1"])

    prior_state = {"archive_drained": ["https://instagram.com/alice"]}
    plan = plan_archive_cycle(
        ["https://instagram.com/alice", "https://instagram.com/bob"],
        state=prior_state,
        dispatch=_dispatch,
    )
    # Alice is skipped entirely; only Bob hits the adapter.
    assert calls == ["https://instagram.com/bob"]
    alice = plan.per_seed[0]
    assert alice.drained is True and alice.adapter is None
    bob = plan.per_seed[1]
    assert bob.ok and bob.urls == ["u1"]


# --------------------------------------------------------------------------- #
#  Error handling                                                             #
# --------------------------------------------------------------------------- #


def test_plan_archive_records_missing_adapter_as_error():
    dispatch = _dispatch_factory({})
    plan = plan_archive_cycle(["https://unknown.site/x"], dispatch=dispatch)
    assert plan.per_seed[0].error == "no adapter registered"
    assert plan.urls == []
    # Seed is NOT drained — operator may add a new adapter next cycle.
    assert "https://unknown.site/x" not in plan.next_state["archive_drained"]


def test_plan_archive_records_adapter_error():
    ada = _FakeAdapter("instagrapi")
    dispatch = _dispatch_factory({
        "https://instagram.com/alice": (ada, AdapterResult(error="login required")),
    })
    plan = plan_archive_cycle(["https://instagram.com/alice"], dispatch=dispatch)
    assert plan.per_seed[0].error == "login required"
    assert plan.per_seed[0].adapter == "instagrapi"
    assert plan.urls == []


def test_plan_archive_summary_reports_counts():
    ada = _FakeAdapter("instagrapi")
    dispatch = _dispatch_factory({
        "https://instagram.com/alice": (ada, AdapterResult(urls=["u1", "u2"])),
        "https://instagram.com/bob": (ada, AdapterResult(error="rate-limited")),
    })
    plan = plan_archive_cycle(
        ["https://instagram.com/alice", "https://instagram.com/bob"], dispatch=dispatch
    )
    s = plan.as_summary()
    assert "seeds=2" in s
    assert "ok=1" in s
    assert "errors=1" in s
    assert "urls=2" in s


def test_plan_archive_respects_enabled_adapters_flag():
    ada = _FakeAdapter("instagrapi")
    seen_flags: list[Optional[set[str]]] = []

    def _dispatch(seed, cursor, max_urls, enabled):
        seen_flags.append(enabled)
        return ada, AdapterResult(urls=["u1"])

    plan_archive_cycle(
        ["https://instagram.com/alice"],
        enabled_adapters={"instagrapi", "yt-dlp"},
        dispatch=_dispatch,
    )
    assert seen_flags == [{"instagrapi", "yt-dlp"}]


def test_plan_archive_caps_max_urls_per_seed():
    ada = _FakeAdapter("instagrapi")
    seen_caps: list[int] = []

    def _dispatch(seed, cursor, max_urls, enabled):
        seen_caps.append(max_urls)
        return ada, AdapterResult(urls=["u"] * max_urls)

    plan_archive_cycle(
        ["https://instagram.com/alice"],
        max_urls_per_seed=50,
        dispatch=_dispatch,
    )
    assert seen_caps == [50]
