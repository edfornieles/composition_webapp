"""
Multi-engine search fan-out.

Given a free-text concept ("vaporwave record sleeve", "brutalist
architecture"), build a list of **search-result-page URLs** across every
enabled engine (Google Images, Bing Images, Yandex Images, DuckDuckGo,
Pinterest search). Those URLs are then fed to the normal extractor /
downloader pipeline, which already knows how to pull images out of a
search-result page.

Why it's a separate module
==========================
The legacy campaign flow picks ONE engine per query. That's fine for
small batches but wastes signal — each engine has different coverage,
and aesthetic-continuity aggregation benefits from the union.

This module is the planner piece: pure-Python, no network, side-effect
free. The caller decides whether to pipe the returned URLs through
``parallel_download_urls`` (typical) or process them serially.

Engine layout
=============
Each entry in :data:`ENGINES` is a small :class:`SearchEngine` that
knows:
  * ``name``       — stable identifier we record in stats
  * ``enabled``    — default on/off, flipped by feature flag
  * ``build_url``  — ``(query, page) → str``

Adding an engine is a ~10-line append. Unit tests cover URL escaping
and the "per-query page-budget" logic.

Integration contract
====================
    plan_search_fanout(queries, *, pages_per_engine=2, enabled_engines=None)
        → SearchPlan(urls, per_engine, per_query)

``urls`` is the flattened, deduped list of *search-result page* URLs
ready to hand to ``_extract_candidates_from_page`` or the parallel
downloader. ``per_engine`` / ``per_query`` are breakdowns for reporting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from urllib.parse import quote_plus


@dataclass(frozen=True)
class SearchEngine:
    name: str
    enabled: bool
    build_url: Callable[[str, int], str]


def _google(query: str, page: int) -> str:
    # ``page`` is a 0-indexed offset; Google uses start=N with N a multiple of ~20.
    start = max(0, page) * 20
    q = quote_plus(query)
    if start == 0:
        return f"https://www.google.com/search?q={q}&tbm=isch"
    return f"https://www.google.com/search?q={q}&tbm=isch&start={start}"


def _bing(query: str, page: int) -> str:
    # Bing uses ``first=N`` starting at 1 for the first result.
    first = max(0, page) * 35 + 1
    q = quote_plus(query)
    return f"https://www.bing.com/images/search?q={q}&first={first}"


def _yandex(query: str, page: int) -> str:
    p = max(0, page)
    q = quote_plus(query)
    return f"https://yandex.com/images/search?text={q}&p={p}"


def _duckduckgo(query: str, page: int) -> str:
    # DDG images is JS-heavy but Playwright handles it; the URL itself is stable.
    q = quote_plus(query)
    return f"https://duckduckgo.com/?q={q}&iax=images&ia=images"


def _pinterest(query: str, page: int) -> str:
    q = quote_plus(query)
    return f"https://www.pinterest.com/search/pins/?q={q}"


ENGINES: tuple[SearchEngine, ...] = (
    SearchEngine("google", True, _google),
    SearchEngine("bing", True, _bing),
    SearchEngine("yandex", True, _yandex),
    SearchEngine("duckduckgo", True, _duckduckgo),
    SearchEngine("pinterest", True, _pinterest),
)


@dataclass
class SearchPlan:
    urls: list[str] = field(default_factory=list)
    per_engine: dict[str, int] = field(default_factory=dict)
    per_query: dict[str, int] = field(default_factory=dict)

    def as_summary(self) -> str:
        engines = ",".join(
            f"{k}={v}" for k, v in sorted(self.per_engine.items())
        )
        return (
            f"search-fanout: queries={len(self.per_query)} "
            f"urls={len(self.urls)} [{engines}]"
        )


def _normalize_query(q) -> Optional[str]:
    """Accept either a plain string or a ``{"q": "..."}``-shaped dict."""
    if q is None:
        return None
    if isinstance(q, dict):
        text = str(q.get("q") or q.get("query") or "").strip()
    else:
        text = str(q).strip()
    return text or None


def plan_search_fanout(
    queries: Iterable,
    *,
    pages_per_engine: int = 2,
    enabled_engines: Optional[set[str]] = None,
    engines: Optional[tuple[SearchEngine, ...]] = None,
) -> SearchPlan:
    """
    Expand each query into (engine × page) search-result-page URLs.

    Parameters
    ----------
    queries:
        Iterable of free-text concepts. Also accepts ``{"q": "..."}``
        dicts so existing AI-generated query packs can be passed in
        unchanged.
    pages_per_engine:
        How many paginated result pages to request per (engine, query)
        pair. Keep this low (1-3) — most engines start returning junk
        after page 3 and the extractor already dedupes.
    enabled_engines:
        Feature-flag subset. ``None`` = "whichever engines are marked
        enabled by default". Pass an explicit set (e.g. the campaign
        settings) to narrow further.
    engines:
        Injection point for tests.

    Returns
    -------
    SearchPlan with the flattened URL list, per-engine counts (observability),
    and per-query counts (helps diagnose which concepts are under-served).
    """
    pool = engines if engines is not None else ENGINES
    per_engine: dict[str, int] = {}
    per_query: dict[str, int] = {}
    urls: list[str] = []
    seen: set[str] = set()

    active = [
        e for e in pool
        if e.enabled and (enabled_engines is None or e.name in enabled_engines)
    ]

    pages = max(1, int(pages_per_engine))

    for raw in queries:
        q = _normalize_query(raw)
        if not q:
            continue
        before = len(urls)
        for engine in active:
            for page in range(pages):
                try:
                    url = engine.build_url(q, page)
                except Exception:
                    continue
                if not url or url in seen:
                    continue
                seen.add(url)
                urls.append(url)
                per_engine[engine.name] = per_engine.get(engine.name, 0) + 1
        per_query[q] = len(urls) - before

    return SearchPlan(urls=urls, per_engine=per_engine, per_query=per_query)
