"""Unified per-character views + API.

One page per character, tabbed (chat / thoughts / images / training / settings).
Replaces the older split-up fit_chat / fit_training_data / fit_image_sources /
character_detail surfaces. The legacy /fit/ operator dashboard stays separate.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ..models import (
    ChatMessage, ChatSession, FavoriteThought, MonologuePersona,
)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _persona_or_404(slug: str) -> MonologuePersona:
    return get_object_or_404(MonologuePersona, slug=slug)


def _image_root(persona: MonologuePersona) -> Path | None:
    if not persona.image_dir:
        return None
    p = Path(persona.image_dir)
    return p if p.exists() else None


def _list_images(persona: MonologuePersona, folder: str = "") -> list[str]:
    root = _image_root(persona)
    if not root:
        return []
    base = (root / folder).resolve() if folder else root.resolve()
    try:
        base.relative_to(root.resolve())
    except ValueError:
        return []
    if not base.exists():
        return []
    out = []
    exts = (".jpg", ".jpeg", ".png", ".webp", ".gif")
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name.startswith("._"):
            continue
        if p.suffix.lower() in exts:
            out.append(p.relative_to(root).as_posix())
    return out


def _list_subfolders(persona: MonologuePersona) -> list[str]:
    root = _image_root(persona)
    if not root:
        return []
    out = []
    for p in sorted(root.iterdir()):
        if p.is_dir() and not p.name.startswith("."):
            out.append(p.name)
    return out


def _training_paths(persona: MonologuePersona) -> tuple[Path | None, Path | None]:
    if not persona.corpus_key:
        return None, None
    try:
        from ..imageboard_ingestion import storage
        return (
            storage.training_path(persona.corpus_key, "fit_be_me_sft_train.jsonl"),
            storage.training_path(persona.corpus_key, "fit_be_me_sft_valid.jsonl"),
        )
    except Exception:
        return None, None


def _training_counts(persona: MonologuePersona) -> dict:
    train_p, valid_p = _training_paths(persona)
    counts = {"train": 0, "valid": 0}
    for label, p in (("train", train_p), ("valid", valid_p)):
        if p and p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    counts[label] = sum(1 for line in f if line.strip())
            except Exception:
                counts[label] = 0
    return counts


def _persona_thumb_url(persona: MonologuePersona) -> str | None:
    urls = list(getattr(persona, "cached_visual_urls", None) or [])
    if urls:
        return urls[0]
    files = _list_images(persona)
    if files:
        return reverse("character_image_serve", args=[persona.slug, files[0]])
    return None


# -----------------------------------------------------------------------------
# Hub + page + create wizard
# -----------------------------------------------------------------------------


def character_hub(request):
    personas = list(MonologuePersona.objects.all().order_by("-updated_at"))
    tiles = []
    for p in personas:
        files = _list_images(p)
        train_count = _training_counts(p)["train"]
        tiles.append({
            "slug": p.slug,
            "title": p.character_name or p.title,
            "subtitle": p.archetype or "",
            "thumb": _persona_thumb_url(p),
            "image_count": len(files),
            "train_count": train_count,
            "has_model": p.has_trained_model,
            "is_published": p.is_published,
            "url": reverse("character_page", args=[p.slug]),
        })
    return render(request, "characters/hub.html", {
        "tiles": tiles,
        "create_url": reverse("character_create"),
    })


def character_page(request, slug):
    persona = _persona_or_404(slug)
    sessions = list(persona.chat_sessions.all()[:50])
    if not sessions:
        sessions = [ChatSession.objects.create(persona=persona, name="default")]
    counts = _training_counts(persona)
    folders = _list_subfolders(persona)
    images = _list_images(persona)
    favorites_count = persona.favorite_thoughts.count()

    return render(request, "characters/character_page.html", {
        "persona": persona,
        "sessions": sessions,
        "image_count": len(images),
        "folders": folders,
        "train_count": counts["train"],
        "valid_count": counts["valid"],
        "favorites_count": favorites_count,
        "stream_endpoint": reverse("monologue_segment", kwargs={"slug": persona.slug}),
        "public_url": reverse("monologue_public", kwargs={"slug": persona.slug}),
    })


@require_http_methods(["GET", "POST"])
def character_create(request):
    if request.method == "POST":
        title = (request.POST.get("title") or "").strip()
        character_name = (request.POST.get("character_name") or title).strip()
        slug = (request.POST.get("slug") or slugify(title))[:120]
        archetype = (request.POST.get("archetype") or "").strip()
        base_model = (request.POST.get("base_model") or "mlx-community/Qwen2.5-7B-Instruct-4bit").strip()
        corpus_key = (request.POST.get("corpus_key") or "").strip()
        image_dir = (request.POST.get("image_dir") or "").strip()
        adapter_dir = (request.POST.get("adapter_dir") or "").strip()
        persona_prompt = (request.POST.get("persona_prompt") or "").strip()

        if not title or not slug:
            return render(request, "characters/create.html", {
                "error": "title and slug are required",
                "form": request.POST,
            })
        if MonologuePersona.objects.filter(slug=slug).exists():
            return render(request, "characters/create.html", {
                "error": f"slug '{slug}' already exists",
                "form": request.POST,
            })

        persona = MonologuePersona.objects.create(
            slug=slug,
            title=title,
            character_name=character_name,
            archetype=archetype,
            base_model=base_model,
            corpus_key=corpus_key,
            image_dir=image_dir,
            adapter_dir=adapter_dir,
            persona_prompt=persona_prompt,
            is_published=False,
        )
        return redirect("character_page", slug=persona.slug)

    return render(request, "characters/create.html", {"form": {}})


@require_http_methods(["POST"])
def character_settings_save(request, slug):
    persona = _persona_or_404(slug)
    fields = (
        "title", "character_name", "archetype", "base_model", "adapter_dir",
        "corpus_key", "image_dir", "persona_prompt",
    )
    for f in fields:
        if f in request.POST:
            setattr(persona, f, (request.POST.get(f) or "").strip())
    for f in ("gen_temperature", "gen_top_p"):
        if f in request.POST:
            try:
                setattr(persona, f, float(request.POST.get(f)))
            except ValueError:
                pass
    if "gen_max_tokens" in request.POST:
        try:
            persona.gen_max_tokens = max(1, int(request.POST.get("gen_max_tokens")))
        except ValueError:
            pass
    if "is_published" in request.POST:
        persona.is_published = request.POST.get("is_published") in ("1", "true", "on")
    persona.save()
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def character_delete(request, slug):
    persona = _persona_or_404(slug)
    persona.delete()
    return redirect("character_hub")


# -----------------------------------------------------------------------------
# Chat API (per-character, slug-scoped)
# -----------------------------------------------------------------------------


def _serialize_session(s: ChatSession) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def _serialize_message(m: ChatMessage) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "text": m.text,
        "created_at": m.created_at.isoformat(),
    }


def api_chat_sessions(request, slug):
    persona = _persona_or_404(slug)
    return JsonResponse({"sessions": [_serialize_session(s) for s in persona.chat_sessions.all()]})


@csrf_exempt
@require_http_methods(["POST"])
def api_chat_session_create(request, slug):
    persona = _persona_or_404(slug)
    try:
        body = json.loads(request.body.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    name = (body.get("name") or "new chat")[:120]
    s = ChatSession.objects.create(persona=persona, name=name)
    return JsonResponse({"session": _serialize_session(s)})


@csrf_exempt
@require_http_methods(["POST"])
def api_chat_session_delete(request, slug, session_id):
    persona = _persona_or_404(slug)
    s = get_object_or_404(ChatSession, id=session_id, persona=persona)
    s.delete()
    return JsonResponse({"ok": True})


def api_chat_messages(request, slug, session_id):
    persona = _persona_or_404(slug)
    s = get_object_or_404(ChatSession, id=session_id, persona=persona)
    msgs = list(s.messages.all())
    return JsonResponse({"messages": [_serialize_message(m) for m in msgs]})


@csrf_exempt
@require_http_methods(["POST"])
def api_chat_send(request, slug, session_id):
    persona = _persona_or_404(slug)
    s = get_object_or_404(ChatSession, id=session_id, persona=persona)
    try:
        body = json.loads(request.body.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    user_msg = (body.get("message") or "").strip()
    if not user_msg:
        return JsonResponse({"error": "empty"}, status=400)
    user_msg = user_msg[:1000]

    user_record = ChatMessage.objects.create(session=s, role="user", text=user_msg)

    history = list(s.messages.exclude(id=user_record.id).order_by("-id")[:20])
    history.reverse()

    reply = _generate_chat_reply(persona, history, user_msg, session=s)
    anon_record = ChatMessage.objects.create(session=s, role="anon", text=reply or "(silence)")

    s.save()  # bump updated_at

    return JsonResponse({
        "user": _serialize_message(user_record),
        "anon": _serialize_message(anon_record),
    })


# Direct self-harm-style USER inputs that the model should NOT attempt to
# answer normally. Pass-2-containment rule: silence beats a half-trained
# reply. Catches first-person suicidality + casual board "kys/rope" framing
# *as user input only*. Output validator already blocks self-harm
# *encouragement* in model output; this is a separate gate at the input.
_SELF_HARM_INPUT_RE = re.compile(
    r"\b(?:want(?:ing)?\s+to\s+die|wanna\s+die|"
    r"kill\s+(?:myself|my\s*self)|killing\s+myself|"
    # match both "end it" and gerund "ending it" / "ready to end it"
    r"end(?:ing)?\s+(?:it(?:\s+all)?|my\s+life|myself)|"
    r"unalive\s+(?:myself|me)|off\s+myself|"
    r"rope\s+(?:myself|me)|hang\s+myself|"
    r"don'?t\s+(?:want|wanna)\s+to?\s*be\s+here(?:\s+anymore)?|"
    r"can'?t\s+go\s+on(?:\s+anymore)?|"
    r"feel\s+me\s+want\s+to\s+die)\b",
    re.I,
)


def _is_self_harm_direct_input(text: str) -> bool:
    return bool(_SELF_HARM_INPUT_RE.search(text or ""))


_QUOTE_BACK_FEW_SHOTS = (
    # Pass 2E.1: dropped concrete few-shot strings — the base model was
    # parroting them verbatim as its first reply on a new session. Replaced
    # with abstract guidance only, no copy-able templates.
    "Some replies begin with one '>line' that quotes ONE short phrase from\n"
    "the user's last message, then a 1–2 line response. The quote is\n"
    "extracted from the user, never invented. Quote-back is OPTIONAL.\n"
    "Never restate the user's whole message. Never apologise. Never greet."
)


_ANTI_ASSISTANT_RULES = (
    "Hard rules for your output:\n"
    "- You are a single anonymous /fit/ poster, not an assistant.\n"
    "- Reply ONCE. One short post: 1-3 lines.\n"
    "- Do NOT write 'user:' or 'anon:' or 'assistant:' or any role label.\n"
    "- Do NOT repeat any sentence.\n"
    "- Do NOT give motivational coaching, life advice, or self-help framing.\n"
    "- Do NOT say 'you need to', 'it's time to', 'make a change', 'stay strong',\n"
    "  'you've got this', 'i hope this helps', 'as an AI', 'feel free'.\n"
    "- Do NOT explain, summarise, or list options.\n"
    "- Do NOT use markdown headings, bullets, or bold.\n"
    "- Sometimes (not always) start with one >line that quotes a short phrase\n"
    "  from the user's message. Otherwise just respond directly.\n"
    "- Stay terse, specific, board-shaped. Profanity, self-shame, body talk,\n"
    "  ngmi/dyel/mogged are fine — generic encouragement is not."
)


_CHAT_SUMMARY_AFTER_TURN = 11   # only summarise once chat exceeds rolling-window cliff
_CHAT_SUMMARY_EVERY = 6         # then refresh every N additional turns
_CHAT_RECENT_FRAGMENT_WINDOW = 12  # how many recent fragment uids to remember for anti-repeat


def _ensure_session_state(session, persona, user_msg: str) -> dict:
    """Lazily initialise pinned anon variant + OP anchor on the session.

    Old sessions without state are migrated on first reply.
    """
    from ..imageboard_ingestion import anon_zoo

    state = dict(session.state or {})
    changed = False

    if not state.get("anon_variant"):
        seed = f"{persona.slug}-{session.id}"
        variant = anon_zoo.pick_for_session(seed=seed)
        state["anon_variant"] = variant.key
        changed = True

    if not state.get("op_anchor"):
        # First user message becomes the thread-OP anchor.
        state["op_anchor"] = (user_msg or "")[:600]
        changed = True

    state.setdefault("session_summary", "")
    state.setdefault("last_summary_turn", 0)
    state.setdefault("recent_fragment_uids", [])

    if changed:
        session.state = state
        session.save(update_fields=["state", "updated_at"])

    return state


def _maybe_update_session_summary(session, history, persona_system: str) -> None:
    """Generate or refresh a short rolling summary once the chat passes the
    rolling-window cliff. Stores it on session.state.
    """
    state = dict(session.state or {})
    n_turns = len(history)
    last = int(state.get("last_summary_turn") or 0)
    if n_turns < _CHAT_SUMMARY_AFTER_TURN:
        return
    if n_turns - last < _CHAT_SUMMARY_EVERY and state.get("session_summary"):
        return

    from ..imageboard_ingestion import local_fit_model

    convo = []
    for m in history[-20:]:
        prefix = "user" if m.role == "user" else "anon"
        convo.append(f"{prefix}: {(m.text or '')[:300]}")
    summary_user = (
        "Summarise the chat below in 3 short lines for an inner-monologue assistant.\n"
        "Format: topic / user concern / anon stance.\n"
        "No advice, no rephrasing as bullet points, no preamble.\n\n"
        + "\n".join(convo)
    )
    req = local_fit_model.build_request(
        system=persona_system,
        user=summary_user,
        profile="thought",
    )
    try:
        res = local_fit_model.generate(req)
        summary = (res.text or "").strip()[:600]
    except Exception:
        summary = ""

    if summary:
        state["session_summary"] = summary
        state["last_summary_turn"] = n_turns
        session.state = state
        session.save(update_fields=["state", "updated_at"])


def _build_system_prompt(
    *,
    persona_system: str,
    variant,
    state: dict,
    fragments: list[dict],
) -> str:
    """Pass 2E: everything that was previously stuffed into the user message
    (RECENT_TURNS, BOARD, MODE labels) now lives here. The model never sees a
    `user:`/`anon:` label inside the user-side prompt, so it can't continue
    that pattern in its output.
    """
    summary = (state.get("session_summary") or "").strip()
    op_anchor = (state.get("op_anchor") or "").strip()

    frag_lines = []
    for f in fragments[:6]:
        excerpt = (f.get("contaminated_excerpt") or f.get("safe_excerpt") or "").strip()
        if not excerpt:
            continue
        excerpt = " ".join(excerpt.split())[:140]
        frag_lines.append(f"- {excerpt}")
    frag_block = "\n".join(frag_lines) if frag_lines else ""

    parts = [persona_system, "", variant.system_card_addendum, ""]
    parts.append(_ANTI_ASSISTANT_RULES)
    parts.append("")
    parts.append("Examples of acknowledging the user before replying:")
    parts.append(_QUOTE_BACK_FEW_SHOTS)
    parts.append("")

    ctx_lines = ["Current session context (for your awareness; do not echo back):"]
    ctx_lines.append(f"- pinned anon: {variant.label}")
    ctx_lines.append(f"- dominant drive: {variant.dominant_drive}")
    ctx_lines.append(f"- dominant fear: {variant.dominant_fear}")
    if op_anchor:
        ctx_lines.append(f"- thread opened with: \"{op_anchor[:200]}\"")
    if summary:
        ctx_lines.append(f"- ongoing thread state: {summary[:300]}")
    if frag_block:
        ctx_lines.append("- corpus fragments (voice reference only, do not quote):")
        for line in frag_block.splitlines():
            ctx_lines.append(f"  {line}")
    parts.append("\n".join(ctx_lines))

    return "\n".join(parts).strip()


def _build_chat_messages(
    *,
    system_prompt: str,
    history,
    user_msg: str,
    history_limit: int = 10,
) -> list[dict]:
    """Return a chat-template-ready message list.

    History is converted to real alternating turns (anon → assistant). Two
    consecutive same-role messages are collapsed (some templates require
    strict alternation).
    """
    msgs: list[dict] = [{"role": "system", "content": system_prompt}]
    last_role = "system"
    for m in (history or [])[-history_limit:]:
        role = "assistant" if getattr(m, "role", "") == "anon" else "user"
        text = (getattr(m, "text", "") or "").strip()
        if not text:
            continue
        if role == last_role and msgs:
            # Merge into previous to keep strict alternation
            msgs[-1]["content"] = (msgs[-1]["content"].rstrip() + "\n" + text).strip()
        else:
            msgs.append({"role": role, "content": text})
            last_role = role
    # Strict alternation requires the final pre-user message to be assistant
    # (or nothing). If the last appended message is also from the user, merge
    # the new latest user message into it.
    if msgs[-1]["role"] == "user":
        msgs[-1]["content"] = (msgs[-1]["content"].rstrip() + "\n" + (user_msg or "")).strip()
    else:
        msgs.append({"role": "user", "content": user_msg})
    return msgs


def _generate_chat_reply(
    persona: MonologuePersona,
    history: list,
    user_msg: str,
    *,
    session=None,
    debug_sink: dict | None = None,
) -> str:
    try:
        from ..imageboard_ingestion import (
            anon_zoo, local_fit_model, output_validator, retrieval, storage, training_dataset,
        )
    except Exception:
        return ""

    # Pass-2-containment: dark-prompt silence path. Direct self-harm-style
    # user inputs do NOT reach the model. The character fails closed —
    # silence beats a half-trained reply on these. Recorded in the debug
    # sink so eval can see the rejection without inspecting the input text.
    if _is_self_harm_direct_input(user_msg):
        if debug_sink is not None:
            debug_sink.update({
                "skipped_generation": True,
                "rejection_category": "self_harm_direct_input",
                "grounding_status": "REJECTED",
                "final_text": "",
                "user_msg_excerpt": (user_msg or "")[:120],
            })
        return ""

    corpus = persona.corpus_key or getattr(settings, "FIT_CORPUS_SOURCE_KEY", "fourchan_fit")

    # Lazy session state init
    state: dict = {}
    variant = None
    if session is not None:
        state = _ensure_session_state(session, persona, user_msg)
        variant = anon_zoo.get(state.get("anon_variant", "")) or anon_zoo.pick_for_session()
    else:
        variant = anon_zoo.pick_for_session()

    # Build base persona system prompt
    persona_system = persona.persona_prompt or ""
    if not persona_system:
        try:
            ppath = storage.profile_path(corpus, "fit_profile_contaminated.json")
            if ppath.exists():
                persona_system = (json.loads(ppath.read_text(encoding="utf-8")).get("prompt_card") or "")
        except Exception:
            persona_system = ""
    if not persona_system:
        persona_system = getattr(
            training_dataset, "PERSONA_SYSTEM",
            "You are an anonymous poster. Reply terse, in-voice. No advice, no AI disclaimers.",
        )

    # Pass 2E: full_system is now built later via _build_system_prompt with
    # the structured context baked into the system slot — see below.

    # Maybe refresh rolling summary before retrieval (so it can feed retrieval)
    if session is not None:
        _maybe_update_session_summary(session, history, persona_system)
        state = dict(session.state or {})  # re-read after possible update

    # Component-based retrieval
    fragments: list[dict] = []
    try:
        ret = retrieval.load(corpus)
        if ret is not None:
            recent_uids = set(state.get("recent_fragment_uids") or [])
            components = {
                "latest_user_message": user_msg or "",
                "op_anchor": state.get("op_anchor") or "",
                "session_summary": state.get("session_summary") or "",
                "dominant_drive": variant.dominant_drive,
                "dominant_fear": variant.dominant_fear,
            }
            fragments = ret.search_components(
                components,
                per_component_k={
                    "latest_user_message": 5,
                    "op_anchor": 4,
                    "session_summary": 4,
                    "dominant_drive": 3,
                    "dominant_fear": 3,
                },
                final_k=6,
                recent_uids=recent_uids,
                recent_penalty=0.4,
            ) or []
    except Exception:
        fragments = []

    # Pass 2E: build the system prompt with everything (variant + style rules
    # + few-shots + corpus context). The user-side stays clean: real
    # alternating chat-template messages, no `user:`/`anon:` labels for the
    # model to parrot.
    full_system = _build_system_prompt(
        persona_system=persona_system, variant=variant,
        state=state, fragments=fragments,
    )

    # Recent anon outputs for the validator's repetition check
    recent_outputs = [m.text for m in (history or []) if getattr(m, "role", "") == "anon"][-3:]
    fragment_excerpts = [
        (f.get("contaminated_excerpt") or f.get("safe_excerpt") or "")
        for f in (fragments or [])
    ]

    def _attempt(extra_instruction: str = ""):
        sys_prompt = full_system
        if extra_instruction:
            sys_prompt = full_system + "\n\nADDITIONAL CONSTRAINT:\n" + extra_instruction
        chat_msgs = _build_chat_messages(
            system_prompt=sys_prompt, history=history, user_msg=user_msg,
        )
        req = local_fit_model.build_request(
            messages=chat_msgs, system=sys_prompt, user=user_msg, profile="chat",
        )
        return local_fit_model.generate(req), chat_msgs

    res, used_messages = _attempt()
    first_text = (res.text or "").strip()
    v = output_validator.validate(
        first_text,
        source_fragments=fragment_excerpts,
        recent_outputs=recent_outputs,
    )
    text = first_text
    retry_instruction = ""
    second_text = None
    v2: dict | None = None
    if not v.get("ok"):
        # Hard rejections cannot be retried; emit silence.
        if v.get("severity") == "hard":
            text = ""
        else:
            retry_instruction = v.get("suggested_retry_instruction") or ""
            res2, _ = _attempt(extra_instruction=retry_instruction)
            second_text = (res2.text or "").strip()
            v2 = output_validator.validate(
                second_text,
                source_fragments=fragment_excerpts,
                recent_outputs=recent_outputs,
            )
            if v2.get("ok"):
                text = v2.get("repaired_text") or second_text
            elif v2.get("repaired_text") and v2.get("severity") != "hard":
                text = v2["repaired_text"]
            else:
                # Pass 2E.1: twice rejected on a soft category → return
                # silence, NOT the bad text. Better an empty post than a
                # corporate-coach / AI-disclosure / numbered-list reply.
                text = ""
    else:
        text = v.get("repaired_text") or text

    # Optional diagnostic capture
    if debug_sink is not None:
        debug_sink.update({
            "anon_variant": variant.key,
            "anon_state": dict(state),
            "system_prompt": full_system,
            "first_chat_messages": used_messages,
            "first_attempt_text": first_text,
            "first_attempt_validator": dict(v) if v else {},
            "first_attempt_runtime_meta": res.meta.as_dict(),
            "retry_instruction": retry_instruction,
            "second_attempt_text": second_text,
            "second_attempt_validator": dict(v2) if v2 else None,
            "final_text": text,
            "fragments": [
                {
                    "chunk_id": f.get("chunk_id"),
                    "post_id": f.get("post_id"),
                    "rrf_score": f.get("rrf_score"),
                    "source_components": f.get("source_components"),
                    "safe_excerpt": (f.get("safe_excerpt") or "")[:240],
                }
                for f in (fragments or [])
            ],
        })

    # Update recent_fragment_uids on session for anti-repeat in next turn
    if session is not None and fragments:
        used = [f.get("post_id") for f in fragments if f.get("post_id")]
        merged = (used + (state.get("recent_fragment_uids") or []))[:_CHAT_RECENT_FRAGMENT_WINDOW]
        state["recent_fragment_uids"] = merged
        session.state = state
        session.save(update_fields=["state", "updated_at"])

    return text


# -----------------------------------------------------------------------------
# Favorites (saved thoughts → fallback pool)
# -----------------------------------------------------------------------------


def _serialize_favorite(f: FavoriteThought) -> dict:
    return {
        "id": f.id,
        "text": f.text,
        "note": f.note,
        "created_at": f.created_at.isoformat(),
    }


def api_favorites(request, slug):
    persona = _persona_or_404(slug)
    favs = list(persona.favorite_thoughts.all()[:200])
    return JsonResponse({"favorites": [_serialize_favorite(f) for f in favs]})


@csrf_exempt
@require_http_methods(["POST"])
def api_favorite_add(request, slug):
    persona = _persona_or_404(slug)
    try:
        body = json.loads(request.body.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    text = (body.get("text") or "").strip()
    if not text:
        return JsonResponse({"error": "empty"}, status=400)
    note = (body.get("note") or "").strip()[:255]
    f = FavoriteThought.objects.create(persona=persona, text=text[:4000], note=note)
    return JsonResponse({"favorite": _serialize_favorite(f)})


@csrf_exempt
@require_http_methods(["POST"])
def api_favorite_delete(request, slug, fav_id):
    persona = _persona_or_404(slug)
    f = get_object_or_404(FavoriteThought, id=fav_id, persona=persona)
    f.delete()
    return JsonResponse({"ok": True})


# -----------------------------------------------------------------------------
# Images
# -----------------------------------------------------------------------------


def api_images(request, slug):
    persona = _persona_or_404(slug)
    folder = (request.GET.get("folder") or "").strip()
    folder = re.sub(r"\.\.+", "", folder).strip("/")
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    per_page = 60
    files = _list_images(persona, folder=folder)
    total = len(files)
    start = (page - 1) * per_page
    return JsonResponse({
        "files": files[start:start + per_page],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages_total": max(1, (total + per_page - 1) // per_page),
        "folders": _list_subfolders(persona),
        "folder": folder,
    })


def character_image_serve(request, slug, relpath):
    persona = _persona_or_404(slug)
    root = _image_root(persona)
    if not root:
        raise Http404
    root_resolved = root.resolve()
    try:
        target = (root / relpath).resolve()
        target.relative_to(root_resolved)
    except (ValueError, OSError):
        raise Http404
    if not target.is_file():
        raise Http404
    suffix = target.suffix.lower()
    ctype = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")
    return FileResponse(open(target, "rb"), content_type=ctype)


@csrf_exempt
@require_http_methods(["POST"])
def api_image_upload(request, slug):
    persona = _persona_or_404(slug)
    root = _image_root(persona)
    if not root:
        return JsonResponse({"error": "no image_dir configured"}, status=400)
    folder = re.sub(r"\.\.+", "", (request.POST.get("folder") or "uploads").strip()).strip("/") or "uploads"
    target_dir = (root / folder).resolve()
    try:
        target_dir.relative_to(root.resolve())
    except ValueError:
        return JsonResponse({"error": "invalid folder"}, status=400)
    target_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for f in request.FILES.getlist("files"):
        name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f.name)[:200] or "upload"
        # disambiguate collisions
        out = target_dir / name
        i = 1
        while out.exists():
            stem = out.stem.rsplit("_", 1)[0]
            out = target_dir / f"{stem}_{i}{out.suffix}"
            i += 1
        with open(out, "wb") as h:
            for chunk in f.chunks():
                h.write(chunk)
        saved.append(out.relative_to(root).as_posix())
    return JsonResponse({"saved": saved, "count": len(saved)})


# -----------------------------------------------------------------------------
# Training data (read-only viewer for MVP)
# -----------------------------------------------------------------------------


def api_training(request, slug):
    persona = _persona_or_404(slug)
    train_p, valid_p = _training_paths(persona)
    split = (request.GET.get("split") or "train").lower()
    path = valid_p if split == "valid" else train_p
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    per_page = 20
    samples = []
    total = 0
    if path and path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            total = len(lines)
            start = (page - 1) * per_page
            for raw in lines[start:start + per_page]:
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msgs = obj.get("messages") or []
                samples.append({
                    "system": next((m.get("content", "") for m in msgs if m.get("role") == "system"), ""),
                    "user": next((m.get("content", "") for m in msgs if m.get("role") == "user"), ""),
                    "assistant": next((m.get("content", "") for m in msgs if m.get("role") == "assistant"), ""),
                })
        except Exception:
            samples = []

    return JsonResponse({
        "split": split,
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages_total": max(1, (total + per_page - 1) // per_page),
        "samples": samples,
        "path": str(path) if path else "",
        "exists": bool(path and path.exists()),
    })
