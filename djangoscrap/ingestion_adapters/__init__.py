"""
Adapter framework for pluggable ingestion sources.

This package defines a uniform ``Adapter`` protocol plus a priority-ordered
``registry`` so new sourcing surfaces (Pinterest scroll-harvester,
instagrapi, per-site DOM extractors, multi-engine search fan-outs…) can be
added without touching the core cycle code.

Design in one paragraph
=======================
Adapters return URLs. The rest of the pipeline (URL rewriter → HEAD
probe → parallel downloader → dedupe → QC → DB) processes whatever the
adapter produced the same way, regardless of how the adapter found the
URLs. This means every quality improvement we've built (rewriter,
dedupe, QC, HEAD probe, throttle, parallel downloads) applies uniformly
to Yandex results, Pinterest board scrolls, instagrapi profiles, and
anything else we add.

Adapters do NOT download images themselves; they only emit URLs. Mixing
responsibilities (e.g. "instagrapi also saves posts to disk") bypasses
dedupe/QC and was the reason old instaloader paths produced broken
tiles. Keep adapters as pure URL producers.
"""

from djangoscrap.ingestion_adapters.base import (
    Adapter,
    AdapterResult,
)
from djangoscrap.ingestion_adapters.registry import (
    register,
    unregister,
    pick_for,
    list_registered,
)

__all__ = [
    "Adapter",
    "AdapterResult",
    "register",
    "unregister",
    "pick_for",
    "list_registered",
]
