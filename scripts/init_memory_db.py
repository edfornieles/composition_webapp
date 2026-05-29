#!/usr/bin/env python3
"""Initialize the chan_anon character memory database (SQLite + JSON).

Per the chan_anon framing memory: continuity belongs OUTSIDE the weights —
in retrieval, character files, and an episodic/semantic/relationship/
symbolic memory store. This script lays down the schema.

The memory model:

  characters/{character_id}.core.json
      Stable identity: core_wound, core_desire, public_mask, board, age,
      origin notes, beliefs that don't change.

  characters/{character_id}.state.json
      Mutable emotional/physical state. Updated each generation via
      state_delta from the director-layer output.

  memory/episodic.sqlite
      Concrete events. Saved selectively (importance threshold, recurrence,
      belief-change, unresolved tension, symbolic richness).

  memory/relationships.sqlite
      Recurring people/figures. Belief about them, last interaction,
      unresolved actions, emotional charge.

  memory/symbolic.sqlite
      Recurring images, objects, atmospheres, dream material — clustered
      by theme (e.g. body_shame: mirror, fluorescent light, school pool,
      father's stomach, protein dust).

  memory/semantic.sqlite
      Beliefs derived from event accumulation (e.g. "she would lose interest
      if she saw him clearly").

The runtime context builder reads from these stores and assembles a compact
context packet (NOT the whole DB) for each generation.

Usage:
    python scripts/init_memory_db.py --root /Volumes/Oom/chan_corpus/runtime
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

EPISODIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodic (
    memory_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id    TEXT NOT NULL,
    event           TEXT NOT NULL,
    emotion         TEXT,
    importance      REAL DEFAULT 0.5,            -- 0..1
    created_at      INTEGER NOT NULL,            -- unix
    last_recalled_at INTEGER,
    recall_count    INTEGER DEFAULT 0,
    scene_board     TEXT,
    scene_summary   TEXT,
    related_persons TEXT,                         -- JSON list of person_ids
    symbol_cluster  TEXT,                         -- foreign-key-ish to symbolic.cluster
    archive_post_ids TEXT,                        -- JSON list "board:thread:post"
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_ep_char  ON episodic(character_id);
CREATE INDEX IF NOT EXISTS idx_ep_imp   ON episodic(importance);
CREATE INDEX IF NOT EXISTS idx_ep_sym   ON episodic(symbol_cluster);
"""

RELATIONSHIPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS relationships (
    person_id           TEXT NOT NULL,
    character_id        TEXT NOT NULL,
    display_name        TEXT,
    relationship_type   TEXT,         -- desired/avoided/admired/contemptuous/etc.
    belief_about        TEXT,
    last_interaction    TEXT,
    last_interaction_at INTEGER,
    felt_emotion        TEXT,
    unresolved_action   TEXT,
    charge              REAL DEFAULT 0.0,    -- emotional intensity 0..1
    encounter_count     INTEGER DEFAULT 0,
    PRIMARY KEY (person_id, character_id)
);
CREATE INDEX IF NOT EXISTS idx_rel_char  ON relationships(character_id);
CREATE INDEX IF NOT EXISTS idx_rel_charge ON relationships(charge);
"""

SYMBOLIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbolic (
    cluster_id      TEXT NOT NULL,
    character_id    TEXT NOT NULL,
    symbols         TEXT NOT NULL,    -- JSON list of strings
    associated_emotions TEXT,         -- JSON list
    linked_memory_ids   TEXT,         -- JSON list of episodic.memory_id
    last_surfaced_at    INTEGER,
    surface_count       INTEGER DEFAULT 0,
    PRIMARY KEY (cluster_id, character_id)
);
CREATE INDEX IF NOT EXISTS idx_sym_char ON symbolic(character_id);
"""

SEMANTIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic (
    belief_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id    TEXT NOT NULL,
    belief          TEXT NOT NULL,
    formed_at       INTEGER NOT NULL,
    last_reinforced_at INTEGER,
    confidence      REAL DEFAULT 0.5,
    source_memory_ids TEXT,           -- JSON list of episodic.memory_id
    contradicting_memory_ids TEXT
);
CREATE INDEX IF NOT EXISTS idx_sem_char  ON semantic(character_id);
"""

WORKING_SCHEMA = """
CREATE TABLE IF NOT EXISTS working (
    -- transient — last few turns; truncated/aged out
    turn_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id    TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    role            TEXT NOT NULL,    -- scene/perception/thought/action/state/output
    payload         TEXT NOT NULL     -- JSON
);
CREATE INDEX IF NOT EXISTS idx_work_char ON working(character_id, created_at);
"""

CHARACTER_TEMPLATE_CORE = {
    "schema_version": 1,
    "character_id": "fit_anon",
    "board": "fit",
    "core_identity": {
        "core_wound": "feels physically inadequate and socially invisible",
        "core_desire": "to become undeniable",
        "public_mask": "discipline, contempt, ironic detachment",
        "age_bracket": "early 20s",
        "voice_tags": ["compressed", "ironic", "self-deprecating", "physically grounded"],
    },
    "stable_beliefs": [
        "softness is failure",
        "attention must be earned through visible change",
    ],
    "origin_notes": (
        "Anonymity is the substrate. This character is an OPTIONAL runtime "
        "overlay — when no character context is supplied, generations are "
        "anonymous-by-default."
    ),
}

CHARACTER_TEMPLATE_STATE = {
    "schema_version": 1,
    "character_id": "fit_anon",
    "state": {
        "self_disgust": 0.5,
        "discipline_drive": 0.5,
        "social_hunger": 0.5,
        "energy": 0.5,
        "tenderness": 0.5,
    },
    "recent_events": [],
    "active_symbols": [],
    "last_updated": 0,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="Runtime root (memory/, characters/ go under here).")
    ap.add_argument("--seed-character", default="fit_anon",
                    help="Write a starter character_id.core.json + .state.json template (or 'none').")
    args = ap.parse_args()

    root = Path(args.root)
    mem_dir = root / "memory"
    chars_dir = root / "characters"
    mem_dir.mkdir(parents=True, exist_ok=True)
    chars_dir.mkdir(parents=True, exist_ok=True)

    schemas = [
        ("episodic.sqlite", EPISODIC_SCHEMA),
        ("relationships.sqlite", RELATIONSHIPS_SCHEMA),
        ("symbolic.sqlite", SYMBOLIC_SCHEMA),
        ("semantic.sqlite", SEMANTIC_SCHEMA),
        ("working.sqlite", WORKING_SCHEMA),
    ]
    for fname, schema in schemas:
        db = mem_dir / fname
        conn = sqlite3.connect(db)
        conn.executescript(schema)
        conn.commit()
        conn.close()
        print(f"  {fname}", flush=True)

    if args.seed_character and args.seed_character.lower() != "none":
        cid = args.seed_character
        core_path = chars_dir / f"{cid}.core.json"
        state_path = chars_dir / f"{cid}.state.json"
        if not core_path.exists():
            tpl = dict(CHARACTER_TEMPLATE_CORE); tpl["character_id"] = cid
            core_path.write_text(json.dumps(tpl, indent=2))
            print(f"  seeded {core_path.name}", flush=True)
        if not state_path.exists():
            tpl = dict(CHARACTER_TEMPLATE_STATE); tpl["character_id"] = cid
            state_path.write_text(json.dumps(tpl, indent=2))
            print(f"  seeded {state_path.name}", flush=True)

    print(f"\nMemory store initialized at {root}", flush=True)


if __name__ == "__main__":
    main()
