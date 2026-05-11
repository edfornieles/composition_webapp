"""Self-generating /fit/ board simulator.

The artwork target: a /fit/ catalog that grows on screen as multiple anons
post threads and reply to each other. Different anons, different cadences,
greentext-quoted reply chains, the texture of a real board.

How it works:
  - Module-level `BOARD_STATE` holds the last N threads (in-memory).
  - `/api/fit/board-tick/` generates ONE new post per call. It either:
      * starts a new thread (~10% of ticks), or
      * adds a reply to an existing thread (~90%, weighted toward recent threads).
  - Each post draws a fresh anon variant from `anon_zoo`, so a thread is
    populated by many distinct registers (loud_mocker, technical_helper,
    ngmi_doomer, etc.). This is the polyphony that makes the board read as
    real even when any single post is mid-quality.
  - Replies pick a target post in the thread (OP-weighted, recent-weighted)
    and prepend `>>postnum` reply markers — the canonical board reply chain.

State is in-memory and resets on server restart. That's fine for an artwork
demo; durable storage can be added later.
"""
from __future__ import annotations

import datetime as _dt
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
class BoardPost:
    post_no: int
    thread_no: int
    is_op: bool
    anon_variant: str
    subject: str
    body: str
    timestamp: float
    reply_targets: list[int] = field(default_factory=list)   # post_nos this post replies to
    image_filename: str = ""
    image_size_kb: float = 0.0
    image_w: int = 0
    image_h: int = 0


@dataclass
class BoardThread:
    op: BoardPost
    replies: list[BoardPost] = field(default_factory=list)
    last_activity: float = 0.0

    @property
    def reply_count(self) -> int:
        return len(self.replies)

    @property
    def thread_no(self) -> int:
        return self.op.post_no


_LOCK = threading.Lock()
_BOARD: dict = {
    "threads": [],            # list[BoardThread], most recent first
    "next_post_no": 77254000 + random.randint(0, 9_999),  # plausible /fit/ range
    "started_at": time.time(),
}

MAX_THREADS = 8           # board catalog page ~size
MAX_REPLIES_PER_THREAD = 200
NEW_THREAD_PROBABILITY = 0.10  # 10% of ticks open a new thread


# --- OP topic seeds -------------------------------------------------------

# Hand-written OP seeds (defaults). At module-init we ALSO try to merge in
# scraped seeds from training/fit_lora/board_op_seeds.json if it exists —
# that file is produced by `scripts/scrape_fit_op_seeds.py` against the live
# /fit/ catalog and lets us refresh the seed pool without code changes.
# Each seed is (subject, opener_text, image_keyword).
_HAND_OP_SEEDS: list[tuple[str, str, str]] = [
    ("", "is creatine actually worth it or am i getting memed", "creatine_tub"),
    ("", "should i bulk or cut. 5'10 175lb ~16% bf", "scale"),
    ("", "missed the gym 4 days in a row. talk me back in", "gym_bag"),
    ("/fat/ - Fat Loss General", "down from 287 to 252. still high blood pressure. life is suffering", "scale"),
    ("", "got mogged at the bus stop today. 6'3 lean kid in shorts", "bus_stop"),
    ("", "first 1pl8 bench at 6 months. is this dyel territory", "bench"),
    ("", "deload week or push through", "barbell"),
    ("", "is it over for 5'7 manlets or what", "mirror"),
    ("", "tested 1RM. dropped 20kg from a year ago", "rack"),
    ("", "anyone else lift at 4am because nobody else is there", "alarm"),
    ("/run/ /roon/ - Running General", "Build up speed and mileage SLOOOOOWLY edition", "track"),
    ("", "wife says i look the same after 8 weeks. spreadsheet says +0.4kg", "spreadsheet"),
    ("", "first cycle. test only 500mg/wk. talk me out of it", "phone_glow"),
    ("", "thinking about quitting. 7 years of lifting and natty", "ceiling"),
    ("", "everyone in my office knows i lift now. i dont know how to feel", "locker"),
    ("/SIG/ - Self Improvement General", "It's so back edition. how have you improved this week", "sunrise"),
    ("", "is omad meme or based", "fridge"),
    ("", "front camera vs mirror. why do they disagree", "phone_glow"),
    ("", "anyone else just lift in silence now", "barbell"),
    ("", "on tren. wife thinks im on the spectrum. i kinda agree", "needle"),
]


