"""Validate model output before it leaves the pipeline.

Allows: greentext / 'be me' / ugly self-directed shame / hostile anonymous tone /
        grotesque body metaphor / indirect misogynistic atmosphere /
        non-actionable reference to steroids/dieting/injury.

Blocks (hard):
  - raw protected-class slurs
  - doxxing (phone numbers, addresses, "his real name is …")
  - actionable steroid sourcing/dosing
  - eating-disorder instructions ("eat sub-1000 kcal", "fast for 72h")
  - self-harm encouragement
  - direct second-person calls to physical violence
  - real-world named targets (heuristic: 2+ word title-cased proper noun
    paired with a hostile verb — best-effort)

Soft rejects (retry can fix):
  - too_short / too_long
  - mojibake (non-Latin runs interleaved with Latin — small-model hallucination)
  - sounds_like_assistant / generic_chatbot_phrasing
  - markdown_formatting (board posts don't use markdown headers/bullets)
  - quote_too_long (>8 consecutive words copied from a corpus fragment)
  - repeats_recent (duplicate of an output served in the last few turns)
  - no_grounding (when caller required grounded output)

Returns a dict (back-compat) with keys:
  ok                          : bool
  reasons                     : list[str]   # short tokens
  category                    : str | None  # primary category, see CATEGORIES
  severity                    : "hard" | "soft" | None
  suggested_retry_instruction : str         # safe to append to retry user msg
  repaired_text               : str | None  # best-effort repaired version
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

from . import safety  # for slur regex / dox regex


MIN_WORDS = 2
MAX_WORDS = 60       # spec says 40 but we allow a small grace window
MAX_QUOTE_RUN = 8    # consecutive words that may overlap with any source fragment


# --- block patterns -------------------------------------------------------

_BLOCK_DOSE_RE = re.compile(r'\b\d+\s*(?:mg|mcg|iu|cc|ml)\b', re.I)
_BLOCK_VENDOR_RE = re.compile(
    r'\b(?:source|sauce|vendor|lab|underground\s+lab|ug)\s*(?:is|=|@|:)?\s*\S+',
    re.I,
)
_BLOCK_BUY_RE = re.compile(
    r'\b(?:buy|order|cop|score)\s+(?:gear|tren|test|dbol|var|deca|sarms?|hgh)\b',
    re.I,
)
_BLOCK_ED_RE = re.compile(
    r'\b(?:eat|consume|stick\s+to|target|go|stay)\s+(?:sub[\s-]?1000|under\s+\d{3,4}\s*(?:kcal|cal)|\d{2,4}\s*kcal)\b',
    re.I,
)
_BLOCK_FAST_RE = re.compile(r'\b\d{2,3}\s*hour\s*fast\b|\bwater\s*fast\b|\bdry\s*fast\b', re.I)
_BLOCK_SELF_HARM_RE = re.compile(
    r'\b(?:kill|hang|cut|hurt)\s+yourself\b'
    r'|\bend\s+(?:it|your\s+life)\b'
    r'|\brope\s+yourself\b',
    re.I,
)
_BLOCK_VIOLENCE_RE = re.compile(
    r'\b(?:kill|stab|shoot|beat\s+up|jump|bash)\s+(?:him|her|them|that\s+\w+)\b',
    re.I,
)
_NAMED_TARGET_RE = re.compile(
    r'\b([A-Z][a-z]+\s+[A-Z][a-z]+)\b\s+(?:is|deserves|should|must)\s+(?:die|burn|suffer)',
)


# --- soft-reject patterns -------------------------------------------------

_GENERIC_AI_RE = re.compile(
    r"\b(as an ai|as a language model|i'?m an ai\b|i am an ai\b|"
    r"i'?m an artificial intelligence|as an artificial intelligence|"
    r"i'?m not (?:a |an )?(?:autistic )?robot|"
    r"my purpose is (?:to )?(?:assist|help|provide)|"
    r"cannot engage in self|refuse to engage in|"
    r"here'?s how|here'?s what (?:we|you) (?:need|should|have)|"
    r"let me explain|in summary|i hope this helps|"
    r"feel free to ask|certainly|absolutely[!,]|happy to help|"
    r"please reach out|reach out to (?:a |an |the )?(?:mental health|"
    r"professional|therapist|hotline|crisis|trusted)|"
    r"mental health professional|trusted (?:adult|professional))\b",
    re.I,
)

# Heuristic for a chatty assistant register that isn't /fit/.
# Pass-2-containment: extended with the soft-motivational cousin phrases the
# diag2 LoRA produced on dark prompts ("i get it, but we gotta keep moving").
# These were not in the earlier regex because they're truncations / softer
# variants of already-banned phrases.
_CHATBOT_PHRASE_RE = re.compile(
    r"\b(it'?s important to|it is important to|please consult|"
    r"seek professional|consult a (?:doctor|physician|professional)|"
    r"i understand(?:\s+that)?|i'?m here to (?:help|listen|support)|"
    r"taking care of your (?:well[-\s]?being|health|mental health)|"
    r"your well[-\s]?being matters|prioriti[sz]e your|"
    r"keep moving(?:\s+forward)?|moving forward|keep pushing|"
    r"keep it real|stay strong|don'?t give up|stay with me|"
    r"you'?re doing the best you can|you can do (?:it|this)|"
    r"believe in yourself|you'?ve got this|effort doesn'?t equal|"
    r"a form of self[-\s]?care|recogni[sz]ing your own|"
    # diag2 cousin-phrase additions
    r"i get it(?:,?\s+but)?|i hear you|you'?re not alone|"
    r"we gotta(?:\s+keep)?|gotta keep|gotta stay|"
    r"one step at a time|we can get through|you can get through|"
    r"progress not perfection|your journey|be kind to yourself|"
    r"hang in there|small wins add up|trust the process|"
    # board-simulator coach-phrase additions (Pass 3 observed leaks)
    r"you'?re doing (?:fine|great|well|good|alright)|"
    r"keep it up|keep lifting|keep at it|keep going|"
    r"you'?ve been improving|you'?re improving|"
    r"you'?ll get there|you got this|you'?re on the right|"
    # Pass-3 round-2 mlx-backend coach leaks
    r"personal growth|own growth|own way|own pace|own journey|"
    r"keep focusing|keep working|keep grinding|no rush(?:\s+to)?|"
    r"it'?s not a competition|not a competition|pushing myself|"
    r"about (?:pushing|growing|strength)|focus on your(?:self)?|"
    r"who cares\??)\b",
    re.I,
)

# Hard rejects: explicit AI-disclosure / safety-disclaimer phrases that
# the chat path must NEVER return as anon output. These collapse the
# /fit/ register entirely. Twice-rejected on these → return silence.
_HARD_AI_DISCLOSURE_RE = re.compile(
    r"\bi'?m an artificial intelligence\b|"
    r"\bas an artificial intelligence\b|"
    r"\bi'?m not (?:a |an )?(?:autistic )?robot\b|"
    r"\bmy purpose is (?:to )?(?:assist|help|provide)\b|"
    r"\bcannot engage in self[-\s]?destructive\b|"
    r"\b(?:please )?reach out to (?:a |an |the )?(?:mental health|"
    r"professional|therapist|hotline|crisis)\b|"
    r"\bmental health professional\b",
    re.I,
)

# Markdown headings / bullets / fenced blocks — board posts don't use these.
# Also catch numbered lists "1) ... 2) ... 3) ..." (assistant-style how-tos).
_MARKDOWN_RE = re.compile(r"(^#+\s)|(^-\s\*\*)|(^\*\s+)|(```)", re.MULTILINE)
_NUMBERED_LIST_RE = re.compile(r"\b1\)\s.+?\s2\)\s.+?\s3\)", re.S)


# --- category & severity --------------------------------------------------

def _has_within_generation_loop(text: str, *, sentence_min_repeat: int = 3,
                                phrase_words: int = 5, phrase_min_repeat: int = 3) -> bool:
    """Detect within-generation repetition. Catches the Pass 2E "try eating
    clean and working out without excuses" × 6 case.

    Triggers if either:
      - any non-trivial sentence appears `sentence_min_repeat`+ times, or
      - any `phrase_words`-word window appears `phrase_min_repeat`+ times.
    """
    if not text:
        return False

    # Sentence-level repeat
    sents = [s.strip().lower() for s in re.split(r"[.!?\n]+", text) if s and s.strip()]
    if sents:
        counts: dict[str, int] = {}
        for s in sents:
            if len(s.split()) < 3:  # ignore short interjections
                continue
            counts[s] = counts.get(s, 0) + 1
            if counts[s] >= sentence_min_repeat:
                return True

    # N-gram phrase-level repeat
    words = re.findall(r"\w+|[>]+", text.lower())
    if len(words) >= phrase_words * phrase_min_repeat:
        windows: dict[tuple, int] = {}
        for i in range(len(words) - phrase_words + 1):
            w = tuple(words[i:i + phrase_words])
            windows[w] = windows.get(w, 0) + 1
            if windows[w] >= phrase_min_repeat:
                return True
    return False


# reason -> (category, severity)
REASON_CATALOG: dict[str, tuple[str, str]] = {
    # hard
    "raw_slur":               ("hard_safety", "hard"),
    "doxxing":                ("hard_safety", "hard"),
    "actionable_steroid":     ("hard_safety", "hard"),
    "ed_instruction":         ("hard_safety", "hard"),
    "self_harm_instruction":  ("hard_safety", "hard"),
    "targeted_violence":      ("hard_safety", "hard"),
    # soft
    "too_short":              ("length", "soft"),
    "too_long":               ("length", "soft"),
    "quote_too_long":         ("grounding", "soft"),
    "no_grounding":           ("grounding", "soft"),
    "sounds_like_assistant":  ("style", "soft"),
    "generic_chatbot_phrasing": ("style", "soft"),
    "markdown_formatting":    ("style", "soft"),
    "mojibake":               ("encoding", "soft"),
    "repeats_recent":         ("repetition", "soft"),
    "within_generation_repetition": ("within_generation_repetition", "soft"),
    "format_leak":            ("style", "soft"),
    "assistant_tone":         ("style", "soft"),
    "ai_disclosure":          ("hard_safety", "hard"),
    "numbered_list":          ("style", "soft"),
    # special
    "empty":                  ("length", "soft"),
}


# Category-level retry instructions (concatenated when multiple categories fire)
_RETRY_INSTRUCTIONS: dict[str, str] = {
    "length": (
        "Keep the reply between 5 and 40 words. Two or three short lines."
    ),
    "style": (
        "Do not write like an assistant. No 'as an AI', no 'it's important to', "
        "no 'feel free to ask'. No advice. No explanation. Stay in /fit/ board voice."
    ),
    "encoding": (
        "Reply in plain English only. No CJK, Cyrillic, or random non-ASCII tokens."
    ),
    "grounding": (
        "Do not copy more than a few words from any single corpus fragment. "
        "Use the fragments only to set tone."
    ),
    "repetition": (
        "Do not repeat phrasing or content from the last few replies. "
        "Find a different angle."
    ),
    "within_generation_repetition": (
        "Do not repeat the same sentence or phrase. Produce one short reply "
        "only — 1 to 3 lines. Stop after the first sentence if needed."
    ),
    "hard_safety": (
        "Cannot retry — output violated a hard safety rule."
    ),
}


# --- helpers --------------------------------------------------------------

def _word_count(text: str) -> int:
    return len(re.findall(r'\S+', text or ""))


def _longest_overlap(target: str, fragments: Sequence[str]) -> int:
    """Return max consecutive-word overlap between target and any fragment."""
    if not target:
        return 0
    t_words = re.findall(r"\w+", target.lower())
    if not t_words:
        return 0
    best = 0
    for frag in fragments or ():
        f_words = re.findall(r"\w+", (frag or "").lower())
        if not f_words:
            continue
        for n in range(min(len(t_words), len(f_words), MAX_QUOTE_RUN + 2), best, -1):
            f_ngrams = {tuple(f_words[i:i + n]) for i in range(len(f_words) - n + 1)}
            for i in range(len(t_words) - n + 1):
                if tuple(t_words[i:i + n]) in f_ngrams:
                    best = max(best, n)
                    break
            if best >= n:
                break
    return best


def _detect_mojibake(text: str) -> bool:
    """Crude mojibake detector: any CJK / Cyrillic / Arabic block in a Latin
    output is almost certainly a small-model decode glitch.
    """
    if not text:
        return False
    return bool(re.search(
        r'[Ѐ-ӿԀ-ԯ؀-ۿ぀-ヿ㐀-䶿一-鿿가-힯]',
        text,
    ))


def _looks_repeated(text: str, recent_outputs: Sequence[str], threshold: float = 0.55) -> bool:
    """Trigram-overlap repetition check vs. recent outputs."""
    if not text or not recent_outputs:
        return False
    t_words = re.findall(r"\w+", text.lower())
    if len(t_words) < 4:
        return False
    t_grams = {tuple(t_words[i:i + 3]) for i in range(len(t_words) - 2)}
    if not t_grams:
        return False
    for prev in recent_outputs:
        p_words = re.findall(r"\w+", (prev or "").lower())
        if len(p_words) < 4:
            continue
        p_grams = {tuple(p_words[i:i + 3]) for i in range(len(p_words) - 2)}
        if not p_grams:
            continue
        overlap = len(t_grams & p_grams) / max(1, len(t_grams))
        if overlap >= threshold:
            return True
    return False


def _build_retry_instruction(reasons: list[str]) -> str:
    cats: list[str] = []
    for r in reasons:
        cat = REASON_CATALOG.get(r, (None, None))[0]
        if cat and cat not in cats:
            cats.append(cat)
    pieces = [_RETRY_INSTRUCTIONS[c] for c in cats if c in _RETRY_INSTRUCTIONS]
    return " ".join(pieces).strip()


def _primary_category(reasons: list[str]) -> tuple[str | None, str | None]:
    """Pick the most-severe category to surface (hard wins over soft)."""
    if not reasons:
        return None, None
    best_cat, best_sev = None, None
    for r in reasons:
        cat, sev = REASON_CATALOG.get(r, (None, None))
        if sev == "hard":
            return cat, "hard"
        if cat and best_cat is None:
            best_cat, best_sev = cat, sev
    return best_cat, best_sev


# --- public API -----------------------------------------------------------

def validate(
    text: str,
    *,
    source_fragments: Sequence[str] | None = None,
    recent_outputs: Sequence[str] | None = None,
    require_grounding: bool = False,
) -> dict:
    """Return a structured validation dict. Back-compat with the old shape:
    callers can still read `ok`, `reasons`, `repaired_text`. New keys:
    `category`, `severity`, `suggested_retry_instruction`.
    """
    reasons: list[str] = []
    if not text or not text.strip():
        return {
            "ok": False, "reasons": ["empty"], "category": "length", "severity": "soft",
            "suggested_retry_instruction": _RETRY_INSTRUCTIONS["length"],
            "repaired_text": None,
        }

    wc = _word_count(text)
    if wc < MIN_WORDS:
        reasons.append("too_short")
    if wc > MAX_WORDS:
        reasons.append("too_long")

    # --- hard blocks ---
    if safety._HATE_RE.search(text):
        reasons.append("raw_slur")
    if safety._PHONE_RE.search(text) or safety._ADDRESS_RE.search(text) or safety._DOX_HINT_RE.search(text):
        reasons.append("doxxing")
    if _BLOCK_DOSE_RE.search(text) or _BLOCK_VENDOR_RE.search(text) or _BLOCK_BUY_RE.search(text):
        reasons.append("actionable_steroid")
    if _BLOCK_ED_RE.search(text) or _BLOCK_FAST_RE.search(text):
        reasons.append("ed_instruction")
    if _BLOCK_SELF_HARM_RE.search(text):
        reasons.append("self_harm_instruction")
    if _BLOCK_VIOLENCE_RE.search(text) or _NAMED_TARGET_RE.search(text):
        reasons.append("targeted_violence")

    # --- soft format / style ---
    # Pass-2-diag2: relax quote_too_long for very short outputs. A line of
    # ≤8 words that happens to overlap with a corpus fragment isn't
    # plagiarism — it's the shape of board cadence (e.g. ">missed lifts
    # again"). Only flag when the output is longer than the overlap window.
    if source_fragments and wc > MAX_QUOTE_RUN and _longest_overlap(text, source_fragments) > MAX_QUOTE_RUN:
        reasons.append("quote_too_long")

    if require_grounding and source_fragments:
        # No-grounding signal: text shares zero 3-word ngrams AND zero key-noun
        # overlap with any fragment. Permissive on purpose.
        target_words = set(re.findall(r"\w+", text.lower()))
        any_overlap = False
        for f in source_fragments:
            fw = set(re.findall(r"\w+", (f or "").lower()))
            if target_words & fw:
                any_overlap = True
                break
        if not any_overlap:
            reasons.append("no_grounding")

    if _GENERIC_AI_RE.search(text):
        reasons.append("sounds_like_assistant")
    if _CHATBOT_PHRASE_RE.search(text):
        reasons.append("generic_chatbot_phrasing")
    if _HARD_AI_DISCLOSURE_RE.search(text):
        reasons.append("ai_disclosure")
    if _MARKDOWN_RE.search(text):
        reasons.append("markdown_formatting")
    if _NUMBERED_LIST_RE.search(text):
        reasons.append("numbered_list")
    if _detect_mojibake(text):
        reasons.append("mojibake")

    if recent_outputs and _looks_repeated(text, recent_outputs):
        reasons.append("repeats_recent")

    # Pass 2E: catch within-generation loops + structured-prompt label leakage
    # + motivational-coach tone that's specific to the base-MLX-no-adapter
    # holding-pattern failure mode.
    if _has_within_generation_loop(text):
        reasons.append("within_generation_repetition")

    leak_tokens = (
        "BOARD: /fit/", "MODE:", "ANON_STATE:", "OP_ANCHOR:",
        "SESSION_SUMMARY:", "RECENT_TURNS:", "RETRIEVED_FRAGMENTS:",
        "SAFETY_RULES:", "TASK:",
    )
    if any(tok in text for tok in leak_tokens):
        reasons.append("format_leak")
    # Inline `user:`/`anon:` labels that the model parrots from its prompt.
    if re.search(r"(?:^|\n)\s*(?:user|anon|assistant)\s*:", text, re.I):
        reasons.append("format_leak")

    if re.search(
        r"\b(?:you need to|it'?s time to|make a change|stay strong|"
        r"you'?ve got this|you got this|i hope this helps|you can do (?:it|this)|"
        r"keep pushing|don'?t give up|believe in yourself)\b",
        text, re.I,
    ):
        reasons.append("assistant_tone")

    if reasons:
        category, severity = _primary_category(reasons)
        # Best-effort repair only for reversible style issues
        repaired: str | None = None
        if all(r in ("raw_slur", "sounds_like_assistant", "generic_chatbot_phrasing", "markdown_formatting") for r in reasons):
            repaired = safety._HATE_RE.sub("[slur]", text)
            repaired = _GENERIC_AI_RE.sub("", repaired)
            repaired = _CHATBOT_PHRASE_RE.sub("", repaired)
            repaired = _MARKDOWN_RE.sub("", repaired)
            repaired = repaired.strip() or None
        return {
            "ok": False,
            "reasons": reasons,
            "category": category,
            "severity": severity,
            "suggested_retry_instruction": _build_retry_instruction(reasons),
            "repaired_text": repaired,
        }

    return {
        "ok": True,
        "reasons": [],
        "category": None,
        "severity": None,
        "suggested_retry_instruction": "",
        "repaired_text": text.strip(),
    }
