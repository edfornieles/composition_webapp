from __future__ import annotations

import base64
import json
import os
import re
import uuid
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

COGNITIVE_FRAMEWORKS = """
Ground outputs in these human cognition principles:
1) Predictive processing: thoughts arise to reduce mismatch between expectation and current cues.
2) Interoception/allostasis: body signals (fatigue, arousal, pain, hunger, breath) shape salience.
3) Social inference (theory of mind): infer what others think of the character and adjust behavior.
4) Goal arbitration: near-term actions compete with long-term identity goals.
5) Habit loop: cue -> routine -> reward expectation influences automatic tendencies.
""".strip()


def _safe_list(val: Any, fallback: list[str]) -> list[str]:
    if isinstance(val, list):
        out = []
        for item in val[:8]:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out or fallback
    return fallback


def _extract_sentence(text: str) -> str:
    raw = (text or "").strip().replace("\n", " ")
    if not raw:
        return ""
    for sep in [". ", "! ", "? "]:
        if sep in raw:
            first = raw.split(sep, 1)[0].strip()
            if first:
                return first + sep.strip()
    return raw


def _ensure_pov(prompt: str) -> str:
    p = (prompt or "").strip()
    if not p:
        p = "First-person POV through the character's eyes in their current space, cinematic realism."
    low = p.lower()
    if "first-person" in low or "pov" in low:
        return p
    return "First-person POV through the character's eyes. " + p


def _contextual_prompt(scenario) -> str:
    location = (getattr(scenario, "current_location", "") or "campus housing").strip()
    anchors = []
    sb = getattr(scenario, "scene_bible", {}) or {}
    if isinstance(sb, dict):
        raw = sb.get("anchors") or []
        if isinstance(raw, list):
            anchors = [str(x).strip() for x in raw if str(x).strip()][:5]
    anchor_txt = ", ".join(anchors) if anchors else "hallway shadows, worn furniture, personal desk clutter"
    return (
        f"First-person POV through {scenario.character_name or 'the character'}'s eyes at {location}; "
        f"visible anchors: {anchor_txt}; cinematic realism, coherent continuity, natural lighting."
    )


def _pollinations_url(prompt: str, scenario, salt: int = 0) -> str:
    seed = (uuid.uuid4().int + int(salt)) % 100000 + 1
    return "https://image.pollinations.ai/prompt/" + quote((prompt or "")[:350]) + f"?seed={seed}&width=1536&height=1024&nologo=true"


def _semantic_backup_url(location: str, anchors: list[str], salt: int = 0) -> str:
    raw = [location] + list(anchors or [])
    words: list[str] = []
    for item in raw:
        cleaned = re.sub(r"[^a-z0-9 ,_-]+", " ", (item or "").lower())
        for w in re.split(r"[\s,]+", cleaned):
            if w and w not in {"the", "and", "with", "from", "this", "that", "into"}:
                words.append(w)
    if not words:
        words = ["room", "interior", "firstperson"]
    query = ",".join(words[:5])
    seed = (uuid.uuid4().int + int(salt)) % 100000 + 1
    # loremflickr supports keyword-based photos + lock for deterministic variation.
    return f"https://loremflickr.com/1600/1024/{quote(query)}?lock={seed}"