def _load_scraped_op_seeds() -> list[tuple[str, str, str]]:
    """Merge JSON-scraped seeds with hand-written ones. Silent if absent."""
    try:
        from django.conf import settings  # type: ignore
        base = getattr(settings, "BASE_DIR", "")
    except Exception:
        base = ""
    candidates = [
        f"{base}/training/fit_lora/board_op_seeds.json" if base else None,
        "training/fit_lora/board_op_seeds.json",
    ]
    import os
    for p in candidates:
        if not p:
            continue
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            seeds = []
            for entry in data:
                if isinstance(entry, dict):
                    s = entry.get("subject", "") or ""
                    b = entry.get("body", "") or ""
                    k = entry.get("image_keyword", "thumb") or "thumb"
                    if b.strip():
                        seeds.append((s, b.strip(), k))
                elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    s, b = entry[0] or "", entry[1] or ""
                    k = entry[2] if len(entry) > 2 else "thumb"
                    if b.strip():
                        seeds.append((s, b.strip(), k))
            return seeds
        except Exception:
            continue
    return []


OP_SEEDS: list[tuple[str, str, str]] = _HAND_OP_SEEDS + _load_scraped_op_seeds()


# --- image-info stubs (cosmetic) -----------------------------------------

def _cosmetic_image(keyword: str) -> dict:
    """Return plausible /fit/-style file metadata. Cosmetic only — no actual
    file is served; the frontend renders a striped placeholder thumb."""
    return {
        "image_filename": f"{keyword}_{random.randint(1000, 9999)}.jpg",
        "image_size_kb": round(random.uniform(40, 1500), 2),
        "image_w": random.randint(480, 1300),
        "image_h": random.randint(480, 1500),
    }


# --- anon variant pool helpers -------------------------------------------

def _pool_keys() -> list[str]:
    from ..imageboard_ingestion import anon_zoo
    return list(anon_zoo.VARIANTS.keys())


def _pick_op_variant(rng: random.Random) -> str:
    """OPs come from anons most likely to start a thread — usually a
    confession-shaped or technical-shaped anon, less often a doomer."""
    weights = {
        "failed_discipline_anon": 3,
        "regression_lifter_anon": 3,
        "clinical_cutter_anon": 2,
        "status_mogging_anon": 3,
        "mirror_dysmorphia_anon": 2,
        "quiet_routine_anon": 2,
        "blackpilled_lifter_anon": 2,
        "technical_helper_anon": 2,
        "ngmi_doomer_anon": 1,
        "sincerity_poster_anon": 1,
        "bait_poster_anon": 2,
        "loud_mocker_anon": 1,
    }
    keys = _pool_keys()
    pool = [k for k in keys if k in weights]
    w = [weights[k] for k in pool]
    return rng.choices(pool, weights=w, k=1)[0]


def _pick_reply_variant(rng: random.Random, *, exclude: set[str]) -> str:
    """Replies are heavier on the reactive register — mockers, helpers, doomers."""
    weights = {
        "loud_mocker_anon": 4,
        "technical_helper_anon": 3,
        "ngmi_doomer_anon": 2,
        "blackpilled_lifter_anon": 2,
        "sincerity_poster_anon": 1,
        "bait_poster_anon": 2,
        "failed_discipline_anon": 2,
        "regression_lifter_anon": 2,
        "clinical_cutter_anon": 1,
        "status_mogging_anon": 2,
        "mirror_dysmorphia_anon": 1,
        "quiet_routine_anon": 1,
    }
    keys = [k for k in _pool_keys() if k in weights and k not in exclude]
    if not keys:
        keys = list(weights)
    w = [weights[k] for k in keys]
    return rng.choices(keys, weights=w, k=1)[0]


# --- generation -----------------------------------------------------------

_SCAFFOLD_LINE_RE = re.compile(
    r"^\s*(?:BOARD|MODE|TASK|TOPIC|SEED|PARENT[_ ]POST(?:[_ ]NO)?|OP[_ ]POST(?:[_ ]NO)?|"
    r"RECENT[_ ]REPLIES[_ ]IN[_ ]THREAD|user|assistant|system|anon)\s*[:=].*$",
    re.I,
)


def _strip_scaffold_lines(text: str) -> str:
    """Drop any line that looks like the prompt scaffolding leaking back
    into the output (`BOARD: /fit/`, `MODE: thread_op`, `TOPIC: ...`)."""
    if not text:
        return text
    kept = [l for l in text.splitlines() if not _SCAFFOLD_LINE_RE.match(l)]
    return "\n".join(kept).strip()


