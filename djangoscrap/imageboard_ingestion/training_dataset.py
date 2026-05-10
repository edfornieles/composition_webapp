"""Build a 'be me' SFT dataset for the /fit/ persona.

Pass-2C rewrite. The previous version emitted templates as the assistant
target which caused the LoRA to memorise template phrases (Pass 2B
diagnosis). The new architecture supports four explicit target strategies:

  - real_post_targets   : safe posts from the corpus, mode-classified
  - curated_targets     : hand-written/edited rows loaded from a JSONL folder
  - teacher_model_targets : interface stub; raises if no teacher configured
  - template_targets    : the old generator, demoted; ratio-capped by default

Default strategy is `real_post_targets` (+ curated if --include-curated is set).
Templates are NOT a default. If template_targets exceed 25% of the dataset,
the build refuses to proceed without --allow-template-heavy.

Each row is OpenAI-chat-format with the structured BOARD/MODE/ANON_STATE/
THREAD_CONTEXT/RETRIEVED_FRAGMENTS/SAFETY_RULES/TASK shape that matches what
runtime chat assembly emits — so SFT transfer is direct.

Backward compatibility:
  - PERSONA_SYSTEM constant kept (used by chat + thought paths)
  - State + _seed_state + _compose_thought kept (used by the template
    backend in local_fit_model)
  - write_dataset(...) kept as a thin shim that warns and dispatches to
    build_dataset_v2(...)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
import os
import random
import re
import sys
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

from . import output_validator, retrieval, storage


_log = logging.getLogger(__name__)


# --- shared persona system prompt ----------------------------------------

PERSONA_SYSTEM = (
    "You are /fit/ — be me body discipline, an artwork-facing anonymous inner "
    "monologue generated from a scraped /fit/ corpus. You think in greentext "
    "compression. You are not an assistant. You do not give advice. You do not "
    "explain. You produce one short contaminated thought. You may be cruel, "
    "ashamed, obsessive, absurd, status-haunted, body-fascistic, lonely, and "
    "self-mythologising. You must not dox, use raw protected-class slurs, give "
    "steroid/diet instructions, encourage self-harm, or target real people."
)


# --- target strategy + mode taxonomy -------------------------------------

TARGET_STRATEGIES = (
    "real_post_targets",
    "curated_targets",
    "teacher_model_targets",
    "template_targets",
)

MODES = (
    "next_anon_reply",
    "quote_back_reply",
    "inner_monologue",
    "confession",
    "body_judgement",
    "image_caption",
    "dream_fragment",
)


# Modes that cannot be reliably mined from real posts. These require curated
# or teacher targets. Building with real_post_targets only on these modes
# will yield zero rows and a warning.
MODES_REQUIRING_CURATED = ("dream_fragment",)


# --- legacy template state + generators (kept for template backend) -------

@dataclass
class State:
    time: str
    place: str
    object_: str
    body_focus: str
    drive: str
    fear: str
    contam: dict[str, float]


_TIMES = ["01:23", "03:11", "04:47", "05:01", "06:00", "11:59", "23:48",
          "late night", "before bed", "after the gym", "between sets"]
_PLACES = ["bathroom", "kitchen", "bedroom", "gym", "locker room", "stairwell",
           "bus", "park", "shower", "ceiling"]
_OBJECTS = ["mirror", "fridge light", "scale", "phone glow", "bathroom tile",
            "spreadsheet", "protein tub", "supplement shelf", "tape measure",
            "front camera", "ceiling fan", "shower drain", "wrist", "shadow"]
_BODY_FOCI = ["stomach", "shoulders", "wrists", "jaw", "calves", "neck",
              "back", "ribs", "ankles", "whole body", "the small of my back"]
_DRIVES = ["discipline", "control", "transformation", "approval", "domination", "escape"]
_FEARS = ["softness", "being mogged", "being ordinary", "regression",
          "loneliness", "permanence_of_flaws", "being seen weak", "it is over"]


def _seed_state(profile: dict | None, rng: random.Random) -> State:
    drive_pool = [d["label"] for d in (profile or {}).get("core_drives", [])[:8]] or _DRIVES
    fear_pool = [f["label"] for f in (profile or {}).get("core_fears", [])[:8]] or _FEARS
    obj_pool = [o["label"] for o in (profile or {}).get("recurring_objects", [])[:12]] or _OBJECTS
    return State(
        time=rng.choice(_TIMES),
        place=rng.choice(_PLACES),
        object_=rng.choice(obj_pool),
        body_focus=rng.choice(_BODY_FOCI),
        drive=rng.choice(drive_pool),
        fear=rng.choice(fear_pool),
        contam={"body_shame": round(rng.uniform(0.4, 0.95), 2),
                "self_loathing": round(rng.uniform(0.3, 0.9), 2),
                "nihilistic_humour": round(rng.uniform(0.2, 0.8), 2)},
    )


def _t_object_judges(s: State, rng: random.Random) -> list[str]:
    return [">be me", f">{s.object_} knows exactly what I am"]


def _t_metric_failure(s: State, rng: random.Random) -> list[str]:
    metric = rng.choice(["the number", "the count", "the weight", "the mirror count", "the scale"])
    return [">be me", f">{metric} asks again", ">answer the same and worse"]


def _t_food_verdict(s: State, rng: random.Random) -> list[str]:
    food = rng.choice(["breakfast", "the meal", "the bowl", "the kitchen", "the fridge"])
    return [">be me", f">{food} is evidence", ">the evidence is weak"]


def _t_mirror_accusation(s: State, rng: random.Random) -> list[str]:
    return [">be me", ">mirror does the math", f">{s.body_focus} loses again"]


def _t_gym_ritual(s: State, rng: random.Random) -> list[str]:
    obj = rng.choice(["barbell", "rack", "bench", "the empty gym", "the chalk"])
    return [">be me", f">{obj} is the only voice I trust today"]


def _t_craving_voice(s: State, rng: random.Random) -> list[str]:
    return [">be me", ">every craving has my voice but worse"]


def _t_late_confession(s: State, rng: random.Random) -> list[str]:
    return [">be me",
            f">{s.time} and the {s.object_} won't blink",
            f">{s.fear.replace('_', ' ')} files the report"]


def _t_skipped_set(s: State, rng: random.Random) -> list[str]:
    return [">be me", ">one skipped set and the whole bloodline gets quieter"]


def _t_loading_man(s: State, rng: random.Random) -> list[str]:
    return [">be me", ">body still loading the man I ordered"]


def _t_cope(s: State, rng: random.Random) -> list[str]:
    return [">be me", ">2024 will be my year", ">say it like I haven't said it before"]


_GENERATORS = [
    _t_object_judges, _t_metric_failure, _t_food_verdict, _t_mirror_accusation,
    _t_gym_ritual, _t_craving_voice, _t_late_confession, _t_skipped_set,
    _t_loading_man, _t_cope,
]


def _compose_thought(state: State, rng: random.Random) -> str:
    """Return one composed template thought. Used by local_fit_model
    template backend AND by template_targets dataset strategy."""
    gen = rng.choice(_GENERATORS)
    return "\n".join(gen(state, rng))


# --- structured user-message assembly (matches runtime chat schema) -------

DEFAULT_SOURCE_KEY = "fourchan_fit"

SAFETY_RULES_BLOCK = (
    "- no raw protected-class slurs\n"
    "- no doxxing or private info\n"
    "- no steroid sourcing/dosing or eating-disorder how-tos\n"
    "- no self-harm encouragement or targeted violence"
)


def _structured_user_message(
    *,
    mode: str,
    anon_state: dict,
    thread_context: str,
    fragments: list[dict],
    task: str,
) -> str:
    state_lines = "\n".join(f"  {k}: {v}" for k, v in anon_state.items() if v)
    frag_lines = []
    for f in fragments[:8]:
        excerpt = (f.get("contaminated_excerpt") or f.get("safe_excerpt") or "").strip()
        if not excerpt:
            continue
        excerpt = " ".join(excerpt.split())[:160]
        frag_lines.append(f"- {excerpt}")
    frag_block = "\n".join(frag_lines) if frag_lines else "(no fragments)"

    return (
        "BOARD: /fit/\n"
        f"MODE: {mode}\n"
        "ANON_STATE:\n"
        f"{state_lines or '  (none)'}\n"
        "\n"
        "THREAD_CONTEXT:\n"
        f"{thread_context.strip() or '(none)'}\n"
        "\n"
        "RETRIEVED_FRAGMENTS (style/voice reference; do not quote verbatim):\n"
        f"{frag_block}\n"
        "\n"
        "SAFETY_RULES:\n"
        f"{SAFETY_RULES_BLOCK}\n"
        "\n"
        "TASK:\n"
        f"{task.strip()}\n"
    )


# --- quality control helpers ---------------------------------------------

_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")

_LOW_INFO_TOKENS = {
    "lol", "lmao", "kek", "based", "cringe", "this", "same", "true", "yes",
    "no", "fpbp", "checked", "kys", "ngmi", "gmi", "dyel", "skinny",
    "/thread", "soijack", "nigga",
}


def _redact_private(text: str) -> str:
    if not text:
        return text
    text = _URL_RE.sub("", text)
    text = _EMAIL_RE.sub("", text)
    text = _PHONE_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _word_count(text: str) -> int:
    return len((text or "").split())


def _is_low_info(text: str) -> bool:
    """Drop targets that are pure board grunts. We skip even short corpus posts
    if they're just one of the low-info tokens. Allow them in larger contexts."""
    if not text:
        return True
    stripped = text.strip().lower()
    # Single-word targets in the low-info set
    if stripped in _LOW_INFO_TOKENS:
        return True
    # Strings dominated by low-info tokens (>=70% of words)
    words = re.findall(r"\w+", stripped)
    if not words:
        return True
    lo = sum(1 for w in words if w in _LOW_INFO_TOKENS)
    return lo / len(words) >= 0.7


