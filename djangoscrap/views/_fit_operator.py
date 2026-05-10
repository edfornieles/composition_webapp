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
    """Standalone chat page — no admin chrome, no login required."""
    from ..models import MonologuePersona, ChatSession
    persona = get_object_or_404(MonologuePersona, slug=_FIT_SLUG)
    session = persona.chat_sessions.order_by("id").first()
    if not session:
        session = ChatSession.objects.create(persona=persona, name="default")
    return render(request, "fit_chat.html", {
        "persona": persona,
        "session_id": session.id,
        "slug": _FIT_SLUG,
    })


def fit_stream_page(request):
    """Standalone 'be me' thought stream with keep/skip feedback."""
    return render(request, "fit_stream.html", {})


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
