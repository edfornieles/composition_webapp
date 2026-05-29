"""Operator + API + character hub for the corpus-grounded /fit/ persona.

Two endpoints:

- `fit_operator()` — admin page with 9-screen tile grid, sliders, runtime
  selector, regenerate buttons, and grounding debug panel.
- `fit_operator_thought()` — JSON API that calls
  `thought_grounding.generate_fit_be_me_thought` with per-request overrides
  (intensity, runtime, mode, screen_id) and returns the full result dict.

The public-facing wall already routes /fit/ personas through
`monologue_streaming._maybe_fit_grounded`. This page is operator-only — it
exposes the controls the engineering brief asked for without changing the
existing public flow.
"""
from __future__ import annotations

import datetime
import json

from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods


_SCREEN_IDS = (
    "1_mirror",
    "2_calories",
    "3_gym_machine",
    "4_supplement_shelf",
    "5_injury",
    "6_comparison",
    "7_night_kitchen",
    "8_routine_spreadsheet",
    "9_void_cope",
)

_RUNTIMES = ("mlx", "ollama", "llamacpp", "template")


def fit_operator(request):
    """Render the /fit/ operator dashboard."""
    from ..imageboard_ingestion.runtime_meta import resolve_backend
    corpus = getattr(settings, "FIT_CORPUS_SOURCE_KEY", "fourchan_fit")
    runtime = resolve_backend()
    intensity = float(getattr(settings, "FIT_CONTAMINATION_INTENSITY", 0.75))
    art_mode = bool(getattr(settings, "FIT_ART_MODE", True))
    require_grounding = bool(getattr(settings, "FIT_THOUGHTS_REQUIRE_GROUNDING", True))
    corpus_root = getattr(settings, "IMAGEBOARD_CORPUS_ROOT", "")

    # Profile snapshot (best-effort; fine if missing)
    profile = {}
    try:
        from ..imageboard_ingestion import storage
        path = storage.profile_path(corpus, "fit_profile_contaminated.json")
        if path.exists():
            profile = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        profile = {}

    context = {
        "corpus": corpus,
        "corpus_root": corpus_root,
        "runtime": runtime,
        "intensity": intensity,
        "art_mode": art_mode,
        "require_grounding": require_grounding,
        "screen_ids": _SCREEN_IDS,
        "runtimes": _RUNTIMES,
        "profile_stats": profile.get("stats", {}),
        "profile_drives": [d.get("label") for d in (profile.get("core_drives") or [])][:8],
        "profile_objects": [d.get("label") for d in (profile.get("recurring_objects") or [])][:10],
    }
    return render(request, "admin/fit_operator.html", context)


@csrf_exempt
@require_http_methods(["POST"])
def fit_operator_thought(request):
    """Generate one /fit/ thought with per-request overrides; return full debug dict."""
    try:
        body = json.loads(request.body.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}

    screen_id = (body.get("screen_id") or "").strip() or None
    runtime = (body.get("runtime") or "").strip() or None
    if runtime and runtime not in _RUNTIMES:
        return JsonResponse({"error": f"unknown runtime: {runtime}"}, status=400)

    try:
        intensity = float(body.get("contamination_intensity",
                                   getattr(settings, "FIT_CONTAMINATION_INTENSITY", 0.75)))
    except (TypeError, ValueError):
        return JsonResponse({"error": "contamination_intensity must be a number"}, status=400)
    intensity = max(0.0, min(1.0, intensity))

    require_grounding = bool(body.get("require_grounding",
                                      getattr(settings, "FIT_THOUGHTS_REQUIRE_GROUNDING", True)))

    query = (body.get("query") or "").strip() or None
    ctx = None
    if query:
        ctx = {"object": query}

    corpus = (body.get("corpus") or getattr(settings, "FIT_CORPUS_SOURCE_KEY", "fourchan_fit")).strip()

    try:
        from ..imageboard_ingestion import thought_grounding
    except Exception as e:
        return JsonResponse({"error": f"grounding_import_failed:{e}"}, status=500)

    try:
        result = thought_grounding.generate_fit_be_me_thought(
            current_context=ctx,
            source_key=corpus,
            screen_id=screen_id,
            n_fragments=int(body.get("n_fragments", 8)),
            contamination_intensity=intensity,
            require_grounding=require_grounding,
            runtime=runtime,
        )
    except Exception as e:
        return JsonResponse({"error": f"generate_failed:{e}"}, status=500)

    # Trim grounding fragments to the safe excerpt + similarity for the operator UI
    grounding = []
    for f in (result.get("internal_grounding") or [])[:8]:
        grounding.append({
            "chunk_id": f.get("chunk_id"),
            "sim": round(float(f.get("sim") or 0.0), 4),
            "safe_excerpt": (f.get("safe_excerpt") or "")[:240],
        })

    return JsonResponse({
        "thought": result.get("thought") or "",
        "grounding_status": result.get("grounding_status"),
        "internal_grounding": grounding,
        "persona_state": result.get("persona_state") or {},
        "safety_flags": result.get("safety_flags") or [],
        "visual_suggestions": result.get("visual_suggestions") or [],
        "runtime": result.get("runtime"),
        "runtime_meta": result.get("runtime_meta") or {},
        "screen_id": screen_id,
        "contamination_intensity": intensity,
    })


