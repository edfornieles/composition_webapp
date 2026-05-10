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

def _generate_op_body(seed_text: str, variant_key: str, rng: random.Random) -> str:
    """OP bodies use the seed text directly. The OP_SEEDS list is already
    hand-written /fit/ openers; running them through the LoRA was leaking
    TASK instructions ("1-2 lines") into the post body. The artwork values
    a clean OP over a paraphrased one — replies do the voice work."""
    return seed_text


def _strip_parent_echo(text: str, parent_body: str) -> str:
    """If the reply contains a verbatim block of the parent body, strip it.
    The model sometimes copies the parent into its reply as part of the
    quote-back gesture; we keep `>>postno` but drop the bulk echo."""
    if not text or not parent_body:
        return text
    parent_lines = {l.strip().lower() for l in parent_body.splitlines() if l.strip()}
    kept = []
    for line in text.splitlines():
        s = line.strip().lower()
        # Allow >>NNN reply refs and short greentext quotes through
        if s.startswith(">>") or len(s) < 8:
            kept.append(line)
            continue
        # Drop lines that are a verbatim parent line
        if s in parent_lines:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


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
                         rng: random.Random) -> str:
    """Generate a reply that references the parent post by number + cadence."""
    from ..imageboard_ingestion import anon_zoo, local_fit_model, output_validator

    variant = anon_zoo.get(variant_key)
    persona_system = (
        "You are FitAnon replying in a /fit/ thread. Produce ONE short reply, "
        "1-3 lines. Begin with `>>NNNNNNNN` referencing the post you reply to. "
        "Then optionally ONE `>quoted phrase` (3-8 words from the parent), then "
        "a 1-2 line response of YOUR OWN. Do NOT copy the parent body verbatim. "
        "Do NOT repeat the same word/phrase 3+ times. Stay in /fit/ voice. "
        "No therapy register, no motivational coaching, no AI disclosures."
    )
    if variant:
        persona_system += "\n\n" + variant.system_card_addendum

    parent_excerpt = parent_post.body[:200].strip()
    user_prompt = (
        "BOARD: /fit/\n"
        "MODE: thread_reply\n"
        f"PARENT_POST_NO: {parent_post.post_no}\n"
        f"PARENT_POST_BODY:\n  {parent_excerpt}\n\n"
        f"Reply as anon. Begin with >>{parent_post.post_no}. 1-3 short lines. "
        "Acknowledge / mock / correct the parent. Do not echo it verbatim."
    )
    msgs = [
        {"role": "system", "content": persona_system},
        {"role": "user", "content": user_prompt},
    ]
    req = local_fit_model.build_request(
        messages=msgs, system=persona_system, user=user_prompt,
        profile="chat", max_tokens=60, repetition_penalty=1.18,
    )
    res = local_fit_model.generate(req)
    text = (res.text or "").strip()
    if not text:
        return ""
    # Strip HTML / role labels / scaffolding leakage
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"^(?:user|anon|assistant|board|mode|task)\s*:\s*", "", text, flags=re.I | re.M)
    # Strip parent-body echoes
    text = _strip_parent_echo(text, parent_post.body)
    # Cap to 4 lines
    lines = [l.strip() for l in text.splitlines() if l.strip()][:4]
    text = "\n".join(lines).strip()
    if not text:
        return ""
    # Within-output loop check (catches "Yeah in 2019 Yeah in 2023...")
    if _has_yeah_loop(text):
        return ""
    v = output_validator.validate(text, recent_outputs=[op.body, parent_post.body])
    if not v.get("ok") and v.get("severity") == "hard":
        return ""
    return v.get("repaired_text") or text


# --- thread / post construction ------------------------------------------

def _new_post_no() -> int:
    """Atomic post-no allocator. Tightly bumps to keep numbers monotonic."""
    n = _BOARD["next_post_no"]
    _BOARD["next_post_no"] = n + random.randint(1, 7)
    return n


def _start_thread(rng: random.Random) -> Optional[BoardThread]:
    """Pick a fresh OP seed + variant + generate body."""
    seed_subject, seed_text, image_kw = rng.choice(OP_SEEDS)
    variant_key = _pick_op_variant(rng)
    body = _generate_op_body(seed_text, variant_key, rng)
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
    body = _generate_reply_body(target, thread.op, variant_key, rng)
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
            t = _start_thread(rng)
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
