"""Performance + health endpoints used to gauge the running system.

Surfaces:
- /api/health/        process metrics (memory, CPU, fds, uptime), recent request stats
- /api/page_beacon/   POST endpoint that pages call every ~10s with their FPS/RAM/video count
- /admin/health/      HTML dashboard listing recent beacons + process metrics
"""
from __future__ import annotations

import json
import os
import resource
import threading
import time
from collections import deque

from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST


_PROCESS_START = time.time()

# Ring buffer of recent page beacons, keyed by tab_id. Bounded so a flood of
# pages can't OOM the server. Lock-protected because beacons can race.
_BEACON_LOCK = threading.Lock()
_BEACONS: "deque[dict]" = deque(maxlen=512)


def _process_metrics() -> dict:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    # ru_maxrss is bytes on macOS, kilobytes on Linux. Detect by sanity-checking.
    rss_bytes = ru.ru_maxrss if ru.ru_maxrss > 1024 * 1024 else ru.ru_maxrss * 1024
    return {
        "pid": os.getpid(),
        "uptime_s": round(time.time() - _PROCESS_START, 1),
        "rss_mb": round(rss_bytes / (1024 * 1024), 1),
        "user_cpu_s": round(ru.ru_utime, 2),
        "sys_cpu_s": round(ru.ru_stime, 2),
        "page_faults": ru.ru_majflt,
    }


@require_GET
def health_view(request):
    """Returns process metrics + most recent page beacons for the operator."""
    with _BEACON_LOCK:
        beacons = list(_BEACONS)
    # Only keep beacons from the last 60s to avoid stale readings dominating.
    cutoff = time.time() - 60
    fresh = [b for b in beacons if b.get("server_received_at", 0) >= cutoff]
    return JsonResponse({
        "process": _process_metrics(),
        "live_pages": len(fresh),
        "beacons": fresh[-32:],
    })


@csrf_exempt
@require_POST
def page_beacon_view(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=400)
    payload["server_received_at"] = time.time()
    payload["remote_addr"] = request.META.get("REMOTE_ADDR", "")
    # Trim any pathologically large fields so a misbehaving page can't bloat us.
    for k, v in list(payload.items()):
        if isinstance(v, str) and len(v) > 512:
            payload[k] = v[:512]
    with _BEACON_LOCK:
        _BEACONS.append(payload)
    return JsonResponse({"ok": True})


def health_dashboard(request):
    return render(request, "admin/health.html", {})
