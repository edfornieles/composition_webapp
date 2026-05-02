"""
Account-archiver mode for the ingestion campaign.

Goal
====
When a batch's ``campaign_mode == "account_archive"``, the campaign is not
aggregating aesthetic references across the whole web — it's walking a
handful of *specific accounts* (e.g. an Instagram profile, a Tumblr blog,
a Pinterest user page) and pulling everything new since the last cycle.

Semantics are different from scheduled campaigns in three important ways:

1. Seed URLs are stable. They live in ``campaign_state["archive_seeds"]``
   (a list of URLs) and are never replaced by an AI search plan.
2. Each seed carries its own cursor (e.g. ``last_post_pk`` for IG,
   ``max_id`` for Tumblr). Cursors live in
   ``campaign_state["archive_cursors"][seed_url]`` and are merged back into
   ``campaign_state`` after every cycle so the next cycle resumes cleanly.
3. A seed is considered *drained* once an extract returns zero new URLs.
   Drained seeds are skipped on subsequent cycles until the operator
   clears them (a batch may reset ``archive_drained`` to re-archive).

This module exposes a single entry point:

    plan_archive_cycle(seeds, state, *, max_urls_per_seed, enabled_adapters)
        → ArchivePlan(urls, next_state, per_seed)

The planner is **pure-Python and side-effect-free** — it does not write
to the DB and does not download anything. Callers (``views._run_campaign_cycle``)
integrate it by feeding the returned URLs into the normal download
pipeline and persisting ``next_state`` on the batch.

Keeping the planner isolated lets us unit-test the resume / drain logic
without standing up adapters, credentials, or the browser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from djangoscrap.ingestion_adapters import registry as _adapters_registry
from djangoscrap.ingestion_adapters.base import Adapter, AdapterResult


@dataclass
class SeedResult:
    """What happened when we walked a single seed this cycle."""

    seed_url: str
    adapter: Optional[str]
    urls: list[str] = field(default_factory=list)
    drained: bool = False
    error: Optional[str] = None
    next_cursor: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class ArchivePlan:
    """Aggregate output of a single archive-cycle planning pass."""

    urls: list[str] = field(default_factory=list)
    next_state: dict = field(default_factory=dict)
    per_seed: list[SeedResult] = field(default_factory=list)

    def as_summary(self) -> str:
        drained = sum(1 for s in self.per_seed if s.drained)
        errored = sum(1 for s in self.per_seed if not s.ok)
        ok = len(self.per_seed) - errored
        return (
            f"archive: seeds={len(self.per_seed)} ok={ok} drained={drained} "
            f"errors={errored} urls={len(self.urls)}"
        )


# Type alias: (seed_url, cursor_state, max_urls, enabled_adapters) → (Adapter|None, AdapterResult|None)
DispatchFn = Callable[
    [str, dict, int, Optional[set[str]]],
    tuple[Optional[Adapter], Optional[AdapterResult]],
]


def _default_dispatch(
    seed_url: str, cursor: dict, max_urls: int, enabled_adapters: Optional[set[str]]
) -> tuple[Optional[Adapter], Optional[AdapterResult]]:
    """Thin wrapper around the real registry — injectable for tests."""
    return _adapters_registry.dispatch(
        seed_url,
        max_urls=max_urls,
        state=dict(cursor or {}),
        enabled_adapters=enabled_adapters,
    )


def plan_archive_cycle(
    seeds: Iterable[str],
    state: Optional[dict] = None,
    *,
    max_urls_per_seed: int = 200,
    enabled_adapters: Optional[set[str]] = None,
    dispatch: DispatchFn = _default_dispatch,
) -> ArchivePlan:
    """
    Walk each seed URL through the adapter registry exactly once.

    Parameters
    ----------
    seeds:
        Account / profile / blog URLs to archive. Order is preserved in the
        returned plan; duplicates are dropped.
    state:
        Current ``campaign_state`` JSON for the batch. We read:
          * ``archive_cursors[seed]`` — opaque cursor handed to the adapter
          * ``archive_drained``       — list of seeds already exhausted
        Both keys are optional and default to empty.
    max_urls_per_seed:
        Upper bound on how many URLs any single seed may contribute this
        cycle. Keeps a single chatty profile from starving other seeds.
    enabled_adapters:
        Feature-flag set passed through to the registry dispatch. ``None``
        means "whatever the registry says is enabled".
    dispatch:
        Injection point for tests — defaults to the real adapter registry.

    Returns
    -------
    ArchivePlan with the flattened URL list (in seed order), a fresh
    ``next_state`` dict (safe to assign back onto the batch), and a
    per-seed breakdown for reporting / debugging.
    """
    state = dict(state or {})
    prior_cursors: dict[str, dict] = dict(state.get("archive_cursors") or {})
    prior_drained: list[str] = list(state.get("archive_drained") or [])
    drained_set = set(prior_drained)

    # Preserve input order while deduping — seeds may be re-entered by the
    # operator and we don't want to double-walk them in one cycle.
    seen_seeds: set[str] = set()
    ordered_seeds: list[str] = []
    for s in seeds:
        if not s or s in seen_seeds:
            continue
        seen_seeds.add(s)
        ordered_seeds.append(s)

    next_cursors = dict(prior_cursors)
    all_urls: list[str] = []
    per_seed: list[SeedResult] = []
    seen_urls: set[str] = set()

    for seed in ordered_seeds:
        if seed in drained_set:
            # Drained seeds are a no-op until the operator clears them.
            per_seed.append(
                SeedResult(
                    seed_url=seed,
                    adapter=None,
                    drained=True,
                    next_cursor=dict(prior_cursors.get(seed) or {}),
                )
            )
            continue

        cursor = dict(prior_cursors.get(seed) or {})
        adapter, result = dispatch(seed, cursor, max_urls_per_seed, enabled_adapters)

        if adapter is None:
            per_seed.append(
                SeedResult(
                    seed_url=seed,
                    adapter=None,
                    error="no adapter registered",
                    next_cursor=cursor,
                )
            )
            continue

        if result is None or result.error:
            per_seed.append(
                SeedResult(
                    seed_url=seed,
                    adapter=adapter.name,
                    error=(result.error if result else "no result"),
                    next_cursor=cursor,
                )
            )
            continue

        # Dedupe against URLs collected earlier in this cycle (some creators
        # cross-post to multiple platforms).
        seed_urls: list[str] = []
        for u in result.urls or []:
            if u in seen_urls:
                continue
            seen_urls.add(u)
            seed_urls.append(u)

        adapter_next_state = dict(result.next_state or {})
        if adapter_next_state:
            next_cursors[seed] = adapter_next_state
        elif seed in next_cursors:
            # Adapter explicitly returned empty state → clear any stale cursor.
            del next_cursors[seed]

        is_drained = len(seed_urls) == 0
        if is_drained:
            drained_set.add(seed)

        all_urls.extend(seed_urls)
        per_seed.append(
            SeedResult(
                seed_url=seed,
                adapter=adapter.name,
                urls=seed_urls,
                drained=is_drained,
                next_cursor=adapter_next_state,
            )
        )

    # Build the new state dict. We preserve any unrelated keys the caller
    # had stashed (aesthetic continuity, search plans, etc.) and only
    # mutate the archive-specific ones.
    next_state = dict(state)
    next_state["archive_cursors"] = next_cursors
    next_state["archive_drained"] = sorted(drained_set)

    return ArchivePlan(urls=all_urls, next_state=next_state, per_seed=per_seed)