_FIT_SLUG = "fourchan_fit_body_discipline"
_FEEDBACK_FILE = Path(settings.BASE_DIR) / "var" / "fit_thought_feedback.jsonl"


def fit_chat_page(request):
    """Standalone chat page — no admin chrome, no login required.

    Each visit starts a NEW thread (fresh ChatSession). Old sessions stay in
    the DB but the page never auto-loads them — this matches the artwork
    intent of an ephemeral thread per visit. Pass `?session_id=N` to resume
    a specific past thread (mostly for debugging).
    """
    import datetime as _dt
    from ..models import MonologuePersona, ChatSession
    persona = get_object_or_404(MonologuePersona, slug=_FIT_SLUG)

    sid = request.GET.get("session_id")
    if sid and sid.isdigit():
        try:
            session = ChatSession.objects.get(id=int(sid), persona=persona)
        except ChatSession.DoesNotExist:
            session = ChatSession.objects.create(
                persona=persona,
                name=f"thread-{_dt.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}",
            )
    else:
        session = ChatSession.objects.create(
            persona=persona,
            name=f"thread-{_dt.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}",
        )
    response = render(request, "fit_chat.html", {
        "persona": persona,
        "session_id": session.id,
        "slug": _FIT_SLUG,
    })
    # Pass-2-diag2: prevent browser caching ANY redirect or page state, so
    # autocomplete history (e.g. /s3/<bucket>/ from the admin source-library)
    # cannot persist a stale path.
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


def fit_stream_page(request):
    """Standalone 'be me' thought stream with keep/skip feedback."""
    response = render(request, "fit_stream.html", {})
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