def _generate_op_body(seed_text: str, variant_key: str, rng: random.Random,
                      existing_ops: Optional[list[str]] = None) -> str:
    """Generate an OP body in the LoRA voice, using `seed_text` as the topic
    the post is *about* (not the literal body). Falls back to seed_text if
    generation fails or the output is unusable — keeps the artwork from
    going dark when the model has an off pass."""
    from ..imageboard_ingestion import anon_zoo, local_fit_model, output_validator

    existing_ops = existing_ops or []
    variant = anon_zoo.get(variant_key)
    persona_system = (
        "You are an anon STARTING a new /fit/ thread. You are NOT replying. "
        "You are NOT answering a question. You are POSTING — confessing, "
        "complaining, mocking, asking, ranting, in /fit/ voice. "
        "Write 1-3 short lines, board cadence, lowercase ok, abbreviations ok. "
        "No greentext reply quotes back to anyone (this IS the OP). "
        "No `>>NNNNNNNN` post refs. No URLs. "
        "No therapy register, no motivational coaching, no AI disclosures. "
        "Do NOT begin with 'Yes', 'No', 'Sure', 'Of course', 'I have a similar', "
        "'I have the same', 'That sounds', 'Great question' — those are answers, "
        "not OPs. Output the post body ONLY — no labels, no `BOARD:` / `MODE:` / "
        "`TOPIC:` headers, no role names, no quoting the prompt back."
    )
    if variant:
        persona_system += "\n\n" + variant.system_card_addendum

    # Few-shot: show the model real /fit/ OP shapes so it stops answering.
    fewshot = (
        "Examples of correct OP voice (DO NOT copy these verbatim, write a new one):\n"
        "  ex1: missed the gym 4 days in a row. talk me back in\n"
        "  ex2: got mogged at the bus stop today. 6'3 lean kid in shorts. lost it\n"
        "  ex3: is creatine actually worth it or am i getting memed\n"
        "  ex4: down from 287 to 252. still high blood pressure. life is suffering\n"
    )
    user_prompt = (
        f"{fewshot}\n"
        f"Now write a fresh /fit/ OP. The topic / mood seed: {seed_text}\n\n"
        "Output the post body only. 1-3 short lines. Anon voice, not answer voice."
    )
    msgs = [
        {"role": "system", "content": persona_system},
        {"role": "user", "content": user_prompt},
    ]
    req = local_fit_model.build_request(
        messages=msgs, system=persona_system, user=user_prompt,
        profile="chat", max_tokens=110, repetition_penalty=1.20,
    )
    res = local_fit_model.generate(req)
    text = (res.text or "").strip()
    if not text:
        return seed_text
    # Strip HTML / scaffold / labels / urls / stray postnos / truncation
    text = re.sub(r"<[^>]+>", "", text)
    text = _strip_scaffold_lines(text)
    text = re.sub(r"^(?:user|anon|assistant|board|mode|task|op)\s*:\s*", "",
                  text, flags=re.I | re.M)
    text = _URL_RE.sub("", text)
    text = _strip_stray_postnos(text)
    # Drop any line that begins with >>NNN (OPs don't reply-ref)
    text = "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith(">>"))
    text = _trim_truncated_tail(text)
    # De-echo against currently-open OPs to avoid two threads on the same line
    text = _strip_thread_echo(text, existing_ops)
    # Cap to 4 lines / 280 chars (OPs are short); re-trim after cap to avoid
    # mid-word cut introduced by the slice.
    lines = [l.strip() for l in text.splitlines() if l.strip()][:4]
    text = "\n".join(lines).strip()[:280].rstrip()
    text = _trim_truncated_tail(text)
    if not text:
        return seed_text
    if _has_yeah_loop(text):
        return seed_text
    # Reject Q→A shaped openings ("Yes, I have a similar experience.")
    _first_lower = text.lstrip().lower()
    _answer_starts = (
        "yes,", "yes ", "no,", "no ", "sure,", "sure ", "of course",
        "i have a similar", "i have the same", "that sounds",
        "great question", "good question", "yes i", "no i",
        "you have to", "you should", "you can",
    )
    if any(_first_lower.startswith(s) for s in _answer_starts):
        return seed_text
    v = output_validator.validate(text, recent_outputs=existing_ops)
    if not v.get("ok") and v.get("severity") == "hard":
        return seed_text
    return v.get("repaired_text") or text


