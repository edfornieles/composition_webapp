"""Self-generating /fit/ greentext story stream.

The artwork target: a wall of greentext stories about anon's life — coherent
short narratives, 8-14 lines each, ritual opener (>be me / >be N / >setting),
beat-by-beat scene, turn at the end. NOT board threads. NOT one-liners.
Single OPs in the standalone greentext form documented at greentextarchive.net.

How it works:
  - Module-level `_STREAM` holds the last N stories (in-memory).
  - `/api/greentext/tick/` generates ONE new story per call. The seed pool
    is life-event shaped (not gym-question shaped), so the model writes
    *about a thing that happened* rather than *answering a question*.
  - Each story is post-processed to enforce greentext shape: every line
    must start with `>`, lowercase, length 5-14 lines, no moral endings,
    no duplicate `>be me`, max one `>mfw`.
  - Falls back to a hand-written exemplar if the model output fails the
    shape check — keeps the wall from going empty.

State is in-memory and resets on server restart.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods


# --- state ----------------------------------------------------------------

@dataclass
class GreentextStory:
    post_no: int
    anon_variant: str
    body: str           # full greentext, lines joined by \n, each starting with >
    timestamp: float
    seed_topic: str
    line_count: int


_LOCK = threading.Lock()
_STREAM: dict = {
    "stories": [],      # list[GreentextStory], most recent first
    "next_post_no": 77260000 + random.randint(0, 9_999),
    "started_at": time.time(),
}

MAX_STORIES = 12


# --- seed pool: life-event topics ----------------------------------------

# Seeds are framed as MEMORIES — "the time X happened" — not questions.
# Each is a topic the model writes a greentext story ABOUT.
_TOPIC_SEEDS: list[str] = [
    # /fit/ specific — gym social misfires
    "first time walking into planet fitness, 6am, no idea what i'm doing",
    "the day a girl asked me to spot her and i panicked",
    "got mogged by a 6'3 lean kid at the bus stop in october",
    "the wedding where my cousin called me out for being skinnyfat",
    "mom found my creatine tub and thought it was steroids",
    "tren rage incident at the family dinner table",
    "wife caught me flexing in the bathroom mirror for the third time",
    "first 1pl8 bench after eight months of trying",
    "locker room comparison day, post-shower, no towel",
    "missed the gym four days, scale moved the wrong way",
    "spreadsheet said i gained 0.4kg in three weeks",
    # /fit/-adjacent — body image / lifestyle
    "the day i wore a fitted shirt to work for the first time",
    "tinder match said i looked taller in my pictures",
    "beach day, no shirt, family reunion, age 24",
    "tried to bench press my crush at a party in 2019, dropped her",
    "the cut that ruined christmas",
    "got recognized by a guy from my old gym at a wedding",
    "the night i ate a whole rotisserie chicken on omad",
    "found my old high school photos, was actually mogging",
    # cringe / autism shapes
    "the time i made eye contact with a mirror, thought it was a guy",
    "tried to ask the gym front desk girl about protein powder",
    "first day back at the gym after a year off, wore the old clothes",
    "told the doctor i lift, he said yeah i can tell",
    "the day i realized my form had been wrong for three years",
    "tried intermittent fasting, fainted on the bus home",
    # comfy / wholesome /tfw/ shapes
    "the rainy sunday morning where i actually wanted to go to the gym",
    "found an old workout journal from when i was 19",
    "spotted a kid struggling with the bench, helped him out",
    "the empty 5am gym, just me and the squat rack",
    # absurdist / short
    "be me, gym sock supervisor, in charge of making sure the socks are paired",
    "be me, ordered the wrong size shirt online, it's a crop top now",
]


# --- few-shot exemplars (also used as fallbacks) -------------------------

_FEWSHOT_STORIES: list[str] = [
    # /fit/ cringe — gym social misfire
    (">be me\n"
     ">dyel skellington, 5'9, 145lb\n"
     ">first day back at the gym after deload week\n"
     ">see mirin qt at the squat rack\n"
     ">load the bar with 1pl8, look casual\n"
     ">she racks 1.5pl8 next to me\n"
     ">try to make eye contact in the mirror\n"
     ">mirror is on her side\n"
     ">just see myself looking at her looking at me looking\n"
     ">leave through the back door\n"
     ">walk home in the rain"),

    # comfy /tfw
    (">be me\n"
     ">22, broke, no gf\n"
     ">5am, gym is empty\n"
     ">just me and the squat rack\n"
     ">load the bar, no music\n"
     ">hear the heater click on\n"
     ">tfw the bar feels lighter than yesterday\n"
     ">walk out at sunrise\n"
     ">coffee at the gas station, two creams\n"
     ">today is going to be ok"),

    # absurdist short
    (">be me\n"
     ">gym sock supervisor\n"
     ">in charge of making sure the socks are paired\n"
     ">go down to the laundry room\n"
     ">socks are unpaired\n"
     ">how do i make them paired again"),
]


# --- generation ----------------------------------------------------------

def _pool_keys() -> list[str]:
    from ..imageboard_ingestion import anon_zoo
    return list(anon_zoo.VARIANTS.keys())


def _pick_story_variant(rng: random.Random) -> str:
    """Weight toward narrators who carry confession-shaped stories well."""
    weights = {
        "failed_discipline_anon": 4,
        "regression_lifter_anon": 3,
        "mirror_dysmorphia_anon": 4,
        "clinical_cutter_anon": 2,
        "status_mogging_anon": 3,
        "quiet_routine_anon": 3,
        "blackpilled_lifter_anon": 2,
        "sincerity_poster_anon": 2,
        "bait_poster_anon": 1,
        "loud_mocker_anon": 1,
        "ngmi_doomer_anon": 2,
        "technical_helper_anon": 1,
    }
    keys = _pool_keys()
    pool = [k for k in keys if k in weights]
    w = [weights[k] for k in pool]
    return rng.choices(pool, weights=w, k=1)[0]


_MORAL_PHRASES_RE = re.compile(
    r"\b(?:and that'?s when i (?:learned|realized|knew)|"
    r"moral of the story|the lesson (?:here|is)|"
    r"i'?ve learned (?:that|to)|"
    r"taught me (?:that|to)|"
    r"now i know that|"
    r"now i understand|"
    r"i (?:realized|understood) (?:that |then ))",
    re.I,
)


def _enforce_greentext_shape(text: str) -> Optional[str]:
    """Coerce model output into greentext shape, or return None if hopeless.

    Rules:
      - Every line must start with `>`. Lines that don't are dropped UNLESS
        they're the only content; if dropping them leaves <5 lines we bail.
      - Lowercase the text (except preserve `MFW` / `TFW` caps as the spec).
      - Cap at 16 lines, require min 5.
      - First line should be `>be me` (or close); if not we don't force it
        but reject if there's NO `>be` anywhere.
      - Reject if `>be me` appears 2+ times mid-story.
      - Reject if `>mfw` appears 3+ times.
      - Reject if any moral-shaped phrase appears anywhere.
    """
    if not text:
        return None
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"^(?:user|anon|assistant|system|board|mode|task|story)\s*[:=].*$",
                  "", text, flags=re.I | re.M)
    # Strip leading code fences if present
    text = re.sub(r"^```\w*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    # Per-line filter + within-story dedup (catches LoRA mode-collapse loops
    # like the same beat repeated 12 times)
    raw_lines = [l.rstrip() for l in text.splitlines()]
    kept = []
    seen_norms: set[str] = set()
    seen_count_total = 0
    for l in raw_lines:
        ls = l.lstrip()
        if not ls:
            continue
        if not ls.startswith(">"):
            if ls.startswith(("-", "*", "•")):
                ls = ">" + ls[1:].lstrip()
            else:
                continue
        if ls.startswith(">>"):
            continue
        if not re.match(r"^>\s*\S", ls):
            continue
        # Lowercase, preserve MFW/TFW caps
        line_lower = ls.lower()
        if "MFW" in ls:
            line_lower = line_lower.replace("mfw", "MFW")
        if "TFW" in ls:
            line_lower = line_lower.replace("tfw", "TFW")
        # Within-story dedup: normalize (strip punctuation AND ordinal/
        # numeric tokens so "become father of N daughter" collapses across
        # N=second/third/fourth/...)
        norm = re.sub(r"[^\w\s']", " ", line_lower.lower())
        norm = re.sub(r"\s+", " ", norm).strip()
        # Strip ordinal/number tokens for the comparison key
        _ORDINAL_RE = re.compile(
            r"\b(?:\d+|first|second|third|fourth|fifth|sixth|seventh|"
            r"eighth|ninth|tenth|eleventh|twelfth|thirteenth|fourteenth|"
            r"fifteenth|sixteenth|seventeenth|eighteenth|nineteenth|twentieth|"
            r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b"
        )
        cmp_norm = _ORDINAL_RE.sub("N", norm)
        seen_count_total += 1
        if cmp_norm and cmp_norm in seen_norms:
            continue
        # Prefix collapse: any prior norm shares first 4+ leading tokens
        # AND the line lengths are comparable (within 50%).
        is_prefix_dupe = False
        candidate_words = cmp_norm.split()
        if len(candidate_words) >= 4:
            head4 = " ".join(candidate_words[:4])
            for prior in seen_norms:
                pw = prior.split()
                if len(pw) >= 4 and " ".join(pw[:4]) == head4:
                    if 0.5 <= len(candidate_words) / max(len(pw), 1) <= 2.0:
                        is_prefix_dupe = True
                        break
        if is_prefix_dupe:
            continue
        seen_norms.add(cmp_norm)
        kept.append(line_lower)
    # Drop the dedup-ratio gate. The pattern-collapse handler below dedups
    # repeats; we just need enough UNIQUE beats to make a story.
    if len(kept) < 6:
        return None
    # Pattern-collapse: if any 2-word line-prefix appears in 3+ lines, drop
    # the duplicates beyond the first occurrence (keep one as the original
    # beat). Previous version rejected the entire story which was too harsh
    # for v2 outputs that have a few repeats embedded in otherwise-good arcs.
    from collections import Counter
    prefix_counts: Counter = Counter()
    line_prefixes = []
    for l in kept:
        words = re.sub(r"[^\w\s]", " ", l.lower()).split()
        words = [w for w in words if w and w != ">"]
        prefix = (words[0], words[1]) if len(words) >= 2 else None
        line_prefixes.append(prefix)
        if prefix:
            prefix_counts[prefix] += 1
    if prefix_counts and max(prefix_counts.values()) >= 3:
        # Drop the 2nd+ occurrences of any collapse-pattern prefix.
        seen_prefix: set = set()
        new_kept = []
        for line, prefix in zip(kept, line_prefixes):
            if prefix and prefix_counts[prefix] >= 3:
                if prefix in seen_prefix:
                    continue
                seen_prefix.add(prefix)
            new_kept.append(line)
        kept = new_kept
        if len(kept) < 5:
            return None
    # Trim trailing incomplete line UNCONDITIONALLY (was gated by len>5,
    # which left "she also" / "she starts" in 5-line stories). If trim takes
    # us below 5 lines, reject below.
    while kept:
        tail = kept[-1].rstrip()
        last_tokens = tail.split()
        last_word = last_tokens[-1].rstrip(",;:.").lower() if last_tokens else ""
        incomplete = (
            tail.endswith(('"', "'", ",", "—", "-", "(", "the", "a", "an",
                           "and", "but", "or", "to", "of", "with", "in",
                           "for", "from", "be", "is", "are", "she", "he",
                           "they", "we", "i", "my", "her", "his"))
            or len(last_word) < 5
            or last_word.endswith("'")
            or last_word in ("didn", "couldn", "wouldn", "shouldn", "wasn",
                              "isn", "aren", "weren", "doesn", "haven", "hasn",
                              "also", "very", "even", "still", "just")
        )
        if not incomplete:
            break
        kept.pop()
    if len(kept) < 6:
        return None
    # Fuzzy within-story dedup: drop any line whose content-word set overlaps
    # >65% with any prior kept line. Catches near-duplicates like "she said X"
    # + "she also said X" that the exact-match dedup misses.
    def _content_words(line: str) -> set:
        toks = set(re.findall(r"[a-z]{3,}", line.lower()))
        return toks - {"the", "and", "but", "for", "with", "she", "her",
                       "his", "him", "they", "them", "this", "that", "was",
                       "were", "are", "you", "your", "from", "have", "has",
                       "had", "been", "also", "even", "very", "still"}
    deduped = []
    prior_sets = []
    for line in kept:
        cw = _content_words(line)
        near_dup = False
        if len(cw) >= 3:
            for pw in prior_sets:
                if pw and len(cw & pw) / max(len(cw | pw), 1) > 0.75:
                    near_dup = True
                    break
        if near_dup:
            continue
        deduped.append(line)
        prior_sets.append(cw)
    kept = deduped
    if len(kept) < 6:
        return None
    kept = kept[:16]
    body = "\n".join(kept)
    # Quality gates
    if _MORAL_PHRASES_RE.search(body):
        return None
    if sum(1 for l in kept if l.startswith(">be me")) > 1:
        return None
    if sum(1 for l in kept if l.lower().startswith(">mfw")) > 2:
        return None
    # Reject prose-disguised-as-greentext: any line >180 chars OR a line
    # that contains 3+ sentence-terminating periods.
    for l in kept:
        if len(l) > 180:
            return None
        if l.count(". ") >= 3:
            return None
    # Punchline-shape gate: last line MUST start with a punchline opener OR
    # end with a question mark, AND be ≤120 chars. With best-of-N, this
    # rejects soft endings; the LoRA gets multiple shots to land one.
    last = kept[-1].lower().strip()
    _punchline_starts = (
        ">mfw", ">tfw", ">still", ">just", ">kek", ">now", ">i ", ">i'",
        ">im", ">i'm", ">nobody", ">fucking", ">fuck", ">guess", ">maybe",
        ">turns out", ">she", ">he", ">they", ">it was", ">it's", ">its",
        ">she said", ">he said", ">walk home", ">go home", ">go back",
        ">leave", ">delete", ">oh ", ">oh shit", ">oh fuck", ">oh god",
        ">so ", ">wat", ">what now", ">now what", ">my body", ">my life",
        ">never", ">no ", ">not ", ">why", ">how do i",
        ">all i", ">all that", ">end up", ">ended up", ">to this day",
        ">there is", ">there was", ">there's", ">finally", ">eventually",
        ">decide", ">give up", ">stop", ">close", ">all over",
    )
    # Punchline gate: ≤140 chars AND opener-match or short question.
    is_punchline = (len(last) <= 140 and (
        last.endswith("?") or
        any(last.startswith(op) for op in _punchline_starts)
    ))
    if not is_punchline:
        return None
    # Multi-protagonist drift reject: stories where multiple distinct
    # "my X" subjects appear lose focus (user feedback: "is the man he meets
    # or his parents — pick ONE point per story").
    body_lower = body.lower()
    multi_subjects = sum(1 for p in (
        "my parents", "my brother", "my sister", "my cousin", "my coworker",
        "my dad and", "my mom and", "we both", "we all", "they both",
        "they all", "and my dad", "and my mom", "and my wife",
    ) if p in body_lower)
    if multi_subjects >= 2:
        return None
    # Multi-scene drift reject: 4+ distinct settings in one story signals
    # the model lost focus.
    settings_hit = sum(1 for s in (
        "gym", "wedding", "bus stop", "school", "work", "office",
        "library", "beach", "park", "kitchen", "bedroom", "shower",
        "locker", "doctor", "bathroom", "car",
    ) if s in body_lower)
    if settings_hit >= 5:
        return None
    return body


def _normalize_for_compare_simple(s: str) -> str:
    """Strip punctuation+whitespace for line-vs-line equality testing."""
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


_PUNCHLINE_OPENERS = (
    ">mfw", ">tfw", ">still", ">just", ">kek", ">now", ">i ",
    ">nobody", ">fucking", ">fuck", ">guess", ">maybe", ">turns out",
    ">she", ">he", ">it was", ">it's", ">she said", ">he said",
)


def _is_punchline_shaped(last_line: str) -> bool:
    """Last line is a /fit/-style punchline if it's short AND starts with
    one of the punchline opener tokens OR is a question."""
    if not last_line:
        return False
    ll = last_line.strip().lower()
    if len(ll) > 120:
        return False
    if ll.endswith("?"):
        return True
    return any(ll.startswith(op) for op in _PUNCHLINE_OPENERS)


def _generate_punchline(body: str, seed_topic: str, variant_key: str,
                        rng: random.Random) -> Optional[str]:
    """Second pass: ask the model to write ONE final `>` line that lands
    the joke. Self-deprecating, anti-climactic, OR callback to a detail
    above. /fit/ doesn't moralize — the punchline is the failure itself,
    the silent realization, or the ironic deflation."""
    from ..imageboard_ingestion import anon_zoo, local_fit_model

    variant = anon_zoo.get(variant_key)
    persona_system = (
        "You write ONE punchline line for a /fit/ greentext story. "
        "Output: a SINGLE line beginning with `>`. No preamble. No multiple "
        "lines. Max 12 words. Lowercase. No period.\n\n"
        "GOOD punchline shapes for /fit/:\n"
        "  - self-deprecating callback: `>still no gf`, `>still dyel`, "
        "`>still walking home in the rain`\n"
        "  - mfw realization: `>mfw she meant the other guy`, "
        "`>mfw the mirror was the door`\n"
        "  - anti-climax: `>nobody noticed`, `>nothing happened`, "
        "`>went home and ate ice cream`\n"
        "  - ironic deflation: `>i was the manlet all along`, "
        "`>turns out it was just me`\n"
        "  - silent question: `>was that the joke`, `>am i the npc`\n"
        "  - callback to a concrete object in the story (the 1pl8, the bus stop, "
        "the spreadsheet) — re-using that exact object as the punchline noun\n\n"
        "DO NOT write: 'and that's when i learned', 'the lesson is', "
        "'i realized that...', anything explaining the joke, anything "
        "moralizing, anything wholesome (unless the story is comfy). "
        "DO NOT write more than one line."
    )
    if variant:
        persona_system += "\n\n" + variant.system_card_addendum

    user_prompt = (
        f"Greentext story so far:\n{body}\n\n"
        f"Topic: {seed_topic}\n\n"
        "Write ONE final `>` line — the punchline. Just the one line, nothing else."
    )
    msgs = [
        {"role": "system", "content": persona_system},
        {"role": "user", "content": user_prompt},
    ]
    req = local_fit_model.build_request(
        messages=msgs, system=persona_system, user=user_prompt,
        profile="chat", max_tokens=40, repetition_penalty=1.15,
    )
    res = local_fit_model.generate(req)
    text = (res.text or "").strip()
    if not text:
        return None
    # Take just the first non-empty `>` line
    for raw in text.splitlines():
        l = raw.strip()
        if not l:
            continue
        if l.startswith(">>"):
            continue
        if not l.startswith(">"):
            # Convert bare line to greentext if it's clearly content
            l = ">" + l
        # Reject if it looks moralizing
        if _MORAL_PHRASES_RE.search(l):
            continue
        # Strip trailing period
        if l.endswith("."):
            l = l[:-1]
        if len(l) > 120:
            continue
        return l.lower()
    return None


def _generate_greentext_story(seed_topic: str, variant_key: str,
                              rng: random.Random,
                              recent_bodies: Optional[list[str]] = None) -> Optional[str]:
    """Generate one greentext story. Returns the formatted body or None.

    Pipeline: body pass → shape enforcement → cross-story dedup →
    punchline pass (if existing last line isn't punchline-shaped)."""
    from ..imageboard_ingestion import local_fit_model

    # System prompt carries ALL the constraints. User prompt is JUST
    # `topic: X` to match the training format and avoid prompt-leakage
    # (the LoRA over-fit on the simple training prompt copies any extra
    # instructions verbatim into the body).
    persona_system = (
        "You are anon writing a greentext STORY for /fit/. "
        "Output is a 4chan greentext: 6-10 lines, EVERY line begins with `>`. "
        "Lines are FRAGMENTS, not sentences. All lowercase. No periods. "
        "Slang ok: mfw, tfw, kek, desu — sparingly, max one `>mfw` per story. "
        "Ritual opener: `>be me` then `>be [age or role]` then setting. "
        "Each middle line is a CUT in time, one beat (action, sensation, micro-event). "
        "ONE place. ONE moment. ONE turn. SINGLE PROTAGONIST (just me/anon — "
        "not 'we' or 'my parents'). Do NOT drift across multiple scenes. "
        "Last line is the PUNCHLINE — short, self-deprecating, anti-climactic, "
        "callback, or `>mfw` realization that FLIPS the meaning. Never moralize. "
        "The narrator is the butt of the joke. Concrete details over generic ones. "
        "Output ONLY the greentext lines. Do NOT echo this instruction text."
    )

    user_prompt = f"topic: {seed_topic}"

    msgs = [
        {"role": "system", "content": persona_system},
        {"role": "user", "content": user_prompt},
    ]
    req = local_fit_model.build_request(
        messages=msgs, system=persona_system, user=user_prompt,
        profile="chat", max_tokens=380, repetition_penalty=1.30,
    )
    res = local_fit_model.generate(req)
    text = (res.text or "").strip()
    if not text:
        return None
    shaped = _enforce_greentext_shape(text)
    if not shaped:
        return None

    # Cross-story dedup — compare head (first 3 lines) AND punchline (last line)
    # against recent bodies. Use NORMALIZED match (strip punctuation/case)
    # so near-identical heads with minor differences don't slip through.
    lines_now = [l for l in shaped.splitlines() if l.strip()]
    def _norm(s):
        return re.sub(r"\W+", " ", (s or "").lower()).strip()
    head_norm = _norm("\n".join(lines_now[:3]))
    tail_norm = _norm(lines_now[-1]) if lines_now else ""
    # Also check first-2-lines for opener pattern reuse
    head2_norm = _norm("\n".join(lines_now[:2]))
    for prior in (recent_bodies or []):
        prior_lines = [l for l in (prior or "").splitlines() if l.strip()]
        prior_head_norm = _norm("\n".join(prior_lines[:3]))
        prior_head2_norm = _norm("\n".join(prior_lines[:2]))
        prior_tail_norm = _norm(prior_lines[-1]) if prior_lines else ""
        if prior_head_norm and prior_head_norm == head_norm:
            return None
        # First-2-line opener match (e.g. "be me 23 / i live in small city" reused)
        if prior_head2_norm and prior_head2_norm == head2_norm and len(head2_norm) >= 30:
            return None
        if prior_tail_norm and prior_tail_norm == tail_norm and len(tail_norm) >= 12:
            return None

    # Punchline pass disabled. The v2/v4 LoRAs over-fit to "always output
    # `>be me`" — when asked for a single punchline line, they regenerate
    # the standard opener instead of writing a closing beat. The dup-check
    # below correctly rejected those, but the pass became dead weight.
    # The v4 LoRA was trained on punchline-only stories, so it should
    # produce decent endings in the body pass.
            shaped = "\n".join(lines_now)

    return shaped


def _new_post_no() -> int:
    n = _STREAM["next_post_no"]
    _STREAM["next_post_no"] = n + random.randint(3, 17)
    return n


# --- inline quality scoring (for best-of-N selection) -------------------

def _score_candidate(body: str, seed: str) -> tuple[float, str]:
    """Cheap inline scorer used to pick the best of N candidates. Mirrors
    the eval-time rubric's biggest signals (punchline, coherence, repetition,
    voice). Returns (score, reason) — score is 0-30 (3 dims × 10)."""
    lines = [l.strip() for l in body.splitlines() if l.strip().startswith(">")]
    if not lines:
        return 0.0, "no_lines"

    # Punchline (0-10). Reward SHORT + opener-match heavily; longer
    # continuations get less credit even if they technically open with a
    # punchline word.
    last = lines[-1].lower()
    pl = 3.0
    strong_short_openers = (">mfw", ">tfw", ">still", ">kek", ">i'm",
                            ">im", ">i ", ">she said", ">he said")
    if len(last) <= 60 and any(last.startswith(op) for op in strong_short_openers):
        pl += 6.0  # tight punchline
    elif len(last) <= 80 and any(last.startswith(op) for op in _PUNCHLINE_OPENERS):
        pl += 4.0
    elif len(last) <= 120 and any(last.startswith(op) for op in _PUNCHLINE_OPENERS):
        pl += 2.0
    elif last.endswith("?") and len(last) <= 80:
        pl += 4.0
    if re.search(r"\b(?:moral|lesson|learned|realized that)\b", last):
        pl -= 3.0

    # Coherence (0-10): on-topic + line variety
    seed_tokens = set(re.findall(r"[a-z]+", seed.lower())) - {
        "the", "a", "an", "to", "of", "in", "on", "at", "for", "be", "me"
    }
    body_text = " ".join(lines).lower()
    body_tokens = set(re.findall(r"[a-z]+", body_text))
    overlap = (len(seed_tokens & body_tokens) / max(len(seed_tokens), 1)) if seed_tokens else 0
    co = 6.0
    if overlap >= 0.4: co += 3.0
    elif overlap >= 0.2: co += 1.5
    first_words = [(_normalize_for_compare_simple(l).split() or [""])[0] for l in lines]
    if first_words and (len(set(first_words)) / len(first_words)) >= 0.5:
        co += 1.0
    co = min(10.0, co)

    # Voice authenticity (0-10): /fit/ markers + concrete numbers/objects.
    # /fit/ slang (dyel, mog, mfw, tfw, etc.) is worth MORE than generic
    # fitness words (gym, lift) — the slang is the actual voice tell.
    va = 4.0
    slang_strong = ("dyel", "mog", "mogged", "mirin", "manlet", "chad", "qt",
                    "skellington", "skinnyfat", "tfw", "mfw", "kek", "ngmi",
                    "wagmi", "tren", "1pl8", "2pl8", "3pl8", "natty", "roid",
                    "gomad")
    slang_hits = sum(1 for m in slang_strong if re.search(rf"\b{m}\b", body_text))
    va += min(4.0, slang_hits * 1.5)
    generic_markers = ("gym", "lift", "bench", "squat", "deadlift", "mirror",
                       "wife", "gf", "girlfriend", "mom", "dad", "scale",
                       "creatine", "protein", "cycle", "bulk", "cut",
                       "rack", "bar", "shorts", "tank", "locker", "shower",
                       "5am", "6am")
    generic_hits = sum(1 for m in generic_markers if re.search(rf"\b{m}\b", body_text))
    va += min(2.0, generic_hits * 0.4)
    # Reward concrete numbers (heights, weights, ages, rep counts)
    if re.search(r"\b\d+(?:lbs|kg|pl8|rep|reps|kcal)\b", body_text):
        va += 1.0
    if re.search(r"\b\d'\d{1,2}\b", body_text):  # height like 5'10
        va += 0.5
    # Penalize coach/therapy register
    if re.search(r"\b(?:keep going|stay strong|you can do it|trust the process|"
                 r"believe in yourself|step at a time)\b", body_text):
        va -= 3.0
    va = max(0.0, min(10.0, va))

    total = pl + co + va
    return total, f"pl={pl:.1f} co={co:.1f} va={va:.1f}"


def _generate_best_of_n(seed_topic: str, variant_key: str,
                        rng: random.Random, n: int,
                        recent_bodies: Optional[list[str]] = None) -> Optional[str]:
    """Generate N candidate stories, score each, return the best.

    This is the highest-leverage quality move: the LoRA is non-deterministic,
    so generating multiple candidates and picking the best by punchline +
    coherence + voice gives a much higher quality floor than single-shot."""
    best_body = None
    best_score = -1.0
    best_reason = ""
    for i in range(n):
        body = _generate_greentext_story(seed_topic, variant_key, rng,
                                         recent_bodies=recent_bodies)
        if not body:
            continue
        score, reason = _score_candidate(body, seed_topic)
        if score > best_score:
            best_score = score
            best_body = body
            best_reason = reason
    return best_body


# --- tick ----------------------------------------------------------------

def greentext_tick(rng: Optional[random.Random] = None) -> dict:
    """Single tick: produce one new greentext story and prepend to the stream."""
    rng = rng or random.Random()
    with _LOCK:
        recent_bodies = [s.body for s in _STREAM["stories"][:6]]
    seed_topic = rng.choice(_TOPIC_SEEDS)
    variant_key = _pick_story_variant(rng)
    # Best-of-5: generate five candidates, score, return best. 7 was too
    # memory-heavy on 7B + crashed the server.
    body = _generate_best_of_n(seed_topic, variant_key, rng, n=5,
                               recent_bodies=recent_bodies)
    if not body:
        return {"action": "noop", "reason": "generation_failed"}
    line_count = len([l for l in body.splitlines() if l.strip()])
    post_no = _new_post_no()
    story = GreentextStory(
        post_no=post_no, anon_variant=variant_key, body=body,
        timestamp=time.time(), seed_topic=seed_topic, line_count=line_count,
    )
    with _LOCK:
        _STREAM["stories"].insert(0, story)
        if len(_STREAM["stories"]) > MAX_STORIES:
            _STREAM["stories"] = _STREAM["stories"][:MAX_STORIES]
    return {
        "action": "new_story",
        "post_no": post_no,
        "variant": variant_key,
        "line_count": line_count,
    }


# --- views ---------------------------------------------------------------

def _serialize_story(s: GreentextStory) -> dict:
    return {
        "post_no": s.post_no,
        "anon_variant": s.anon_variant,
        "body": s.body,
        "timestamp": s.timestamp,
        "seed_topic": s.seed_topic,
        "line_count": s.line_count,
    }


def _serialize_state() -> dict:
    with _LOCK:
        stories = list(_STREAM["stories"])
        return {
            "started_at": _STREAM["started_at"],
            "story_count": len(stories),
            "stories": [_serialize_story(s) for s in stories],
        }


def greentext_page(request):
    """Render the greentext story wall. JS polls /api/greentext/tick/."""
    response = render(request, "greentext.html", {})
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


@csrf_exempt
@require_http_methods(["POST"])
def greentext_tick_view(request):
    try:
        result = greentext_tick()
    except Exception as e:
        return JsonResponse({"error": f"{type(e).__name__}: {e}"}, status=500)
    return JsonResponse({"tick": result, "state": _serialize_state()})


@require_http_methods(["GET"])
def greentext_state_view(request):
    return JsonResponse(_serialize_state())


@csrf_exempt
@require_http_methods(["POST"])
def greentext_reset_view(request):
    """Clear the in-memory story stream. Useful when iterating on generation."""
    with _LOCK:
        _STREAM["stories"] = []
    return JsonResponse({"ok": True})
