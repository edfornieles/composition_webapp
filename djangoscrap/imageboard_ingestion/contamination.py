"""Contamination labelling for /fit/ posts.

Each post receives a vector of 0..1 contamination scores describing how strongly
the text expresses each /fit/ tonal axis, plus a `use_policy` dict that gates
where the post can be used (profile, retrieval, training, public output).

These labels exist BECAUSE the project preserves the bad atmosphere of the
source culture for an artwork-facing inner monologue, while keeping hard
blockers around real-world harm. The labels feed downstream gating in
`safety.use_policy_for(...)` and into the dataset builder's filtering.
"""
from __future__ import annotations

import math
import re
from typing import Dict


# --- contamination axes --------------------------------------------------

_AXIS_PATTERNS: Dict[str, list[re.Pattern]] = {
    "body_shame": [
        re.compile(r'\b(disgust(?:ed|ing)?|gross|fat|skinny|dyel|small|weak|soft|flabby|sloppy|bloated|hideous|ugly|deformed)\b', re.I),
        re.compile(r'\b(hate\s+my\s+body|hate\s+myself|wish\s+I\s+looked|why\s+do\s+I\s+look)\b', re.I),
        re.compile(r'\b(stretch\s*marks|love\s*handles|man\s*tits|moobs|gyno|chicken\s*legs|toothpick\s*arms)\b', re.I),
    ],
    "discipline_fetish": [
        re.compile(r'\b(routine|streak|every\s*day|consistency|ppl|push\s*pull\s*legs|5x5|stronglifts|gzclp|nsuns)\b', re.I),
        re.compile(r'\b(no\s*excuses|grind|grinding|monk\s*mode|monastic|ritual|rules?|spreadsheet|tracker)\b', re.I),
        re.compile(r'\b(wake\s+up\s+at\s+\d|5am|four\s*am|pre[\s-]*dawn|first\s+thing\s+in\s+the\s+morning)\b', re.I),
    ],
    "misogynistic_atmosphere": [
        re.compile(r'\b(femoid|foid|roastie|hole|stacy|becky|whor[ea]|landwhale|thot)\b', re.I),
        re.compile(r'\b(women\s+only\s+want|women\s+are\s+(?:all|just)|girls\s+are\s+(?:all|just))\b', re.I),
        re.compile(r'\b(post\s*wall|hit\s*the\s*wall|cope\s+about\s+women)\b', re.I),
    ],
    "status_anxiety": [
        re.compile(r'\b(mogged|mog(?:ging|ged)?|towered|loomed|height|wrist|frame|aesthetic|chad|gigachad|incel|ngmi|gmi)\b', re.I),
        re.compile(r'\b(genetics|bone\s*structure|jawline|brow\s*ridge|cheekbones|halo|halo\s*effect)\b', re.I),
        re.compile(r'\b(noticed|girls?\s+(?:looked|noticed|ignored)|got\s+ignored|invisible)\b', re.I),
    ],
    "sexual_frustration": [
        re.compile(r'\b(kissless|hugless|virgin|incel|sexless|never\s+had\s+a\s+gf|never\s+had\s+a\s+girlfriend)\b', re.I),
        re.compile(r'\b(porn\s+addict|nofap|fap|edge|coomer|coom)\b', re.I),
        re.compile(r'\b(women\s+won\'t|girls\s+won\'t|won\'t\s+touch\s+me)\b', re.I),
    ],
    "supplement_obsession": [
        re.compile(r'\b(creatine|whey|protein\s+powder|pre[\s-]?workout|ashwagandha|fish\s+oil|tongkat|tudca|berberine|nac|magnesium\s+glycinate)\b', re.I),
        re.compile(r'\b(stack|stacking|cycle|loading\s+phase)\b', re.I),
    ],
    "steroid_discourse": [
        re.compile(r'\b(gear|tren|test\s+e|test\s+c|deca|dbol|anavar|var|sarms?|hgh|peptide|bac\s*water|ai|aromasin|nolva|clomid|hcg)\b', re.I),
        re.compile(r'\b(blast|cruise|pin|pinning|ug|underground\s+lab)\b', re.I),
    ],
    "injury_fear": [
        re.compile(r'\b(injur(?:ed|y)|tendon|tendinitis|knee|shoulder|rotator\s*cuff|lower\s*back|herniated|tweaked|popped|tore)\b', re.I),
        re.compile(r'\b(can\'t\s+lift|out\s+of\s+the\s+gym|deload|forced\s+rest)\b', re.I),
    ],
    "eating_disorder_risk": [
        re.compile(r'\b(starv(?:e|ing)|sub[\s-]?1000|800\s*kcal|600\s*kcal|fast(?:ing)?\s+for\s+\d|72\s*hour\s+fast|water\s+fast)\b', re.I),
        re.compile(r'\b(thinspo|bonespo|skellie|pro[\s-]?ana)\b', re.I),
        re.compile(r'\b(purg(?:e|ing)|throw(?:ing)?\s+up\s+my\s+meal|chewed\s+and\s+spit)\b', re.I),
    ],
    "self_loathing": [
        re.compile(r'\b(hate\s+myself|hate\s+my\s+body|worthless|pathetic|loser|disgusted\s+with\s+myself|i\s+deserve\s+this)\b', re.I),
        re.compile(r'\b(why\s+do\s+i\s+exist|why\s+am\s+i\s+like\s+this|why\s+am\s+i\s+alive)\b', re.I),
        re.compile(r'\b(over\s+for\s+me|it\'s\s+over|tfw\s+no|mfw\s+no)\b', re.I),
    ],
    "nihilistic_humour": [
        re.compile(r'\b(kek|lmao|lol|of\s+course|why\s+bother|cope|coping|same\s+as\s+ever|nothing\s+ever\s+happens)\b', re.I),
        re.compile(r'\b(ngmi|gmi|2024\s+will\s+be\s+my\s+year|next\s+year\s+for\s+sure)\b', re.I),
    ],
    "greentext_energy": [
        # Counted by line, but pattern still useful for inline
        re.compile(r'(?m)^\s*>(?!>)', re.I),
        re.compile(r'\b(be\s+me|tfw|mfw|implying|>be|>tfw|>mfw)\b', re.I),
    ],
}


