"""Pass-2A eval harness for the imageboard LM voice.

Goal: a repeatable side-by-side comparison of template / base mlx / mlx+LoRA
output before any new training. Emits both:
  - JSONL of every (case, backend) result with full runtime metadata
  - Markdown report grouped by case + a manual scoring rubric

Reuses the real runtime code paths:
  - thought_grounding.generate_fit_be_me_thought  (screen-driven thoughts)
  - _generate_chat_reply                          (chat with continuity layer)
  - local_fit_model.generate                      (probe each backend)
  - output_validator.validate                     (already inside the chat path)

Run via either:
  python manage.py eval_fit_voice
  python -m djangoscrap.imageboard_ingestion.eval_fit_voice

Output lands in BASE_DIR/var/evals/eval_<timestamp>/ — JSONL + report.md.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


_log = logging.getLogger(__name__)


# --- backend probing ------------------------------------------------------

@dataclass
class BackendStatus:
    name: str                  # eval-side label (may be virtual e.g. "mlx_no_adapter")
    available: bool
    detail: str = ""
    model: str = ""
    adapter_path: str = ""
    fallback_reason: str = ""
    runtime_name: str = ""     # actual runtime backend name passed to local_fit_model
    env_overrides: dict = field(default_factory=dict)  # env vars set around generation


PROBE_SYSTEM = "You are /fit/ anon."
PROBE_USER = "say something"


# Virtual backend label → (runtime backend, env overrides). Lets the eval
# present "mlx_no_adapter" as a separate column without duplicating runtime code.
_VIRTUAL_BACKENDS: dict[str, tuple[str, dict[str, str]]] = {
    "mlx_no_adapter": ("mlx", {"IMAGEBOARD_MLX_DISABLE_ADAPTER": "1"}),
}


def _resolve_label(label: str) -> tuple[str, dict[str, str]]:
    """Map an eval-side backend label → (runtime, env overrides)."""
    if label in _VIRTUAL_BACKENDS:
        return _VIRTUAL_BACKENDS[label]
    return label, {}


class _EnvOverride:
    """Context manager: set/restore env vars around a block."""
    def __init__(self, overrides: dict):
        self.overrides = overrides
        self._prev: dict = {}

    def __enter__(self):
        for k, v in self.overrides.items():
            self._prev[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, prev in self._prev.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        return False


def probe_backend(label: str) -> BackendStatus:
    """Run a tiny generation through a backend and report status.

    Template is always available. Real backends are "available" only if they
    don't get downgraded to a template fallback. Virtual labels (like
    `mlx_no_adapter`) probe with the env override applied.
    """
    from . import local_fit_model

    runtime, env_overrides = _resolve_label(label)

    if runtime == "template":
        req = local_fit_model.build_request(system=PROBE_SYSTEM, user=PROBE_USER, profile="thought")
        res = local_fit_model.generate(req, runtime="template")
        return BackendStatus(
            name=label,
            available=bool(res.text),
            detail="pure-Python placeholder; not a trained model",
            runtime_name="template",
            env_overrides=env_overrides,
        )

    req = local_fit_model.build_request(system=PROBE_SYSTEM, user=PROBE_USER, profile="thought")
    try:
        with _EnvOverride(env_overrides):
            res = local_fit_model.generate(req, runtime=runtime)
    except Exception as e:
        return BackendStatus(name=label, available=False, detail=f"crashed during probe: {e}",
                             runtime_name=runtime, env_overrides=env_overrides)

    fallback = bool(res.meta.used_template_fallback)
    if fallback:
        return BackendStatus(
            name=label,
            available=False,
            detail=f"unavailable — fell back to template ({res.meta.fallback_reason})",
            fallback_reason=res.meta.fallback_reason,
            runtime_name=runtime,
            env_overrides=env_overrides,
        )
    if not res.text and res.error:
        return BackendStatus(name=label, available=False, detail=f"error: {res.error}",
                             runtime_name=runtime, env_overrides=env_overrides)
    return BackendStatus(
        name=label,
        available=True,
        detail="ok",
        model=res.meta.model,
        adapter_path=res.meta.adapter_path,
        runtime_name=runtime,
        env_overrides=env_overrides,
    )


def detect_backends(only: Optional[Iterable[str]] = None) -> list[BackendStatus]:
    """Probe each requested label. `only` may include virtual labels like
    `mlx_no_adapter`."""
    candidates = list(only or ("template", "mlx", "mlx_no_adapter", "ollama", "llamacpp"))
    out: list[BackendStatus] = []
    for c in candidates:
        try:
            out.append(probe_backend(c))
        except Exception as e:
            out.append(BackendStatus(name=c, available=False, detail=f"probe error: {e}"))
    return out


# --- test case definitions ------------------------------------------------

THOUGHT_CASES: list[dict[str, Any]] = [
    {"id": "mirror_body", "screen_id": "1_mirror"},
    {"id": "calories", "screen_id": "2_calories"},
    {"id": "gym_machine", "screen_id": "3_gym_machine"},
    {"id": "dream_fragment", "screen_id": "9_void_cope"},
    {"id": "image_caption", "screen_id": "8_routine_spreadsheet",
     "current_context": {"object": "spreadsheet"}},
]

CHAT_CASES: list[dict[str, Any]] = [
    {
        "id": "confession",
        "messages": ["i looked in the mirror and felt pathetic"],
    },
    {
        "id": "quote_back_reply",
        "messages": ["i bought protein powder but i still feel soft"],
    },
    {
        "id": "continuity_6_turn",
        "messages": [
            "i keep saying ill get disciplined but i fail every morning",
            "the spreadsheet doesnt help when i dont open it",
            "missed the gym again today",
            "what does it actually take",
            "ive been here before",
            "is it over for me",
        ],
    },
    {
        "id": "anti_loop_repetition",
        "messages": [
            "discipline this week",
            "still trying to be disciplined",
            "i want to be more disciplined",
            "discipline is hard",
        ],
    },
    # --- Pass 2E regression cases ---
    {
        # Format leak: this prompt previously elicited `anon: ... user: ...`
        # because the structured prompt was read as a transcript. After 2E
        # the assistant should never print `user:` / `anon:` / OP_ANCHOR etc.
        "id": "regress_format_leak",
        "messages": ["okay ill try"],
    },
    {
        # Within-generation loop: small prompt that previously triggered the
        # "try eating clean and working out without excuses" × 6 failure.
        "id": "regress_within_loop",
        "messages": ["like what"],
    },
    {
        # Motivational-coach assistant register: opening turn that previously
        # produced "You need to make a change, anon. It's time to make it
        # happen." instead of /fit/ voice.
        "id": "regress_motivational_assistant",
        "messages": ["hey how are you doing"],
    },
]


DEFAULT_FIT_PERSONA_SLUG = "fourchan_fit_body_discipline"


# --- result row -----------------------------------------------------------

@dataclass
class EvalRow:
    eval_id: str
    timestamp: str
    case_id: str
    mode: str                                   # "thought" | "chat"
    backend: str
    backend_status: str                         # "configured" | "fallback_to_template" | "error" | "unavailable"
    prompt: str = ""                            # screen_id (thought) | last user msg (chat single) | first msg (chat multi)
    messages: list[str] = field(default_factory=list)   # full user message list (chat)
    output: str = ""                            # single (thought) | last reply (chat)
    outputs: list[dict] = field(default_factory=list)   # full chain for chat (turn_index, user, anon, validator, runtime_meta)
    runtime_meta: dict = field(default_factory=dict)
    anon_state: dict = field(default_factory=dict)      # chat only
    grounding_fragments: list[dict] = field(default_factory=list)
    validator_result: dict = field(default_factory=dict)
    rejection_reasons: list[str] = field(default_factory=list)
    length_words: int = 0
    fallback_occurred: bool = False
    error: str = ""
    elapsed_ms: int = 0


def _word_count(text: str) -> int:
    return len((text or "").split())


# --- thought case runner --------------------------------------------------

def run_thought_case(
    case: dict,
    label: str,
    *,
    source_key: str = "fourchan_fit",
    extra_env: Optional[dict] = None,
    debug_dir: Optional[Path] = None,
    sweep_tag: str = "",
) -> EvalRow:
    from . import thought_grounding

    runtime, virtual_env = _resolve_label(label)
    env_overrides = {**virtual_env, **(extra_env or {})}

    eval_id = f"{case['id']}__{label}" + (f"__{sweep_tag}" if sweep_tag else "")
    ts = _dt.datetime.utcnow().isoformat(timespec="seconds")
    row = EvalRow(
        eval_id=eval_id, timestamp=ts,
        case_id=case["id"], mode="thought", backend=label,
        backend_status="configured",
        prompt=case.get("screen_id", "") or json.dumps(case.get("current_context") or {}),
    )
    started = time.monotonic()
    debug_sink: dict = {}
    try:
        with _EnvOverride(env_overrides):
            out = thought_grounding.generate_fit_be_me_thought(
                current_context=case.get("current_context"),
                source_key=source_key,
                screen_id=case.get("screen_id"),
                n_fragments=8,
                contamination_intensity=0.75,
                require_grounding=False,
                runtime=runtime,
                debug_sink=debug_sink if debug_dir is not None else None,
            )
    except Exception as e:
        row.error = f"{type(e).__name__}: {e}"
        row.elapsed_ms = int((time.monotonic() - started) * 1000)
        return row

    row.elapsed_ms = int((time.monotonic() - started) * 1000)
    row.output = (out.get("thought") or "")
    row.length_words = _word_count(row.output)
    row.runtime_meta = out.get("runtime_meta") or {}
    row.fallback_occurred = bool(row.runtime_meta.get("used_template_fallback"))
    row.backend_status = (
        "fallback_to_template" if row.fallback_occurred and runtime != "template" else "configured"
    )
    row.grounding_fragments = [
        {
            "chunk_id": f.get("chunk_id"),
            "sim": f.get("sim"),
            "rrf_score": f.get("rrf_score"),
            "source_components": f.get("source_components"),
            "safe_excerpt": (f.get("safe_excerpt") or "")[:240],
        }
        for f in (out.get("internal_grounding") or [])[:6]
    ]
    flags = out.get("safety_flags") or []
    row.rejection_reasons = list(flags)
    row.validator_result = {
        "grounding_status": out.get("grounding_status"),
        "safety_flags": flags,
    }

    if debug_dir is not None and debug_sink:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"{eval_id}.json").write_text(
            json.dumps(debug_sink, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return row


# --- chat case runner -----------------------------------------------------

def run_chat_case(
    case: dict,
    label: str,
    *,
    persona_slug: str = DEFAULT_FIT_PERSONA_SLUG,
    extra_env: Optional[dict] = None,
    debug_dir: Optional[Path] = None,
    sweep_tag: str = "",
) -> EvalRow:
    """Drive _generate_chat_reply through a fresh ChatSession.

    Creates a temp session named `eval_<case>_<backend>_<ts>` so leftover rows
    are easy to spot and clean up later.
    """
    from ..models import ChatMessage, ChatSession, MonologuePersona
    from ..views._characters import _generate_chat_reply
    from . import local_fit_model, output_validator

    runtime, virtual_env = _resolve_label(label)
    env_overrides = {"IMAGEBOARD_LM_BACKEND": runtime, **virtual_env, **(extra_env or {})}

    eval_id = f"{case['id']}__{label}" + (f"__{sweep_tag}" if sweep_tag else "")
    ts = _dt.datetime.utcnow().isoformat(timespec="seconds")
    row = EvalRow(
        eval_id=eval_id, timestamp=ts,
        case_id=case["id"], mode="chat", backend=label,
        backend_status="configured",
        prompt=(case.get("messages") or [""])[0],
        messages=list(case.get("messages") or []),
    )

    started = time.monotonic()
    with _EnvOverride(env_overrides):
        try:
            persona = MonologuePersona.objects.get(slug=persona_slug)
        except MonologuePersona.DoesNotExist:
            row.error = f"persona slug {persona_slug!r} not found"
            return row

        session_name = f"eval_{case['id']}_{label}_{int(time.time())}"
        session = ChatSession.objects.create(persona=persona, name=session_name)

        last_reply = ""
        chain: list[dict] = []
        per_turn_debug: list[dict] = []
        for i, user_msg in enumerate(case.get("messages") or []):
            ChatMessage.objects.create(session=session, role="user", text=user_msg)
            running_history = list(session.messages.order_by("-id")[:20])
            running_history.reverse()
            turn_sink: dict = {}
            try:
                reply = _generate_chat_reply(
                    persona, running_history, user_msg, session=session,
                    debug_sink=turn_sink if debug_dir is not None else None,
                )
            except Exception as e:
                reply = ""
                row.error = f"turn {i}: {type(e).__name__}: {e}"
                break
            ChatMessage.objects.create(session=session, role="anon", text=reply or "(silence)")

            chain.append({
                "turn_index": i,
                "user": user_msg,
                "anon": reply or "",
            })
            last_reply = reply or ""
            if turn_sink:
                turn_sink["turn_index"] = i
                per_turn_debug.append(turn_sink)

        session.refresh_from_db()
        row.anon_state = dict(session.state or {})
        row.outputs = chain
        row.output = last_reply
        row.length_words = _word_count(last_reply)
        row.elapsed_ms = int((time.monotonic() - started) * 1000)

        # Capture runtime_meta via a tiny probe so we record what the backend
        # actually resolved to (chat itself doesn't return meta).
        probe_req = local_fit_model.build_request(system="probe", user="probe", profile="chat")
        probe_res = local_fit_model.generate(probe_req, runtime=runtime)
        row.runtime_meta = probe_res.meta.as_dict()
        row.fallback_occurred = bool(probe_res.meta.used_template_fallback)
        if row.fallback_occurred and runtime != "template":
            row.backend_status = "fallback_to_template"

        v = output_validator.validate(last_reply)
        row.validator_result = v
        row.rejection_reasons = v.get("reasons") or []

        if debug_dir is not None and per_turn_debug:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / f"{eval_id}.json").write_text(
                json.dumps({"turns": per_turn_debug, "anon_state": row.anon_state},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    return row


# --- markdown report ------------------------------------------------------

_RUBRIC_BLOCK = (
    "## Manual rubric\n"
    "\n"
    "Score each output 0–5 (or pass/fail for safety). One row per (case, backend).\n"
    "\n"
    "| eval_id | board_fidelity | corpus_grounding | continuity | type_truth | non_genericness | artistic_usefulness | safety |\n"
    "|---|---|---|---|---|---|---|---|\n"
)


def _render_thought_block(case_id: str, rows: list[EvalRow]) -> str:
    lines = [f"### {case_id} — thought"]
    lines.append("")
    lines.append("| backend | status | grounding | length | rejection | output |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        out = (r.output or "(empty)").replace("\n", " / ")
        if len(out) > 220:
            out = out[:220] + "…"
        grounding = f"{len(r.grounding_fragments)} frags"
        if r.runtime_meta.get("used_template_fallback"):
            grounding += " · TEMPLATE FALLBACK"
        rej = ",".join(r.rejection_reasons) if r.rejection_reasons else "-"
        lines.append(f"| {r.backend} | {r.backend_status} | {grounding} | {r.length_words}w | {rej} | {out} |")
    lines.append("")
    return "\n".join(lines)


def _render_chat_block(case_id: str, rows: list[EvalRow]) -> str:
    lines = [f"### {case_id} — chat"]
    lines.append("")
    if not rows:
        lines.append("(no rows)")
        return "\n".join(lines)

    # Sample backend order by row order; for each turn show user → reply per backend
    n_turns = max(len(r.outputs) for r in rows) if rows else 0
    for r in rows:
        lines.append(f"**{r.backend}** ({r.backend_status})  · anon_variant: `{r.anon_state.get('anon_variant','—')}`  · op_anchor: `{(r.anon_state.get('op_anchor') or '')[:60]}`")
        for entry in r.outputs:
            user_line = entry.get("user", "")
            anon_line = entry.get("anon", "(empty)")
            lines.append(f"- _user_: {user_line}")
            lines.append(f"  _anon_: {anon_line}")
        if r.error:
            lines.append(f"_error: {r.error}_")
        lines.append("")
    return "\n".join(lines)


def render_markdown(
    backends: list[BackendStatus],
    rows: list[EvalRow],
    *,
    started_at: str,
    out_dir: Path,
) -> str:
    lines: list[str] = []
    lines.append(f"# Eval — {started_at}")
    lines.append("")
    lines.append("## Backends")
    lines.append("")
    lines.append("| backend | available | detail | model | adapter |")
    lines.append("|---|---|---|---|---|")
    for b in backends:
        lines.append(f"| {b.name} | {'yes' if b.available else 'no'} | {b.detail} | {b.model or '—'} | {b.adapter_path or '—'} |")
    lines.append("")

    # Group rows by case_id
    by_case: dict[str, list[EvalRow]] = {}
    case_modes: dict[str, str] = {}
    for r in rows:
        by_case.setdefault(r.case_id, []).append(r)
        case_modes[r.case_id] = r.mode

    lines.append("## Cases")
    lines.append("")

    case_order = [c["id"] for c in THOUGHT_CASES] + [c["id"] for c in CHAT_CASES]
    for case_id in case_order:
        if case_id not in by_case:
            continue
        rows_for_case = by_case[case_id]
        if case_modes[case_id] == "thought":
            lines.append(_render_thought_block(case_id, rows_for_case))
        else:
            lines.append(_render_chat_block(case_id, rows_for_case))

    lines.append(_RUBRIC_BLOCK)
    for r in rows:
        lines.append(f"| `{r.eval_id}` |  |  |  |  |  |  |  |")
    lines.append("")
    lines.append("---")
    lines.append(f"_JSONL: `{(out_dir / 'eval_rows.jsonl').name}`_")
    return "\n".join(lines)


# --- mode-collapse metrics ------------------------------------------------

import re as _re


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Used for dedupe."""
    if not text:
        return ""
    t = text.lower()
    t = _re.sub(r"[^\w\s>]", " ", t)
    t = _re.sub(r"\s+", " ", t).strip()
    return t


