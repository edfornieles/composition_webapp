"""Greentext quote-back helper.

The single highest-impact gesture for making /fit/ replies feel "heard" is
quoting one phrase of the user's message back as a `>line` before responding.
This module extracts a short, safe-to-quote phrase.

We deliberately do NOT quote:
  - phone numbers, emails, URLs, addresses (privacy)
  - very long passages (>10 words; turns into wall of text)
  - markdown / code blocks (not the board's register)
  - text matching the project's hard safety rules (slurs, doses, etc.)
  - empty / single-word messages (nothing meaningful to quote)
"""
from __future__ import annotations

import re
from typing import Optional


_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def _scrub_private(text: str) -> str:
    text = _CODE_FENCE_RE.sub("", text)
    text = _URL_RE.sub("", text)
    text = _EMAIL_RE.sub("", text)
    text = _PHONE_RE.sub("", text)
    return text


def _split_clauses(text: str) -> list[str]:
    """Split on clause boundaries that survive the board's lowercase style."""
    if not text:
        return []
    # Treat ?, !, ., ; as boundaries; commas only when surrounded by space.
    parts = re.split(r"[?!.;\n]+|\s+,\s+", text)
    return [p.strip() for p in parts if p and p.strip()]


def extract_quote_phrase(user_message: str, *, min_words: int = 2, max_words: int = 10) -> Optional[str]:
    """Return a short, safe-to-quote phrase from the user message, or None.

    Heuristic: scrub private info, split into clauses, pick the first clause
    that's between min_words and max_words. If none match, fall back to the
    first max_words words of the scrubbed text. Returns None when nothing
    quotable remains (empty / too short / safety-stripped to nothing).
    """
    if not user_message:
        return None

    scrubbed = _scrub_private(user_message).strip()
    if not scrubbed:
        return None

    # Late safety check: imageboard_ingestion.safety has slur/dox patterns.
    # We import lazily so this module stays importable without Django.
    try:
        from . import safety as _safety
        if _safety._HATE_RE.search(scrubbed) or _safety._DOX_HINT_RE.search(scrubbed):
            return None
    except Exception:
        pass

    candidates = _split_clauses(scrubbed)
    for c in candidates:
        words = c.split()
        if min_words <= len(words) <= max_words:
            return " ".join(words).lower()

    # Fallback: first `max_words` words of the scrubbed text, if there are at
    # least `min_words`.
    words = scrubbed.split()
    if len(words) < min_words:
        return None
    return " ".join(words[:max_words]).lower()


def render_greentext_quote(phrase: str) -> str:
    """Return a single `>phrase` line suitable for prepending to a reply."""
    if not phrase:
        return ""
    # Strip any leading > the user already added (don't double-quote).
    cleaned = phrase.lstrip(">").strip()
    return f">{cleaned}" if cleaned else ""