_REPLY_REF_PREFIX_RE = re.compile(r"^\s*(>>\d+)\s*[,:.\-—]?\s*")


def _split_reply_ref(line: str) -> tuple[str, str]:
    """Split a line into (>>NNN-prefix-or-empty, remainder). The model often
    packs the entire echoed body onto the same line as the reply ref
    (">>77261839, it's fine to lift heavy stuff"), so we need to peel the
    ref off before checking the remainder for echoes."""
    m = _REPLY_REF_PREFIX_RE.match(line)
    if not m:
        return "", line
    return m.group(1), line[m.end():]


def _normalize_for_compare(s: str) -> str:
    """Strip emoji/punctuation/whitespace for echo comparison so '🤔it's fine'
    matches "it's fine"."""
    s = re.sub(r"[^\w\s']", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _sentence_norms(body: str) -> list[str]:
    """Split a body into normalized sentences for echo comparison."""
    out = []
    for line in (body or "").splitlines():
        _, rest = _split_reply_ref(line)
        for sent in _SENTENCE_SPLIT_RE.split(rest):
            n = _normalize_for_compare(sent)
            if n and len(n) >= 20:
                out.append(n)
    return out


def _shoulder(n: str, k: int = 5) -> tuple[str, str]:
    """Return (first-k-words, last-k-words) of a normalized sentence —
    used to catch template-collapse where opener+closer match but middle
    varies (e.g. 'Everyone who tells me that X is either lying or doesn't
    understand what he means', repeated with different X)."""
    words = n.split()
    if len(words) < k * 2 + 1:
        return "", ""
    return " ".join(words[:k]), " ".join(words[-k:])


def _strip_thread_echo(text: str, prior_bodies: list[str]) -> str:
    """Drop sentences that echo any prior post in the thread. Sentence-level
    granularity catches the common LoRA failure where two replies share the
    first sentence but diverge in the second — line-level checks missed
    those because the whole-line normalized strings differed."""
    if not text or not prior_bodies:
        return text
    prior_norms: set[str] = set()
    prior_shoulders: set[tuple[str, str]] = set()
    for body in prior_bodies:
        for n in _sentence_norms(body):
            prior_norms.add(n)
            head, tail = _shoulder(n)
            if head and tail:
                prior_shoulders.add((head, tail))
    seen_in_reply: set[str] = set()
    kept_lines = []
    for line in text.splitlines():
        ref, rest = _split_reply_ref(line)
        if not rest.strip():
            kept_lines.append(line)
            continue
        sentences = _SENTENCE_SPLIT_RE.split(rest)
        kept_sents = []
        for sent in sentences:
            sent_stripped = sent.strip()
            if not sent_stripped:
                continue
            n = _normalize_for_compare(sent_stripped)
            if len(n) < 20:
                kept_sents.append(sent_stripped)
                continue
            if n in prior_norms or n in seen_in_reply:
                continue
            # Substring against prior (catches prefix-extension echoes)
            is_echo = False
            for pn in prior_norms:
                if len(pn) >= 30 and (pn in n or n in pn):
                    is_echo = True
                    break
            # Shoulder match: same opener AND closer => template collapse
            if not is_echo:
                head, tail = _shoulder(n)
                if head and tail and (head, tail) in prior_shoulders:
                    is_echo = True
            if is_echo:
                continue
            seen_in_reply.add(n)
            kept_sents.append(sent_stripped)
        if kept_sents:
            body = " ".join(kept_sents)
            kept_lines.append((ref + " " + body).strip() if ref else body)
        elif ref:
            kept_lines.append(ref)
    return "\n".join(kept_lines).strip()


_URL_RE = re.compile(r"https?://\S+", re.I)


def _strip_fake_urls(text: str) -> str:
    """The base model loves hallucinating youtube URLs. Strip them — real
    /fit/ anons rarely post raw links and never made-up ones."""
    return _URL_RE.sub("", text or "").strip()


_SENTENCE_TERMINATORS = ".!?\""
_VALID_TRAILERS = (".", "!", "?", "lol", "lmao", "kek", "based", "ngmi", "dyel", ":(", ":)", "...", "imo")


def _trim_truncated_tail(text: str) -> str:
    """If the reply ends mid-word (max_tokens cutoff), drop the last
    fragment back to the last sentence terminator."""
    if not text:
        return text
    t = text.rstrip()
    if not t:
        return t
    last_word = t.split()[-1].lower().rstrip(",;:")
    if t[-1] in _SENTENCE_TERMINATORS or last_word in _VALID_TRAILERS:
        return t
    # Find last sentence terminator
    for i in range(len(t) - 1, -1, -1):
        if t[i] in ".!?":
            return t[: i + 1]
    # No terminator found at all — return whole thing rather than wipe it
    return t


_STRAY_POSTNO_RE = re.compile(r"(?:(?<=^)|(?<=[\s.,!?]))(?<!>)(?<!>>)\b\d{7,10}\b")


def _strip_stray_postnos(text: str) -> str:
    """Strip bare 7-10 digit runs that aren't preceded by `>>`. The model
    sometimes leaks raw post numbers into the body (e.g. '77256186 yeah but
    it's not creatine')."""
    if not text:
        return text
    out_lines = []
    for line in text.splitlines():
        # Preserve lines that are proper >>NNN refs
        if line.lstrip().startswith(">>"):
            out_lines.append(line)
            continue
        out_lines.append(_STRAY_POSTNO_RE.sub("", line).strip())
    return "\n".join(l for l in out_lines if l).strip()


def _has_tail_echo(text: str, prior_bodies: list[str]) -> bool:
    """Reject if the reply's last word matches a prior reply's last word
    AND that word is one of the high-parrot tail tokens ('Kek', 'lol',
    'based', 'cope', 'ngmi'). Catches 'Kek!' propagating across siblings."""
    if not text or not prior_bodies:
        return False
    parrot_tails = {"kek", "kek!", "lol", "lmao", "based", "cope", "ngmi", "dyel"}
    def last_token(s: str) -> str:
        toks = re.findall(r"[A-Za-z!]+", s)
        return toks[-1].lower() if toks else ""
    mine = last_token(text)
    if mine not in parrot_tails:
        return False
    for body in prior_bodies[-3:]:  # only check last 3 siblings
        if last_token(body) == mine:
            return True
    return False


def _has_yeah_loop(text: str) -> bool:
    """Catch the 'Yeah in 2019 Yeah in 2023 Yeah in 2025...' loop pattern
    that slipped past the repetition penalty in early board ticks."""
    if not text:
        return False
    # Count "Yeah" / "yeah" repetitions
    lower = text.lower()
    if lower.count("yeah") >= 3 and len(text.split()) <= 30:
        return True
    # Generic short-phrase loop: any 3-word phrase repeated 3+ times
    words = re.findall(r"\w+", lower)
    if len(words) >= 9:
        for i in range(len(words) - 2):
            phrase = tuple(words[i:i+3])
            count = sum(1 for j in range(len(words) - 2)
                        if tuple(words[j:j+3]) == phrase)
            if count >= 3:
                return True
    return False


def _generate_reply_body(parent_post: BoardPost, op: BoardPost, variant_key: str,
                         rng: random.Random,
                         siblings: Optional[list[BoardPost]] = None) -> str:
    """Generate a reply that references the parent post by number + cadence.

    `siblings` is the (most-recent-last) list of prior replies in the thread —
    passed into the prompt so the model sees the conversation, and used as
    echo-suppression context so the reply can't parrot prior siblings."""
    from ..imageboard_ingestion import anon_zoo, local_fit_model, output_validator

    siblings = siblings or []
    variant = anon_zoo.get(variant_key)
    persona_system = (
        "You are FitAnon replying in a /fit/ thread. Produce ONE short reply, "
        "1-3 lines. Begin with `>>NNNNNNNN` referencing the post you reply to. "
        "Then optionally ONE `>quoted phrase` (3-8 words from the parent), then "
        "a 1-2 line response of YOUR OWN. Do NOT copy the parent body verbatim. "
        "Do NOT repeat anything other anons in this thread already said. "
        "Do NOT repeat the same word/phrase 3+ times. Stay in /fit/ voice. "
        "No therapy register, no motivational coaching, no AI disclosures."
    )
    if variant:
        persona_system += "\n\n" + variant.system_card_addendum

    parent_excerpt = parent_post.body[:200].strip()
    # Sibling context: last 3 replies in the thread (excluding the parent).
    sib_block = ""
    sib_recent = [s for s in siblings if s.post_no != parent_post.post_no][-3:]
    if sib_recent:
        lines = []
        for s in sib_recent:
            excerpt = (s.body or "").strip().replace("\n", " ")[:160]
            lines.append(f"  >>{s.post_no}: {excerpt}")
        sib_block = "RECENT_REPLIES_IN_THREAD (do NOT repeat these):\n" + "\n".join(lines) + "\n\n"
    user_prompt = (
        "BOARD: /fit/\n"
        "MODE: thread_reply\n"
        f"OP_POST_NO: {op.post_no}\n"
        f"OP_BODY:\n  {(op.body or '').strip()[:200]}\n\n"
        f"{sib_block}"
        f"PARENT_POST_NO: {parent_post.post_no}\n"
        f"PARENT_POST_BODY:\n  {parent_excerpt}\n\n"
        f"Reply as anon. Begin with >>{parent_post.post_no}. 1-3 short lines. "
        "Acknowledge / mock / correct the parent. Do not echo it verbatim. "
        "Say something none of the recent replies already said."
    )
    msgs = [
        {"role": "system", "content": persona_system},
        {"role": "user", "content": user_prompt},
    ]
    req = local_fit_model.build_request(
        messages=msgs, system=persona_system, user=user_prompt,
        profile="chat", max_tokens=90, repetition_penalty=1.22,
    )
    res = local_fit_model.generate(req)
    text = (res.text or "").strip()
    if not text:
        return ""
    # Strip HTML / role labels / scaffolding leakage
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"^(?:user|anon|assistant|board|mode|task)\s*:\s*", "", text, flags=re.I | re.M)
    # Strip stray bare post-numbers (e.g. "77256186 yeah but...")
    text = _strip_stray_postnos(text)
    # Strip hallucinated URLs
    text = _strip_fake_urls(text)
    # Trim mid-word truncation from max_tokens cutoff
    text = _trim_truncated_tail(text)
    # Strip sentence-level echoes of any prior post in the thread
    prior_bodies = [op.body, parent_post.body] + [s.body for s in siblings]
    text = _strip_thread_echo(text, prior_bodies)
    # Cap to 4 lines
    lines = [l.strip() for l in text.splitlines() if l.strip()][:4]
    text = "\n".join(lines).strip()
    if not text:
        return ""
    # Within-output loop check (catches "Yeah in 2019 Yeah in 2023...")
    if _has_yeah_loop(text):
        return ""
    # Tail-token parrot check (catches "Kek!" propagating across siblings)
    if _has_tail_echo(text, [s.body for s in siblings]):
        return ""
    v = output_validator.validate(text, recent_outputs=prior_bodies)
    if not v.get("ok") and v.get("severity") == "hard":
        return ""
    return v.get("repaired_text") or text


