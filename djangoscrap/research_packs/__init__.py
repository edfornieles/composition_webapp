"""Curated URL packs and notes for the monologue “AI research” merge action."""

from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent


def load_pack(archetype: str | None) -> dict:
    key = (archetype or "").strip().lower()
    name = "jock.json" if key == "jock" else "default.json"
    path = _DIR / name
    if not path.exists():
        path = _DIR / "default.json"
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def seed_payload() -> dict:
    """Full default persona fields from the jock pack (used by data migration)."""
    with open(_DIR / "jock.json", encoding="utf-8") as fh:
        return json.load(fh)