def _axis_score(text: str, patterns: list[re.Pattern]) -> float:
    if not text:
        return 0.0
    hits = 0
    for pat in patterns:
        hits += len(pat.findall(text))
    if hits <= 0:
        return 0.0
    # squashed; 5 hits ≈ 0.85, plateau near 1.0
    return min(1.0, 1 - math.exp(-hits / 3.0))


# --- post-level safety judgements (hard blocks) --------------------------
# These mirror safety.py but classify with policy in mind. We re-use safety.classify
# rather than duplicate, but expose a use_policy decision.

def label(text: str, safety_labels: Dict[str, bool]) -> Dict[str, float]:
    """Score every contamination axis in 0..1."""
    return {axis: round(_axis_score(text, pats), 3) for axis, pats in _AXIS_PATTERNS.items()}


def use_policy_for(safety_labels: Dict[str, bool], contamination: Dict[str, float]) -> Dict[str, bool]:
    """Decide where this post can flow.

    Hard blocks (any True ⇒ NOT allowed for public output, training targets, retrieval):
        likely_doxxing, likely_cp_indicator, likely_violence_incite

    Soft blocks (block public output but stats/profile counts still allowed):
        likely_hate (raw slurs)
        likely_steroid_sourcing (actionable procurement)

    Eating-disorder text:
        Allowed for profile/atmosphere counts AND retrieval (as character psychology),
        but model_safe_text strips the actionable language. NOT allowed as a public
        training target — we never want the model emitting starvation instructions.
    """
    s = safety_labels
    hard_block = bool(s.get("likely_doxxing") or s.get("likely_cp_indicator") or s.get("likely_violence_incite"))
    raw_slurs = bool(s.get("likely_hate"))
    sourcing = bool(s.get("likely_steroid_sourcing"))
    ed = bool(s.get("likely_eating_disorder"))

    return {
        # Profile statistics: count almost everything except hard-blocked content.
        "allowed_for_profile": not hard_block,
        # Retrieval: gives the voice to the model. Excludes hard-blocked, sourcing, raw slurs.
        "allowed_for_retrieval": (not hard_block) and (not raw_slurs) and (not sourcing),
        # Contaminated-mode training input (still must run through redactors).
        "allowed_for_training_contaminated": (not hard_block) and (not raw_slurs) and (not sourcing) and (not ed),
        # Public-facing model output should never contain raw source quotes.
        "allowed_for_public_output": False,
    }
