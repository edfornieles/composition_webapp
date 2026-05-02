"""
Build inner-monologue text segments for MonologuePersona.

MVP: curated `stream_seed_text` / `style_corpus` + small archetype fallbacks.
Future: call an LLM with system_prompt_hint + retrieved snippets from source_urls / ingested corpus.
Respect site ToS and robots when ingesting from the open web.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import MonologuePersona

ARCHETYPE_FALLBACK: dict[str, list[str]] = {
    "default": [
        "Another minute down. The room hums and I’m still here, still ticking.",
        "What was that again? Doesn’t matter. It’ll circle back like everything else.",
        "I should move. I don’t. The ceiling knows my name today.",
    ],
    "jock": [
        "Leg day tomorrow. Coach said rest. Rest is for the highlight reel, not for me.",
        "Someone’s counting reps in the other room. I count wins they never see.",
        "Protein, water, sleep — boring religion. Works though. Always works.",
    ],
    "poet": [
        "The rain invents a grammar I almost understand.",
        "Metaphor is a small animal; I feed it silence.",
        "If the moon is a coin, I spend it on verbs.",
    ],
}


def build_segments(persona: "MonologuePersona") -> list[str]:
    """Return ordered lines/blocks to stream (one internal thought per segment)."""
    raw = (persona.stream_seed_text or "").strip()
    if raw:
        parts = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
        if len(parts) >= 2:
            return parts
        parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", raw) if len(s.strip()) > 6]
        return parts or [raw]

    corpus = (persona.style_corpus or "").strip()
    if corpus:
        bits = [s.strip() for s in re.split(r"(?<=[.!?])\s+", corpus) if len(s.strip()) > 8]
        out = []
        for b in bits:
            if b and b[-1] not in ".!?":
                b = b + "."
            out.append(b)
        return out if out else [corpus]

    key = (persona.archetype or "default").strip().lower()
    return list(ARCHETYPE_FALLBACK.get(key, ARCHETYPE_FALLBACK["default"]))
