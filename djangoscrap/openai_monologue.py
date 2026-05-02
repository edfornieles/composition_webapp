"""
Optional OpenAI call for monologue “research” — suggests URLs + tone notes from model knowledge.
Verify URLs before treating them as authoritative; browsing is not performed here.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:
    from .models import MonologuePersona


def run_monologue_research(persona: "MonologuePersona") -> tuple[list[str], str, str, str | None]:
    """
    Returns (urls, corpus_addition, system_fragment, error_message).
    error_message is set on failure; other fields may still be empty lists/strings.
    """
    if not getattr(settings, "OPENAI_API_KEY", ""):
        return [], "", "", "No OPENAI_API_KEY in environment."

    try:
        from openai import OpenAI
    except ImportError as e:
        return [], "", "", f"OpenAI package not installed: {e}"

    archetype = (persona.archetype or "").strip() or "general character"
    title = (persona.title or "").strip() or "Untitled"
    corpus_excerpt = (persona.style_corpus or "")[:3500]
    hint = (persona.system_prompt_hint or "")[:1500]

    schema_hint = """{
  "urls": ["https://example.org/..."],
  "corpus_addition": "Original paragraph of analytical notes for a writer (under 150 words).",
  "system_prompt_fragment": "Optional short steering phrase for first-person monologue generation (under 60 words)."
}"""

    style_block = (
        "Existing style notes (excerpt):\n" + corpus_excerpt
        if corpus_excerpt
        else "No style notes yet."
    )

    user_msg = f"""You curate reference material for a fictional first-person “stream of consciousness” web page.

Archetype / label: {archetype}
Page title: {title}

{style_block}

Existing system prompt hint (excerpt):
{hint}

Respond with a single JSON object only (no markdown fences), matching this shape:
{schema_hint}

Rules:
- "urls": 8–14 strings. Each must be a plausible https URL on real, stable sites (Wikipedia, major news, .gov, .edu, official sports bodies, PMC/NCBI, etc.). Prefer URLs you are confident exist; omit uncertain links.
- "corpus_addition": your own words; do not quote copyrighted text.
- "system_prompt_fragment": optional; omit or use empty string if redundant."""

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    model = getattr(settings, "OPENAI_RESEARCH_MODEL", "gpt-4o-mini")

    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0.35,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "You output only valid JSON. You suggest real-reference URLs carefully; when unsure, omit rather than guess.",
                },
                {"role": "user", "content": user_msg},
            ],
        )
    except Exception as e:
        return [], "", "", str(e)

    raw = (resp.choices[0].message.content or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return [], "", "", f"Invalid JSON from model: {e}"
    if not isinstance(data, dict):
        return [], "", "", "Model response was not a JSON object."

    urls_out: list[str] = []
    for u in data.get("urls") or []:
        if not isinstance(u, str):
            continue
        u = u.strip()
        if u.startswith("http://") or u.startswith("https://"):
            urls_out.append(u)

    corpus = (data.get("corpus_addition") or "").strip()
    if not isinstance(corpus, str):
        corpus = ""

    frag = (data.get("system_prompt_fragment") or "").strip()
    if not isinstance(frag, str):
        frag = ""

    return urls_out, corpus, frag, None