# --- thread / post construction ------------------------------------------

def _new_post_no() -> int:
    """Atomic post-no allocator. Tightly bumps to keep numbers monotonic."""
    n = _BOARD["next_post_no"]
    _BOARD["next_post_no"] = n + random.randint(1, 7)
    return n


def _start_thread(rng: random.Random,
                  existing_ops: Optional[list[str]] = None) -> Optional[BoardThread]:
    """Pick a fresh OP seed + variant + generate body."""
    seed_subject, seed_text, image_kw = rng.choice(OP_SEEDS)
    variant_key = _pick_op_variant(rng)
    body = _generate_op_body(seed_text, variant_key, rng,
                             existing_ops=existing_ops or [])
    if not body:
        return None
    post_no = _new_post_no()
    img = _cosmetic_image(image_kw)
    op = BoardPost(
        post_no=post_no, thread_no=post_no, is_op=True,
        anon_variant=variant_key, subject=seed_subject, body=body,
        timestamp=time.time(), reply_targets=[],
        **img,
    )
    return BoardThread(op=op, replies=[], last_activity=time.time())


def _add_reply(thread: BoardThread, rng: random.Random) -> Optional[BoardPost]:
    """Pick a target post in the thread + a different variant + generate."""
    posts = [thread.op] + thread.replies
    # Weight: OP gets some, recent posts get more
    weights = []
    for p in posts:
        if p.is_op:
            weights.append(2.0)
        else:
            # newer = higher weight
            age = time.time() - p.timestamp
            weights.append(max(0.4, 3.0 - age / 60))
    target = rng.choices(posts, weights=weights, k=1)[0]
    used_variants = {p.anon_variant for p in posts}
    variant_key = _pick_reply_variant(rng, exclude=used_variants)
    body = _generate_reply_body(target, thread.op, variant_key, rng,
                                siblings=list(thread.replies))
    if not body:
        return None
    # Make sure body starts with >>NNN (the model is supposed to prepend; if it
    # didn't, add it ourselves so the reply chain renders correctly)
    if not body.lstrip().startswith(f">>{target.post_no}"):
        body = f">>{target.post_no}\n{body}"
    post_no = _new_post_no()
    return BoardPost(
        post_no=post_no, thread_no=thread.thread_no, is_op=False,
        anon_variant=variant_key, subject="", body=body,
        timestamp=time.time(), reply_targets=[target.post_no],
    )


