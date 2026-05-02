from __future__ import annotations

import re
from typing import Any

from django.db.models import Q

from .models import Composition


def _thoughtscape_words(raw: str) -> list[str]:
    txt = (raw or "").lower().strip()
    if not txt:
        return []
    words = re.findall(r"[a-z0-9_#-]+", txt)
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        w = w.strip().lstrip("#")
        if len(w) < 2:
            continue
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _thoughtscape_intensity_bucket(thought_text: str, scenario_state: str, scene_context: str = "") -> str:
    st = (scenario_state or "").strip().lower()
    if st in {"asleep", "sleep", "calm"}:
        return "low"
    if st in {"distracted", "intense", "agitated", "urgent"}:
        return "high"
    words = set(_thoughtscape_words(f"{thought_text} {scene_context}"))
    high_markers = {"panic", "fight", "sex", "desire", "obsession", "power", "rush", "danger", "rage", "fear"}
    low_markers = {"sleep", "dream", "calm", "rest", "soft", "quiet", "still"}
    hi = len(words & high_markers)
    lo = len(words & low_markers)
    if hi >= max(1, lo + 1):
        return "high"
    if lo > hi:
        return "low"
    return "mid"


def _thoughtscape_cadence_for_bucket(bucket: str, tick: int) -> tuple[int, int]:
    t = max(0, int(tick or 0))
    if bucket == "low":
        cadence = 24 + (t % 7)  # 24..30
        replacements = 1
    elif bucket == "high":
        cadence = 4 + (t % 3)  # 4..6
        replacements = 2 + (t % 2)  # 2..3
    else:
        cadence = 10 + (t % 5)  # 10..14
        replacements = 1 + (t % 2)  # 1..2
    cadence = max(3, min(30, cadence))
    replacements = max(1, min(3, replacements))
    return cadence, replacements


