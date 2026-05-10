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
    name: str
    available: bool
    detail: str = ""           # human-readable summary
    model: str = ""
    adapter_path: str = ""
    fallback_reason: str = ""


PROBE_SYSTEM = "You are /fit/ anon."
PROBE_USER = "say something"


def probe_backend(name: str) -> BackendStatus:
    """Run a tiny generation through a specific backend and report status.

    Template is always available. Real backends are "available" only if they
    don't get downgraded to a template fallback.
    """
    from . import local_fit_model

    if name == "template":
        # Verify the template path actually returns something
        req = local_fit_model.build_request(system=PROBE_SYSTEM, user=PROBE_USER, profile="thought")
        res = local_fit_model.generate(req, runtime="template")
        return BackendStatus(
            name="template",
            available=bool(res.text),
            detail="pure-Python placeholder; not a trained model",
        )

    req = local_fit_model.build_request(system=PROBE_SYSTEM, user=PROBE_USER, profile="thought")
    try:
        res = local_fit_model.generate(req, runtime=name)
    except Exception as e:
        return BackendStatus(name=name, available=False, detail=f"crashed during probe: {e}")

    fallback = bool(res.meta.used_template_fallback)
    if fallback:
        return BackendStatus(
            name=name,
            available=False,
            detail=f"unavailable — fell back to template ({res.meta.fallback_reason})",
            fallback_reason=res.meta.fallback_reason,
        )
    if not res.text and res.error:
        return BackendStatus(name=name, available=False, detail=f"error: {res.error}")
    return BackendStatus(
        name=name,
        available=True,
        detail="ok",
        model=res.meta.model,
        adapter_path=res.meta.adapter_path,
    )


def detect_backends(only: Optional[Iterable[str]] = None) -> list[BackendStatus]:
    candidates = list(only or ("template", "mlx", "ollama", "llamacpp"))
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

