"""
Reverse-image search fan-out ("find more like this").

Given a seed image (already living inside the batch, so we know its path
and URL), build a list of **reverse-image-search result page URLs** across
every enabled engine. Those URLs go through the normal extractor +
downloader pipeline just like a keyword search.

Supported engines
=================
* **Yandex**  — best visual-match coverage, URL-driven, no auth.
* **Google**  — decent coverage; URL format stable.
* **Bing**    — smaller but complements the others.
* **TinEye**  — great for finding higher-resolution originals.

Every engine supports "reverse-by-URL" (we hand it a publicly-hostable
image URL) rather than requiring multipart form upload. This keeps the
fan-out purely URL-based, so it composes with ``parallel_download_urls``
and the rest of the pipeline without special-casing.

Design notes
============
* Pure-Python planner, no network, no DB writes. Matches the style of
  :mod:`djangoscrap.ingestion_archiver` and :mod:`djangoscrap.ingestion_search_fanout`.
* Accepts either a bare URL or a ``SeedImage`` dataclass (carries url +
  optional CLIP embedding for centroid-aware ranking — not used yet, but
  exposed so Phase 6 can plug in).
* Engines that would need a file upload (most don't nowadays) are simply
  omitted here. If one ever needs it, add an ``UploadSearchEngine``
  variant with a ``build_multipart`` callable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from urllib.parse import quote_plus


@dataclass
class SeedImage:
    """Describes one reference image to reverse-search against."""

    url: str
    weight: float = 1.0  # relative importance if a caller wants to rank
    note: str = ""       # human label, passed through for telemetry


@dataclass(frozen=True)
class ReverseEngine:
    name: str
    enabled: bool
    build_url: Callable[[str], str]


def _yandex_rev(image_url: str) -> str:
    # Yandex's reverse-image endpoint takes the image URL as ``url``.
    return f"https://yandex.com/images/search?rpt=imageview&url={quote_plus(image_url)}"


def _google_rev(image_url: str) -> str:
    # Google's by-image search endpoint.
    return f"https://www.google.com/searchbyimage?image_url={quote_plus(image_url)}"


def _bing_rev(image_url: str) -> str:
    # Bing accepts the URL under ``mediaurl``.
    return (
        f"https://www.bing.com/images/search?view=detailv2&iss=sbi"
        f"&mediaurl={quote_plus(image_url)}"
    )


def _tineye_rev(image_url: str) -> str:
    return f"https://tineye.com/search?url={quote_plus(image_url)}"


ENGINES: tuple[ReverseEngine, ...] = (
    ReverseEngine("yandex-rev", True, _yandex_rev),
    ReverseEngine("google-rev", True, _google_rev),
    ReverseEngine("bing-rev", True, _bing_rev),
    ReverseEngine("tineye-rev", True, _tineye_rev),
)


@dataclass
class CbirPlan:
    urls: list[str] = field(default_factory=list)
    per_engine: dict[str, int] = field(default_factory=dict)
    per_seed: dict[str, int] = field(default_factory=dict)

    def as_summary(self) -> str:
        engines = ",".join(
            f"{k}={v}" for k, v in sorted(self.per_engine.items())
        )
        return (
            f"cbir: seeds={len(self.per_seed)} urls={len(self.urls)} "
            f"[{engines}]"
        )


def _normalize_seed(s) -> Optional[SeedImage]:
    if isinstance(s, SeedImage):
        return s if s.url else None
    if isinstance(s, dict):
        url = str(s.get("url") or "").strip()
        if not url:
            return None
        return SeedImage(
            url=url,
            weight=float(s.get("weight") or 1.0),
            note=str(s.get("note") or ""),
        )
    if isinstance(s, str):
        url = s.strip()
        return SeedImage(url=url) if url else None
    return None


def plan_cbir_fanout(
    seeds: Iterable,
    *,
    enabled_engines: Optional[set[str]] = None,
    engines: Optional[tuple[ReverseEngine, ...]] = None,
) -> CbirPlan:
    """
    Expand each seed image into one reverse-search URL per enabled engine.

    Parameters
    ----------
    seeds:
        Iterable of :class:`SeedImage`, ``{"url": …}`` dicts, or bare URL
        strings. Empty / malformed entries are silently skipped.
    enabled_engines:
        Feature-flag set. ``None`` → use every engine marked enabled.
    engines:
        Injection point for tests.

    Returns
    -------
    CbirPlan with the flattened URL list, per-engine counts, and per-seed
    counts for reporting.
    """
    pool = engines if engines is not None else ENGINES
    active = [
        e for e in pool
        if e.enabled and (enabled_engines is None or e.name in enabled_engines)
    ]

    urls: list[str] = []
    seen: set[str] = set()
    per_engine: dict[str, int] = {}
    per_seed: dict[str, int] = {}

    for raw in seeds:
        seed = _normalize_seed(raw)
        if seed is None:
            continue
        if seed.url in per_seed:
            # Already fanned this seed out; skip to keep URL list clean.
            continue
        before = len(urls)
        for engine in active:
            try:
                url = engine.build_url(seed.url)
            except Exception:
                continue
            if not url or url in seen:
                continue
            seen.add(url)
            urls.append(url)
            per_engine[engine.name] = per_engine.get(engine.name, 0) + 1
        per_seed[seed.url] = len(urls) - before

    return CbirPlan(urls=urls, per_engine=per_engine, per_seed=per_seed)
