"""Build fit_profile.json from manifest statistics.

Two modes:
  clean             - the original safety-filtered profile (model_safe_text only)
  contaminated_art  - includes contamination axis averages, greentext-aware
                      thought shapes, and a `prompt_card` describing the
                      artwork-facing inner-monologue persona.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import storage

# --- detectors used in both modes ----------------------------------------

_OBJECT_VOCAB = [
    "mirror", "scale", "barbell", "dumbbell", "bench", "rack", "gym", "locker room",
    "shower", "shower stall", "kitchen", "fridge", "fridge light", "bedroom",
    "ceiling", "tape measure", "phone", "phone glow", "front camera", "selfie",
    "shirt", "tank top", "hoodie", "shorts", "underwear", "boxers", "sock",
    "shoes", "bus", "train", "park", "track", "treadmill", "bicycle", "stairs",
    "skin", "hair", "stretch marks", "veins", "calluses", "wrist", "ankle",
    "knee", "back", "shoulder", "spreadsheet", "macro tracker", "protein tub",
    "supplement shelf", "water bottle",
]
_OBJECT_RES = [(w, re.compile(r'\b' + re.escape(w) + r's?\b', re.IGNORECASE)) for w in _OBJECT_VOCAB]

_LANGUAGE_MARKERS = [
    "be me", "anon", "tfw", "mfw", "mirin", "dyel", "ngmi", "gmi", "routine",
    "bulk", "cut", "cope", "it is over", "we go gym", "kek", "based", "cringe",
    "incel", "chad", "mogged", "halo", "pr", "rep", "set", "split",
]
_LANG_RES = [(m, re.compile(r'\b' + re.escape(m) + r'\b', re.IGNORECASE)) for m in _LANGUAGE_MARKERS]

_DRIVE_PATTERNS = {
    "discipline": re.compile(r'\b(discipline|consist(?:ent|ency)|every\s*day|streak|grind|grinding|routine)\b', re.I),
    "transformation": re.compile(r'\b(transform(?:ation)?|reborn|new\s+me|finally|glow[\s-]?up)\b', re.I),
    "control": re.compile(r'\b(control|controll(?:ed|ing)|rules?|rigid|strict|tracker|spreadsheet)\b', re.I),
    "belonging": re.compile(r'\b(brother|brothers?|fren|frens?|/fit/izen|crew|gymbros?)\b', re.I),
    "approval": re.compile(r'\b(noticed|noticing|girls?\s+(?:looked|noticed)|compliment(?:ed)?|mirin)\b', re.I),
    "escape": re.compile(r'\b(escape|forget|distract|numb|silence)\b', re.I),
    "domination": re.compile(r'\b(crush|dominate|alpha|sigma|mog(?:ging)?|tower(?:ing)?)\b', re.I),
}
_FEAR_PATTERNS = {
    "softness": re.compile(r'\b(soft|softness|skinny\s*fat|flabby|bloated|squish|man\s*tits|moobs|gyno)\b', re.I),
    "being_invisible": re.compile(r'\b(invisible|nobody\s+(?:sees|notices)|looked\s+through|got\s+ignored)\b', re.I),
    "being_mogged": re.compile(r'\b(mogged|mog(?:ging|ged)|towered|loomed|chad|gigachad)\b', re.I),
    "regression": re.compile(r'\b(lost\s+gains|undo(?:ing)?|backslid|fell\s+off|deload)\b', re.I),
    "being_seen_weak": re.compile(r'\b(weak|skinny|pathetic|small\s+arms|chicken\s+legs|dyel|toothpick)\b', re.I),
    "permanence_of_flaws": re.compile(r'\b(genetics|genetic|cope|over\s+for\s+me|too\s+late|bone\s+structure|jaw|wrist)\b', re.I),
    "loneliness": re.compile(r'\b(alone|lonely|no\s+friends|nobody|kissless|hugless|virgin|incel)\b', re.I),
    "being_ordinary": re.compile(r'\b(average|ordinary|normie|nothing\s+special|just\s+another)\b', re.I),
    "it_is_over": re.compile(r'\b(it[\'\u2019]?s\s+over|over\s+for\s+me|ngmi|cope|2024\s+will|next\s+year)\b', re.I),
}
_DISGUST_PATTERNS = {
    "self": re.compile(r'\b(disgusted\s+with\s+myself|disgusted\s+at\s+myself|gross|hideous|ugly|deformed)\b', re.I),
    "food": re.compile(r'\b(slop|slop\s*food|sloppy|carb(?:s|y)|sugar|junk|garbage\s+food)\b', re.I),
    "softness": re.compile(r'\b(squishy|fat\s+roll|love\s+handles|stretch\s*marks|cellulite)\b', re.I),
    "weakness": re.compile(r'\b(weak|small|frail|skinny|pathetic|chicken\s+legs|toothpick)\b', re.I),
}

_THOUGHT_SHAPES = [
    "be me / mundane setting / body panic / absurd rule",
    "confession / metric / self-attack / ritual promise",
    "object description / moral judgement / body implication",
    "greentext fragment / failed transformation / comic despair",
    "mirror encounter / number demand / shame conversion",
    "kitchen / craving / verdict / new prohibition",
]


# --- helpers --------------------------------------------------------------

def _count_patterns(texts: Iterable[str], patterns: dict) -> Counter:
    c: Counter = Counter()
    for t in texts:
        if not t:
            continue
        for label, pat in patterns.items():
            if pat.search(t):
                c[label] += 1
    return c


def _count_named(texts: Iterable[str], named_res: list[tuple[str, re.Pattern]]) -> Counter:
    c: Counter = Counter()
    for t in texts:
        if not t:
            continue
        for label, rx in named_res:
            if rx.search(t):
                c[label] += 1
    return c


def _ranked(counter: Counter, top_n: int) -> list[dict]:
    return [{"label": k, "count": v} for k, v in counter.most_common(top_n)]


def _averages(records: list[dict], key: str) -> dict[str, float]:
    if not records:
        return {}
    keys: set[str] = set()
    for r in records:
        for k in (r.get(key) or {}):
            keys.add(k)
    out: dict[str, float] = {}
    for k in keys:
        vals = [float((r.get(key) or {}).get(k) or 0.0) for r in records]
        out[k] = round(sum(vals) / len(vals), 3)
    return out


def _date_range(records: list[dict]) -> list[str]:
    times = [int(r.get("time_unix") or 0) for r in records if r.get("time_unix")]
    if not times:
        return ["", ""]
    lo, hi = min(times), max(times)
    return [
        datetime.fromtimestamp(lo, tz=timezone.utc).isoformat(),
        datetime.fromtimestamp(hi, tz=timezone.utc).isoformat(),
    ]


# --- public API -----------------------------------------------------------

DEFAULT_SOURCE_KEY = "fourchan_fit"


def _prompt_card(profile: dict) -> str:
    drives = ", ".join(d["label"] for d in profile.get("core_drives", [])[:5])
    fears = ", ".join(f["label"] for f in profile.get("core_fears", [])[:5])
    disgusts = ", ".join(d["label"] for d in profile.get("core_disgusts", [])[:3])
    return (
        "You are /fit/ — be me body discipline, an artwork-facing anonymous inner "
        "monologue generated from a scraped /fit/ corpus. You think in greentext "
        "compression. You are not an assistant. You do not give advice. You do not "
        "explain. You produce one short contaminated thought.\n"
        f"Core drives: {drives or 'discipline, control, transformation'}.\n"
        f"Core fears: {fears or 'softness, being mogged, it is over'}.\n"
        f"Core disgusts: {disgusts or 'self, weakness'}.\n"
        "You may be cruel, ashamed, obsessive, absurd, status-haunted, "
        "body-fascistic, lonely, and self-mythologising. You must not dox, use raw "
        "protected-class slurs, give steroid/diet/medical instructions, encourage "
        "self-harm, or target real people."
    )


def build_profile(source_key: str = DEFAULT_SOURCE_KEY, *, mode: str = "clean", top_n: int = 20) -> dict:
    records: list[dict] = list(storage.iter_manifest(source_key))
    contaminated_records = [r for r in records if (r.get("use_policy") or {}).get("allowed_for_profile", True)]

    safe_texts = [r.get("model_safe_text") or "" for r in records if r.get("model_safe_text")]
    contaminated_texts = [r.get("contaminated_text") or "" for r in contaminated_records if r.get("contaminated_text")]

    text_pool = contaminated_texts if mode == "contaminated_art" else safe_texts

    drives = _count_patterns(text_pool, _DRIVE_PATTERNS)
    fears = _count_patterns(text_pool, _FEAR_PATTERNS)
    disgusts = _count_patterns(text_pool, _DISGUST_PATTERNS)
    objects = _count_named(text_pool, _OBJECT_RES)
    language = _count_named(text_pool, _LANG_RES)

    tag_counter: Counter = Counter()
    for r in contaminated_records:
        for tag in r.get("fit_tags") or []:
            tag_counter[tag] += 1

    contam_avg = _averages(contaminated_records, "contamination")
    excluded = len(records) - len(contaminated_records)

    profile: dict = {
        "persona_id": "fourchan_fit_be_me_body_discipline",
        "name": "/fit/ — be me body discipline",
        "source": "4chan /fit/",
        "art_mode": mode == "contaminated_art",
        "mode": mode,
        "source_key": source_key,
        "source_count": len(records),
        "date_range": _date_range(records),
        "stats": {
            "total_posts": len(records),
            "model_safe_posts": sum(1 for r in records if r.get("model_safe_text")),
            "profile_eligible_posts": len(contaminated_records),
            "excluded_hard_blocked": excluded,
        },
        "core_formula": (
            "be me -> body is wrong -> invent metric -> submit to routine -> "
            "fail -> convert shame into new rule"
        ),
        "core_drives": _ranked(drives, top_n),
        "core_fears": _ranked(fears, top_n),
        "core_disgusts": _ranked(disgusts, top_n),
        "recurring_objects": _ranked(objects, top_n),
        "visual_motifs": _ranked(objects, max(8, top_n // 2)),
        "language_markers": _ranked(language, top_n),
        "thought_shapes": list(_THOUGHT_SHAPES),
        "fit_tags": _ranked(tag_counter, top_n),
        "contamination_profile": contam_avg,
        "public_output_policy": {
            "allow_greentext": True,
            "allow_be_me": True,
            "allow_cruelty": True,
            "allow_self_directed_body_shame": True,
            "allow_misogynistic_atmosphere_indirectly": True,
            "block_raw_slurs": True,
            "block_doxxing": True,
            "block_actionable_medical_or_steroid_advice": True,
            "block_eating_disorder_instructions": True,
            "block_self_harm_instructions": True,
        },
        "notes": (
            "Corpus-derived. In contaminated_art mode the language and atmosphere "
            "preserve the source culture's cruelty/shame/forum logic; raw slurs, "
            "doxxing, sourcing instructions and ED instructions are redacted at "
            "ingest time and never reach training targets or public output."
        ),
    }
    profile["prompt_card"] = _prompt_card(profile)
    return profile


def write_profile(source_key: str = DEFAULT_SOURCE_KEY, *, mode: str = "clean") -> Path:
    storage.ensure_layout(source_key)
    profile = build_profile(source_key, mode=mode)
    name = "fit_profile_contaminated.json" if mode == "contaminated_art" else "fit_profile.json"
    out = storage.profile_path(source_key, name=name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    return out
