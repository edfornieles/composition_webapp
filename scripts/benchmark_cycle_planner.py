"""
Cross-cutting benchmark for the concept-to-campaign planner.

Simulates one operator cycle with a realistic mixed input:
    * 5 free-text concepts
    * 3 profile URLs (archive seeds)
    * 2 reference images (CBIR seeds)

Reports total URL count, per-stage breakdown, and wall time. Should
run in well under 10 ms — anything over that means a regression in
one of the planners.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Make the project importable when invoked as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")

from djangoscrap.ingestion_campaign_planner import (  # noqa: E402
    CampaignSettings,
    build_cycle_brief,
    infer_brief,
)


RAW_INPUTS = [
    "vaporwave record sleeves 1990s pastel",
    "brutalist concrete architecture overhead shot",
    "art nouveau poster typography ornate",
    "minimalist scandinavian interior soft light",
    "retro synthwave neon grid sunset",
    "https://www.instagram.com/vaporwave_archive/",
    "https://www.tumblr.com/tagged/brutalist",
    "https://www.pinterest.com/user/ornament-board/",
    "https://cdn.example.com/refs/vaporwave-hero.jpg",
    "https://cdn.example.com/refs/brutalist-hero.png",
]


def main() -> None:
    brief = infer_brief(concept="aesthetic research", raw_inputs=RAW_INPUTS)
    print(
        f"brief: concept={brief.concept!r} queries={len(brief.queries)} "
        f"archive_seeds={len(brief.archive_seeds)} cbir_seeds={len(brief.cbir_seeds)}"
    )

    settings = CampaignSettings(pages_per_engine=2)

    runs = 50
    started = time.perf_counter()
    for _ in range(runs):
        cycle = build_cycle_brief(brief, settings=settings)
    elapsed_ms = (time.perf_counter() - started) * 1000 / runs

    print()
    print(cycle.as_summary())
    print()
    print(f"unique URLs queued for download: {len(cycle.urls)}")
    print(f"mean wall-clock per build_cycle_brief call: {elapsed_ms:.3f} ms")
    print()
    print("sample URLs (first 5):")
    for u in cycle.urls[:5]:
        print(f"  • {u}")


if __name__ == "__main__":
    main()
