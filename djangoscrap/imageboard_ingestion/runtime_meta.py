"""Runtime metadata + sampling profiles for the imageboard LM.

One place for:
  - sampling profile constants (thought vs. chat)
  - env-var-overridable sampling resolution
  - the RuntimeMeta dataclass that travels with every generation result
  - the canonical "which backend?" resolver, with deprecated-alias warning

Pass-1 scope. Kept dependency-light so it can be imported from anywhere in the
imageboard_ingestion package without pulling Django in.
"""
from __future__ import annotations

import logging
import os
import warnings
from dataclasses import asdict, dataclass, field
from typing import Optional

try:
    from django.conf import settings as _django_settings  # type: ignore
except Exception:  # pragma: no cover - Django optional at import time
    _django_settings = None  # type: ignore


_log = logging.getLogger(__name__)


# --- sampling profiles ----------------------------------------------------

@dataclass(frozen=True)
class SamplingProfile:
    name: str
    temperature: float
    top_p: float
    max_tokens: int


# Compressed inner-monologue: short bursts, low variance.
THOUGHT_PROFILE = SamplingProfile(name="thought", temperature=0.60, top_p=0.82, max_tokens=80)

# Chat reply: slightly more breathing room, still grounded.
CHAT_PROFILE = SamplingProfile(name="chat", temperature=0.68, top_p=0.88, max_tokens=120)


PROFILES: dict[str, SamplingProfile] = {
    THOUGHT_PROFILE.name: THOUGHT_PROFILE,
    CHAT_PROFILE.name: CHAT_PROFILE,
}


# --- env helpers ----------------------------------------------------------

def _setting(name: str, default=None):
    """Read a name from env first, then Django settings, then default.

    Env-first matches the Pass-1 priority: explicit > env > profile > DB.
    Settings.py-baked values shouldn't silently override an operator's env.
    """
    v = os.environ.get(name)
    if v is not None and v != "":
        return v
    if _django_settings is not None:
        v = getattr(_django_settings, name, None)
        if v is not None:
            return v
    return default


def _float_env(name: str) -> Optional[float]:
    raw = _setting(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        _log.warning("Ignoring non-numeric %s=%r", name, raw)
        return None


def _int_env(name: str) -> Optional[int]:
    raw = _setting(name)
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        _log.warning("Ignoring non-numeric %s=%r", name, raw)
        return None


# --- backend selection ----------------------------------------------------

ALLOWED_BACKENDS = ("mlx", "ollama", "llamacpp", "template")
DEFAULT_BACKEND = "template"

_DEPRECATION_WARNED = False


def resolve_backend(explicit: Optional[str] = None) -> str:
    """Pick the active backend.

    Priority: explicit arg > IMAGEBOARD_LM_BACKEND > FIT_LOCAL_RUNTIME (deprecated) > default.
    Emits a one-shot deprecation warning if FIT_LOCAL_RUNTIME is the source.
    """
    global _DEPRECATION_WARNED
    if explicit:
        v = str(explicit).lower().strip()
        if v in ALLOWED_BACKENDS:
            return v

    primary = _setting("IMAGEBOARD_LM_BACKEND")
    if primary:
        v = str(primary).lower().strip()
        if v in ALLOWED_BACKENDS:
            return v

    legacy = _setting("FIT_LOCAL_RUNTIME")
    if legacy:
        v = str(legacy).lower().strip()
        if v in ALLOWED_BACKENDS:
            if not _DEPRECATION_WARNED:
                _DEPRECATION_WARNED = True
                msg = (
                    "FIT_LOCAL_RUNTIME is deprecated. "
                    "Use IMAGEBOARD_LM_BACKEND instead."
                )
                warnings.warn(msg, DeprecationWarning, stacklevel=2)
                _log.warning(msg)
            return v

    return DEFAULT_BACKEND


# --- runtime metadata -----------------------------------------------------

@dataclass
class RuntimeMeta:
    """Travels with every GenerationResult; surfaced to API + UI.

    Fields are flat strings/numbers/bools so this serializes cleanly to JSON.
    """
    backend: str = DEFAULT_BACKEND
    model: str = ""
    adapter_path: str = ""
    used_template_fallback: bool = False
    fallback_reason: str = ""
    profile: str = ""
    temperature: float = 0.0
    top_p: float = 0.0
    max_tokens: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


# --- sampling resolution --------------------------------------------------

def resolve_sampling(
    profile_name: str = "chat",
    *,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> tuple[SamplingProfile, float, float, int]:
    """Resolve the active sampling values for a profile.

    Priority for each value:
      explicit arg > env var > profile default

    Returns the profile object plus the resolved (temperature, top_p, max_tokens).
    Caller can pass the resolved values straight into a backend.
    """
    profile = PROFILES.get(profile_name) or CHAT_PROFILE

    if temperature is None:
        temperature = _float_env("IMAGEBOARD_LM_TEMPERATURE")
    if temperature is None:
        temperature = profile.temperature

    if top_p is None:
        top_p = _float_env("IMAGEBOARD_LM_TOP_P")
    if top_p is None:
        top_p = profile.top_p

    if max_tokens is None:
        max_tokens = _int_env("IMAGEBOARD_LM_MAX_TOKENS")
    if max_tokens is None:
        max_tokens = profile.max_tokens

    # Defensive clamping. Some backends explode on out-of-range values.
    temperature = max(0.0, min(2.0, float(temperature)))
    top_p = max(0.05, min(1.0, float(top_p)))
    max_tokens = max(8, min(2048, int(max_tokens)))

    return profile, temperature, top_p, max_tokens