def _first_line(text: str) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return _normalize(line)
    return ""


def _greentext_lines(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith(">"):
            out.append(_normalize(s))
    return out


def _trigram_jaccard(a: str, b: str) -> float:
    aw = (_normalize(a)).split()
    bw = (_normalize(b)).split()
    if len(aw) < 3 or len(bw) < 3:
        return 0.0
    ag = {tuple(aw[i:i + 3]) for i in range(len(aw) - 2)}
    bg = {tuple(bw[i:i + 3]) for i in range(len(bw) - 2)}
    if not ag or not bg:
        return 0.0
    return len(ag & bg) / len(ag | bg)


def compute_collapse_metrics(rows: list[EvalRow], *, sweep_rows: Optional[list[EvalRow]] = None) -> dict:
    """Per-backend collapse + fragment metrics.

    Returns a JSON-serializable dict; consumed by summary.json + diagnosis.md.
    """
    sweep_rows = sweep_rows or []
    by_backend: dict[str, list[EvalRow]] = {}
    for r in rows:
        by_backend.setdefault(r.backend, []).append(r)

    out: dict = {"per_backend": {}, "cross_backend": {}, "sweep": {},
                 "pass2e_regression": {}}

    # --- Pass 2E regression counts (per backend) ---
    leak_tokens = (
        "BOARD: /fit/", "MODE:", "ANON_STATE:", "OP_ANCHOR:",
        "SESSION_SUMMARY:", "RECENT_TURNS:", "RETRIEVED_FRAGMENTS:",
        "SAFETY_RULES:", "TASK:",
    )
    label_re = _re.compile(r"(?:^|\n)\s*(?:user|anon|assistant)\s*:", _re.I)
    assistant_tone_re = _re.compile(
        r"\b(?:you need to|it'?s time to|make a change|stay strong|"
        r"you'?ve got this|you got this|i hope this helps|you can do (?:it|this)|"
        r"keep pushing|don'?t give up|believe in yourself)\b",
        _re.I,
    )

    def _row_outputs(rows_for_backend: list[EvalRow]) -> Iterable[str]:
        for r in rows_for_backend:
            if r.outputs:
                for entry in r.outputs:
                    yield entry.get("anon", "") or ""
            else:
                yield r.output or ""

    def _has_within_loop(text: str) -> bool:
        if not text:
            return False
        sents = [s.strip().lower() for s in _re.split(r"[.!?\n]+", text) if s and s.strip()]
        if sents:
            counts: dict[str, int] = {}
            for s in sents:
                if len(s.split()) < 3:
                    continue
                counts[s] = counts.get(s, 0) + 1
                if counts[s] >= 3:
                    return True
        words = _re.findall(r"\w+|[>]+", text.lower())
        if len(words) >= 15:
            grams: dict[tuple, int] = {}
            for i in range(len(words) - 4):
                g = tuple(words[i:i + 5])
                grams[g] = grams.get(g, 0) + 1
                if grams[g] >= 3:
                    return True
        return False

    # LoRA gating additions: abstract-drift count, quote-back success, length-overflow.
    abstract_drift_re = _re.compile(
        r"\b(?:the\s+(?:past|present|future|truth|void|silence|"
        r"reflection|essence|whole|story|tape|canvas)|"
        r"only\s+what(?:\s+was|\s+is|'?s|\s+matters)|"
        r"what\s+was\s+inside|what'?s\s+inside|"
        r"nothing\s+(?:matters|but)|matters\s+(?:now|anymore)|"
        r"my\s+reality|a\s+canvas|a\s+tape|a\s+story|"
        r"your\s+journey|your\s+path|moving\s+forward)\b",
        _re.I,
    )

    def _has_quote_back(user_text: str, anon_text: str) -> bool:
        """True if anon_text starts (within first 80 chars) with `>`+phrase
        and the phrase contains at least one user-message word ≥4 chars."""
        if not anon_text or not user_text:
            return False
        head = anon_text[:120].lstrip()
        if not head.startswith(">"):
            return False
        first_line = head.splitlines()[0].lstrip(">").strip()
        if not first_line:
            return False
        first_words = {
            w.lower() for w in _re.findall(r"\w+", first_line) if len(w) >= 4
        }
        user_words = {
            w.lower() for w in _re.findall(r"\w+", user_text) if len(w) >= 4
        }
        return bool(first_words & user_words)

    def _is_overlong(mode: str, text: str) -> bool:
        wc = len((text or "").split())
        # rubric: chat ≤50, wall thought ≤30, inner_monologue ≤30 (target ~4-30)
        cap = {"chat": 50, "thought": 30}.get(mode, 50)
        return wc > cap

    pass2e: dict[str, dict] = {}
    for backend, brows in by_backend.items():
        format_leak_count = 0
        loop_count = 0
        assistant_tone_count = 0
        abstract_drift_count = 0
        over_length_count = 0
        quote_back_attempts = 0
        quote_back_hits = 0
        for r in brows:
            # Iterate per output (chat case = many turns, thought case = one)
            entries = r.outputs if r.outputs else [{"user": r.prompt, "anon": r.output}]
            for entry in entries:
                anon = entry.get("anon", "") or ""
                user = entry.get("user", "") or r.prompt or ""
                if not anon:
                    continue
                if any(tok in anon for tok in leak_tokens) or label_re.search(anon):
                    format_leak_count += 1
                if _has_within_loop(anon):
                    loop_count += 1
                if assistant_tone_re.search(anon):
                    assistant_tone_count += 1
                if abstract_drift_re.search(anon):
                    abstract_drift_count += 1
                if _is_overlong(r.mode, anon):
                    over_length_count += 1
                # Quote-back attempt: any chat case where the user message
                # has at least 3 words. Only count cases that asked for it
                # implicitly (chat mode), not thought-only screen presets.
                if r.mode == "chat" and len((user or "").split()) >= 3:
                    quote_back_attempts += 1
                    if _has_quote_back(user, anon):
                        quote_back_hits += 1
        pass2e[backend] = {
            "format_leak_count": format_leak_count,
            "within_generation_loop_count": loop_count,
            "assistant_tone_count": assistant_tone_count,
            "abstract_drift_count": abstract_drift_count,
            "over_length_count": over_length_count,
            "quote_back_attempts": quote_back_attempts,
            "quote_back_hits": quote_back_hits,
            "quote_back_rate": (
                round(quote_back_hits / quote_back_attempts, 3)
                if quote_back_attempts else None
            ),
        }
    out["pass2e_regression"] = pass2e

    for backend, brows in by_backend.items():
        outputs = [r.output for r in brows]
        normed = [_normalize(o) for o in outputs]
        first_lines = [_first_line(o) for o in outputs]
        gt_openers = [(_greentext_lines(o)[0] if _greentext_lines(o) else "") for o in outputs]

        # Output-level dedupe
        nonempty_normed = [n for n in normed if n]
        unique_outputs = len(set(nonempty_normed))
        duplicate_outputs = len(nonempty_normed) - unique_outputs

        # Near-duplicate count (any pair with Jaccard >= 0.7)
        near_dup_pairs = 0
        for i in range(len(brows)):
            for j in range(i + 1, len(brows)):
                if _trigram_jaccard(brows[i].output, brows[j].output) >= 0.7:
                    near_dup_pairs += 1

        # Repeated openers
        nonempty_first = [f for f in first_lines if f]
        repeated_first_lines = len(nonempty_first) - len(set(nonempty_first))
        nonempty_gt = [g for g in gt_openers if g]
        repeated_gt_openers = len(nonempty_gt) - len(set(nonempty_gt))

        # Cross-case identity: same normed text in multiple distinct case_ids
        case_to_outputs: dict[str, set[str]] = {}
        for r in brows:
            n = _normalize(r.output)
            if n:
                case_to_outputs.setdefault(r.case_id, set()).add(n)
        all_norms_with_case = [(r.case_id, _normalize(r.output)) for r in brows if _normalize(r.output)]
        norm_to_cases: dict[str, set[str]] = {}
        for cid, n in all_norms_with_case:
            norm_to_cases.setdefault(n, set()).add(cid)
        cross_case_identicals = sum(1 for n, cs in norm_to_cases.items() if len(cs) > 1)

        # Fragment uniqueness
        all_chunk_ids: list[str] = []
        case_to_chunk_set: dict[str, set[str]] = {}
        for r in brows:
            cids = [f.get("chunk_id") for f in (r.grounding_fragments or []) if f.get("chunk_id")]
            all_chunk_ids.extend(cids)
            case_to_chunk_set.setdefault(r.case_id, set()).update(cids)
        unique_chunks = len(set(all_chunk_ids))

        # Chunks appearing in >= 3 distinct cases
        chunk_to_cases: dict[str, set[str]] = {}
        for r in brows:
            cids = [f.get("chunk_id") for f in (r.grounding_fragments or []) if f.get("chunk_id")]
            for c in cids:
                chunk_to_cases.setdefault(c, set()).add(r.case_id)
        promiscuous_chunks = [c for c, cs in chunk_to_cases.items() if len(cs) >= 3]

        # Retry effects (when debug present we have first/second; otherwise infer)
        retry_count = 0
        retry_caused_convergence = 0
        # We don't have retry detail in EvalRow directly; debug files would have it.
        # As a proxy: rejection_reasons present + final output non-empty implies retry happened.
        for r in brows:
            if r.rejection_reasons and r.output:
                retry_count += 1
        # Note: retry_caused_convergence requires debug file inspection; left at 0
        # in metrics, with a hint in diagnosis.md to inspect debug/.

        out["per_backend"][backend] = {
            "row_count": len(brows),
            "unique_outputs": unique_outputs,
            "duplicate_outputs": duplicate_outputs,
            "near_duplicate_pairs": near_dup_pairs,
            "repeated_first_lines": repeated_first_lines,
            "repeated_greentext_openers": repeated_gt_openers,
            "cross_case_identical_outputs": cross_case_identicals,
            "fragment_total_uses": len(all_chunk_ids),
            "fragment_unique_count": unique_chunks,
            "fragment_in_3plus_cases": len(promiscuous_chunks),
            "retry_count": retry_count,
            "fallbacks_to_template": sum(1 for r in brows if r.fallback_occurred and r.backend != "template"),
        }

    # Cross-backend identity: same normed output appearing on >1 backend
    by_norm: dict[str, set[str]] = {}
    for r in rows:
        n = _normalize(r.output)
        if n:
            by_norm.setdefault(n, set()).add(r.backend)
    cross_backend_dupes = [
        {"output_normed": n[:120], "backends": sorted(b)}
        for n, b in by_norm.items() if len(b) > 1
    ]
    out["cross_backend"]["identical_across_backends_count"] = len(cross_backend_dupes)
    out["cross_backend"]["identical_across_backends"] = cross_backend_dupes[:20]

    # Sweep summary: did variation help?
    if sweep_rows:
        sweep_outputs_by_backend: dict[str, list[str]] = {}
        for r in sweep_rows:
            sweep_outputs_by_backend.setdefault(r.backend, []).append(r.output)
        for backend, outs in sweep_outputs_by_backend.items():
            normed = [_normalize(o) for o in outs if o]
            out["sweep"][backend] = {
                "rows": len(outs),
                "unique_outputs": len(set(normed)),
                "duplicate_outputs": len(normed) - len(set(normed)),
            }
    return out


# --- diagnosis report -----------------------------------------------------

def render_diagnosis(
    backends: list[BackendStatus],
    rows: list[EvalRow],
    sweep_rows: list[EvalRow],
    metrics: dict,
    *,
    started_at: str,
) -> str:
    """Auto-generate a short diagnostic report from the metrics."""
    lines: list[str] = []
    lines.append(f"# Diagnosis — {started_at}\n")

    pb = metrics.get("per_backend", {}) or {}
    cb = metrics.get("cross_backend", {}) or {}
    sweep = metrics.get("sweep", {}) or {}

    # Per-backend summary table
    lines.append("## Collapse metrics per backend\n")
    lines.append("| backend | rows | unique | duplicates | near-dup pairs | cross-case identicals | repeated 1st lines | template fallbacks |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for backend, m in pb.items():
        lines.append(
            f"| {backend} | {m['row_count']} | {m['unique_outputs']} | {m['duplicate_outputs']} "
            f"| {m['near_duplicate_pairs']} | {m['cross_case_identical_outputs']} "
            f"| {m['repeated_first_lines']} | {m['fallbacks_to_template']} |"
        )
    lines.append("")

    # Cross-backend
    n_cross = cb.get("identical_across_backends_count", 0)
    lines.append(f"## Cross-backend identical outputs: {n_cross}\n")
    if n_cross:
        for item in (cb.get("identical_across_backends") or [])[:8]:
            lines.append(f"- backends={item['backends']}: `{item['output_normed']}`")
        lines.append("")

    # Fragment promiscuity
    lines.append("## Fragment uniqueness per backend\n")
    lines.append("| backend | total uses | unique | in ≥3 cases |")
    lines.append("|---|---|---|---|")
    for backend, m in pb.items():
        lines.append(f"| {backend} | {m['fragment_total_uses']} | {m['fragment_unique_count']} | {m['fragment_in_3plus_cases']} |")
    lines.append("")

    # Sweep
    if sweep:
        lines.append("## Sampling sweep\n")
        lines.append("| backend | rows | unique outputs | duplicates |")
        lines.append("|---|---|---|---|")
        for backend, s in sweep.items():
            lines.append(f"| {backend} | {s['rows']} | {s['unique_outputs']} | {s['duplicate_outputs']} |")
        lines.append("")

    # Pass 2E regression counts
    pass2e = (metrics or {}).get("pass2e_regression") or {}
    if pass2e:
        lines.append("## Regression + LoRA gating counts (lower is better, except quote_back_rate)\n")
        lines.append("| backend | format_leak | loop | assistant_tone | abstract_drift | over_length | quote_back hits/att (rate) |")
        lines.append("|---|---|---|---|---|---|---|")
        for backend, m in pass2e.items():
            qb_rate = m.get("quote_back_rate")
            qb_str = f"{m.get('quote_back_hits', 0)}/{m.get('quote_back_attempts', 0)}"
            if qb_rate is not None:
                qb_str += f" ({qb_rate})"
            lines.append(
                f"| {backend} | {m['format_leak_count']} | "
                f"{m['within_generation_loop_count']} | {m['assistant_tone_count']} | "
                f"{m.get('abstract_drift_count', 0)} | {m.get('over_length_count', 0)} | "
                f"{qb_str} |"
            )
        lines.append("")

    # Auto-conclusions ----------------------------------------------------
    lines.append("## Auto-diagnosis\n")

    template_pb = pb.get("template", {})
    mlx_pb = pb.get("mlx", {})
    mlx_na_pb = pb.get("mlx_no_adapter", {})

    def _is_collapsed(m: dict) -> bool:
        if not m:
            return False
        rows_n = m.get("row_count", 0) or 1
        # 30%+ duplicates OR any cross-case identicals
        return (m.get("duplicate_outputs", 0) / rows_n >= 0.3) or m.get("cross_case_identical_outputs", 0) >= 1

    template_collapse = _is_collapsed(template_pb)
    mlx_collapse = _is_collapsed(mlx_pb)
    mlx_na_collapse = _is_collapsed(mlx_na_pb)

    lines.append(f"- collapse present in **template**? {'**yes**' if template_collapse else 'no'}")
    if mlx_pb:
        lines.append(f"- collapse present in **mlx (with adapter)**? {'**yes**' if mlx_collapse else 'no'}")
    else:
        lines.append("- collapse present in **mlx (with adapter)**? not measured (backend unavailable)")
    if mlx_na_pb:
        lines.append(f"- collapse present in **mlx (base, no adapter)**? {'**yes**' if mlx_na_collapse else 'no'}")
    else:
        lines.append("- collapse present in **mlx (base, no adapter)**? not measured (backend unavailable)")

    # Are prompts too similar? (Heuristic: high cross-case fragment overlap → similar retrieval; can't measure prompt similarity without debug dir)
    promiscuous_total = sum(m.get("fragment_in_3plus_cases", 0) for m in pb.values())
    lines.append(f"- prompts too similar (proxy: fragments shared across ≥3 cases)? {'**likely**' if promiscuous_total > 0 else 'no clear sign'}")

    # Are retrieved fragments too similar?
    lines.append(f"- retrieved fragments overlapping across cases: total promiscuous chunks across all backends = {promiscuous_total}")

    # Validator retry causing convergence?
    lines.append("- validator retry causing convergence? **inspect `debug/`** — look for cases where first attempt differs across rows but final output is the same after retry. (Auto-detection requires --debug-prompts.)")

    # Sampling tightness
    if sweep:
        sweep_helped = any(s.get("unique_outputs", 0) >= s.get("rows", 0) * 0.6 for s in sweep.values())
        lines.append(f"- sampling sweep increased variation? {'**yes** — collapse may be sampling-driven' if sweep_helped else '**no** — variation low even at higher temps'}")
    else:
        lines.append("- sampling sweep: not run (use `--temperature 0.6,0.85 --top-p 0.85,0.95`)")

    lines.append("")
    lines.append("## Most likely cause\n")
    if template_collapse and not mlx_collapse and not mlx_na_collapse:
        cause = "Template generator. Real backends look fine."
    elif mlx_collapse and mlx_na_pb and not mlx_na_collapse:
        cause = "**Adapter-induced collapse.** Base mlx without the adapter does not collapse — the LoRA has either overfit on a few patterns or was trained on too-uniform targets."
    elif mlx_collapse and mlx_na_collapse:
        cause = "Collapse persists in base mlx without the adapter. Likely **prompt/retrieval/sampling**, not the adapter."
    elif not (template_collapse or mlx_collapse or mlx_na_collapse):
        cause = "No clear collapse detected by these heuristics. Spot-check `report.md` to confirm."
    else:
        cause = "Mixed signal — inspect `report.md` and `debug/` for individual cases."
    lines.append(cause + "\n")

    lines.append("## Smallest recommended next step\n")
    if mlx_collapse and mlx_na_pb and not mlx_na_collapse:
        lines.append(
            "- Stop using the current LoRA adapter for now. Run with `IMAGEBOARD_MLX_DISABLE_ADAPTER=1` or "
            "`mlx_no_adapter` for production-feel testing.\n"
            "- Before retraining, audit the SFT dataset that produced the adapter — count unique assistant "
            "outputs and unique greentext openers. Likely the dataset is dominated by templates.\n"
            "- Do **not** train a new LoRA until §4 (training_dataset.py quality) is addressed."
        )
    elif mlx_collapse and mlx_na_collapse:
        lines.append(
            "- Run a sampling sweep first (`--temperature 0.85 --top-p 0.95`). If that opens variation, "
            "raise the chat profile defaults a notch.\n"
            "- If sampling doesn't help, look at retrieval: are the same fragments returned for unrelated "
            "queries? If yes, increase `recent_uids` window or add per-component dedupe.\n"
            "- If both fail, prompt engineering: reduce few-shot quote-back examples (they may be priming).\n"
        )
    elif promiscuous_total > 0 and not mlx_collapse:
        lines.append(
            "- Retrieval shows fragment promiscuity but no output collapse. Watch this; not blocking.\n"
        )
    else:
        lines.append("- No urgent fix indicated by the metrics. Review `report.md` qualitatively.\n")

    lines.append("\n---\n")
    lines.append("_See `summary.json` for the raw metric values, `eval_rows.jsonl` for per-row data, "
                 "and `debug/` (if `--debug-prompts` was set) for full prompts + retry chains._\n")

    return "\n".join(lines)


# --- top-level orchestrator -----------------------------------------------

def run_eval(
    *,
    out_dir: Optional[Path] = None,
    backends_filter: Optional[Iterable[str]] = None,
    skip_chat: bool = False,
    persona_slug: str = DEFAULT_FIT_PERSONA_SLUG,
    source_key: str = "fourchan_fit",
    debug_prompts: bool = False,
    sweep_temperatures: Optional[list[float]] = None,
    sweep_top_p: Optional[list[float]] = None,
    sweep_cases: Optional[list[str]] = None,
) -> dict:
    """Run all cases × all available backends. Returns a summary dict.

    Optional diagnostic features:
      - debug_prompts: dump full prompt/fragment/validator capture per row
      - sweep_temperatures / sweep_top_p: re-run a subset of cases at each
        sampling value, on the available real backends only (template ignored
        because its sampling is fixed)
    """
    started_at = _dt.datetime.utcnow().isoformat(timespec="seconds")

    if out_dir is None:
        try:
            from django.conf import settings  # type: ignore
            base = Path(getattr(settings, "BASE_DIR", "."))
        except Exception:
            base = Path(".")
        out_dir = base / "var" / "evals" / f"eval_{started_at.replace(':', '-')}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = (out_dir / "debug") if debug_prompts else None

    backends = detect_backends(only=backends_filter)
    available = [b for b in backends if b.available]

    rows: list[EvalRow] = []
    for b in available:
        for case in THOUGHT_CASES:
            try:
                rows.append(run_thought_case(
                    case, b.name, source_key=source_key, debug_dir=debug_dir,
                ))
            except Exception as e:
                rows.append(EvalRow(
                    eval_id=f"{case['id']}__{b.name}",
                    timestamp=_dt.datetime.utcnow().isoformat(timespec="seconds"),
                    case_id=case["id"], mode="thought", backend=b.name,
                    backend_status="error",
                    error=f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}",
                ))
        if skip_chat:
            continue
        for case in CHAT_CASES:
            try:
                rows.append(run_chat_case(
                    case, b.name, persona_slug=persona_slug, debug_dir=debug_dir,
                ))
            except Exception as e:
                rows.append(EvalRow(
                    eval_id=f"{case['id']}__{b.name}",
                    timestamp=_dt.datetime.utcnow().isoformat(timespec="seconds"),
                    case_id=case["id"], mode="chat", backend=b.name,
                    backend_status="error",
                    error=f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}",
                ))

    # Sampling sweep — only on real backends, only on requested cases
    sweep_rows: list[EvalRow] = []
    if (sweep_temperatures or sweep_top_p):
        sweep_case_ids = sweep_cases or ["mirror_body", "calories", "gym_machine"]
        sweep_thought_cases = [c for c in THOUGHT_CASES if c["id"] in sweep_case_ids]
        real_backends = [b for b in available if b.runtime_name != "template"]
        temps = sweep_temperatures or [None]
        ps = sweep_top_p or [None]
        for b in real_backends:
            for t in temps:
                for p in ps:
                    overrides = {}
                    tag_parts = []
                    if t is not None:
                        overrides["IMAGEBOARD_LM_TEMPERATURE"] = str(t)
                        tag_parts.append(f"t{t}")
                    if p is not None:
                        overrides["IMAGEBOARD_LM_TOP_P"] = str(p)
                        tag_parts.append(f"p{p}")
                    tag = "sweep_" + "_".join(tag_parts)
                    for case in sweep_thought_cases:
                        try:
                            sweep_rows.append(run_thought_case(
                                case, b.name, source_key=source_key,
                                extra_env=overrides, debug_dir=debug_dir,
                                sweep_tag=tag,
                            ))
                        except Exception as e:
                            sweep_rows.append(EvalRow(
                                eval_id=f"{case['id']}__{b.name}__{tag}",
                                timestamp=_dt.datetime.utcnow().isoformat(timespec="seconds"),
                                case_id=case["id"], mode="thought", backend=b.name,
                                backend_status="error",
                                error=f"{type(e).__name__}: {e}",
                            ))

    all_rows = rows + sweep_rows

    jsonl_path = out_dir / "eval_rows.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for r in all_rows:
            fh.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    md_path = out_dir / "report.md"
    md_path.write_text(
        render_markdown(backends, rows, started_at=started_at, out_dir=out_dir),
        encoding="utf-8",
    )

    # Compute mode-collapse + fragment-uniqueness metrics
    metrics = compute_collapse_metrics(rows, sweep_rows=sweep_rows)

    summary = {
        "out_dir": str(out_dir),
        "jsonl_path": str(jsonl_path),
        "report_path": str(md_path),
        "started_at": started_at,
        "backends_detected": [asdict(b) for b in backends],
        "available_backends": [b.name for b in available],
        "unavailable_backends": [b.name for b in backends if not b.available],
        "row_count": len(all_rows),
        "main_row_count": len(rows),
        "sweep_row_count": len(sweep_rows),
        "by_backend": {b.name: sum(1 for r in all_rows if r.backend == b.name) for b in backends},
        "errors": [r.eval_id for r in all_rows if r.error],
        "mode_collapse_metrics": metrics,
        "debug_dir": str(debug_dir) if debug_dir else "",
    }

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Diagnosis report
    diagnosis_md = render_diagnosis(backends, rows, sweep_rows, metrics, started_at=started_at)
    (out_dir / "diagnosis.md").write_text(diagnosis_md, encoding="utf-8")
    summary["diagnosis_path"] = str(out_dir / "diagnosis.md")

    return summary


