"""8kun adapter stub.

Disabled by default. Enable with settings.ENABLE_8KUN_INGESTION = True.

The 8kun JSON layout mirrors infinity-style boards (catalog.json / res/{no}.json),
but the host is intentionally not encoded here. When/if this is enabled, the host
must be supplied via settings.EIGHTKUN_HOST and reviewed against the project's
safety policy. No requests are issued from this module today.
"""
from __future__ import annotations

from typing import Tuple


class EightKunDisabled(RuntimeError):
    pass


def _require_enabled() -> None:
    try:
        from django.conf import settings  # type: ignore
        enabled = bool(getattr(settings, "ENABLE_8KUN_INGESTION", False))
        host = getattr(settings, "EIGHTKUN_HOST", None)
    except Exception:
        enabled = False
        host = None
    if not enabled or not host:
        raise EightKunDisabled(
            "8kun ingestion is disabled. Set ENABLE_8KUN_INGESTION=True and EIGHTKUN_HOST to enable."
        )


def fetch_catalog(board: str) -> Tuple[list, dict]:  # pragma: no cover - stub
    _require_enabled()
    raise NotImplementedError("8kun adapter is a stub; implement when explicitly enabled.")


def fetch_thread(board: str, thread_no: int) -> Tuple[dict, dict]:  # pragma: no cover - stub
    _require_enabled()
    raise NotImplementedError("8kun adapter is a stub; implement when explicitly enabled.")