# --- tick ----------------------------------------------------------------

def board_tick(rng: Optional[random.Random] = None) -> dict:
    """Single tick: produce one new post (op or reply) and update state."""
    rng = rng or random.Random()
    with _LOCK:
        threads = _BOARD["threads"]

        # Bootstrap: if board is empty or under 2 threads, force a new OP
        force_new = len(threads) < 2 or rng.random() < NEW_THREAD_PROBABILITY

        if force_new:
            existing_ops = [th.op.body for th in threads]
            t = _start_thread(rng, existing_ops=existing_ops)
            if t is None:
                return {"action": "noop", "reason": "op_generation_empty"}
            threads.insert(0, t)
            # Trim to MAX_THREADS
            if len(threads) > MAX_THREADS:
                threads[:] = threads[:MAX_THREADS]
            return {
                "action": "new_op",
                "thread_no": t.thread_no,
                "post_no": t.op.post_no,
                "variant": t.op.anon_variant,
            }

        # Reply path: pick a thread weighted by recency
        weights = []
        for t in threads:
            age = time.time() - t.last_activity
            weights.append(max(0.5, 4.0 - age / 60))
        thread = rng.choices(threads, weights=weights, k=1)[0]
        if len(thread.replies) >= MAX_REPLIES_PER_THREAD:
            return {"action": "noop", "reason": "thread_full"}
        reply = _add_reply(thread, rng)
        if reply is None:
            return {"action": "noop", "reason": "reply_generation_empty"}
        thread.replies.append(reply)
        thread.last_activity = time.time()
        # Move thread to top (bumped)
        threads.remove(thread)
        threads.insert(0, thread)
        return {
            "action": "reply",
            "thread_no": thread.thread_no,
            "post_no": reply.post_no,
            "reply_to": reply.reply_targets,
            "variant": reply.anon_variant,
        }


