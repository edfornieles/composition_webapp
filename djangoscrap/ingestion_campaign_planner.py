"""
Concept-to-campaign auto-config (Phase 6).

Stitches the three Phase-4 planners into a single coherent cycle:

    * :func:`djangoscrap.ingestion_search_fanout.plan_search_fanout`    (4B)
    * :func:`djangoscrap.ingestion_archiver.plan_archive_cycle`         (4A)
    * :func:`djangoscrap.ingestion_cbir.plan_cbir_fanout`               (4C)

North-star behaviour
====================
Operator types a free-text brief and optionally drops in a few reference
images or accounts. Everything else is auto-configured:

1. **Query pack** is already produced by the existing AI helper
   (``_build_ai_query_pack``) — this module doesn't rebuild it; it
   accepts a pre-computed ``queries`` list so LLM coupling stays optional.
2. The **search fan-out** converts each query into SERP URLs across the
   enabled engines.
3. The **archive planner** walks any profile seeds (IG handles, Tumblr
   blogs, Pinterest boards) with resume cursors.
4. The **CBIR planner** reverse-searches each reference image across
   the enabled visual engines.
5. The three URL lists are concatenated, deduped, and returned as a
   single ``CycleBrief`` ready to hand to the downloader fan-out.

Invariants
==========
* **Pure-Python / side-effect-free.** No DB, no network, no disk writes.
* Output ordering is stable across calls with the same input — helps
  caching and makes unit tests deterministic.
* ``state`` flows through unchanged except for the archive cursors the
  4A planner mutates. Callers are expected to persist the returned
  ``next_state`` back onto the batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from djangoscrap.ingestion_archiver import (
    ArchivePlan,
    SeedResult,
    plan_archive_cycle,
)
from djangoscrap.ingestion_cbir import CbirPlan, plan_cbir_fanout
from djangoscrap.ingestion_search_fanout import SearchPlan, plan_search_fanout


@dataclass
class CampaignBrief:
    """User/AI-supplied inputs that describe what to aggregate."""

    concept: str = ""
    queries: list = field(default_factory=list)          # list[str|dict]
    archive_seeds: list[str] = field(default_factory=list)
    cbir_seeds: list = field(default_factory=list)       # SeedImage|dict|str


@dataclass
class CampaignSettings:
    """Knobs the operator can tune (sensible defaults baked in)."""

    pages_per_engine: int = 2
    archive_max_urls_per_seed: int = 200
    enabled_search_engines: Optional[set[str]] = None
    enabled_cbir_engines: Optional[set[str]] = None
    enabled_adapters: Optional[set[str]] = None

    def __post_init__(self) -> None:
        # Frozen-ish: normalize None to None (not empty set) so the
        # downstream planners can tell "no filter" from "filter to
        # nothing".
        if self.enabled_search_engines is not None:
            self.enabled_search_engines = set(self.enabled_search_engines)
        if self.enabled_cbir_engines is not None:
            self.enabled_cbir_engines = set(self.enabled_cbir_engines)
        if self.enabled_adapters is not None:
            self.enabled_adapters = set(self.enabled_adapters)


@dataclass
class CycleBrief:
    """Unified plan for one ingestion cycle."""

    urls: list[str] = field(default_factory=list)
    next_state: dict = field(default_factory=dict)
    search_plan: Optional[SearchPlan] = None
    archive_plan: Optional[ArchivePlan] = None
    cbir_plan: Optional[CbirPlan] = None

    def as_summary(self) -> str:
        """Single-line observability — feed into ``campaign_last_report``."""
        parts = []
        if self.search_plan:
            parts.append(self.search_plan.as_summary())
        if self.archive_plan:
            parts.append(self.archive_plan.as_summary())
        if self.cbir_plan:
            parts.append(self.cbir_plan.as_summary())
        parts.append(f"cycle-total-urls={len(self.urls)}")
        return " | ".join(parts)


def build_cycle_brief(
    brief: CampaignBrief,
    state: Optional[dict] = None,
    settings: Optional[CampaignSettings] = None,
) -> CycleBrief:
    """
    Compose search / archive / CBIR plans into one cycle brief.

    Each sub-planner is optional — if the brief has no queries, the
    search plan is skipped, etc. That way a "pure archive" campaign
    (just sync these 3 IG accounts) costs nothing extra, and a
    "pure discovery" campaign (just search these 12 concepts) also
    works without seeds or references.
    """
    state = dict(state or {})
    settings = settings or CampaignSettings()

    search_plan: Optional[SearchPlan] = None
    archive_plan: Optional[ArchivePlan] = None
    cbir_plan: Optional[CbirPlan] = None

    # Preserve insertion order: search first (broadest coverage), then
    # archive (highest signal for known accounts), then CBIR (slowest,
    # best for tight aesthetic continuity). De-duping at the end means
    # downstream never sees the same URL twice even if two stages found
    # it.
    if brief.queries:
        search_plan = plan_search_fanout(
            brief.queries,
            pages_per_engine=settings.pages_per_engine,
            enabled_engines=settings.enabled_search_engines,
        )

    next_state = state
    if brief.archive_seeds:
        archive_plan = plan_archive_cycle(
            brief.archive_seeds,
            state=state,
            max_urls_per_seed=settings.archive_max_urls_per_seed,
            enabled_adapters=settings.enabled_adapters,
        )
        next_state = archive_plan.next_state

    if brief.cbir_seeds:
        cbir_plan = plan_cbir_fanout(
            brief.cbir_seeds,
            enabled_engines=settings.enabled_cbir_engines,
        )

    combined: list[str] = []
    seen: set[str] = set()
    for source in (search_plan, archive_plan, cbir_plan):
        if source is None:
            continue
        for u in source.urls:
            if not u or u in seen:
                continue
            seen.add(u)
            combined.append(u)

    return CycleBrief(
        urls=combined,
        next_state=next_state,
        search_plan=search_plan,
        archive_plan=archive_plan,
        cbir_plan=cbir_plan,
    )


# --------------------------------------------------------------------------- #
#  Concept → CampaignBrief (mostly a classifier over user input)              #
# --------------------------------------------------------------------------- #


def classify_seed(raw: str) -> str:
    """
    Return one of ``"archive" | "cbir" | "query"`` for a raw input line.

    Heuristics:
      * URL ending in ``.jpg/.png/.webp/.gif`` → CBIR reference.
      * URL with a recognised profile host (instagram/tumblr/pinterest
        /twitter/x) → archive seed.
      * Anything else is treated as a free-text search query.

    Intentionally heuristic — this is "best effort" so a user can paste
    a mixed list and have it split correctly without annotation. Callers
    that know what they're doing can skip this and fill ``CampaignBrief``
    fields directly.
    """
    if not raw:
        return "query"
    s = raw.strip()
    lower = s.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        if any(
            lower.rsplit("?", 1)[0].endswith(ext)
            for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
        ):
            return "cbir"
        for host_tag in (
            "instagram.com/",
            "tumblr.com/",
            "pinterest.com/",
            "pinterest.co.uk/",
            "pinterest.ca/",
            "twitter.com/",
            "x.com/",
        ):
            if host_tag in lower:
                return "archive"
        # Any other URL — safest as a query-equivalent (caller may just
        # want us to crawl it with the generic extractor). We don't
        # currently auto-route generic URLs anywhere, so mark as query.
        return "query"
    return "query"


def infer_brief(
    concept: str,
    raw_inputs: Iterable[str],
    *,
    queries: Optional[list] = None,
) -> CampaignBrief:
    """
    Partition an operator's mixed paste-list into the three brief slots.

    ``queries`` is an optional pre-built list (e.g. from
    ``_build_ai_query_pack``); anything classified as a query from
    ``raw_inputs`` is appended to it.
    """
    archive: list[str] = []
    cbir: list[str] = []
    qs: list = list(queries or [])
    seen: set[str] = set()

    for raw in raw_inputs:
        if not raw:
            continue
        s = raw.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        kind = classify_seed(s)
        if kind == "archive":
            archive.append(s)
        elif kind == "cbir":
            cbir.append(s)
        else:
            qs.append(s)

    return CampaignBrief(
        concept=concept,
        queries=qs,
        archive_seeds=archive,
        cbir_seeds=cbir,
    )


__all__ = [
    "CampaignBrief",
    "CampaignSettings",
    "CycleBrief",
    "build_cycle_brief",
    "classify_seed",
    "infer_brief",
    # Re-export the sub-results for callers that want to reach in:
    "SearchPlan",
    "ArchivePlan",
    "CbirPlan",
    "SeedResult",
]
