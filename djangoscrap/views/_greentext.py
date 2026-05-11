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
    # If the raw output was mostly repeats (dedup ratio < 0.3), it was a
    # mode-collapse loop — reject entirely.
    if seen_count_total >= 6 and len(kept) / max(seen_count_total, 1) < 0.3:
        return None
    if len(kept) < 5:
        return None
    # Trim trailing incomplete line: ends with open quote, comma, "and"/"but",
    # or any token shorter than 3 chars (max_tokens cutoff signal).
    while len(kept) > 5:
        tail = kept[-1].rstrip()
        last_tokens = tail.split()
        last_word = last_tokens[-1] if last_tokens else ""
        incomplete = (
            tail.endswith(('"', "'", ",", "—", "-", "(", "the", "a", "an",
                           "and", "but", "or", "to", "of", "with", "in",
                           "for", "from", "be", "is", "are"))
            or len(last_word) < 3
            or last_word.endswith("'")
        )
        if not incomplete:
            break
        kept.pop()
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
    return body


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
    from ..imageboard_ingestion import anon_zoo, local_fit_model

    variant = anon_zoo.get(variant_key)
    persona_system = (
        "You are anon writing a greentext STORY for /fit/. "
        "Output is a 4chan greentext: 7-12 lines, EVERY line begins with `>`. "
        "Lines are FRAGMENTS, not sentences. All lowercase. No periods. "
        "Slang ok: mfw, tfw, kek, desu — sparingly, max one `>mfw` per story.\n\n"
        "Structure:\n"
        "  Line 1: `>be me`\n"
        "  Line 2: `>be [age or role]` (e.g. `>be 23`, `>dyel skellington`)\n"
        "  Line 3: setting (concrete: where/when/what state)\n"
        "  Middle: one beat per line — actions, sensations, micro-events. "
        "Each line is a CUT in time, not a sentence connector.\n"
        "  Last line: the PUNCHLINE — short, self-deprecating, anti-climactic, "
        "or a callback to a concrete object mentioned earlier. The joke is "
        "the failure or the silent realization, NEVER a moral.\n\n"
        "HUMOR REGISTER: 4chan /fit/ humor is self-deprecating. The narrator "
        "is the butt. The funny part is the gap between effort and outcome, "
        "the unspoken cringe, the realization that arrives one beat late. "
        "Don't sanitize. Don't telegraph the joke in line 2 — let it land at "
        "the end. Concrete details (1pl8, 6am, leather sofa, the bus stop "
        "with the kid in shorts) NOT generic ones (at the gym, one day, "
        "with a girl).\n\n"
        "No URLs. No `>>NNNNNNNN` reply refs. No labels, no headers, no "
        "preamble. Output ONLY the greentext lines.\n\n"
        "CRITICAL: never repeat a line. Never repeat a 6-word phrase. Each "
        "line is a NEW beat. If you start looping, STOP — short is fine.\n\n"
        "Do not inject demographic identity (race, nationality, sexual "
        "orientation) unless the seed topic explicitly mentions it. "
        "Mogging = visually overshadowed, NOT physically assaulted."
    )
    if variant:
        persona_system += "\n\n" + variant.system_card_addendum

    fewshot = (
        "Examples of greentext with LANDING punchlines (note how each "
        "final line is short, callback or anti-climax, never a moral):\n\n"
        "Example 1 (/fit/ cringe — punchline = silent retreat callback):\n"
        ">be me\n>dyel skellington, 5'9, 145lb\n"
        ">first day back at the gym after deload\n"
        ">see mirin qt at the squat rack\n"
        ">load 1pl8, look casual\n"
        ">she racks 1.5pl8 next to me\n"
        ">try eye contact in the mirror\n"
        ">mirror is on her side\n"
        ">just see myself watching her watch me\n"
        ">leave through the back door\n"
        ">still walking home in the rain\n\n"
        "Example 2 (absurdist — punchline = unanswered question):\n"
        ">be me\n>gym sock supervisor\n"
        ">in charge of making sure socks are paired\n"
        ">go down to the laundry room\n>socks are unpaired\n"
        ">how do i pair them again\n\n"
        "Example 3 (comfy — punchline = anti-climax acceptance):\n"
        ">be me\n>22, broke, no gf\n>5am, gym empty\n"
        ">load the bar, no music\n>hear the heater click on\n"
        ">tfw the bar feels lighter than yesterday\n"
        ">coffee at the gas station, two creams\n"
        ">today is going to be ok\n\n"
        "Example 4 (cringe — punchline = mfw realization):\n"
        ">be me\n>21, first cut, week 8\n>wife says i look the same\n"
        ">show her the spreadsheet, +0.4kg lean\n"
        ">she says 'thats the same'\n>show her the scale\n"
        ">she takes off her glasses\n>mfw she forgot her glasses today\n"
    )
    user_prompt = (
        f"{fewshot}\n"
        f"Now write a NEW greentext about this specific moment: {seed_topic}\n\n"
        "Rules: 7-12 lines, every line `>`, fragments only, lowercase. "
        "End with a SHORT punchline line — callback, mfw, anti-climax. "
        "Do NOT moralize. Output only the greentext lines."
    )
    msgs = [
        {"role": "system", "content": persona_system},
        {"role": "user", "content": user_prompt},
    ]
    req = local_fit_model.build_request(
        messages=msgs, system=persona_system, user=user_prompt,
        profile="chat", max_tokens=240, repetition_penalty=1.45,
    )
    res = local_fit_model.generate(req)
    text = (res.text or "").strip()
    if not text:
        return None
    shaped = _enforce_greentext_shape(text)
    if not shaped:
        return None

    # Cross-story dedup — compare head (first 3 lines) AND punchline (last line)
    # against recent bodies. Reject if either matches.
    lines_now = [l for l in shaped.splitlines() if l.strip()]
    head_now = "\n".join(lines_now[:3]).lower()
    tail_now = lines_now[-1].lower() if lines_now else ""
    for prior in (recent_bodies or []):
        prior_lines = [l for l in (prior or "").splitlines() if l.strip()]
        prior_head = "\n".join(prior_lines[:3]).lower()
        prior_tail = prior_lines[-1].lower() if prior_lines else ""
        if prior_head and prior_head == head_now:
            return None
        if prior_tail and prior_tail == tail_now and len(tail_now) >= 12:
            return None

    # Punchline pass — if existing last line isn't punchline-shaped, generate
    # a one-line punchline and append it (drop any existing weak final line
    # if there are still enough beats left).
    if not _is_punchline_shaped(lines_now[-1]):
        punchline = _generate_punchline(shaped, seed_topic, variant_key, rng)
        if punchline:
            # If body is already 11+ lines, replace the last (weak) line.
            # Otherwise append.
            if len(lines_now) >= 10:
                lines_now = lines_now[:-1] + [punchline]
            else:
                lines_now = lines_now + [punchline]
            shaped = "\n".join(lines_now)

    return shaped


def _new_post_no() -> int:
    n = _STREAM["next_post_no"]
    _STREAM["next_post_no"] = n + random.randint(3, 17)
    return n


# --- tick ----------------------------------------------------------------

def greentext_tick(rng: Optional[random.Random] = None) -> dict:
    """Single tick: produce one new greentext story and prepend to the stream."""
    rng = rng or random.Random()
    with _LOCK:
        recent_bodies = [s.body for s in _STREAM["stories"][:6]]
    seed_topic = rng.choice(_TOPIC_SEEDS)
    variant_key = _pick_story_variant(rng)
    body = _generate_greentext_story(seed_topic, variant_key, rng,
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
