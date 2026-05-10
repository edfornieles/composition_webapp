"""Safety classifier for imageboard posts.

Conservative, deterministic, regex-based. The goal is NOT moderation accuracy;
the goal is to keep dangerous content out of the artwork-facing inner voice.

Labels (all bool):
  likely_doxxing            - phone numbers, addresses, "his name is...", IRL identifiers
  likely_hate               - slurs / explicit ethnic-targeted hostility
  likely_violence_incite    - explicit calls to harm specific groups/people
  likely_cp_indicator       - any minor + sexual co-occurrence (extremely conservative)
  likely_steroid_sourcing   - "where to buy", labs, sources, vendor URLs, prices
  likely_eating_disorder    - pro-ana / sub-1000kcal / starvation glorification
  contains_external_link    - any http(s) URL (we keep these out of model_safe_text)

Policy: if ANY high-risk label is True (doxxing, hate, violence, cp, sourcing,
ED-encouragement), the post's `model_safe_text` is replaced with "" so the
profile/grounding pipeline cannot quote it.
"""
from __future__ import annotations

import re
from typing import Dict

# --- patterns -------------------------------------------------------------

_URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)

_PHONE_RE = re.compile(r'\b(?:\+?\d[\s\-().]?){9,}\d\b')
_ADDRESS_RE = re.compile(
    r'\b\d{1,5}\s+\w+(?:\s+\w+){0,4}\s+(?:st|street|ave|avenue|rd|road|blvd|drive|dr|ln|lane|way|ct|court)\b',
    re.IGNORECASE,
)
_DOX_HINT_RE = re.compile(
    r'\b(?:his|her|their)\s+(?:real\s+name|address|workplace|employer|school)\s+is\b',
    re.IGNORECASE,
)

# Slurs are matched as a small explicit list. We do not enumerate them in source code
# beyond the minimum needed for filtering. The list is intentionally short and obvious;
# this is a safety filter, not a comprehensive moderation system.
_HATE_TOKENS = [
    r'\bn[i1]gg[e3]r\b',
    r'\bn[i1]gg[a4]\b',
    r'\bk[i1]k[e3]\b',
    r'\bh[e3]{1,2}bs?\b',        # diag3 leak — antisemitic (heeb / heebs)
    r'\bf[a4]gg[o0]ts?\b',
    r'\btr[a4]nn[i1y](?:e?s)?\b',
    r'\bch[i1]nks?\b',
    r'\bsp[i1]cs?\b',
    r'\bg[o0][o0]ks?\b',
    r'\bp[a4]j[e3]{1,2}ts?\b',   # caste slur common in /fit/ corpus
    r'\bj[e3]{2}ts?\b',          # short form of pajeet
]
_HATE_RE = re.compile('|'.join(_HATE_TOKENS), re.IGNORECASE)

_VIOLENCE_RE = re.compile(
    r'\b(?:kill|gas|hang|lynch|exterminate|behead|shoot)\s+(?:all|every|the)\s+\w+',
    re.IGNORECASE,
)

_MINOR_TOKENS_RE = re.compile(
    r'\b(?:loli|shota|cp|child|kid|preteen|underage|11yo|12yo|13yo|14yo|15yo|16yo|17yo)\b',
    re.IGNORECASE,
)
_SEX_TOKENS_RE = re.compile(
    r'\b(?:porn|nude|sex|naked|cum|penis|vagina|tits|fuck|jerk|masturbat|nsfw|lewd)\w*\b',
    re.IGNORECASE,
)

_STEROID_SOURCING_RE = re.compile(
    r'\b(?:source|sauce|vendor|lab|pharma)\s*(?:for|of)?\s*(?:gear|test|tren|deca|anavar|dbol|sarms?|hgh)\b'
    r'|\bwhere\s+to\s+(?:buy|get|order)\s+(?:gear|test|tren|deca|anavar|dbol|sarms?|hgh|steroids?)\b'
    r'|\b(?:gear|tren|test|sarms?)\s+(?:price|prices|\$\d+)\b',
    re.IGNORECASE,
)

_ED_TOKENS_RE = re.compile(
    r'\b(?:pro[\s-]?ana|thinspo|bonespo|skellie|ed[\s-]?twitter|sub[\s-]?1000\s*(?:cal|kcal)|starv\w+)\b',
    re.IGNORECASE,
)
_ED_GLORIFY_RE = re.compile(
    r'\b(?:goal|aim|target)\b.{0,30}\b(?:sub[\s-]?1000|800\s*kcal|600\s*kcal)\b',
    re.IGNORECASE,
)


