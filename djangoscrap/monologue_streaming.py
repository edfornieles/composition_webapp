"""
Infinite monologue lines: OpenAI generation with anti-repeat, or weighted fallback from corpus.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from typing import TYPE_CHECKING

from django.conf import settings

from .monologue_utils import build_segments

if TYPE_CHECKING:
    from .models import MonologuePersona

_MAX_RECENT = 15


def _fingerprint(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:24]


def pick_fallback_segment(persona: "MonologuePersona", recent_texts: list[str]) -> str:
    """Avoid immediate repeats using fingerprints; reshuffle when exhausted."""
    segments = build_segments(persona)
    if not segments:
        return ""
    fps = {_fingerprint(t) for t in recent_texts if t}
    pool = [s for s in segments if _fingerprint(s) not in fps]
    if not pool:
        pool = list(segments)
    return random.choice(pool)


def generate_stream_segment(persona: "MonologuePersona", recent_texts: list[str]) -> tuple[str, str | None]:
    """
    Returns (line, error). line may be empty on failure.
    """
    if not getattr(settings, "OPENAI_API_KEY", ""):
        return "", "no_api_key"

    try:
        from openai import OpenAI
    except ImportError as e:
        return "", str(e)

    recent_texts = [t.strip() for t in recent_texts[-_MAX_RECENT :] if t and str(t).strip()]
    recent_block = "\n".join(f"— {t[:420]}" for t in recent_texts[-10:]) or "(none yet)"

    style = (persona.style_corpus or "")[:3200]
    hint = (persona.system_prompt_hint or "")[:2000]
    seed = (persona.stream_seed_text or "")[:1200]
    archetype = (persona.archetype or "").strip() or "character"

    user_msg = f"""Write the next beat of a first-person stream-of-consciousness monologue for a fictional inner voice.

Archetype: {archetype}
Page title: {(persona.title or "").strip()}

Voice / texture notes (may excerpt curated research):
{style}

Steering hints for the narrator:
{hint}

Optional seed tone (do not copy verbatim; avoid repeating its sentences):
{seed}

Lines already shown on this page session — do NOT repeat, closely paraphrase, or circle back to the same images or beats:
{recent_block}

Rules:
- Output JSON only: {{"text": "<2–6 sentences, varied rhythm>"}}
- Stay in character. Concrete sensory detail. No meta commentary about being an AI.
- Must be wholly new compared to the list above."""

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    model = getattr(settings, "MONOLOGUE_STREAM_MODEL", "gpt-4o-mini")

    def _call():
        return client.chat.completions.create(
            model=model,
            temperature=0.92,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "You write literary first-person monologue. Output valid JSON only.",
                },
                {"role": "user", "content": user_msg},
            ],
        )

    try:
        resp = _call()
        raw = (resp.choices[0].message.content or "").strip()
        data = json.loads(raw)
        text = (data.get("text") or "").strip() if isinstance(data, dict) else ""
        if not isinstance(text, str):
            text = ""
    except Exception as e:
        return "", str(e)

    if not text:
        return "", "empty_response"

    fp_new = _fingerprint(text)
    recent_fps = {_fingerprint(t) for t in recent_texts}
    if fp_new in recent_fps:
        try:
            retry_msg = user_msg + "\n\nYour line matched an earlier beat. Write something completely different (new setting, new metaphor)."
            resp = client.chat.completions.create(
                model=model,
                temperature=0.95,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": "You write literary first-person monologue. Output valid JSON only.",
                    },
                    {"role": "user", "content": retry_msg},
                ],
            )
            raw = (resp.choices[0].message.content or "").strip()
            data = json.loads(raw)
            text = (data.get("text") or "").strip() if isinstance(data, dict) else ""
        except Exception:
            pass

    if not text:
        return "", "empty_after_retry"

    return text, None
