"""Read-only feedback signal loader.

Two existing sources:
  - var/fit_thought_feedback.jsonl     (keep/skip ratings from /wall/)
  - djangoscrap.models.FavoriteThought (operator-curated saves)

Pass 1 deliberately does not consume feedback in the runtime — this module
only loads, parses, and exposes counts. Pass 2 will wire it into eval (as
positive examples) and training (as preference seeds).

Robust to missing files / missing Django setup so it can be imported from
smoke tests without bringing the whole app online.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


# --- jsonl thought feedback (from /wall/) ---------------------------------

@dataclass
class ThoughtFeedbackEntry:
    rating: str          # "keep" | "skip"
    thought: str
    grounding_status: str = ""
    ts: str = ""


def _default_jsonl_path() -> Path:
    """Resolve var/fit_thought_feedback.jsonl from Django settings.BASE_DIR
    when available, else from the project layout's expected location.
    """
    try:
        from django.conf import settings  # type: ignore
        base = Path(getattr(settings, "BASE_DIR", ""))
        if base and base.exists():
            return base / "var" / "fit_thought_feedback.jsonl"
    except Exception:
        pass
    # Fallback: relative to this file (../../var)
    here = Path(__file__).resolve()
    return here.parent.parent.parent / "var" / "fit_thought_feedback.jsonl"


def iter_thought_feedback(path: Optional[Path] = None) -> Iterator[ThoughtFeedbackEntry]:
    p = path or _default_jsonl_path()
    if not p.exists():
        return
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                yield ThoughtFeedbackEntry(
                    rating=str(rec.get("rating") or ""),
                    thought=str(rec.get("thought") or ""),
                    grounding_status=str(rec.get("grounding_status") or ""),
                    ts=str(rec.get("ts") or ""),
                )
    except OSError:
        return


def thought_feedback_counts(path: Optional[Path] = None) -> dict:
    out = {"keep": 0, "skip": 0, "other": 0, "total": 0}
    for e in iter_thought_feedback(path):
        out["total"] += 1
        if e.rating in ("keep", "skip"):
            out[e.rating] += 1
        else:
            out["other"] += 1
    return out


def kept_thoughts(path: Optional[Path] = None, *, limit: Optional[int] = None) -> list[str]:
    """Return the text of all 'keep' entries, newest-first up to limit."""
    out: list[str] = []
    for e in iter_thought_feedback(path):
        if e.rating == "keep" and e.thought:
            out.append(e.thought)
    out.reverse()
    if limit is not None:
        out = out[:limit]
    return out


# --- favourites (DB-backed) -----------------------------------------------

def favorite_thought_texts(persona_slug: Optional[str] = None, *, limit: int = 200) -> list[str]:
    """Read FavoriteThought.text for a persona. Returns [] if Django is not
    configured (e.g. inside a smoke test that skips DB setup)."""
    try:
        from ..models import FavoriteThought  # type: ignore
    except Exception:
        return []
    try:
        qs = FavoriteThought.objects.all()
        if persona_slug:
            qs = qs.filter(persona__slug=persona_slug)
        return [f.text for f in qs.order_by("-created_at")[:limit] if (f.text or "").strip()]
    except Exception:
        return []


def feedback_summary(*, persona_slug: Optional[str] = None) -> dict:
    """One-shot summary used by eval / docs. Doesn't load full bodies."""
    counts = thought_feedback_counts()
    favs = len(favorite_thought_texts(persona_slug=persona_slug, limit=10_000))
    return {
        "thought_feedback_counts": counts,
        "favorites_count": favs,
        "jsonl_path": str(_default_jsonl_path()),
    }