def _thoughtscape_layer_for_index(rows: int, cols: int, idx: int) -> str:
    r = idx // cols
    if rows <= 2:
        return "conscious" if r == 0 else "subconscious"
    top_cut = max(1, rows // 3)
    bottom_cut = rows - max(1, rows // 3)
    if r < top_cut:
        return "conscious"
    if r >= bottom_cut:
        return "subconscious"
    return "transitional"


def _thoughtscape_weight(layer: str) -> float:
    if layer == "conscious":
        return 1.0
    if layer == "transitional":
        return 0.65
    if layer == "subconscious":
        return 0.4
    return 0.0


def parse_screen_tile_index(screen_id: str, total: int) -> int:
    sid = str(screen_id or "").strip().lower()
    if not sid:
        return 0
    raw = sid.replace("screen-", "").replace("screen_", "").replace("screen", "")
    try:
        idx = int(raw)
    except (TypeError, ValueError):
        idx = 1
    idx = max(1, min(total, idx))
    return idx - 1


def _candidate_compositions(limit: int = 500) -> list[dict[str, Any]]:
    comps = Composition.objects.exclude(url__isnull=True).exclude(url__exact="").order_by("-date")[:limit]
    out: list[dict[str, Any]] = []
    for c in comps:
        out.append(
            {
                "id": int(c.id),
                "name": c.name or f"Composition {c.id}",
                "url": (c.url or "").strip(),
                "hashtags": [
                    str(t).strip().lower()
                    for t in (c.composition_hashtags or [])
                    if str(t).strip()
                ],
            }
        )
    return out


def compute_wall_tick(run, tick_id: int) -> dict[str, Any]:
    profile = run.profile
    rows = max(1, int(profile.rows or 3))
    cols = max(1, int(profile.cols or 3))
    total = rows * cols

    thought_text = (run.thought_text or "").strip()
    scenario_state = (run.scenario_state or "neutral").strip().lower()
    scene_location = (run.scene_location or "").strip()
    scene_people = [str(x).strip() for x in (run.scene_people or []) if str(x).strip()]
    scene_context = " ".join([scene_location] + scene_people)
    words = _thoughtscape_words(thought_text)
    context_words = _thoughtscape_words(scene_context)
    thought_word_set = set(words)
    context_word_set = set(context_words)

    intensity = _thoughtscape_intensity_bucket(thought_text, scenario_state, scene_context=scene_context)
    cadence_s, replacements = _thoughtscape_cadence_for_bucket(intensity, tick=tick_id)

    all_comps = _candidate_compositions(limit=500)
    pool_ids = set()
    for raw in (run.pool_composition_ids or []):
        try:
            pool_ids.add(int(raw))
        except (TypeError, ValueError):
            continue
    if pool_ids:
        pool = [c for c in all_comps if c["id"] in pool_ids]
    else:
        pool = all_comps

    persona_ids: list[int] = []
    for key in (run.active_character_keys or []):
        s = str(key or "").strip()
        if not s.startswith("persona:"):
            continue
        try:
            persona_ids.append(int(s.split(":", 1)[1]))
        except (TypeError, ValueError):
            continue
    if persona_ids and pool:
        allowed_ids = set(
            Composition.objects.filter(id__in=[c["id"] for c in pool])
            .filter(Q(allowed_personas__isnull=True) | Q(allowed_personas__id__in=persona_ids))
            .values_list("id", flat=True)
        )
        if allowed_ids:
            pool = [c for c in pool if c["id"] in allowed_ids]

    prev_map: dict[int, int] = {}
    for row in (run.previous_assignments or []):
        try:
            i = int(row.get("tileIndex"))
            cid = int(row.get("compositionId"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= i < total:
            prev_map[i] = cid

    pinned_map: dict[int, int] = {}
    for k, v in (run.pinned_assignments or {}).items():
        try:
            i = int(k)
            cid = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= i < total:
            pinned_map[i] = cid

    comp_by_id = {c["id"]: c for c in pool}
    comp_slots = list(range(total))
    rot = tick_id % len(comp_slots) if comp_slots else 0
    ordered = comp_slots[rot:] + comp_slots[:rot]
    unpinned_ordered = [i for i in ordered if i not in pinned_map]
    to_replace = set(unpinned_ordered[: min(replacements, len(unpinned_ordered))])

    used_ids = set()
    for i, cid in prev_map.items():
        if i not in to_replace:
            used_ids.add(cid)
    for cid in pinned_map.values():
        used_ids.add(cid)

    def pick_for(tile_index: int, layer: str) -> tuple[dict | None, str]:
        layer_w = _thoughtscape_weight(layer)
        best = None
        best_score = -1e9
        for c in pool:
            cid = c["id"]
            if cid in used_ids:
                continue
            tags = c["hashtags"]
            thought_overlap = len(set(tags) & thought_word_set)
            context_overlap = len(set(tags) & context_word_set)
            overlap = thought_overlap + context_overlap
            score = (overlap * (2.4 * layer_w)) + (0.25 * layer_w)
            score += ((cid * 31 + tick_id * 17 + tile_index * 13) % 19) / 1000.0
            if cid == prev_map.get(tile_index):
                score -= 0.9
            if score > best_score:
                best_score = score
                best = c
        if best is None:
            return None, "no_candidate"
        used_ids.add(best["id"])
        return best, "picked_weighted"

    tiles: list[dict[str, Any]] = []
    for i in range(total):
        layer = _thoughtscape_layer_for_index(rows, cols, i)
        chosen = None
        reason = ""

        pinned_cid = pinned_map.get(i)
        if pinned_cid and pinned_cid in comp_by_id:
            chosen = comp_by_id[pinned_cid]
            reason = "pinned"
        if chosen is None and i not in to_replace and prev_map.get(i) in comp_by_id:
            chosen = comp_by_id[prev_map[i]]
            reason = "kept_previous"
        if chosen is None:
            chosen, reason = pick_for(i, layer)
        if chosen is None and pool:
            chosen = pool[(tick_id + i) % len(pool)]
            reason = "fallback_round_robin"

        tiles.append(
            {
                "tileIndex": i,
                "kind": "composition",
                "layer": layer,
                "composition": chosen,
                "reason": reason or "none",
            }
        )

    assignments = []
    for t in tiles:
        comp = t.get("composition")
        if not comp:
            continue
        assignments.append({"tileIndex": int(t["tileIndex"]), "compositionId": int(comp["id"])})

    return {
        "rows": rows,
        "cols": cols,
        "intensity": intensity,
        "cadence_seconds": cadence_s,
        "replacements_per_tick": replacements,
        "words": words[:24],
        "context_words": context_words[:24],
        "scene_location": scene_location,
        "scene_people": scene_people[:12],
        "tiles": tiles,
        "assignments": assignments,
    }