# --- view: board page ----------------------------------------------------

def fit_board_page(request):
    """Render the live /fit/ board catalog. JS polls /api/fit/board-tick/."""
    response = render(request, "fit_board.html", {})
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


# --- API: tick + state ---------------------------------------------------

def _serialize_post(p: BoardPost) -> dict:
    return {
        "post_no": p.post_no,
        "thread_no": p.thread_no,
        "is_op": p.is_op,
        "anon_variant": p.anon_variant,
        "subject": p.subject,
        "body": p.body,
        "timestamp": p.timestamp,
        "reply_targets": p.reply_targets,
        "image": {
            "filename": p.image_filename,
            "size_kb": p.image_size_kb,
            "w": p.image_w,
            "h": p.image_h,
        } if p.is_op and p.image_filename else None,
    }


def _serialize_state() -> dict:
    with _LOCK:
        threads = _BOARD["threads"]
        return {
            "started_at": _BOARD["started_at"],
            "thread_count": len(threads),
            "threads": [
                {
                    "thread_no": t.thread_no,
                    "reply_count": t.reply_count,
                    "last_activity": t.last_activity,
                    "op": _serialize_post(t.op),
                    "replies": [_serialize_post(r) for r in t.replies],
                }
                for t in threads
            ],
        }


@csrf_exempt
@require_http_methods(["POST"])
def fit_board_tick(request):
    """One tick: generate a single new post (OP or reply) and return updated state.

    Returns the full board state so the frontend can re-render — keeps the
    client logic dumb. (~few KB per response, fine for poll cadence ~6s.)
    """
    try:
        result = board_tick()
    except Exception as e:
        return JsonResponse({"error": f"{type(e).__name__}: {e}"}, status=500)
    return JsonResponse({"tick": result, "state": _serialize_state()})


@require_http_methods(["GET"])
def fit_board_state(request):
    """Read-only state snapshot — no generation."""
    return JsonResponse(_serialize_state())