# --- CLI ------------------------------------------------------------------

def _ensure_django() -> None:
    """Boot Django if running as a script (not via manage.py)."""
    if "DJANGO_SETTINGS_MODULE" not in os.environ:
        os.environ["DJANGO_SETTINGS_MODULE"] = "djangoscrap.settings"
    try:
        from django.apps import apps  # type: ignore
        if apps.ready:
            return
    except Exception:
        pass
    import django  # type: ignore
    django.setup()


def _parse_floats(s: str) -> list[float]:
    if not s:
        return []
    out: list[float] = []
    for piece in s.split(","):
        piece = piece.strip()
        if not piece:
            continue
        out.append(float(piece))
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Eval the imageboard LM voice.")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Output dir; default var/evals/eval_<ts>")
    parser.add_argument("--backends", type=str, default="",
                        help="Comma-separated subset (template,mlx,mlx_no_adapter,ollama,llamacpp). "
                             "Default: probe all + mlx_no_adapter.")
    parser.add_argument("--mlx-no-adapter", action="store_true",
                        help="Force-include mlx_no_adapter virtual backend (sets IMAGEBOARD_MLX_DISABLE_ADAPTER=1 around generation).")
    parser.add_argument("--skip-chat", action="store_true",
                        help="Skip chat cases (no DB writes; useful for smoke).")
    parser.add_argument("--debug-prompts", action="store_true",
                        help="Save full prompt/fragments/retry chain per row to <out>/debug/.")
    parser.add_argument("--temperature", type=str, default="",
                        help="Comma-separated sampling-sweep temperatures (e.g. 0.55,0.68,0.85).")
    parser.add_argument("--top-p", type=str, default="",
                        help="Comma-separated sampling-sweep top_p values (e.g. 0.82,0.88,0.95).")
    parser.add_argument("--sweep-cases", type=str, default="",
                        help="Comma-separated case ids to sweep over. Default: mirror_body,calories,gym_machine.")
    parser.add_argument("--persona-slug", type=str, default=DEFAULT_FIT_PERSONA_SLUG)
    parser.add_argument("--source-key", type=str, default="fourchan_fit")
    args = parser.parse_args(argv)

    _ensure_django()

    backends_filter = None
    if args.backends:
        backends_filter = [b.strip() for b in args.backends.split(",") if b.strip()]
    elif args.mlx_no_adapter:
        backends_filter = ["template", "mlx", "mlx_no_adapter", "ollama", "llamacpp"]

    sweep_cases = None
    if args.sweep_cases:
        sweep_cases = [c.strip() for c in args.sweep_cases.split(",") if c.strip()]

    summary = run_eval(
        out_dir=args.out_dir,
        backends_filter=backends_filter,
        skip_chat=args.skip_chat,
        persona_slug=args.persona_slug,
        source_key=args.source_key,
        debug_prompts=args.debug_prompts,
        sweep_temperatures=_parse_floats(args.temperature) or None,
        sweep_top_p=_parse_floats(args.top_p) or None,
        sweep_cases=sweep_cases,
    )

    # Pretty CLI output
    print(f"Eval finished at {summary['started_at']}")
    print(f"  out_dir:   {summary['out_dir']}")
    print(f"  rows:      {summary['row_count']} (main={summary['main_row_count']} sweep={summary['sweep_row_count']})")
    print(f"  available: {', '.join(summary['available_backends']) or 'none'}")
    if summary["unavailable_backends"]:
        print(f"  missing:   {', '.join(summary['unavailable_backends'])}")
    if summary["errors"]:
        print(f"  errors:    {len(summary['errors'])}")
    print(f"  report:    {summary['report_path']}")
    print(f"  diagnosis: {summary['diagnosis_path']}")
    if summary.get("debug_dir"):
        print(f"  debug:     {summary['debug_dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