def generate_advanced_tick(scenario) -> tuple[dict[str, Any], str | None]:
    """
    Returns (payload, error). payload keys:
    thought, subconscious(list), desires(list), visual_prompt, image_url
    """
    key = (getattr(settings, "OPENAI_API_KEY", None) or "").strip()
    if not key:
        fallback_prompt = _contextual_prompt(scenario)
        fallback_img = _pollinations_url(fallback_prompt, scenario, salt=1)
        fallback = {
            "thought": _extract_sentence(scenario.last_thought)
            or "Breathe. Notice. Keep moving.",
            "subconscious": _safe_list(
                scenario.last_subconscious,
                _safe_list(scenario.seed_subconscious, ["ambient tension", "body scanning"]),
            ),
            "desires": _safe_list(
                scenario.last_desires,
                _safe_list(scenario.seed_desires, ["belonging", "competence"]),
            ),
            "visual_prompt": _ensure_pov(scenario.last_visual_prompt or fallback_prompt),
            "image_url": fallback_img,
            "location": scenario.current_location or "bedroom",
            "minutes_advance": 1,
            "sim_minutes": int(scenario.sim_minutes or (8 * 60)),
            "continuity_note": scenario.continuity_note or "Maintain the same room and emotional tone.",
            "cognitive_trace": [
                "predictive mismatch: uncertainty about next social cue",
                "interoception: body tension drives vigilance",
                "goal arbitration: status vs belonging",
            ],
            "scene_anchors": (scenario.scene_bible or {}).get("anchors", []) or ["rumpled bed", "striped window light", "desk clutter"],
            "relationship_state": scenario.relationship_graph or ["girlfriend: close but tense after missed calls"],
            "image_generated": True,
            "backup_image_url": _semantic_backup_url(
                scenario.current_location or "bedroom",
                (scenario.scene_bible or {}).get("anchors", []) if isinstance(scenario.scene_bible, dict) else [],
                salt=1,
            ),
        }
        return fallback, "No OPENAI_API_KEY configured."

    try:
        from openai import OpenAI
    except ImportError as e:
        return {}, f"OpenAI package missing: {e}"

    model = (os.getenv("MONOLOGUE_STREAM_MODEL") or "gpt-4o-mini").strip()
    client = OpenAI(api_key=key, timeout=4.0, max_retries=0)

    seed_desires = _safe_list(scenario.seed_desires, ["belonging", "status", "safety"])
    seed_sub = _safe_list(scenario.seed_subconscious, ["social threat scan", "reward prediction"])

    prompt = f"""
Generate the next INTERNAL state for a persistent character simulation.

Character:
- name: {scenario.character_name or scenario.title}
- archetype: {scenario.archetype}
- brief: {scenario.character_brief}
- world context: {scenario.world_context}
- tone hint: {scenario.tone_hint}

Previous thought: {scenario.last_thought}
Previous visual prompt: {scenario.last_visual_prompt}
Previous desires: {json.dumps(scenario.last_desires or seed_desires)}
Previous subconscious signals: {json.dumps(scenario.last_subconscious or seed_sub)}
Current location: {scenario.current_location or "bedroom"}
Simulated clock (minutes after midnight): {int(scenario.sim_minutes or (8*60))}
Continuity note from last tick: {scenario.continuity_note}
Scene bible (stable environment anchors): {json.dumps(scenario.scene_bible or {}, ensure_ascii=False)}
Relationship graph (people + dynamics): {json.dumps(scenario.relationship_graph or [], ensure_ascii=False)}

Output strict JSON only:
{{
  "thought_sentence": "one concise first-person sentence (max 22 words)",
  "subconscious_signals": ["3-5 short bullet-like phrases"],
  "active_desires": ["3-5 short phrases"],
  "cognitive_trace": ["3-5 short phrases: what shaped the thought this tick"],
  "visual_prompt": "A vivid first-person cinematic image prompt describing exactly what this character sees now; include lighting, place, motion hints.",
  "scene_anchors": ["3-6 stable visual anchors that should persist across nearby ticks"],
  "perceived_anchor": "one anchor currently in view",
  "relationship_state": ["2-5 short lines describing current social dynamics in this moment"],
  "location": "short place label",
  "minutes_advance": 1,
  "continuity_note": "one sentence about what remains consistent with prior scene"
}}

{COGNITIVE_FRAMEWORKS}
""".strip()

    try:
        comp = client.chat.completions.create(
            model=model,
            temperature=0.85,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
        )
        raw = (comp.choices[0].message.content or "").strip()
        data = json.loads(raw) if raw else {}
    except Exception as e:
        fallback = {
            "thought": _extract_sentence(scenario.last_thought)
            or "I need to keep moving and read the room again.",
            "subconscious": _safe_list(
                scenario.last_subconscious,
                _safe_list(scenario.seed_subconscious, ["social threat scan", "fatigue check"]),
            ),
            "desires": _safe_list(
                scenario.last_desires,
                _safe_list(scenario.seed_desires, ["status", "connection"]),
            ),
            "visual_prompt": _ensure_pov(scenario.last_visual_prompt or _contextual_prompt(scenario)),
            "image_url": _pollinations_url(
                _ensure_pov(scenario.last_visual_prompt or "First-person POV bedroom cinematic realistic"),
                scenario,
                salt=2,
            ),
            "location": scenario.current_location or "bedroom",
            "minutes_advance": 1,
            "sim_minutes": int(scenario.sim_minutes or (8 * 60)) + 1,
            "continuity_note": scenario.continuity_note or "Maintain room continuity and mood.",
            "image_generated": True,
            "cognitive_trace": [
                "predictive processing fallback: keep prior model of scene",
                "allostatic pressure: conserve effort under uncertainty",
                "social model: hold current impression assumptions",
            ],
            "scene_anchors": (scenario.scene_bible or {}).get("anchors", []) or ["bedframe edge", "phone on bedside table"],
            "relationship_state": scenario.relationship_graph or ["girlfriend: emotionally important, currently uncertain"],
            "backup_image_url": _semantic_backup_url(
                scenario.current_location or "bedroom",
                (scenario.scene_bible or {}).get("anchors", []) if isinstance(scenario.scene_bible, dict) else [],
                salt=2,
            ),
        }
        return fallback, str(e)

    thought = _extract_sentence(str(data.get("thought_sentence") or ""))
    if not thought:
        thought = "I should keep moving before the moment closes."
    subconscious = _safe_list(data.get("subconscious_signals"), seed_sub)
    desires = _safe_list(data.get("active_desires"), seed_desires)
    cognitive_trace = _safe_list(
        data.get("cognitive_trace"),
        [
            "prediction error focus: update what feels uncertain",
            "interoceptive weighting: body state biases attention",
            "social inference: estimate others' judgments",
        ],
    )
    visual_prompt = str(data.get("visual_prompt") or "").strip()
    if not visual_prompt:
        visual_prompt = (
            f"First-person POV cinematic scene from {scenario.character_name or scenario.title}, "
            "natural light, strong atmosphere, documentary realism."
        )
    visual_prompt = _ensure_pov(visual_prompt)

    location = str(data.get("location") or "").strip() or (scenario.current_location or "bedroom")
    try:
        minutes_advance = int(data.get("minutes_advance") or 1)
    except (TypeError, ValueError):
        minutes_advance = 1
    minutes_advance = max(1, min(15, minutes_advance))
    continuity_note = str(data.get("continuity_note") or "").strip()
    if not continuity_note:
        continuity_note = "Keep scene details coherent with previous frame."

    scene_anchors = _safe_list(
        data.get("scene_anchors"),
        ((scenario.scene_bible or {}).get("anchors") if isinstance(scenario.scene_bible, dict) else []) or [
            "bedframe edge",
            "window light stripe",
            "cluttered desk",
        ],
    )
    relationship_state = _safe_list(
        data.get("relationship_state"),
        scenario.relationship_graph or ["girlfriend: emotionally close, currently distant by text"],
    )
    perceived_anchor = str(data.get("perceived_anchor") or "").strip()
    if not perceived_anchor:
        perceived_anchor = scene_anchors[0] if scene_anchors else "window light"
    if perceived_anchor.lower() not in thought.lower():
        thought = f"{thought.rstrip('.!?')}, eyes on the {perceived_anchor}."

    image_url = scenario.last_image_url or ""
    image_ok = False
    try:
        img = client.images.generate(
            model="gpt-image-1",
            prompt=visual_prompt,
            size="1024x1024",
        )
        if getattr(img, "data", None):
            first = img.data[0]
            direct_url = getattr(first, "url", "") or ""
            if direct_url:
                image_url = direct_url
                image_ok = True
            else:
                b64 = getattr(first, "b64_json", "") or ""
                if b64:
                    blob = base64.b64decode(b64)
                    rel = f"advanced_thoughts/{uuid.uuid4().hex}.png"
                    saved = default_storage.save(rel, ContentFile(blob))
                    image_url = default_storage.url(saved)
                    image_ok = True
    except Exception:
        image_ok = False

    # Couple thought to perception: if we fail to produce a new image, keep previous thought/state.
    if not image_ok and scenario.last_image_url:
        fallback_img = _pollinations_url(visual_prompt, scenario, salt=3)
        return {
            "thought": thought,
            "subconscious": _safe_list(scenario.last_subconscious, subconscious),
            "desires": _safe_list(scenario.last_desires, desires),
            "visual_prompt": _ensure_pov(scenario.last_visual_prompt or visual_prompt),
            "image_url": fallback_img,
            "location": scenario.current_location or location,
            "minutes_advance": minutes_advance,
            "sim_minutes": int(scenario.sim_minutes or (8 * 60)) + minutes_advance,
            "continuity_note": scenario.continuity_note or continuity_note,
            "image_generated": True,
            "cognitive_trace": _safe_list(scenario.last_subconscious, cognitive_trace),
            "scene_anchors": scene_anchors or ["bedroom window light", "sweaty shirt on chair"],
            "relationship_state": relationship_state or ["girlfriend: close, trying to reconnect"],
            "perceived_anchor": perceived_anchor,
            "backup_image_url": _semantic_backup_url(
                location,
                scene_anchors or ["bedroom window light", "sweaty shirt on chair"],
                salt=3,
            ),
        }, "Primary image generation timed out; used fallback POV frame."
    if not image_ok and not scenario.last_image_url:
        fallback_img = _pollinations_url(visual_prompt, scenario, salt=4)
        image_url = fallback_img
        image_ok = True

    payload = {
        "thought": thought,
        "subconscious": subconscious,
        "desires": desires,
        "visual_prompt": visual_prompt,
        "image_url": image_url,
        "location": location,
        "minutes_advance": minutes_advance,
        "sim_minutes": int(scenario.sim_minutes or (8 * 60)) + minutes_advance,
        "continuity_note": continuity_note,
        "image_generated": bool(image_ok),
        "cognitive_trace": cognitive_trace,
        "scene_anchors": scene_anchors,
        "relationship_state": relationship_state,
        "perceived_anchor": perceived_anchor,
        "backup_image_url": _semantic_backup_url(location, scene_anchors, salt=4),
    }
    return payload, None

