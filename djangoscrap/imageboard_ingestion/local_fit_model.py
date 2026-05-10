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

@dataclass
class GenerationRequest:
    system: str
    user: str
    # Sampling fields kept for backward compat; defaults are now safer than 0.95.
    # Callers should prefer build_request() which routes through resolve_sampling().
    max_tokens: int = CHAT_PROFILE.max_tokens
    temperature: float = CHAT_PROFILE.temperature
    top_p: float = CHAT_PROFILE.top_p
    profile: str = CHAT_PROFILE.name


@dataclass
class GenerationResult:
    text: str
    runtime: str  # backward-compat free-form label, kept for old callers
    error: Optional[str] = None
    debug: dict = field(default_factory=dict)
    meta: RuntimeMeta = field(default_factory=RuntimeMeta)


def build_request(
    *,
    system: str,
    user: str,
    profile: str = "chat",
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> GenerationRequest:
    """Build a GenerationRequest with sampling resolved via env + profile."""
    prof, t, p, m = resolve_sampling(
        profile, temperature=temperature, top_p=top_p, max_tokens=max_tokens
    )
    return GenerationRequest(
        system=system, user=user,
        temperature=t, top_p=p, max_tokens=m, profile=prof.name,
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


# --- backend: mlx ---------------------------------------------------------

_MLX_CACHE: dict = {}


def _mlx_paths() -> tuple[str, str]:
    base_model = str(_setting("IMAGEBOARD_MLX_MODEL", _setting("FIT_MLX_BASE_MODEL", "mlx-community/Qwen2.5-3B-Instruct-4bit")))
    adapter_dir = str(_setting("IMAGEBOARD_MLX_ADAPTER", _setting("FIT_MLX_ADAPTER_DIR", "")) or "")
    # Diagnostic override: force base-model-only generation even when an
    # adapter is configured. Used by the eval harness for collapse diagnosis.
    if str(os.environ.get("IMAGEBOARD_MLX_DISABLE_ADAPTER", "")).strip() in ("1", "true", "yes"):
        adapter_dir = ""
    return base_model, adapter_dir


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
    try:
        from mlx_lm import load, generate  # type: ignore
    except Exception as e:
        return GenerationResult(text="", runtime="mlx", error=f"mlx_lm_import_failed:{e}", meta=meta)

    cache_key = (base_model, adapter_dir)
    cached = _MLX_CACHE.get(cache_key)
    if cached is None:
        try:
            kwargs = {"adapter_path": adapter_dir} if adapter_dir and Path(adapter_dir).exists() else {}
            model, tokenizer = load(base_model, **kwargs)
        except Exception as e:
            return GenerationResult(text="", runtime="mlx", error=f"mlx_load_failed:{e}", meta=meta)
        _MLX_CACHE[cache_key] = (model, tokenizer)
    else:
        model, tokenizer = cached

    prompt = (
        f"<|im_start|>system\n{req.system}<|im_end|>\n"
        f"<|im_start|>user\n{req.user}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )
    try:
        sampler = None
        try:
            from mlx_lm.sample_utils import make_sampler  # type: ignore
            sampler = make_sampler(temp=float(req.temperature), top_p=float(req.top_p))
        except Exception:
            sampler = None
        gen_kwargs = {"max_tokens": int(req.max_tokens), "verbose": False}
        if sampler is not None:
            gen_kwargs["sampler"] = sampler
        out = generate(model, tokenizer, prompt=prompt, **gen_kwargs)
    except Exception as e:
        return GenerationResult(text="", runtime="mlx", error=f"mlx_generate_failed:{e}", meta=meta)
    return GenerationResult(text=str(out).strip(), runtime="mlx", meta=meta)


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