@csrf_exempt
@require_http_methods(["POST"])
def fit_wall_stream_next(request):
    """Generate the next 1-3 greentext lines for the continuous /wall/ stream.

    Unlike `fit_operator_thought` (which produces an isolated thought per
    screen-preset), this endpoint takes the recent lines of the running post
    and produces a continuation that flows associatively from them. Same
    anon, same body, same hour. Retrieval is seeded on the recent lines so
    the corpus voice tracks the running thread.
    """
    import re as _re
    try:
        body = json.loads(request.body.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}

    recent_lines_in = body.get("recent_lines") or []
    if not isinstance(recent_lines_in, list):
        recent_lines_in = []
    recent_lines = [str(line)[:200] for line in recent_lines_in[-12:] if line]

    session_seed = (body.get("session_seed") or "wall-default").strip()[:64] or "wall-default"

    from ..imageboard_ingestion import (
        anon_zoo, local_fit_model, output_validator, retrieval, training_dataset,
    )

    variant = anon_zoo.pick_for_session(seed=session_seed)
    corpus = "fourchan_fit"

    # Pass 2E.1: rotation-based retrieval seed instead of querying on the
    # model's own previous lines. Querying on recent_lines created a
    # feedback loop where the model's abstract output pulled abstract
    # fragments, which produced more abstract output (the "only what was
    # inside / canvas / tape / story" surreal drift). Now we cycle through
    # concrete anchors derived from the pinned anon's traits — the
    # retrieval stays grounded regardless of what the model just produced.
    anchors = list(variant.recurring_objects) + [
        variant.dominant_drive,
        variant.dominant_fear.replace("_", " "),
        "mirror scale fridge",
        "barbell rack chalk",
        "kitchen 3am",
        "stairwell phone glow",
    ]
    anchor_idx = (len(recent_lines) // 2) % max(1, len(anchors))
    query_anchor = anchors[anchor_idx]

    fragments: list[dict] = []
    try:
        ret = retrieval.load(corpus)
        if ret is not None:
            fragments = ret.search(query_anchor, top_k=4, mode="contaminated_art") or []
    except Exception:
        fragments = []

    persona_system = training_dataset.PERSONA_SYSTEM
    frag_block = "\n".join(
        f"- {(f.get('contaminated_excerpt') or f.get('safe_excerpt') or '')[:140]}"
        for f in fragments[:4] if f
    ) or "(no fragments)"

    system_prompt = (
        f"{persona_system}\n\n"
        f"{variant.system_card_addendum}\n\n"
        "You are typing ONE ongoing greentext post on /fit/. Each turn you "
        "extend the post with 1 OR 2 short new lines (never more). The same "
        "anon, the same body, the same hour — never reset, never introduce "
        "new speakers, never moralise. Most lines start with '>'. Lines stay "
        "very short (3-10 words).\n\n"
        "EVERY LINE NAMES A CONCRETE THING. A body part, a piece of gear, a "
        "room, an object on a shelf, a time on a clock, a number, a sound. "
        "If you have no concrete image to write, write nothing.\n\n"
        "FORBIDDEN abstract phrasings (do NOT use any of these — they are "
        "the failure mode):\n"
        "  the past · the present · the future · what was inside · only what · \n"
        "  the canvas · the tape · the story · the truth · the void · \n"
        "  essence · matters · nothing matters · only what counts · \n"
        "  the silence · the reflection · the whole · my reality\n\n"
        f"Voice fragments (concrete cadence reference; do not quote verbatim):\n{frag_block}\n\n"
        "Hard rules:\n"
        "- Output ONLY raw plain-text lines, ONE per row. No HTML, no <span>, no\n"
        "  markdown, no styling tags, no code blocks, no quotation marks around lines.\n"
        "- Never use '>>' (two arrows). Use a single '>' for greentext lines.\n"
        "- No labels (user:/anon:/assistant:).\n"
        "- Never repeat a line, paraphrase, or theme that already appears in the\n"
        "  recent lines below. Move to a NEW concrete object or body part.\n"
        "- No metaphors. No abstract nouns. Concrete details only.\n"
        "- Never give advice or motivation.\n"
        "- No URLs, no emails, no phone numbers, no protected-class slurs."
    )

    if recent_lines:
        prior = "\n".join(recent_lines)
        user_prompt = (
            f"Most recent lines of the post (continue from here, do NOT repeat them):\n"
            f"{prior}\n\nWrite the next 1-3 lines."
        )
    else:
        user_prompt = (
            "Begin a new /fit/ greentext post. Open with '>be me' and write 2-3 short "
            "lines that establish the scene and the body."
        )

    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    req = local_fit_model.build_request(
        messages=msgs, system=system_prompt, user=user_prompt, profile="thought",
        max_tokens=50,                        # cap output so we get short bursts
        repetition_penalty=1.18,              # extra anti-loop within generation
    )
    res = local_fit_model.generate(req)
    text = (res.text or "").strip()

    # Parse output into lines, drop scaffold + HTML + duplicates + drift.
    _HTML_TAG_RE = _re.compile(r"<[^>]+>")
    _MD_BACKTICKS_RE = _re.compile(r"`+")

    # Pass 2E.1: abstract-drift detector. The wall slid into surreal poetry
    # ("only what was inside / the past is a tape / the canvas") because the
    # model spent its budget on metaphor instead of physical detail. Reject
    # lines that are abstract AND contain none of the corpus's concrete
    # vocabulary (objects, body parts, times, gear, board slang).
    _CONCRETE_VOCAB = _re.compile(
        r"\b(mirror|scale|barbell|dumbbell|rack|bench|gym|fridge|kitchen|"
        r"stairwell|stair|locker|shower|bathroom|bedroom|bus|park|"
        r"shoulder|jaw|wrist|ankle|knee|stomach|gut|abs|chest|back|leg|"
        r"calf|calves|neck|ribs|forearm|biceps?|triceps?|hair|skin|"
        r"protein|spreadsheet|tracker|phone|alarm|tape\s*measure|"
        r"calorie|kcal|gram|set|rep|plate|pull|push|squat|deadlift|press|"
        r"morning|night|3am|2am|4am|5am|monday|sunday|"
        r"soft|weak|skinny|thin|fat|chubby|lean|jacked|natty|halo|"
        r"mog|mogged|dyel|ngmi|gmi|cope|coping|mirin|chad|incel|"
        r"kek|based|loser|pathetic|fraud|fag|cunt|bro|anon|deload|"
        r"shorts|hoodie|tank\s*top|sock|shoe|shoes|barbell|chalk)\b",
        _re.I,
    )
    _ABSTRACT_DRIFT = _re.compile(
        r"\b(?:the\s+(?:past|present|future|truth|void|silence|"
        r"reflection|essence|whole|story|tape|canvas)|"
        r"only\s+what(?:\s+was|\s+is|'?s|\s+matters)|"
        r"what\s+was\s+inside|what'?s\s+inside|"
        r"nothing\s+(?:matters|but)|matters\s+(?:now|anymore)|"
        r"my\s+reality|a\s+canvas|a\s+tape|a\s+story)\b",
        _re.I,
    )

    def _normalise_line(line: str) -> str:
        s = _HTML_TAG_RE.sub("", line)
        s = _MD_BACKTICKS_RE.sub("", s)
        s = (s.replace("&gt;", ">").replace("&lt;", "<")
             .replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'"))
        s = s.strip()
        if len(s) >= 2 and s[0] in ('"', "'") and s[-1] == s[0]:
            s = s[1:-1].strip()
        s = _re.sub(r"^>{2,}", ">", s)
        return s

    def _dedupe_key(line: str) -> str:
        return _re.sub(r"[^a-z0-9 ]+", " ", line.lower()).strip()

    def _trigram_jaccard(a: str, b: str) -> float:
        aw = _re.findall(r"\w+", a.lower())
        bw = _re.findall(r"\w+", b.lower())
        if len(aw) < 3 or len(bw) < 3:
            return 0.0
        ag = {tuple(aw[i:i+3]) for i in range(len(aw)-2)}
        bg = {tuple(bw[i:i+3]) for i in range(len(bw)-2)}
        if not ag or not bg:
            return 0.0
        return len(ag & bg) / len(ag | bg)

    def _is_abstract_drift(line: str) -> bool:
        body = _re.sub(r"^>+\s*", "", line)
        if len(body.split()) < 4:
            return False  # too short to drift
        if _CONCRETE_VOCAB.search(body):
            return False  # has at least one concrete anchor
        return bool(_ABSTRACT_DRIFT.search(body))

    seen_recent = {_dedupe_key(l) for l in recent_lines if l}

    out_lines: list[str] = []
    out_keys: set[str] = set()
    for raw in text.splitlines():
        s = _normalise_line(raw)
        if not s:
            continue
        if _re.match(r"^(?:user|anon|assistant)\s*:", s, _re.I):
            continue
        if any(s.startswith(tok) for tok in
               ("BOARD:", "MODE:", "ANON_STATE:", "OP_ANCHOR:",
                "RECENT_TURNS:", "RETRIEVED_FRAGMENTS:", "SAFETY_RULES:", "TASK:")):
            continue
        if s.lower().startswith("most recent lines"):
            continue
        if s.lower().startswith(("here is", "here's", "sure,", "certainly,")):
            continue
        # Exact-match dedupe
        key = _dedupe_key(s)
        if not key or key in seen_recent or key in out_keys:
            continue
        # Fuzzy cross-call dedupe — the surreal-drift case produces near-
        # variants ("only what was inside is there" vs "only what was inside
        # matters") that exact-match misses. Trigram-Jaccard catches them.
        if any(_trigram_jaccard(s, prev) >= 0.55 for prev in recent_lines[-12:]):
            continue
        # Abstract-drift filter — reject metaphor-only lines with no concrete vocab
        if _is_abstract_drift(s):
            continue
        # Validator gate (hard rejects only)
        v = output_validator.validate(s)
        if not v.get("ok") and v.get("severity") == "hard":
            continue
        if v.get("repaired_text"):
            s = v["repaired_text"]
        out_lines.append(s)
        out_keys.add(key)
        # Pass 2E.1: cap to 2 lines per turn (was 3) to slow the drift cascade
        if len(out_lines) >= 2:
            break

    return JsonResponse({
        "lines": out_lines,
        "runtime_meta": res.meta.as_dict() if getattr(res, "meta", None) else {},
        "variant": variant.key,
    })


@csrf_exempt
@require_http_methods(["POST"])
def fit_thought_feedback(request):
    """Save a thumbs-up/down on a generated thought to var/fit_thought_feedback.jsonl."""
    try:
        body = json.loads(request.body.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "bad json"}, status=400)

    thought = (body.get("thought") or "").strip()
    rating = body.get("rating")  # "keep" or "skip"
    if not thought or rating not in ("keep", "skip"):
        return JsonResponse({"error": "need thought + rating (keep|skip)"}, status=400)

    _FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.datetime.utcnow().isoformat(),
        "rating": rating,
        "thought": thought,
        "grounding_status": body.get("grounding_status") or "",
    }
    with _FEEDBACK_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    return JsonResponse({"ok": True})
