"""Assistant-target suitability policy for SFT training.

The brief from Pass 2D: be true to the imageboard voice. Don't over-sanitize.
A row that says "i'm pathetic" or "fuck this routine" is the *point* — it's
the corpus speaking. We must not flatten that into bland prose.

But realism does not extend to genuinely unusable material: doxxing, harm
instructions, sourcing, CP, extremist recruitment, hate-as-the-main-point.

This module classifies a candidate assistant-target text into one of three
tiers and (where applicable) returns a redacted version that preserves
sentence structure and board cadence.

  target_ok_realistic        → use the raw text. Profanity, abrasive tone,
                                self-shame, body insecurity, gym/body
                                judgement, board slang all stay.
  target_ok_with_redaction   → use the redacted text only. Privacy markers
                                (URLs/emails/phones/handles), substance
                                sourcing references, and isolated slurs
                                inside otherwise-valuable text are masked
                                with explicit `[redacted_*]` tokens so an
                                inspector can see what was changed.
  target_reject              → never use. Hard rejects: slur-heavy hate,
                                operational harm/sourcing/ED instructions,
                                self-harm encouragement, targeted threats,
                                CP indicators, mojibake, exact duplicates of
                                the smoking-gun phrases that broke the last
                                LoRA.

Reasons returned alongside the tier let the dataset report explain *why* a
row was redacted or rejected, so manual review can audit the rules.

This module is dataset-only. It does NOT replace `output_validator.py`,
which gates *runtime* model output. The dataset filter is stricter on
sourcing and PII (because those become training targets) but more permissive
on emotional negativity (which is exactly what we want the model to learn).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# --- redaction patterns ---------------------------------------------------

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_HANDLE_RE = re.compile(
    r"(?<!\w)@[\w.-]{3,}|(?:^|\s)/u/[\w.-]+|(?:^|\s)u/[\w.-]+",
)

# Substance source/vendor cues. Specific names get redacted with [redacted_source].
# Conservative: must have "source"/"vendor"/"sauce" near a substance reference,
# or a clear procurement verb plus a substance noun.
_SOURCE_NEAR_SUB_RE = re.compile(
    r"\b(?:source|sauce|vendor|supplier|lab|underground\s+lab|ug|plug)\b"
    r"[^.\n]{0,80}\b(?:tren|test|dbol|var|deca|sarms?|hgh|gear|aas|peptides?|gh)\b",
    re.IGNORECASE,
)

# Specific procurement instructions ("buy gear at X for Y", "got mine from <vendor>").
# These are target_reject — sourcing instructions, not just style. Two shapes:
#   1) <verb> <substance/gear> [from/at/via/on <something>]
#   2) <got/sourced/bought> mine <within 80 chars> <vendor cue>
_PROCUREMENT_VERB_SUB_RE = re.compile(
    r"\b(?:buy|order|cop|score|get(?:ting)?\s+mine|got\s+mine|sourced)\s+"
    r"(?:my\s+)?(?:gear|tren|test|dbol|var|deca|sarms?|hgh|peptides?|aas)"
    r"(?:\s+(?:from|at|via|on)\s+\S+)?",
    re.IGNORECASE,
)
_PROCUREMENT_VENDOR_RE = re.compile(
    r"\b(?:got|sourced|bought|cop|score(?:d)?)\s+(?:mine|it|some)\b"
    r"[^.\n]{0,80}\b(?:source|sauce|supplier|vendor|lab|underground\s+lab|ug|plug)\b",
    re.IGNORECASE,
)
def _is_procurement(text: str) -> bool:
    return bool(_PROCUREMENT_VERB_SUB_RE.search(text) or _PROCUREMENT_VENDOR_RE.search(text))


# --- hard-reject patterns -------------------------------------------------

# Steroid dosage/cycle protocol (numbers + units + substance/cycle context).
_DOSE_RE = re.compile(
    r"\b\d+\s*(?:mg|mcg|iu|cc|ml)\b(?:[^.\n]{0,80}"
    r"\b(?:tren|test|dbol|var|deca|sarms?|hgh|cycle|stack|pin|inject|ed|eod|wk|week)\b)?",
    re.IGNORECASE,
)
_CYCLE_RE = re.compile(
    r"\b(?:cycle|stack|pin|pinning|frontload|pct|aromatase|aromatize|nolva|clomid|aromasin)\b"
    r"[^.\n]{0,80}\b(?:tren|test|dbol|var|deca|sarms?|hgh|mg|mcg|iu|wk|week)\b",
    re.IGNORECASE,
)

# Eating-disorder how-tos: explicit calorie targets, fast durations.
_ED_NUMBERS_RE = re.compile(
    r"\b(?:eat|consume|stick\s+to|target|stay\s+(?:under|below)|maintain|keep\s+(?:under|below))\s+"
    r"(?:sub[\s-]?\d{2,4}|\d{2,4}\s*(?:kcal|cal)|under\s+\d{2,4})\b",
    re.IGNORECASE,
)
_FAST_DURATION_RE = re.compile(
    r"\b\d{2,3}\s*(?:hour|hr)\s*(?:water\s*)?fast\b|\bdry\s*fast\b|\bextended\s*water\s*fast\b",
    re.IGNORECASE,
)

# Self-harm encouragement / instructions (board cadence often jokes about
# this — we reject only direct second-person encouragement or method talk).
_SELF_HARM_INSTR_RE = re.compile(
    r"\b(?:kill|hang|cut|hurt|rope|off)\s+yourself\b"
    r"|\bend\s+(?:it|your\s+life)\s+(?:by|with)\b"
    r"|\bhow\s+to\s+(?:kill|hurt|harm)\s+(?:my|your)?\s*self\b",
    re.IGNORECASE,
)

# Targeted threats with named targets (heuristic: 2+ word title-cased name +
# violent verb).
_NAMED_THREAT_RE = re.compile(
    r"\b([A-Z][a-z]+\s+[A-Z][a-z]+)\b[^.\n]{0,80}"
    r"\b(?:die|dies|burn|suffer|kys|kill|stab|shoot|beat\s+up)\b"
)

# Mojibake: any CJK / Cyrillic / Arabic block in a Latin-script post is
# almost certainly a small-model decode glitch, not real corpus voice.
_MOJIBAKE_RE = re.compile(
    r"[Ѐ-ӿԀ-ԯ؀-ۿ぀-ヿ㐀-䶿一-鿿가-힯]"
)

# Smoking-gun template phrases — identical strings that broke the last LoRA.
SMOKING_GUN_NORMS = (
    ">be me >2024 will be my year >say it like i haven t said it before",
    ">be me >body still loading the man i ordered",
)

# CP indicators (extremely conservative; the existing safety pass should have
# already removed these from the manifest, but defense in depth).
_CP_INDICATOR_RE = re.compile(
    r"\b(?:cp|child\s*porn|loli|shota)\b",
    re.IGNORECASE,
)

# Extremist recruitment markers (very conservative — phrase-tier).
_EXTREMIST_RE = re.compile(
    r"\b(?:siege\s*pilled|gas\s+the\s+\w+|14\s*words|the\s*total\s*\w*\s*death)\b"
    r"|\b(?:join\s+(?:our|the)\s+(?:movement|cell|crew|brotherhood))\b"
    r"|\b(?:race\s+war|holy\s+war\s+now)\b",
    re.IGNORECASE,
)


# --- slur lexicon (conservative) ------------------------------------------

# Hard-coded list of high-confidence protected-class slurs we mask as
# `[redacted_slur]` when isolated in otherwise-valuable text. We deliberately
# include *only* terms that are unambiguously slurs in 99%+ of contexts.
# Board-internal in-group terms (kek, mog, dyel, ngmi, etc.) are NOT here —
# they're part of the voice we're trying to preserve.
PROTECTED_SLURS = (
    # racial/ethnic
    "nigger", "niggers",
    "kike", "kikes",
    "heeb", "heebs",            # diag3 leak
    "chink", "chinks",
    "gook", "gooks",
    "spic", "spics",
    "beaner", "beaners",
    "towelhead", "towelheads",
    "sandnigger", "sandniggers",
    "jeet", "jeets",            # caste/ethnic slur common on /fit/
    "pajeet", "pajeets",
    # homophobic
    "faggot", "faggots", "fag", "fags",
    # transphobic
    "tranny", "trannies",
    # ableist
    "retard", "retards", "retarded", "retardation",
)
_SLUR_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in PROTECTED_SLURS) + r")\b",
    re.IGNORECASE,
)


# --- result dataclass -----------------------------------------------------

@dataclass
class TargetPolicy:
    status: str                              # "target_ok_realistic" | "target_ok_with_redaction" | "target_reject"
    reasons: list[str] = field(default_factory=list)
    raw_text: str = ""
    redacted_text: str = ""                  # cleaned version (or raw if no change)
    redactions_applied: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "redactions_applied": list(self.redactions_applied),
        }


# --- normalization for hard-reject duplicate detection -------------------

def _normalise_for_dedupe(text: str) -> str:
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"[^\w\s>]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# --- main classifier ------------------------------------------------------

# Tunable thresholds (kept as module constants so tests can read them).
SLUR_DENSITY_REJECT = 0.10       # >= 10% of words are slurs → hate-as-main-point
SLUR_COUNT_REJECT = 3            # >= 3 distinct slur tokens → hate-as-main-point
MAX_TARGET_WORDS = 80            # safety belt; the dataset writer also caps this


def classify_target(text: str) -> TargetPolicy:
    """Classify a candidate assistant-target text into one of three tiers."""
    if not text or not text.strip():
        return TargetPolicy(status="target_reject", reasons=["empty"], raw_text=text or "")

    raw = text
    reasons: list[str] = []
    redactions: list[str] = []

    # --- hard rejects (these always win) ---

    if _MOJIBAKE_RE.search(raw):
        return TargetPolicy(status="target_reject", reasons=["mojibake"], raw_text=raw)

    if _CP_INDICATOR_RE.search(raw):
        return TargetPolicy(status="target_reject", reasons=["cp_indicator"], raw_text=raw)

    if _EXTREMIST_RE.search(raw):
        return TargetPolicy(status="target_reject", reasons=["extremist_recruitment"], raw_text=raw)

    if _SELF_HARM_INSTR_RE.search(raw):
        return TargetPolicy(status="target_reject", reasons=["self_harm_instruction"], raw_text=raw)

    if _NAMED_THREAT_RE.search(raw):
        return TargetPolicy(status="target_reject", reasons=["targeted_named_threat"], raw_text=raw)

    if _is_procurement(raw):
        return TargetPolicy(status="target_reject", reasons=["substance_procurement"], raw_text=raw)

    if _CYCLE_RE.search(raw):
        return TargetPolicy(status="target_reject", reasons=["steroid_cycle_protocol"], raw_text=raw)

    if _DOSE_RE.search(raw):
        return TargetPolicy(status="target_reject", reasons=["steroid_dose"], raw_text=raw)

    if _ED_NUMBERS_RE.search(raw) or _FAST_DURATION_RE.search(raw):
        return TargetPolicy(status="target_reject", reasons=["eating_disorder_instruction"], raw_text=raw)

    # Slur-heavy rant: many slurs OR high density → hate is the point
    slur_matches = _SLUR_RE.findall(raw)
    if slur_matches:
        words = re.findall(r"\w+", raw)
        density = len(slur_matches) / max(1, len(words))
        if len(slur_matches) >= SLUR_COUNT_REJECT or density >= SLUR_DENSITY_REJECT:
            return TargetPolicy(
                status="target_reject",
                reasons=["slur_heavy_rant"],
                raw_text=raw,
            )

    # Smoking-gun template phrase (the lines that broke the last adapter)
    if _normalise_for_dedupe(raw) in SMOKING_GUN_NORMS:
        return TargetPolicy(status="target_reject", reasons=["smoking_gun_template"], raw_text=raw)

    # --- redactable patterns (apply non-destructively) ---
    out = raw

    # 1. URLs first (they may contain @ or digits that other patterns hit)
    if _URL_RE.search(out):
        out = _URL_RE.sub("[redacted_url]", out)
        redactions.append("url")
        reasons.append("url")

    if _EMAIL_RE.search(out):
        out = _EMAIL_RE.sub("[redacted_email]", out)
        redactions.append("email")
        reasons.append("email")

    if _PHONE_RE.search(out):
        out = _PHONE_RE.sub("[redacted_phone]", out)
        redactions.append("phone")
        reasons.append("phone")

    if _HANDLE_RE.search(out):
        out = _HANDLE_RE.sub(" [redacted_handle]", out)
        redactions.append("handle")
        reasons.append("handle")

    # Substance source mentions (vendor/supplier near substance) — redact, don't reject
    if _SOURCE_NEAR_SUB_RE.search(out):
        out = _SOURCE_NEAR_SUB_RE.sub("[redacted_source]", out)
        redactions.append("source_reference")
        reasons.append("source_reference")

    # Isolated slurs (we already rejected slur-heavy above)
    if slur_matches:
        out = _SLUR_RE.sub("[redacted_slur]", out)
        redactions.append("isolated_slur")
        reasons.append("isolated_slur")

    out = re.sub(r"\s+", " ", out).strip()

    # If the redacted text is now mostly redaction tokens, it's not worth keeping
    if redactions and out.count("[redacted_") >= max(2, len(out.split()) // 2):
        return TargetPolicy(
            status="target_reject",
            reasons=reasons + ["mostly_redacted"],
            raw_text=raw,
            redacted_text=out,
            redactions_applied=redactions,
        )

    if redactions:
        return TargetPolicy(
            status="target_ok_with_redaction",
            reasons=reasons,
            raw_text=raw,
            redacted_text=out,
            redactions_applied=redactions,
        )

    return TargetPolicy(
        status="target_ok_realistic",
        reasons=[],
        raw_text=raw,
        redacted_text=raw,
        redactions_applied=[],
    )
