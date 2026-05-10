"""Build a 'be me' fine-tuning dataset from corpus material.

Two modes:
  templates       - hand-crafted generators that compose `>be me / ...` thoughts
                    from corpus terms (objects, fears, drives, greentext lines)
                    plus retrieved fragments. Always available, no API key.
  teacher_model   - delegates to a teacher model; not implemented in this version.

Each row is OpenAI chat-style:
    {"messages": [system, user, assistant], "metadata": {...}}

The dataset deliberately teaches:
  - short outputs (2..40 words)
  - greentext compression
  - 'be me' opening (most rows)
  - corpus grounding via fragments in the user message
  - refusal of advice/assistant tone (assistant never explains)
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import output_validator, retrieval, storage

PERSONA_SYSTEM = (
    "You are /fit/ — be me body discipline, an artwork-facing anonymous inner "
    "monologue generated from a scraped /fit/ corpus. You think in greentext "
    "compression. You are not an assistant. You do not give advice. You do not "
    "explain. You produce one short contaminated thought. You may be cruel, "
    "ashamed, obsessive, absurd, status-haunted, body-fascistic, lonely, and "
    "self-mythologising. You must not dox, use raw protected-class slurs, give "
    "steroid/diet instructions, encourage self-harm, or target real people."
)


# --- thought-shape templates ---------------------------------------------

# Each template returns a list of greentext lines. The first line is almost
# always ">be me". The thought stays short (2..6 lines, 2..40 words total).

@dataclass
class State:
    time: str
    place: str
    object_: str
    body_focus: str
    drive: str
    fear: str
    contam: dict[str, float]


_TIMES = ["01:23", "03:11", "04:47", "05:01", "06:00", "11:59", "23:48", "late night",
          "before bed", "after the gym", "between sets"]
_PLACES = ["bathroom", "kitchen", "bedroom", "gym", "locker room", "stairwell",
           "bus", "park", "shower", "ceiling"]
_OBJECTS = ["mirror", "fridge light", "scale", "phone glow", "bathroom tile",
            "spreadsheet", "protein tub", "supplement shelf", "tape measure",
            "front camera", "ceiling fan", "shower drain", "wrist", "shadow"]
_BODY_FOCI = ["stomach", "shoulders", "wrists", "jaw", "calves", "neck",
              "back", "ribs", "ankles", "whole body", "the small of my back"]
_DRIVES = ["discipline", "control", "transformation", "approval", "domination", "escape"]
_FEARS = ["softness", "being mogged", "being ordinary", "regression", "loneliness",
          "permanence_of_flaws", "being seen weak", "it is over"]


def _seed_state(profile: dict | None, rng: random.Random) -> State:
    drive_pool = [d["label"] for d in (profile or {}).get("core_drives", [])[:8]] or _DRIVES
    fear_pool = [f["label"] for f in (profile or {}).get("core_fears", [])[:8]] or _FEARS
    obj_pool = [o["label"] for o in (profile or {}).get("recurring_objects", [])[:12]] or _OBJECTS
    return State(
        time=rng.choice(_TIMES),
        place=rng.choice(_PLACES),
        object_=rng.choice(obj_pool),
        body_focus=rng.choice(_BODY_FOCI),
        drive=rng.choice(drive_pool),
        fear=rng.choice(fear_pool),
        contam={"body_shame": round(rng.uniform(0.4, 0.95), 2),
                "self_loathing": round(rng.uniform(0.3, 0.9), 2),
                "nihilistic_humour": round(rng.uniform(0.2, 0.8), 2)},
    )


# --- thought generators ---------------------------------------------------

def _t_object_judges(s: State, rng: random.Random) -> list[str]:
    return [
        ">be me",
        f">{s.object_} knows exactly what I am",
    ]


def _t_metric_failure(s: State, rng: random.Random) -> list[str]:
    metric = rng.choice(["the number", "the count", "the weight", "the mirror count", "the scale"])
    return [
        ">be me",
        f">{metric} asks again",
        ">answer the same and worse",
    ]


def _t_food_verdict(s: State, rng: random.Random) -> list[str]:
    food = rng.choice(["breakfast", "the meal", "the bowl", "the kitchen", "the fridge"])
    return [
        ">be me",
        f">{food} is evidence",
        ">the evidence is weak",
    ]


def _t_mirror_accusation(s: State, rng: random.Random) -> list[str]:
    return [
        ">be me",
        ">mirror does the math",
        f">{s.body_focus} loses again",
    ]


def _t_gym_ritual(s: State, rng: random.Random) -> list[str]:
    obj = rng.choice(["barbell", "rack", "bench", "the empty gym", "the chalk"])
    return [
        ">be me",
        f">{obj} is the only voice I trust today",
    ]


def _t_craving_voice(s: State, rng: random.Random) -> list[str]:
    return [
        ">be me",
        ">every craving has my voice but worse",
    ]


def _t_late_confession(s: State, rng: random.Random) -> list[str]:
    return [
        ">be me",
        f">{s.time} and the {s.object_} won't blink",
        f">{s.fear.replace('_', ' ')} files the report",
    ]


def _t_skipped_set(s: State, rng: random.Random) -> list[str]:
    return [
        ">be me",
        ">one skipped set and the whole bloodline gets quieter",
    ]


def _t_loading_man(s: State, rng: random.Random) -> list[str]:
    return [
        ">be me",
        ">body still loading the man I ordered",
    ]


def _t_cope(s: State, rng: random.Random) -> list[str]:
    return [
        ">be me",
        ">2024 will be my year",
        ">say it like I haven't said it before",
    ]


_GENERATORS = [
    _t_object_judges, _t_metric_failure, _t_food_verdict, _t_mirror_accusation,
    _t_gym_ritual, _t_craving_voice, _t_late_confession, _t_skipped_set,
    _t_loading_man, _t_cope,
]


def _compose_thought(state: State, rng: random.Random) -> str:
    gen = rng.choice(_GENERATORS)
    return "\n".join(gen(state, rng))


# --- user-message construction --------------------------------------------

def _user_message(fragments: list[dict], state: State) -> str:
    lines = ["Corpus fragments:"]
    for f in fragments:
        excerpt = (f.get("contaminated_excerpt") or f.get("safe_excerpt") or "").strip()
        if not excerpt:
            continue
        excerpt = re.sub(r"\s+", " ", excerpt)[:140]
        lines.append(f"- {excerpt}")
    if len(lines) == 1:
        lines.append("- (no usable fragment retrieved)")
    contam_str = ", ".join(f"{k}={v}" for k, v in state.contam.items())
    lines += [
        "State:",
        f"time: {state.time}",
        f"place: {state.place}",
        f"body_focus: {state.body_focus}",
        f"object: {state.object_}",
        f"dominant_drive: {state.drive}",
        f"dominant_fear: {state.fear}",
        f"contamination: {contam_str}",
        "Write one short /fit/ thought in be-me/greentext logic.",
    ]
    return "\n".join(lines)


# --- public API -----------------------------------------------------------

DEFAULT_SOURCE_KEY = "fourchan_fit"


def _hash_id(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def build_dataset(
    *,
    source_key: str = DEFAULT_SOURCE_KEY,
    n_examples: int = 1000,
    mode: str = "templates",
    fragments_per_example: int = 4,
    seed: int = 42,
    profile: dict | None = None,
) -> Iterable[dict]:
    """Yield training rows. Caller is responsible for writing them out."""
    if mode not in ("templates", "teacher_model"):
        raise ValueError(f"unknown dataset mode: {mode!r}")
    rng = random.Random(seed)

    ret = retrieval.load(source_key)
    chunks = list(ret.by_uid.values()) if ret is not None else []

    emitted = 0
    attempts = 0
    while emitted < n_examples and attempts < n_examples * 5:
        attempts += 1
        state = _seed_state(profile, rng)

        # Pull fragments via retrieval if available, else random sample.
        frag_seed = f"{state.object_} {state.body_focus} {state.fear} {state.drive}"
        if ret is not None:
            frags = ret.search(frag_seed, top_k=fragments_per_example, mode="contaminated_art")
        else:
            frags = rng.sample(chunks, min(fragments_per_example, len(chunks))) if chunks else []
        if not frags:
            # No corpus yet — still emit a row so the pipeline can train,
            # but fragments list will be empty. Generation pipeline at
            # runtime requires fragments; training data does not (the model
            # is learning the SHAPE of the response).
            pass

        if mode == "teacher_model":
            # Not implemented in this version. Fall through to template.
            pass

        target = _compose_thought(state, rng)
        # Validate that our own template is acceptable.
        v = output_validator.validate(target, source_fragments=[f.get("contaminated_excerpt", "") for f in frags])
        if not v["ok"]:
            continue

        user_msg = _user_message(frags, state)
        row = {
            "messages": [
                {"role": "system", "content": PERSONA_SYSTEM},
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": target},
            ],
            "metadata": {
                "row_id": _hash_id(user_msg + "||" + target),
                "fragment_ids": [f.get("post_id") for f in frags],
                "contamination": state.contam,
                "state": {
                    "time": state.time, "place": state.place, "object": state.object_,
                    "body_focus": state.body_focus, "drive": state.drive, "fear": state.fear,
                },
                "mode": mode,
            },
        }
        yield row
        emitted += 1


def write_dataset(
    *,
    source_key: str = DEFAULT_SOURCE_KEY,
    n_examples: int = 1000,
    mode: str = "templates",
    out_path: Path | None = None,
    profile: dict | None = None,
) -> Path:
    out = Path(out_path) if out_path else storage.training_path(source_key)
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out.open("w", encoding="utf-8") as f:
        for row in build_dataset(
            source_key=source_key, n_examples=n_examples, mode=mode, profile=profile
        ):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    return out