def run_thought_case(case: dict, backend: str, *, source_key: str = "fourchan_fit") -> EvalRow:
    from . import thought_grounding

    eval_id = f"{case['id']}__{backend}"
    ts = _dt.datetime.utcnow().isoformat(timespec="seconds")
    row = EvalRow(
        eval_id=eval_id, timestamp=ts,
        case_id=case["id"], mode="thought", backend=backend,
        backend_status="configured",
        prompt=case.get("screen_id", "") or json.dumps(case.get("current_context") or {}),
    )
    started = time.monotonic()
    try:
        out = thought_grounding.generate_fit_be_me_thought(
            current_context=case.get("current_context"),
            source_key=source_key,
            screen_id=case.get("screen_id"),
            n_fragments=8,
            contamination_intensity=0.75,
            require_grounding=False,
            runtime=backend,
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
        "fallback_to_template" if row.fallback_occurred and backend != "template" else "configured"
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
    return row


# --- chat case runner -----------------------------------------------------

def run_chat_case(case: dict, backend: str, *, persona_slug: str = DEFAULT_FIT_PERSONA_SLUG) -> EvalRow:
    """Drive _generate_chat_reply through a fresh ChatSession.

    Creates a temp session named `eval_<case>_<backend>_<ts>` so leftover rows
    are easy to spot and clean up later.
    """
    from django.utils import timezone as _tz
    from ..models import ChatMessage, ChatSession, MonologuePersona
    from ..views._characters import _generate_chat_reply

    eval_id = f"{case['id']}__{backend}"
    ts = _dt.datetime.utcnow().isoformat(timespec="seconds")
    row = EvalRow(
        eval_id=eval_id, timestamp=ts,
        case_id=case["id"], mode="chat", backend=backend,
        backend_status="configured",
        prompt=(case.get("messages") or [""])[0],
        messages=list(case.get("messages") or []),
    )

    # Override backend for this call by setting env var (chat uses the
    # backend resolver, no kwarg). Restore after.
    prev_env = os.environ.get("IMAGEBOARD_LM_BACKEND")
    os.environ["IMAGEBOARD_LM_BACKEND"] = backend

    started = time.monotonic()
    try:
        try:
            persona = MonologuePersona.objects.get(slug=persona_slug)
        except MonologuePersona.DoesNotExist:
            row.error = f"persona slug {persona_slug!r} not found"
            return row

        session_name = f"eval_{case['id']}_{backend}_{int(time.time())}"
        session = ChatSession.objects.create(persona=persona, name=session_name)

        last_reply = ""
        chain: list[dict] = []
        running_history: list = []
        for i, user_msg in enumerate(case.get("messages") or []):
            ChatMessage.objects.create(session=session, role="user", text=user_msg)
            running_history = list(session.messages.order_by("-id")[:20])
            running_history.reverse()
            try:
                reply = _generate_chat_reply(persona, running_history, user_msg, session=session)
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

        # Capture session state at end
        session.refresh_from_db()
        row.anon_state = dict(session.state or {})
        row.outputs = chain
        row.output = last_reply
        row.length_words = _word_count(last_reply)
        row.elapsed_ms = int((time.monotonic() - started) * 1000)

        # We can't reach the runtime_meta via _generate_chat_reply's return;
        # do one targeted probe at the same backend so we record what the
        # backend resolved to during this case.
        from . import local_fit_model
        probe_req = local_fit_model.build_request(
            system="probe", user="probe", profile="chat",
        )
        probe_res = local_fit_model.generate(probe_req, runtime=backend)
        row.runtime_meta = probe_res.meta.as_dict()
        row.fallback_occurred = bool(probe_res.meta.used_template_fallback)
        if row.fallback_occurred and backend != "template":
            row.backend_status = "fallback_to_template"

        # Run validator on the last reply for visibility (chat already does
        # this internally, but we want it surfaced in the row).
        from . import output_validator
        v = output_validator.validate(last_reply)
        row.validator_result = v
        row.rejection_reasons = v.get("reasons") or []
    finally:
        # Restore env
        if prev_env is None:
            os.environ.pop("IMAGEBOARD_LM_BACKEND", None)
        else:
            os.environ["IMAGEBOARD_LM_BACKEND"] = prev_env

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


# --- top-level orchestrator -----------------------------------------------

def run_eval(
    *,
    out_dir: Optional[Path] = None,
    backends_filter: Optional[Iterable[str]] = None,
    skip_chat: bool = False,
    persona_slug: str = DEFAULT_FIT_PERSONA_SLUG,
    source_key: str = "fourchan_fit",
) -> dict:
    """Run all cases × all available backends. Returns a summary dict."""
    started_at = _dt.datetime.utcnow().isoformat(timespec="seconds")

    # Resolve out_dir relative to BASE_DIR
    if out_dir is None:
        try:
            from django.conf import settings  # type: ignore
            base = Path(getattr(settings, "BASE_DIR", "."))
        except Exception:
            base = Path(".")
        out_dir = base / "var" / "evals" / f"eval_{started_at.replace(':', '-')}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Detect backends
    backends = detect_backends(only=backends_filter)
    available = [b for b in backends if b.available]

    # Run cases
    rows: list[EvalRow] = []
    for b in available:
        for case in THOUGHT_CASES:
            try:
                rows.append(run_thought_case(case, b.name, source_key=source_key))
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
                rows.append(run_chat_case(case, b.name, persona_slug=persona_slug))
            except Exception as e:
                rows.append(EvalRow(
                    eval_id=f"{case['id']}__{b.name}",
                    timestamp=_dt.datetime.utcnow().isoformat(timespec="seconds"),
                    case_id=case["id"], mode="chat", backend=b.name,
                    backend_status="error",
                    error=f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}",
                ))

    # Write JSONL
    jsonl_path = out_dir / "eval_rows.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    # Write markdown report
    md_path = out_dir / "report.md"
    md_path.write_text(
        render_markdown(backends, rows, started_at=started_at, out_dir=out_dir),
        encoding="utf-8",
    )

    summary = {
        "out_dir": str(out_dir),
        "jsonl_path": str(jsonl_path),
        "report_path": str(md_path),
        "started_at": started_at,
        "backends_detected": [asdict(b) for b in backends],
        "available_backends": [b.name for b in available],
        "unavailable_backends": [b.name for b in backends if not b.available],
        "row_count": len(rows),
        "by_backend": {
            b.name: sum(1 for r in rows if r.backend == b.name)
            for b in backends
        },
        "errors": [r.eval_id for r in rows if r.error],
    }

    # Drop summary as JSON next to the report
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
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


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Eval the imageboard LM voice.")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Output dir; default var/evals/eval_<ts>")
    parser.add_argument("--backends", type=str, default="",
                        help="Comma-separated subset (template,mlx,ollama,llamacpp). Default: probe all.")
    parser.add_argument("--skip-chat", action="store_true",
                        help="Skip chat cases (no DB writes; useful for smoke).")
    parser.add_argument("--persona-slug", type=str, default=DEFAULT_FIT_PERSONA_SLUG)
    parser.add_argument("--source-key", type=str, default="fourchan_fit")
    args = parser.parse_args(argv)

    _ensure_django()

    backends_filter = None
    if args.backends:
        backends_filter = [b.strip() for b in args.backends.split(",") if b.strip()]

    summary = run_eval(
        out_dir=args.out_dir,
        backends_filter=backends_filter,
        skip_chat=args.skip_chat,
        persona_slug=args.persona_slug,
        source_key=args.source_key,
    )

    # Pretty CLI output
    print(f"Eval finished at {summary['started_at']}")
    print(f"  out_dir:   {summary['out_dir']}")
    print(f"  rows:      {summary['row_count']}")
    print(f"  available: {', '.join(summary['available_backends']) or 'none'}")
    if summary["unavailable_backends"]:
        print(f"  missing:   {', '.join(summary['unavailable_backends'])}")
    if summary["errors"]:
        print(f"  errors:    {len(summary['errors'])}")
    print(f"  report:    {summary['report_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
