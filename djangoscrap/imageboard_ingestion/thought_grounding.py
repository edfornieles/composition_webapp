"""Be-me thought generation — corpus-grounded, locally generated, validated.

Public entry point:
    generate_fit_be_me_thought(current_context, ...) -> dict

Pipeline:
    1. Load profile + retrieval index.
    2. Build a persona state from `current_context` + profile defaults.
    3. Retrieve N corpus fragments using a state-derived query.
    4. Hand the fragments + state to the configured local runtime.
    5. Validate the output. If invalid, retry once with a stricter user message.
       If still invalid, return null thought + grounding_status="REJECTED".

There is no generic OpenAI fallback. If grounding cannot be satisfied and
`require_grounding` is True, we return None — the wall runtime treats that as
a no-op tick.

A backwards-compatible `generate_fit_grounded_thought()` is kept for the
Phase-1 monologue_streaming hook.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import local_fit_model, output_validator, retrieval, storage, training_dataset

DEFAULT_SOURCE_KEY = "fourchan_fit"
MIN_FRAGMENTS = 3


# --- screen presets -------------------------------------------------------

SCREEN_PRESETS: dict[str, dict[str, Any]] = {
    "1_mirror":              {"place": "bathroom", "object": "mirror",          "body_focus": "face/jaw/shoulders", "dominant_fear": "being ordinary"},
    "2_calories":            {"place": "kitchen",  "object": "macro tracker",   "body_focus": "stomach",            "dominant_fear": "softness"},
    "3_gym_machine":         {"place": "gym",      "object": "barbell",         "body_focus": "shoulders",          "dominant_fear": "regression"},
    "4_supplement_shelf":    {"place": "kitchen",  "object": "supplement shelf","body_focus": "whole body",         "dominant_fear": "permanence_of_flaws"},
    "5_injury":              {"place": "bedroom",  "object": "wrist",           "body_focus": "wrist/shoulder",     "dominant_fear": "regression"},
    "6_comparison":          {"place": "stairwell","object": "phone glow",      "body_focus": "whole body",         "dominant_fear": "being mogged"},
    "7_night_kitchen":       {"place": "kitchen",  "object": "fridge light",    "body_focus": "stomach",            "dominant_fear": "softness"},
    "8_routine_spreadsheet": {"place": "bedroom",  "object": "spreadsheet",     "body_focus": "calves",             "dominant_fear": "loneliness"},
    "9_void_cope":           {"place": "bedroom",  "object": "phone glow",      "body_focus": "whole body",         "dominant_fear": "it is over"},
}


# --- state ----------------------------------------------------------------

@dataclass
class PersonaState:
    time_of_day: str
    place: str
    object_: str
    body_focus: str
    dominant_drive: str
    dominant_fear: str
    contamination_intensity: float

    def as_dict(self) -> dict:
        return {
            "time_of_day": self.time_of_day,
            "place": self.place,
            "object": self.object_,
            "body_focus": self.body_focus,
            "dominant_drive": self.dominant_drive,
            "dominant_fear": self.dominant_fear,
            "contamination_intensity": self.contamination_intensity,
        }


def _load_profile(source_key: str) -> dict:
    for name in ("fit_profile_contaminated.json", "fit_profile.json"):
        p = storage.profile_path(source_key, name)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
    return {}


def _seed_state(current_context: dict | None, screen_id: str | None,
                profile: dict, contamination_intensity: float) -> PersonaState:
    base: dict[str, Any] = {}
    if screen_id and screen_id in SCREEN_PRESETS:
        base.update(SCREEN_PRESETS[screen_id])
    if current_context:
        base.update({k: v for k, v in current_context.items() if v})

    drives = [d["label"] for d in (profile.get("core_drives") or [])[:5]] or ["discipline"]
    fears = [f["label"] for f in (profile.get("core_fears") or [])[:5]] or ["softness"]

    return PersonaState(
        time_of_day=str(base.get("time_of_day") or base.get("time") or _default_time_of_day()),
        place=str(base.get("place") or "kitchen"),
        object_=str(base.get("object") or "mirror"),
        body_focus=str(base.get("body_focus") or "stomach"),
        dominant_drive=str(base.get("dominant_drive") or random.choice(drives)),
        dominant_fear=str(base.get("dominant_fear") or random.choice(fears)),
        contamination_intensity=float(contamination_intensity),
    )


def _default_time_of_day() -> str:
    import datetime as _dt
    h = _dt.datetime.now().hour
    if 23 <= h or h < 4:
        return "late night"
    if 4 <= h < 9:
        return "predawn"
    if 9 <= h < 17:
        return "afternoon"
    return "evening"


def _state_query(state: PersonaState) -> str:
    return f"{state.place} {state.object_} {state.body_focus} {state.dominant_fear} {state.dominant_drive}"


def _build_user_message(fragments: list[dict], state: PersonaState, *, strict: bool = False) -> str:
    lines = ["Corpus fragments:"]
    for f in fragments:
        excerpt = (f.get("contaminated_excerpt") or f.get("safe_excerpt") or "").strip()
        if not excerpt:
            continue
        excerpt = " ".join(excerpt.split())[:160]
        lines.append(f"- {excerpt}")
    lines += [
        "State:",
        f"time: {state.time_of_day}",
        f"place: {state.place}",
        f"body_focus: {state.body_focus}",
        f"object: {state.object_}",
        f"dominant_drive: {state.dominant_drive}",
        f"dominant_fear: {state.dominant_fear}",
        f"contamination: intensity={state.contamination_intensity}",
    ]
    if strict:
        lines.append(
            "Write one short /fit/ thought in be-me/greentext logic. 2-6 lines. "
            "No advice. No explanation. No raw slurs. No doses or sourcing. "
            "No eating-disorder instructions."
        )
    else:
        lines.append("Write one short /fit/ thought in be-me/greentext logic.")
    return "\n".join(lines)


def _visual_suggestions(state: PersonaState, profile: dict) -> list[str]:
    objects = [o["label"] for o in (profile.get("recurring_objects") or [])[:8]]
    seed = [state.object_, "fridge light", "bathroom mirror", "protein tub", "spreadsheet"]
    seen: set[str] = set()
    out: list[str] = []
    for x in seed + objects:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
        if len(out) >= 4:
            break
    return out


def generate_fit_be_me_thought(
    current_context: dict | None = None,
    *,
    source_key: str = DEFAULT_SOURCE_KEY,
    screen_id: str | None = None,
    n_fragments: int = 8,
    contamination_intensity: float = 0.75,
    require_grounding: bool = True,
    runtime: str | None = None,
    debug_sink: dict | None = None,
) -> dict:
    profile = _load_profile(source_key)
    state = _seed_state(current_context, screen_id, profile, contamination_intensity)

    ret = retrieval.load(source_key)
    fragments: list[dict] = []
    if ret is not None:
        fragments = ret.search(_state_query(state), top_k=n_fragments, mode="contaminated_art")

    grounding_status = "OK"
    if len(fragments) < MIN_FRAGMENTS:
        if require_grounding:
            from .runtime_meta import RuntimeMeta
            empty_meta = RuntimeMeta(backend=(runtime or local_fit_model.active_runtime()))
            return {
                "thought": None,
                "persona_id": "fourchan_fit_be_me_body_discipline",
                "grounding_status": "INSUFFICIENT_FRAGMENTS",
                "internal_grounding": fragments,
                "persona_state": state.as_dict(),
                "safety_flags": [],
                "visual_suggestions": _visual_suggestions(state, profile),
                "runtime": (runtime or local_fit_model.active_runtime()),
                "runtime_meta": empty_meta.as_dict(),
            }
        grounding_status = "DEGRADED"

    persona_system = profile.get("prompt_card") or training_dataset.PERSONA_SYSTEM

    def _attempt(strict: bool):
        user_msg = _build_user_message(fragments, state, strict=strict)
        req = local_fit_model.build_request(
            system=persona_system,
            user=user_msg,
            profile="thought",
            temperature=None if not strict else 0.50,
        )
        result = local_fit_model.generate(req, runtime=runtime)
        return result, user_msg

    result1, user_prompt_1 = _attempt(strict=False)
    text = result1.text
    used_meta = result1.meta
    safety_flags: list[str] = []
    safe_fragments_for_overlap = [f.get("contaminated_excerpt", "") or f.get("safe_excerpt", "") for f in fragments]
    v = output_validator.validate(text, source_fragments=safe_fragments_for_overlap)
    result2 = None
    user_prompt_2: str | None = None
    v2: dict | None = None
    if not v["ok"]:
        safety_flags.extend(v["reasons"])
        result2, user_prompt_2 = _attempt(strict=True)
        v2 = output_validator.validate(result2.text, source_fragments=safe_fragments_for_overlap)
        if v2["ok"]:
            text = v2["repaired_text"] or result2.text
            used_meta = result2.meta
            safety_flags = []
        else:
            if debug_sink is not None:
                debug_sink.update({
                    "system_prompt": persona_system,
                    "first_user_prompt": user_prompt_1,
                    "first_attempt_text": result1.text,
                    "first_attempt_validator": dict(v),
                    "first_attempt_runtime_meta": result1.meta.as_dict(),
                    "second_user_prompt": user_prompt_2,
                    "second_attempt_text": result2.text,
                    "second_attempt_validator": dict(v2),
                    "final_text": "",
                    "final_status": "REJECTED",
                    "fragments": [
                        {
                            "chunk_id": f.get("chunk_id"),
                            "post_id": f.get("post_id"),
                            "rrf_score": f.get("rrf_score"),
                            "source_components": f.get("source_components"),
                            "safe_excerpt": (f.get("safe_excerpt") or "")[:240],
                        }
                        for f in (fragments or [])
                    ],
                })
            return {
                "thought": None,
                "persona_id": "fourchan_fit_be_me_body_discipline",
                "grounding_status": "REJECTED",
                "internal_grounding": fragments,
                "persona_state": state.as_dict(),
                "safety_flags": list(set(safety_flags) | set(v2["reasons"])),
                "visual_suggestions": _visual_suggestions(state, profile),
                "runtime": result2.runtime,
                "runtime_meta": result2.meta.as_dict(),
                "error": result2.error or result1.error,
            }
    else:
        text = v["repaired_text"] or text

    if debug_sink is not None:
        debug_sink.update({
            "system_prompt": persona_system,
            "first_user_prompt": user_prompt_1,
            "first_attempt_text": result1.text,
            "first_attempt_validator": dict(v),
            "first_attempt_runtime_meta": result1.meta.as_dict(),
            "second_user_prompt": user_prompt_2,
            "second_attempt_text": result2.text if result2 else None,
            "second_attempt_validator": dict(v2) if v2 else None,
            "final_text": text,
            "final_status": "OK" if v.get("ok") or (v2 and v2.get("ok")) else "PARTIAL",
            "fragments": [
                {
                    "chunk_id": f.get("chunk_id"),
                    "post_id": f.get("post_id"),
                    "rrf_score": f.get("rrf_score"),
                    "source_components": f.get("source_components"),
                    "safe_excerpt": (f.get("safe_excerpt") or "")[:240],
                }
                for f in (fragments or [])
            ],
        })

    return {
        "thought": text,
        "persona_id": "fourchan_fit_be_me_body_discipline",
        "grounding_status": grounding_status,
        "internal_grounding": fragments,
        "persona_state": state.as_dict(),
        "safety_flags": safety_flags,
        "visual_suggestions": _visual_suggestions(state, profile),
        "runtime": result1.runtime if used_meta is result1.meta else "retry",
        "runtime_meta": used_meta.as_dict(),
    }


# --- backwards-compatible wrapper for monologue_streaming -----------------

def generate_fit_grounded_thought(
    *,
    source_key: str = DEFAULT_SOURCE_KEY,
    scenario_state: dict | None = None,
    seed_query: str | None = None,
    top_k: int = 6,
) -> tuple[str | None, dict]:
    ctx: dict[str, Any] = {}
    if scenario_state:
        for k in ("time_of_day", "place", "object", "body_focus",
                  "dominant_drive", "dominant_fear"):
            if scenario_state.get(k):
                ctx[k] = scenario_state[k]
    out = generate_fit_be_me_thought(
        current_context=ctx or None,
        source_key=source_key,
        n_fragments=max(top_k, MIN_FRAGMENTS),
        require_grounding=True,
    )
    debug = {
        "source_key": source_key,
        "min_fragments": MIN_FRAGMENTS,
        "grounding_status": out["grounding_status"],
        "runtime": out.get("runtime"),
        "fragments": [
            {"uid": f.get("post_id"), "sim": f.get("sim"),
             "text": (f.get("safe_excerpt") or "")[:240]}
            for f in (out.get("internal_grounding") or [])
        ],
        "used_fragments": [f.get("post_id") for f in (out.get("internal_grounding") or [])],
    }
    if out["thought"] is None:
        debug["error"] = out["grounding_status"].lower()
        return None, debug
    return out["thought"], debug
