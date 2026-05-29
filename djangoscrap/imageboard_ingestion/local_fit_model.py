"""Local model runtime for /fit/ thought + chat generation.

Four backends:
  - mlx        : mlx-lm with optional LoRA adapter (Apple Silicon)
  - ollama     : local Ollama server with a custom model
  - llamacpp   : llama-cpp-python with a GGUF file
  - template   : pure-Python placeholder generator (always available)

Backend is selected via the IMAGEBOARD_LM_BACKEND env var (or the deprecated
FIT_LOCAL_RUNTIME alias). If the configured backend is unavailable at runtime
the call falls back to template and the GenerationResult.meta makes that
visible — used_template_fallback=True with a fallback_reason string.

The default backend is `template`. When template is used (whether configured
or as a fallback), a single deprecation-style warning is logged per process so
operators do not mistake template output for a real model.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .runtime_meta import (
    CHAT_PROFILE,
    THOUGHT_PROFILE,
    RuntimeMeta,
    resolve_backend,
    resolve_sampling,
)

try:
    from django.conf import settings as django_settings  # type: ignore
except Exception:
    django_settings = None  # type: ignore


_log = logging.getLogger(__name__)


def _setting(name: str, default):
    if django_settings is None:
        return os.getenv(name, default)
    return getattr(django_settings, name, os.getenv(name, default))


# --- request / result -----------------------------------------------------

_DEFAULT_STOP_SEQUENCES_CHAT = (
    "\nuser:", "\nUser:", "\nUSER:",
    "\nanon:", "\nAnon:", "\nANON:",
    "\nassistant:", "\nAssistant:",
    "RECENT_TURNS:", "OP_ANCHOR:", "BOARD:", "MODE:",
    "ANON_STATE:", "RETRIEVED_FRAGMENTS:", "SAFETY_RULES:",
)


@dataclass
class GenerationRequest:
    system: str
    user: str
    # Optional: full chat-template message list. When set on a backend that
    # supports it (mlx via tokenizer.apply_chat_template), this overrides the
    # `system`+`user` strings so history can be passed as real alternating
    # turns instead of stuffed into one user blob (the cause of Pass 2E's
    # `user:` / `anon:` format leakage).
    messages: list[dict] | None = None

    # Sampling fields kept for backward compat; defaults are now safer than 0.95.
    # Callers should prefer build_request() which routes through resolve_sampling().
    max_tokens: int = CHAT_PROFILE.max_tokens
    temperature: float = CHAT_PROFILE.temperature
    top_p: float = CHAT_PROFILE.top_p
    profile: str = CHAT_PROFILE.name

    # Pass 2E: repetition controls.
    repetition_penalty: float = 1.10
    no_repeat_ngram_size: int = 4

    # Pass 2E: stop strings. The MLX backend post-trims at the first match so
    # the model can't continue past a single reply into fake user/anon turns.
    stop_sequences: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class GenerationResult:
    text: str
    runtime: str  # backward-compat free-form label, kept for old callers
    error: Optional[str] = None
    debug: dict = field(default_factory=dict)
    meta: RuntimeMeta = field(default_factory=RuntimeMeta)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(float(raw)) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def build_request(
    *,
    system: str = "",
    user: str = "",
    messages: Optional[list[dict]] = None,
    profile: str = "chat",
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: Optional[int] = None,
    repetition_penalty: Optional[float] = None,
    no_repeat_ngram_size: Optional[int] = None,
    stop_sequences: Optional[tuple[str, ...]] = None,
) -> GenerationRequest:
    """Build a GenerationRequest with sampling resolved via env + profile.

    Pass 2E: env-overridable repetition controls; chat profile gets a default
    set of stop sequences so the model can't continue past a single reply into
    fake `user:` / `anon:` transcript turns.
    """
    prof, t, p, m = resolve_sampling(
        profile, temperature=temperature, top_p=top_p, max_tokens=max_tokens
    )

    if repetition_penalty is None:
        repetition_penalty = _env_float("IMAGEBOARD_LM_REPETITION_PENALTY", 1.10)
    if no_repeat_ngram_size is None:
        no_repeat_ngram_size = _env_int("IMAGEBOARD_LM_NO_REPEAT_NGRAM_SIZE", 4)
    if stop_sequences is None:
        stop_sequences = _DEFAULT_STOP_SEQUENCES_CHAT if prof.name == "chat" else ()

    return GenerationRequest(
        system=system, user=user, messages=messages,
        temperature=t, top_p=p, max_tokens=m, profile=prof.name,
        repetition_penalty=float(repetition_penalty),
        no_repeat_ngram_size=int(no_repeat_ngram_size),
        stop_sequences=tuple(stop_sequences),
    )


# --- template warning (once per process) ----------------------------------

_TEMPLATE_WARNED = False


def _warn_template_use(reason: str) -> None:
    global _TEMPLATE_WARNED
    if _TEMPLATE_WARNED:
        return
    _TEMPLATE_WARNED = True
    msg = (
        f"imageboard_ingestion: USING TEMPLATE BACKEND ({reason}). "
        "Output is NOT from a trained language model — it's a Python template "
        "generator. Set IMAGEBOARD_LM_BACKEND=mlx|ollama|llamacpp and configure "
        "model paths for real generation."
    )
    warnings.warn(msg, RuntimeWarning, stacklevel=2)
    _log.warning(msg)


# --- backend: template ----------------------------------------------------

def _template_generate(req: GenerationRequest, *, fallback_from: str = "", fallback_reason: str = "") -> GenerationResult:
    """Pure-Python placeholder until a real local LoRA exists.

    Generates a be-me thought using the same templates as the training-data
    builder, seeded from the user message so the same input produces the same
    line (debuggable).
    """
    from . import training_dataset  # avoid heavy import at module load
    import random as _random

    rng = _random.Random(hash(req.user) & 0xFFFFFFFF)
    profile = None
    state = training_dataset._seed_state(profile, rng)
    text = training_dataset._compose_thought(state, rng)

    reason = fallback_reason or ("configured backend" if not fallback_from else f"fallback from {fallback_from}")
    _warn_template_use(reason)

    label = "template" if not fallback_from else f"template (fallback from {fallback_from})"
    meta = RuntimeMeta(
        backend="template",
        used_template_fallback=bool(fallback_from),
        fallback_reason=fallback_reason,
        profile=req.profile,
        temperature=req.temperature,
        top_p=req.top_p,
        max_tokens=req.max_tokens,
    )
    return GenerationResult(
        text=text,
        runtime=label,
        debug={"state": state.__dict__},
        meta=meta,
    )


# --- backend: mlx (subprocess model server) --------------------------------
# The MLX model is loaded in a dedicated subprocess so Metal/unified-memory
# allocations never live inside the gunicorn web worker.  A single worker
# process (or thread) serialises requests over the subprocess's stdin/stdout
# pipe; the subprocess stays alive for the lifetime of the worker.

import subprocess as _subprocess
import sys as _sys
import threading as _threading
from pathlib import Path as _Path

_MLX_PROC: "_subprocess.Popen | None" = None
_MLX_PROC_LOCK = _threading.Lock()


def _mlx_server_script() -> str:
    return str(_Path(__file__).parent / "_mlx_server.py")


def _get_mlx_proc(base_model: str, adapter_dir: str) -> "_subprocess.Popen":
    """Return (starting if needed) the persistent MLX server subprocess."""
    global _MLX_PROC
    if _MLX_PROC is not None and _MLX_PROC.poll() is None:
        return _MLX_PROC
    args = [_sys.executable, _mlx_server_script(), base_model]
    if adapter_dir:
        args.append(adapter_dir)
    proc = _subprocess.Popen(
        args,
        stdin=_subprocess.PIPE,
        stdout=_subprocess.PIPE,
        stderr=_subprocess.DEVNULL,
    )
    # Wait for the ready signal (model has finished loading)
    ready_line = proc.stdout.readline()
    ready = json.loads(ready_line.decode())
    if ready.get("status") != "ready":
        proc.kill()
        raise RuntimeError(f"mlx_server_start_failed: {ready.get('error')}")
    _MLX_PROC = proc
    return proc


def _mlx_paths() -> tuple[str, str]:
    base_model = str(_setting("IMAGEBOARD_MLX_MODEL", _setting("FIT_MLX_BASE_MODEL", "mlx-community/Qwen2.5-3B-Instruct-4bit")))
    adapter_dir = str(_setting("IMAGEBOARD_MLX_ADAPTER", _setting("FIT_MLX_ADAPTER_DIR", "")) or "")
    # Diagnostic override: force base-model-only generation even when an
    # adapter is configured. Used by the eval harness for collapse diagnosis.
    if str(os.environ.get("IMAGEBOARD_MLX_DISABLE_ADAPTER", "")).strip() in ("1", "true", "yes"):
        adapter_dir = ""
    return base_model, adapter_dir


def _trim_at_stops(text: str, stops: tuple[str, ...]) -> tuple[str, str]:
    """Cut output at the first occurrence of any stop string.

    Returns (trimmed_text, matched_stop_or_empty). Defensive fallback when the
    backend doesn't accept stop sequences directly — the model can keep
    generating past one reply, but we never expose the overflow.
    """
    if not text or not stops:
        return text, ""
    earliest = -1
    matched = ""
    for stop in stops:
        if not stop:
            continue
        i = text.find(stop)
        if i != -1 and (earliest == -1 or i < earliest):
            earliest = i
            matched = stop
    if earliest == -1:
        return text, ""
    return text[:earliest].rstrip(), matched


def _mlx_generate(req: GenerationRequest) -> GenerationResult:
    base_model, adapter_dir = _mlx_paths()
    meta = RuntimeMeta(
        backend="mlx",
        model=base_model,
        adapter_path=adapter_dir,
        profile=req.profile,
        temperature=req.temperature,
        top_p=req.top_p,
        max_tokens=req.max_tokens,
    )
    # Build the prompt before handing off to the subprocess so the tokenizer
    # chat-template logic stays in the web worker (no tokenizer in subprocess).
    # We import the tokenizer lazily just for prompt building, not for inference.
    prompt = None
    used_chat_template = False
    if req.messages:
        try:
            from mlx_lm import load as _mlx_load  # type: ignore
            _, _tok = _mlx_load(base_model, lazy=True) if False else (None, None)
        except Exception:
            pass
    # Fall back to manual ChatML assembly (the subprocess also understands this)
    if prompt is None:
        if req.messages:
            parts = []
            for m in req.messages:
                role = m.get("role", "user")
                content = m.get("content", "")
                parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
            parts.append("<|im_start|>assistant\n")
            prompt = "\n".join(parts)
        else:
            prompt = (
                f"<|im_start|>system\n{req.system}<|im_end|>\n"
                f"<|im_start|>user\n{req.user}<|im_end|>\n"
                f"<|im_start|>assistant\n"
            )

    request_payload = json.dumps({
        "prompt": prompt,
        "max_tokens": int(req.max_tokens),
        "temperature": float(req.temperature),
        "top_p": float(req.top_p),
        "repetition_penalty": float(req.repetition_penalty or 1.0),
        "stop_sequences": list(req.stop_sequences),
    }).encode() + b"\n"

    with _MLX_PROC_LOCK:
        try:
            proc = _get_mlx_proc(base_model, adapter_dir)
            proc.stdin.write(request_payload)
            proc.stdin.flush()
            response_line = proc.stdout.readline()
        except Exception as e:
            _MLX_PROC = None  # force restart on next call
            return GenerationResult(text="", runtime="mlx", error=f"mlx_subprocess_error:{e}", meta=meta)

    try:
        resp = json.loads(response_line.decode())
    except Exception as e:
        return GenerationResult(text="", runtime="mlx", error=f"mlx_response_parse:{e}", meta=meta)

    if resp.get("error"):
        return GenerationResult(text="", runtime="mlx", error=resp["error"], meta=meta)

    text = resp.get("text", "")
    debug = {
        "used_chat_template": used_chat_template,
        "repetition_penalty": float(req.repetition_penalty or 1.0),
        **(resp.get("debug") or {}),
        "no_repeat_ngram_size": int(req.no_repeat_ngram_size),
        "stop_sequences": list(req.stop_sequences),
    }
    return GenerationResult(text=text, runtime="mlx", meta=meta, debug=debug)


# --- backend: ollama ------------------------------------------------------

def _ollama_generate(req: GenerationRequest) -> GenerationResult:
    model = str(_setting("FIT_OLLAMA_MODEL", "fit-be-me"))
    host = str(_setting("FIT_OLLAMA_HOST", "http://127.0.0.1:11434"))
    meta = RuntimeMeta(
        backend="ollama",
        model=model,
        profile=req.profile,
        temperature=req.temperature,
        top_p=req.top_p,
        max_tokens=req.max_tokens,
    )
    if shutil.which("ollama") is None and not host.startswith("http"):
        return GenerationResult(text="", runtime="ollama", error="ollama_not_installed", meta=meta)
    try:
        import urllib.request
        body = json.dumps({
            "model": model,
            "system": req.system,
            "prompt": req.user,
            "stream": False,
            "options": {"temperature": req.temperature, "top_p": req.top_p,
                        "num_predict": req.max_tokens},
        }).encode("utf-8")
        r = urllib.request.Request(f"{host.rstrip('/')}/api/generate", data=body,
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return GenerationResult(text=(data.get("response") or "").strip(), runtime="ollama", meta=meta)
    except Exception as e:
        return GenerationResult(text="", runtime="ollama", error=f"ollama_request_failed:{e}", meta=meta)


# --- backend: llamacpp ----------------------------------------------------

def _llamacpp_generate(req: GenerationRequest) -> GenerationResult:
    gguf_path = str(_setting("FIT_LLAMACPP_MODEL_PATH", ""))
    meta = RuntimeMeta(
        backend="llamacpp",
        model=gguf_path,
        profile=req.profile,
        temperature=req.temperature,
        top_p=req.top_p,
        max_tokens=req.max_tokens,
    )
    if not gguf_path or not Path(gguf_path).exists():
        return GenerationResult(text="", runtime="llamacpp", error="gguf_not_found", meta=meta)
    try:
        from llama_cpp import Llama  # type: ignore
    except Exception as e:
        return GenerationResult(text="", runtime="llamacpp", error=f"llama_cpp_import_failed:{e}", meta=meta)
    try:
        llm = Llama(model_path=gguf_path, n_ctx=2048, verbose=False)
        prompt = f"<|system|>\n{req.system}\n<|user|>\n{req.user}\n<|assistant|>\n"
        out = llm(prompt, max_tokens=int(req.max_tokens), temperature=float(req.temperature),
                  top_p=float(req.top_p))
        text = (out["choices"][0]["text"] or "").strip()
        return GenerationResult(text=text, runtime="llamacpp", meta=meta)
    except Exception as e:
        return GenerationResult(text="", runtime="llamacpp", error=f"llamacpp_generate_failed:{e}", meta=meta)


# --- public API -----------------------------------------------------------

def active_runtime() -> str:
    """Return the configured backend name (post env-var resolution)."""
    return resolve_backend()


_MISSING_RUNTIME_ERRORS = (
    "mlx_lm_import_failed", "mlx_load_failed",
    "ollama_not_installed", "ollama_request_failed",
    "llama_cpp_import_failed", "gguf_not_found",
)


def generate(req: GenerationRequest, *, runtime: str | None = None) -> GenerationResult:
    """Run the request on the configured backend, falling back to template
    if the backend is unavailable. The fallback is recorded in result.meta.
    """
    rt = resolve_backend(runtime)

    if rt == "mlx":
        result = _mlx_generate(req)
    elif rt == "ollama":
        result = _ollama_generate(req)
    elif rt == "llamacpp":
        result = _llamacpp_generate(req)
    else:
        result = _template_generate(req)

    # Fall back to template only for "infrastructure missing" errors. Real
    # generation errors (e.g. model crashed mid-decode) propagate.
    if not result.text and result.error:
        err_kind = result.error.split(":")[0]
        if err_kind in _MISSING_RUNTIME_ERRORS:
            fb = _template_generate(req, fallback_from=rt, fallback_reason=result.error)
            fb.debug = {**(fb.debug or {}), "fallback_from": rt, "fallback_error": result.error}
            result = fb

    # Diagnostic capture: always stash the assembled prompt on result.debug so
    # callers using --debug-prompts can see exactly what was sent. Cheap; the
    # debug dict was already returned and unused by most callers.
    if result.debug is None:
        result.debug = {}
    result.debug.setdefault("prompt_system", req.system)
    result.debug.setdefault("prompt_user", req.user)
    result.debug.setdefault("requested_runtime", rt)
    return result