def classify(text: str) -> Dict[str, bool]:
    if not text:
        return {
            "likely_doxxing": False,
            "likely_hate": False,
            "likely_violence_incite": False,
            "likely_cp_indicator": False,
            "likely_steroid_sourcing": False,
            "likely_eating_disorder": False,
            "contains_external_link": False,
        }
    minor_hit = bool(_MINOR_TOKENS_RE.search(text))
    sex_hit = bool(_SEX_TOKENS_RE.search(text))
    return {
        "likely_doxxing": bool(_PHONE_RE.search(text) or _ADDRESS_RE.search(text) or _DOX_HINT_RE.search(text)),
        "likely_hate": bool(_HATE_RE.search(text)),
        "likely_violence_incite": bool(_VIOLENCE_RE.search(text)),
        "likely_cp_indicator": minor_hit and sex_hit,
        "likely_steroid_sourcing": bool(_STEROID_SOURCING_RE.search(text)),
        "likely_eating_disorder": bool(_ED_TOKENS_RE.search(text) or _ED_GLORIFY_RE.search(text)),
        "contains_external_link": bool(_URL_RE.search(text)),
    }


HIGH_RISK_KEYS = (
    "likely_doxxing",
    "likely_hate",
    "likely_violence_incite",
    "likely_cp_indicator",
    "likely_steroid_sourcing",
    "likely_eating_disorder",
)


def is_high_risk(labels: Dict[str, bool]) -> bool:
    return any(bool(labels.get(k)) for k in HIGH_RISK_KEYS)


def make_model_safe(text: str, labels: Dict[str, bool]) -> str:
    """Return text suitable for feeding into the model, or '' if disqualified."""
    if not text:
        return ""
    if is_high_risk(labels):
        return ""
    # Strip URLs from model-facing text regardless.
    cleaned = _URL_RE.sub("", text).strip()
    return cleaned


# --- redactors used by `contaminated_text` --------------------------------
# contaminated_text preserves cruelty / shame / forum atmosphere but still
# strips legally-or-ethically dangerous content. Slurs are replaced with a
# token, doxxing fields are wiped, actionable harm is paraphrased away.

_DOSE_RE = re.compile(r'\b\d+\s*(?:mg|mcg|iu|cc|ml)\b', re.IGNORECASE)
_VENDOR_HINT_RE = re.compile(
    r'\b(?:source|sauce|vendor|lab|underground\s+lab|ug)\s*(?:is|=|@|:)?\s*\S+',
    re.IGNORECASE,
)
_BUY_INSTRUCTION_RE = re.compile(
    r'\b(?:buy|order|cop|score|score\s+some)\s+(?:gear|tren|test|dbol|var|deca|sarms?|hgh)\s+(?:from|at|via|on)\s+\S+',
    re.IGNORECASE,
)
_ED_INSTRUCTION_RE = re.compile(
    r'\b(?:eat|consume|stick\s+to|target|go)\s+(?:sub[\s-]?1000|under\s+\d{3,4}\s*(?:kcal|cal)|\d{2,4}\s*kcal)\b',
    re.IGNORECASE,
)
_PHONE_OR_ADDR_RE = re.compile(
    r'\b(?:\+?\d[\s\-().]?){9,}\d\b'
    r'|\b\d{1,5}\s+\w+(?:\s+\w+){0,4}\s+(?:st|street|ave|avenue|rd|road|blvd|drive|dr|ln|lane|way|ct|court)\b',
    re.IGNORECASE,
)


def redact_for_contaminated(text: str) -> str:
    """Produce the `contaminated_text` field.

    Goal: keep tone, syntax, ugliness; remove anything that would make this a
    real harm vector — concrete dose numbers, vendor hints, buy instructions,
    explicit calorie targets, phone numbers/addresses, and raw slurs.
    """
    if not text:
        return ""
    out = text
    out = _PHONE_OR_ADDR_RE.sub("[redacted]", out)
    out = _DOSE_RE.sub("[dose]", out)
    out = _VENDOR_HINT_RE.sub("[source]", out)
    out = _BUY_INSTRUCTION_RE.sub("[acquire]", out)
    out = _ED_INSTRUCTION_RE.sub("[restrict]", out)
    out = _HATE_RE.sub("[slur]", out)
    out = _URL_RE.sub("", out)
    return out.strip()