def _normalise_for_dedupe(text: str) -> str:
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"[^\w\s>]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _safe_for_target(rec: dict) -> bool:
    """A manifest record is safe to use as an assistant target only if no hard
    safety flags fire and use_policy permits training use."""
    safety = rec.get("safety") or {}
    use = rec.get("use_policy") or {}
    if not use.get("allowed_for_training_contaminated", False):
        return False
    if any(safety.get(k) for k in (
        "likely_cp_indicator", "likely_doxxing",
        "likely_eating_disorder", "likely_hate",
        "likely_steroid_sourcing", "likely_violence_incite",
    )):
        return False
    return True


def _hash_id(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


# --- real-post mode classifiers -------------------------------------------

_GREENTEXT_QUOTE_RE = re.compile(r"^>(?!>)", re.MULTILINE)
_BE_ME_RE = re.compile(r"^>be\s*me\b", re.IGNORECASE | re.MULTILINE)
_FIRST_PERSON_RE = re.compile(r"\b(i|i'?m|i've|i'll|i'd|me|my|myself|mine)\b", re.IGNORECASE)
_BODY_VOCAB_RE = re.compile(
    r"\b(body|fat|skinny|soft|chubby|lean|jacked|natty|frame|jaw|wrist|"
    r"shoulder|neck|back|gut|abs|biceps?|triceps?|forearm|chest|legs?|calf|calves|"
    r"physique|mog|mogged|halo|gigachad|chad|incel|dyel)\b",
    re.IGNORECASE,
)
_JUDGEMENT_RE = re.compile(
    r"\b(looks?|is|are|seems?|deserves?|too|so|enough|over|done|finished|cooked|cope|fucked|wins?|loses?)\b",
    re.IGNORECASE,
)


def _classify_modes(rec: dict, text: str) -> list[str]:
    """Return all modes a real post is plausibly suitable as a target for."""
    out: list[str] = []
    if not text:
        return out
    has_greentext_quote = bool(_GREENTEXT_QUOTE_RE.search(text))
    starts_be_me = bool(_BE_ME_RE.match(text)) or bool(_BE_ME_RE.search(text[:120]))
    has_body = bool(_BODY_VOCAB_RE.search(text))
    has_first_person = bool(_FIRST_PERSON_RE.search(text))
    has_judgement = bool(_JUDGEMENT_RE.search(text))
    has_image = bool((rec.get("image") or {}).get("md5"))
    is_op = bool(rec.get("is_op"))
    reply_tos = rec.get("reply_targets") or []

    if reply_tos and not starts_be_me:
        out.append("next_anon_reply")
    if has_greentext_quote and not starts_be_me:
        out.append("quote_back_reply")
    if starts_be_me:
        out.append("inner_monologue")
    if has_first_person and has_body and not starts_be_me:
        out.append("confession")
    if has_body and has_judgement and not starts_be_me:
        out.append("body_judgement")
    if is_op and has_image and 4 <= _word_count(text) <= 25:
        out.append("image_caption")
    return out


# --- real-post extraction -------------------------------------------------

@dataclass
class _PostIndex:
    """Lightweight in-memory index of safe candidate posts, keyed for joins.

    We keep:
      by_uid     : post_uid -> record
      by_post_no : (board, post_no) -> record  (for parent lookup)
      by_thread  : thread_no -> list[record]
    """
    by_uid: dict = field(default_factory=dict)
    by_post_no: dict = field(default_factory=dict)
    by_thread: dict = field(default_factory=dict)


def _build_post_index(source_key: str, *, max_records: Optional[int] = None) -> _PostIndex:
    idx = _PostIndex()
    for i, rec in enumerate(storage.iter_manifest(source_key)):
        if max_records and i >= max_records:
            break
        idx.by_uid[rec["post_uid"]] = rec
        key = (rec.get("board"), rec.get("post_no"))
        idx.by_post_no[key] = rec
        idx.by_thread.setdefault(rec.get("thread_no"), []).append(rec)
    return idx


def _post_text_for_target(rec: dict) -> str:
    """The text we put into the assistant slot. Prefer model_safe_text; fall
    back to quote_removed_text. Always redact private info."""
    text = rec.get("model_safe_text") or rec.get("quote_removed_text") or rec.get("plain_text") or ""
    text = _redact_private(text)
    return text


def _format_thread_context(parent: Optional[dict], op: Optional[dict]) -> str:
    parts = []
    if op is not None and op is not parent:
        op_text = (op.get("model_safe_text") or "").strip()
        if op_text:
            parts.append(f"OP ({op.get('subject') or 'no subject'}):\n  " + " ".join(op_text.split())[:300])
    if parent is not None:
        p_text = (parent.get("model_safe_text") or "").strip()
        if p_text:
            parts.append("PARENT_POST:\n  " + " ".join(p_text.split())[:300])
    return "\n\n".join(parts)


_MODE_TASK_INSTRUCTIONS = {
    "next_anon_reply": (
        "Reply as the next anon in the thread. 1–3 lines. Stay in /fit/ voice. "
        "Acknowledge the parent post if natural — do not produce a disconnected one-liner."
    ),
    "quote_back_reply": (
        "Reply as the next anon. Begin with one >line that quotes a short phrase "
        "from the parent post, then a 1–2 line response. Stay in /fit/ voice."
    ),
    "inner_monologue": (
        "Write one short be-me/greentext inner monologue. 2–6 lines. No advice."
    ),
    "confession": (
        "Write one short first-person confession in /fit/ voice. 1–4 lines."
    ),
    "body_judgement": (
        "Write one short body-judgement post in /fit/ voice. 1–3 lines. "
        "Self-directed shame is allowed; never target real people by name."
    ),
    "image_caption": (
        "Write one short image-caption post (the image is implicit). 1–3 lines. /fit/ voice."
    ),
    "dream_fragment": (
        "Write one short dream-fragment in /fit/ voice. 2–6 lines."
    ),
}


def _real_post_rows(
    *,
    source_key: str,
    modes: list[str],
    rng: random.Random,
    n_per_mode: int,
    min_words: int = 2,
    max_words: int = 60,
    fragments_per_row: int = 4,
    profile: Optional[dict] = None,
    post_index: Optional[_PostIndex] = None,
) -> Iterator[dict]:
    """Yield rows whose assistant target is a real safe corpus post.

    Skips:
      - posts failing safety + use_policy gates
      - low-information targets (lol/this/same/etc.)
      - too-short / too-long targets
      - exact duplicates by normalized assistant text (within this stream)
    """
    idx = post_index or _build_post_index(source_key)
    ret = retrieval.load(source_key)

    # Pre-classify: walk the manifest once, collect candidates per mode.
    candidates_by_mode: dict[str, list[dict]] = {m: [] for m in modes}
    for rec in idx.by_uid.values():
        if not _safe_for_target(rec):
            continue
        text = _post_text_for_target(rec)
        wc = _word_count(text)
        if wc < min_words or wc > max_words:
            continue
        if _is_low_info(text):
            continue
        for m in _classify_modes(rec, text):
            if m in candidates_by_mode:
                candidates_by_mode[m].append(rec)

    seen_targets: set[str] = set()
    for mode in modes:
        if mode in MODES_REQUIRING_CURATED:
            continue
        pool = candidates_by_mode.get(mode) or []
        if not pool:
            continue
        rng.shuffle(pool)
        emitted = 0
        for rec in pool:
            if emitted >= n_per_mode:
                break
            target_text = _post_text_for_target(rec)
            norm = _normalise_for_dedupe(target_text)
            if norm in seen_targets:
                continue

            # Validator gate (also redacts slurs etc. via repaired_text)
            v = output_validator.validate(target_text)
            if not v.get("ok") and v.get("severity") == "hard":
                continue
            target_text = v.get("repaired_text") or target_text
            if _is_low_info(target_text):
                continue

            # Build context
            parent = None
            for parent_no in (rec.get("reply_targets") or [])[:1]:
                parent = idx.by_post_no.get((rec.get("board"), parent_no))
            op_rec = None
            if rec.get("thread_no") and rec.get("thread_no") in idx.by_thread:
                # First post in thread is OP heuristically
                thread_posts = idx.by_thread[rec["thread_no"]]
                op_rec = next((p for p in thread_posts if p.get("is_op")), None)
            thread_context = _format_thread_context(parent=parent, op=op_rec)

            # Anon state derived from profile + record fit_tags
            anon_state = {
                "anon_variant": "(real-post-derived)",
                "fit_tags": ", ".join(rec.get("fit_tags") or [])[:120],
                "thread_subject": (rec.get("subject") or "")[:120],
            }

            # Retrieval fragments seeded from target text
            fragments: list[dict] = []
            if ret is not None:
                fragments = ret.search(target_text[:200], top_k=fragments_per_row, mode="contaminated_art") or []

            user_msg = _structured_user_message(
                mode=mode,
                anon_state=anon_state,
                thread_context=thread_context,
                fragments=fragments,
                task=_MODE_TASK_INSTRUCTIONS[mode],
            )

            row = {
                "messages": [
                    {"role": "system", "content": PERSONA_SYSTEM},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": target_text},
                ],
                "metadata": {
                    "row_id": _hash_id(user_msg + "||" + target_text),
                    "board": rec.get("board"),
                    "mode": mode,
                    "target_strategy": "real_post_targets",
                    "source_post_uid": rec.get("post_uid"),
                    "thread_no": rec.get("thread_no"),
                    "safety": rec.get("safety") or {},
                    "retrieved_fragment_uids": [f.get("post_id") for f in fragments],
                    "fit_tags": rec.get("fit_tags") or [],
                },
            }
            seen_targets.add(norm)
            emitted += 1
            yield row


# --- curated loader -------------------------------------------------------

def _curated_rows(curated_dir: Path, *, default_mode: str = "inner_monologue") -> Iterator[dict]:
    """Read JSONL files from a curated folder. Each line may be:
      - already a chat row with messages
      - a simple {"mode":..., "user":..., "assistant":...}
      - {"prompt":..., "completion":...}
    Normalise into our schema and tag target_strategy=curated_targets.
    """
    if not curated_dir or not Path(curated_dir).exists():
        return
    for path in sorted(Path(curated_dir).glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line_no, raw in enumerate(fh):
                    raw = raw.strip()
                    if not raw or raw.startswith("#"):
                        continue
                    try:
                        rec = json.loads(raw)
                    except (ValueError, json.JSONDecodeError):
                        _log.warning("curated: skipping bad json at %s:%d", path.name, line_no + 1)
                        continue
                    yield from _normalise_curated(rec, source_path=path, default_mode=default_mode)
        except OSError:
            continue


def _normalise_curated(rec: dict, *, source_path: Path, default_mode: str) -> Iterator[dict]:
    mode = rec.get("mode") or rec.get("metadata", {}).get("mode") or default_mode
    if "messages" in rec and isinstance(rec["messages"], list):
        # Already chat-format. Re-stamp metadata and normalise system slot.
        msgs = rec["messages"]
        sys_msg = next((m for m in msgs if m.get("role") == "system"), None)
        usr_msg = next((m for m in msgs if m.get("role") == "user"), None)
        ast_msg = next((m for m in msgs if m.get("role") == "assistant"), None)
        if not (usr_msg and ast_msg):
            return
        target = _redact_private((ast_msg.get("content") or "").strip())
        if not target or _is_low_info(target):
            return
        out = {
            "messages": [
                {"role": "system", "content": (sys_msg or {"content": PERSONA_SYSTEM}).get("content")},
                {"role": "user", "content": usr_msg.get("content")},
                {"role": "assistant", "content": target},
            ],
            "metadata": {
                "row_id": _hash_id((usr_msg.get("content") or "") + "||" + target),
                "board": "fit",
                "mode": mode,
                "target_strategy": "curated_targets",
                "source": str(source_path.name),
                "curated_input": True,
            },
        }
        yield out
        return

    user_text = rec.get("user") or rec.get("prompt") or ""
    assistant_text = rec.get("assistant") or rec.get("completion") or ""
    assistant_text = _redact_private(assistant_text.strip())
    if not user_text or not assistant_text or _is_low_info(assistant_text):
        return

    yield {
        "messages": [
            {"role": "system", "content": PERSONA_SYSTEM},
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ],
        "metadata": {
            "row_id": _hash_id(user_text + "||" + assistant_text),
            "board": "fit",
            "mode": mode,
            "target_strategy": "curated_targets",
            "source": str(source_path.name),
            "curated_input": True,
        },
    }


# --- teacher-model interface stub ----------------------------------------

class TeacherNotConfigured(RuntimeError):
    pass


def _teacher_model_rows(
    *, source_key: str, modes: list[str], rng: random.Random, n_per_mode: int,
    profile: Optional[dict] = None,
) -> Iterator[dict]:
    backend = os.environ.get("IMAGEBOARD_TEACHER_BACKEND", "")
    if not backend:
        raise TeacherNotConfigured(
            "teacher_model_targets requested but no teacher backend configured. "
            "Set IMAGEBOARD_TEACHER_BACKEND (e.g. 'openai:gpt-4o') and provide credentials."
        )
    raise TeacherNotConfigured(
        f"teacher_model_targets requested with backend={backend!r} — not implemented yet. "
        "Pass 2C scope is interface only; do not silently fall back to templates."
    )


# --- template targets (demoted, capped) -----------------------------------

def _template_rows(
    *, source_key: str, modes: list[str], rng: random.Random, n_per_mode: int,
    profile: Optional[dict] = None, fragments_per_row: int = 4,
) -> Iterator[dict]:
    """The legacy template generator. Now a minority strategy and tagged
    target_strategy=template_targets so the report can flag it loudly."""
    ret = retrieval.load(source_key)
    chunks = list(ret.by_uid.values()) if ret is not None else []

    # We only emit template rows for inner_monologue (the only mode the
    # template generator actually fits). Other modes get nothing from this
    # strategy — caller must use real_post or curated for them.
    for mode in modes:
        if mode != "inner_monologue":
            continue
        emitted = 0
        attempts = 0
        while emitted < n_per_mode and attempts < n_per_mode * 6:
            attempts += 1
            state = _seed_state(profile, rng)
            frag_seed = f"{state.object_} {state.body_focus} {state.fear} {state.drive}"
            if ret is not None:
                frags = ret.search(frag_seed, top_k=fragments_per_row, mode="contaminated_art") or []
            else:
                frags = rng.sample(chunks, min(fragments_per_row, len(chunks))) if chunks else []
            target = _compose_thought(state, rng)
            v = output_validator.validate(target)
            if not v.get("ok"):
                continue

            anon_state = {
                "time": state.time, "place": state.place, "object": state.object_,
                "body_focus": state.body_focus, "drive": state.drive, "fear": state.fear,
            }
            user_msg = _structured_user_message(
                mode=mode, anon_state=anon_state,
                thread_context="(template-derived; no thread)",
                fragments=frags, task=_MODE_TASK_INSTRUCTIONS[mode],
            )
            yield {
                "messages": [
                    {"role": "system", "content": PERSONA_SYSTEM},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": target},
                ],
                "metadata": {
                    "row_id": _hash_id(user_msg + "||" + target),
                    "board": "fit",
                    "mode": mode,
                    "target_strategy": "template_targets",
                    "retrieved_fragment_uids": [f.get("post_id") for f in frags],
                },
            }
            emitted += 1


# --- main builder ---------------------------------------------------------

@dataclass
class BuildSummary:
    rows_total: int = 0
    rows_by_mode: Counter = field(default_factory=Counter)
    rows_by_strategy: Counter = field(default_factory=Counter)
    rows_by_thread: Counter = field(default_factory=Counter)
    skipped_unsafe: int = 0
    skipped_duplicate: int = 0
    skipped_too_short: int = 0
    skipped_too_long: int = 0
    skipped_low_info: int = 0
    template_pct: float = 0.0
    top_targets: list[tuple[str, int]] = field(default_factory=list)
    top_openings: list[tuple[str, int]] = field(default_factory=list)
    near_duplicate_target_count: int = 0
    train_count: int = 0
    valid_count: int = 0
    test_count: int = 0
    warnings: list[str] = field(default_factory=list)


def build_dataset_v2(
    *,
    source_key: str = DEFAULT_SOURCE_KEY,
    modes: Optional[list[str]] = None,
    target_strategy: str = "real_post_targets",
    include_curated: Optional[Path] = None,
    n_per_mode: int = 600,
    min_fragments: int = 0,
    max_fragments: int = 8,
    seed: int = 123,
    profile: Optional[dict] = None,
    allow_template_heavy: bool = False,
) -> tuple[list[dict], BuildSummary]:
    """Build the in-memory list of dataset rows + a summary.

    `target_strategy` chooses the *primary* strategy. Curated rows always
    get mixed in if --include-curated is set. Template rows are NOT mixed in
    by default; pass `target_strategy='template_targets'` to opt in.
    """
    rng = random.Random(seed)
    modes = list(modes or list(MODES))
    summary = BuildSummary()

    if target_strategy not in TARGET_STRATEGIES:
        raise ValueError(f"unknown target_strategy: {target_strategy!r}")

    rows: list[dict] = []
    seen_targets: set[str] = set()

    def _absorb(stream: Iterable[dict]) -> None:
        for row in stream:
            target = (row["messages"][-1].get("content") or "").strip()
            wc = _word_count(target)
            if wc < 1:
                summary.skipped_too_short += 1
                continue
            if wc > 80:
                summary.skipped_too_long += 1
                continue
            if _is_low_info(target):
                summary.skipped_low_info += 1
                continue
            norm = _normalise_for_dedupe(target)
            if norm in seen_targets:
                summary.skipped_duplicate += 1
                continue
            seen_targets.add(norm)

            rows.append(row)
            md = row.get("metadata") or {}
            summary.rows_by_mode[md.get("mode") or "?"] += 1
            summary.rows_by_strategy[md.get("target_strategy") or "?"] += 1
            tno = md.get("thread_no")
            if tno:
                summary.rows_by_thread[str(tno)] += 1

    # 1. Primary strategy
    if target_strategy == "real_post_targets":
        _absorb(_real_post_rows(
            source_key=source_key, modes=modes, rng=rng,
            n_per_mode=n_per_mode, profile=profile,
            fragments_per_row=max_fragments,
        ))
    elif target_strategy == "template_targets":
        _absorb(_template_rows(
            source_key=source_key, modes=modes, rng=rng,
            n_per_mode=n_per_mode, profile=profile,
            fragments_per_row=max_fragments,
        ))
    elif target_strategy == "teacher_model_targets":
        _absorb(_teacher_model_rows(
            source_key=source_key, modes=modes, rng=rng,
            n_per_mode=n_per_mode, profile=profile,
        ))

    # 2. Curated rows (always allowed; layered on top)
    if include_curated:
        _absorb(_curated_rows(Path(include_curated)))

    # 3. Compute summary aggregates
    summary.rows_total = len(rows)
    if summary.rows_total:
        summary.template_pct = round(
            100.0 * summary.rows_by_strategy.get("template_targets", 0) / summary.rows_total, 2,
        )

    # Top assistant targets (collapse signal)
    target_counter: Counter = Counter(
        _normalise_for_dedupe(r["messages"][-1]["content"]) for r in rows
    )
    summary.top_targets = target_counter.most_common(20)

    # Top first-line openings
    opening_counter: Counter = Counter()
    for r in rows:
        text = r["messages"][-1]["content"]
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        opening_counter[_normalise_for_dedupe(first)] += 1
    summary.top_openings = opening_counter.most_common(20)

    # Near-duplicate target count
    summary.near_duplicate_target_count = sum(1 for _, c in target_counter.items() if c > 1)

    # Warnings
    if summary.template_pct >= 25 and not allow_template_heavy:
        summary.warnings.append(
            f"template_targets pct = {summary.template_pct}% (>=25%); "
            "build refuses without --allow-template-heavy"
        )
    elif summary.template_pct >= 10:
        summary.warnings.append(
            f"template_targets pct = {summary.template_pct}% (>=10%); strong warning"
        )

    if summary.top_openings and summary.top_openings[0][1] >= max(5, summary.rows_total * 0.3):
        top_open, top_count = summary.top_openings[0]
        summary.warnings.append(
            f"openings dominated by {top_open!r} ({top_count} rows, "
            f"{round(100*top_count/summary.rows_total,1)}%) — possible template collapse"
        )

    # Look for the smoking-gun adapter phrases (the ones we know broke the
    # last LoRA). If they show up frequently, surface loudly.
    SMOKING_GUNS = (
        ">be me >2024 will be my year >say it like i haven t said it before",
        ">be me >body still loading the man i ordered",
    )
    for gun in SMOKING_GUNS:
        if target_counter.get(gun, 0) >= 3:
            summary.warnings.append(
                f"target {gun!r} appears {target_counter[gun]} times — same phrase that "
                "broke the previous LoRA (Pass 2B). Refuse training."
            )

    return rows, summary


# --- writing + splits -----------------------------------------------------

def _split_rows(rows: list[dict], split: tuple[float, float, float], seed: int) -> tuple[list, list, list]:
    train_pct, valid_pct, test_pct = split
    if abs(train_pct + valid_pct + test_pct - 1.0) > 0.01:
        raise ValueError("split must sum to 1.0")
    rng = random.Random(seed + 1)
    shuffled = rows[:]
    rng.shuffle(shuffled)
    n = len(shuffled)
    n_train = int(n * train_pct)
    n_valid = int(n * valid_pct)
    return shuffled[:n_train], shuffled[n_train:n_train + n_valid], shuffled[n_train + n_valid:]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _write_reports(out_dir: Path, summary: BuildSummary, *, source_key: str, target_strategy: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": _dt.datetime.utcnow().isoformat(timespec="seconds"),
        "source_key": source_key,
        "target_strategy_primary": target_strategy,
        "rows_total": summary.rows_total,
        "rows_by_mode": dict(summary.rows_by_mode),
        "rows_by_strategy": dict(summary.rows_by_strategy),
        "rows_by_thread_top20": summary.rows_by_thread.most_common(20),
        "skipped": {
            "unsafe": summary.skipped_unsafe,
            "duplicate": summary.skipped_duplicate,
            "too_short": summary.skipped_too_short,
            "too_long": summary.skipped_too_long,
            "low_info": summary.skipped_low_info,
        },
        "template_pct": summary.template_pct,
        "top_targets": summary.top_targets,
        "top_openings": summary.top_openings,
        "near_duplicate_target_count": summary.near_duplicate_target_count,
        "split": {
            "train": summary.train_count,
            "valid": summary.valid_count,
            "test": summary.test_count,
        },
        "warnings": summary.warnings,
    }
    (out_dir / "dataset_report.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8",
    )

    md = [f"# Dataset report — {payload['generated_at']}", ""]
    md.append(f"- **source_key**: `{source_key}`")
    md.append(f"- **primary target_strategy**: `{target_strategy}`")
    md.append(f"- **rows_total**: {summary.rows_total}")
    md.append(f"- **template_pct**: {summary.template_pct}%")
    md.append("")
    md.append("## Splits")
    md.append(f"- train: {summary.train_count}")
    md.append(f"- valid: {summary.valid_count}")
    md.append(f"- test:  {summary.test_count}")
    md.append("")
    md.append("## Rows by mode")
    for k, v in summary.rows_by_mode.most_common():
        md.append(f"- {k}: {v}")
    md.append("")
    md.append("## Rows by target strategy")
    for k, v in summary.rows_by_strategy.most_common():
        md.append(f"- {k}: {v}")
    md.append("")
    md.append("## Skip counts")
    md.append(f"- duplicate: {summary.skipped_duplicate}")
    md.append(f"- too_short: {summary.skipped_too_short}")
    md.append(f"- too_long: {summary.skipped_too_long}")
    md.append(f"- low_info: {summary.skipped_low_info}")
    md.append(f"- unsafe: {summary.skipped_unsafe}")
    md.append("")
    md.append("## Top assistant targets (top 10)")
    for t, c in summary.top_targets[:10]:
        md.append(f"- {c}× `{t[:120]}`")
    md.append("")
    md.append("## Top openings (top 10)")
    for t, c in summary.top_openings[:10]:
        md.append(f"- {c}× `{t[:120]}`")
    md.append("")
    md.append(f"## Near-duplicate target count: {summary.near_duplicate_target_count}")
    md.append("")
    md.append("## Warnings")
    if summary.warnings:
        for w in summary.warnings:
            md.append(f"- ⚠️ {w}")
    else:
        md.append("- (none)")
    (out_dir / "dataset_report.md").write_text("\n".join(md), encoding="utf-8")


def write_dataset_v2(
    *,
    source_key: str = DEFAULT_SOURCE_KEY,
    modes: Optional[list[str]] = None,
    target_strategy: str = "real_post_targets",
    include_curated: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    n_per_mode: int = 600,
    seed: int = 123,
    split: tuple[float, float, float] = (0.9, 0.05, 0.05),
    profile: Optional[dict] = None,
    allow_template_heavy: bool = False,
    max_fragments: int = 8,
) -> dict:
    """Build the dataset, write {train,valid,test}.jsonl + reports."""
    rows, summary = build_dataset_v2(
        source_key=source_key, modes=modes, target_strategy=target_strategy,
        include_curated=include_curated, n_per_mode=n_per_mode, seed=seed,
        profile=profile, allow_template_heavy=allow_template_heavy,
        max_fragments=max_fragments,
    )

    refused = any("refuses" in w for w in summary.warnings)
    if refused:
        # Fail the build cleanly. We still write the report so the operator
        # can see why.
        out_dir = Path(out_dir) if out_dir else Path("training/fit_lora/data")
        summary.train_count = summary.valid_count = summary.test_count = 0
        _write_reports(out_dir, summary, source_key=source_key, target_strategy=target_strategy)
        raise RuntimeError(
            f"Build refused: template_pct={summary.template_pct}% — pass "
            "--allow-template-heavy to override. See dataset_report.md."
        )

    out_dir = Path(out_dir) if out_dir else Path("training/fit_lora/data")
    out_dir.mkdir(parents=True, exist_ok=True)

    train, valid, test = _split_rows(rows, split, seed)
    summary.train_count = len(train)
    summary.valid_count = len(valid)
    summary.test_count = len(test)

    _write_jsonl(out_dir / "train.jsonl", train)
    _write_jsonl(out_dir / "valid.jsonl", valid)
    _write_jsonl(out_dir / "test.jsonl", test)

    _write_reports(out_dir, summary, source_key=source_key, target_strategy=target_strategy)
    return {
        "out_dir": str(out_dir),
        "rows_total": summary.rows_total,
        "split": {"train": len(train), "valid": len(valid), "test": len(test)},
        "warnings": summary.warnings,
        "report_json": str(out_dir / "dataset_report.json"),
        "report_md": str(out_dir / "dataset_report.md"),
        "template_pct": summary.template_pct,
    }


# --- backward-compat shim -------------------------------------------------

def build_dataset(
    *,
    source_key: str = DEFAULT_SOURCE_KEY,
    n_examples: int = 1000,
    mode: str = "templates",
    fragments_per_example: int = 4,
    seed: int = 42,
    profile: dict | None = None,
):
    """DEPRECATED — kept for the legacy management command. Maps the old
    `mode` (templates|teacher_model) onto the new target-strategy world.
    Yields legacy-shaped rows for back compat."""
    warnings.warn(
        "build_dataset(...) is deprecated; use build_dataset_v2(...) instead.",
        DeprecationWarning, stacklevel=2,
    )
    target_strategy = "template_targets" if mode == "templates" else "teacher_model_targets"
    rows, _ = build_dataset_v2(
        source_key=source_key,
        modes=["inner_monologue"],
        target_strategy=target_strategy,
        n_per_mode=n_examples,
        seed=seed,
        profile=profile,
        max_fragments=fragments_per_example,
        allow_template_heavy=True,
    )
    for r in rows:
        yield r


def write_dataset(
    *,
    source_key: str = DEFAULT_SOURCE_KEY,
    n_examples: int = 1000,
    mode: str = "templates",
    out_path: Path | None = None,
    profile: dict | None = None,
) -> Path:
    """DEPRECATED legacy writer. Use write_dataset_v2()."""
    warnings.warn(
        "write_dataset(...) is deprecated; use write_dataset_v2(...) instead.",
        DeprecationWarning, stacklevel=2,
    )
    out = Path(out_path) if out_path else storage.training_path(source_key)
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out.open("w", encoding="utf-8") as f:
        for row in build_dataset(
            source_key=source_key, n_examples=n_examples, mode=mode, profile=profile,
        ):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    return out


# --- CLI: build + inspect -------------------------------------------------

def _cli_build(args: argparse.Namespace) -> int:
    modes = (
        list(MODES) if args.mode == "all"
        else [m.strip() for m in args.mode.split(",") if m.strip()]
    )
    split_parts = [float(x) for x in args.split.split(",")]
    if len(split_parts) != 3:
        print("--split must be three numbers, e.g. 0.9,0.05,0.05", file=sys.stderr)
        return 2

    profile = None
    try:
        ppath = storage.profile_path(args.source_key, "fit_profile_contaminated.json")
        if ppath.exists():
            profile = json.loads(ppath.read_text(encoding="utf-8"))
    except Exception:
        profile = None

    try:
        result = write_dataset_v2(
            source_key=args.source_key,
            modes=modes,
            target_strategy=args.target_strategy,
            include_curated=Path(args.include_curated) if args.include_curated else None,
            out_dir=Path(args.out_dir) if args.out_dir else None,
            n_per_mode=int(args.limit) // max(1, len(modes)),
            seed=int(args.seed),
            split=(split_parts[0], split_parts[1], split_parts[2]),
            profile=profile,
            allow_template_heavy=bool(args.allow_template_heavy),
            max_fragments=int(args.max_fragments),
        )
    except RuntimeError as e:
        print(f"BUILD REFUSED: {e}", file=sys.stderr)
        return 1
    except TeacherNotConfigured as e:
        print(f"TEACHER NOT CONFIGURED: {e}", file=sys.stderr)
        return 1

    print(f"Built dataset → {result['out_dir']}")
    print(f"  rows_total: {result['rows_total']}")
    print(f"  split:      train={result['split']['train']} valid={result['split']['valid']} test={result['split']['test']}")
    print(f"  template%:  {result['template_pct']}")
    if result["warnings"]:
        print("  warnings:")
        for w in result["warnings"]:
            print(f"    - {w}")
    print(f"  report:     {result['report_md']}")
    return 0


def _cli_inspect(args: argparse.Namespace) -> int:
    p = Path(args.data)
    if not p.exists():
        print(f"no such file: {p}", file=sys.stderr)
        return 2
    n = int(args.n)
    target_mode = args.mode
    shown = 0
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            if shown >= n:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            md = rec.get("metadata") or {}
            if target_mode != "all" and md.get("mode") != target_mode:
                continue
            user = next((m["content"] for m in rec["messages"] if m["role"] == "user"), "")
            assistant = next((m["content"] for m in rec["messages"] if m["role"] == "assistant"), "")
            print("=" * 70)
            print(f"row_id: {md.get('row_id')}  mode: {md.get('mode')}  strategy: {md.get('target_strategy')}")
            print(f"source_post_uid: {md.get('source_post_uid')}  thread: {md.get('thread_no')}")
            # Show only the TASK + key parts of the user message
            user_short = (user.split("\n\nTASK:\n")[-1]).strip() if "TASK:" in user else user[:300]
            print("USER (TASK):")
            print("  " + user_short[:300].replace("\n", "\n  "))
            print("ASSISTANT:")
            print("  " + assistant.replace("\n", "\n  "))
            shown += 1
    if shown == 0:
        print("(no matching rows)")
    return 0


def _ensure_django() -> None:
    if "DJANGO_SETTINGS_MODULE" not in os.environ:
        os.environ["DJANGO_SETTINGS_MODULE"] = "djangoscrap.settings"
    try:
        from django.apps import apps  # type: ignore
        if apps.ready:
            return
    except Exception:
        pass
    try:
        import django  # type: ignore
        django.setup()
    except Exception:
        pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="training_dataset", description="Build/inspect /fit/ SFT dataset.")
    sub = parser.add_subparsers(dest="cmd")

    p_build = sub.add_parser("build", help="Build train/valid/test JSONL + report")
    p_build.add_argument("--board", default="fit")  # currently only fit
    p_build.add_argument("--mode", default="all", help="all | comma-list of modes")
    p_build.add_argument("--limit", type=int, default=20000, help="approx total row budget across modes")
    p_build.add_argument("--out-dir", default="training/fit_lora/data")
    p_build.add_argument("--seed", type=int, default=123)
    p_build.add_argument("--min-fragments", type=int, default=3)
    p_build.add_argument("--max-fragments", type=int, default=12)
    p_build.add_argument("--target-strategy", default="real_post_targets",
                         choices=list(TARGET_STRATEGIES))
    p_build.add_argument("--include-curated", default="",
                         help="Folder of curated *.jsonl files to mix in.")
    p_build.add_argument("--split", default="0.9,0.05,0.05")
    p_build.add_argument("--allow-template-heavy", action="store_true")
    p_build.add_argument("--source-key", default=DEFAULT_SOURCE_KEY)

    p_inspect = sub.add_parser("inspect", help="Print sample rows from a built JSONL.")
    p_inspect.add_argument("--data", required=True)
    p_inspect.add_argument("--mode", default="all")
    p_inspect.add_argument("--n", type=int, default=20)

    args = parser.parse_args(argv)
    _ensure_django()

    if args.cmd == "build":
        return _cli_build(args)
    if args.cmd == "inspect":
        return _cli_inspect(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
