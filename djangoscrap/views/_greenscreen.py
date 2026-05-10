"""Greenscreen video grid page — live WebGL chroma-key over a tunable shader.

Also exposes a small bg-pool API: random image URLs from a named pool, used
by the page to rotate imagery behind each cell.

Settings are persisted per clip_key in a JSON file so each clip remembers
its own threshold/smoothness/spill/key colour/pool/rotate values.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

from django.conf import settings as dj_settings
from django.http import JsonResponse, FileResponse, Http404, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

SETTINGS_PATH = Path(dj_settings.BASE_DIR) / "var" / "greenscreen_settings.json"
VALID_CLIP_KEYS = {"a", "b"}


# Named pools the greenscreen page can pull from. Add new entries as more
# character image dirs come online.
BG_POOLS: dict[str, Path] = {}

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _scan_pool(root: Path, limit: int = 5000) -> list[Path]:
    """Sample up to `limit` image paths from a pool root. Skips dotfiles."""
    if not root.exists():
        return []
    out: list[Path] = []
    for p in root.rglob("*"):
        if len(out) >= limit:
            break
        if not p.is_file():
            continue
        if p.name.startswith("._") or p.name.startswith("."):
            continue
        if p.suffix.lower() in _IMAGE_EXTS:
            out.append(p)
    return out


@require_GET
def greenscreen_page(request):
    pools = list(BG_POOLS.keys()) + ["compositions"]
    return render(request, "greenscreen.html", {"pools": pools})


@require_GET
def greenscreen_bg_pool(request, key: str):
    try:
        count = max(1, min(64, int(request.GET.get("count", "9"))))
    except ValueError:
        count = 9

    if key == "compositions":
        from django.core.files.storage import default_storage
        from django.urls import reverse
        from ..models import Composition
        folder = (request.GET.get("folder") or "").strip()
        qs = Composition.objects.prefetch_related("media_assets")
        if folder:
            # only the cells generated inside one folder, skip the source (cell 1)
            qs = qs.filter(grid_id=folder, grid_cell_index__gte=2)
        else:
            qs = qs.filter(grid_cell_index__lte=1)
        qs = list(qs.order_by("-id")[:1000])
        urls: list[str] = []
        for comp in qs:
            picked = False
            for asset in comp.media_assets.all():
                if asset.kind == "preview_10s" and asset.status == "ready" and asset.file:
                    try:
                        urls.append(default_storage.url(asset.file.name))
                        picked = True
                    except Exception:
                        pass
                    break
            if picked:
                continue
            # fallback: live composition page (iframe) so the cell still moves
            # even when no preview_10s has been rendered for this composition yet
            try:
                urls.append(reverse("composition_live_by_id", args=[comp.id]))
            except Exception:
                if comp.img:
                    try:
                        urls.append(default_storage.url(comp.img.name))
                    except Exception:
                        pass
        if not urls:
            return JsonResponse({"pool": key, "urls": [], "folder": folder or None})
        random.shuffle(urls)
        return JsonResponse({"pool": key, "urls": urls[:count], "folder": folder or None})

    pool_root = BG_POOLS.get(key)
    if pool_root is None:
        raise Http404("unknown pool")
    files = _scan_pool(pool_root)
    if not files:
        return JsonResponse({"pool": key, "urls": []})
    sample = random.sample(files, min(count, len(files)))
    urls = [f"/api/greenscreen/bg-image/{key}/?p={p.relative_to(pool_root)}" for p in sample]
    return JsonResponse({"pool": key, "urls": urls})


def _load_all_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_all_settings(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


@require_GET
def greenscreen_settings_get(request, clip_key: str):
    if clip_key not in VALID_CLIP_KEYS:
        raise Http404
    data = _load_all_settings()
    return JsonResponse({"clip": clip_key, "settings": data.get(clip_key) or None})


@csrf_exempt
@require_http_methods(["POST"])
def greenscreen_settings_save(request, clip_key: str):
    if clip_key not in VALID_CLIP_KEYS:
        raise Http404
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponseBadRequest("invalid json")

    cleaned = {
        "key_hex": str(body.get("key_hex", "#3eb851"))[:9],
        "threshold": float(body.get("threshold", 0.4)),
        "smoothness": float(body.get("smoothness", 0.1)),
        "spill": float(body.get("spill", 0.4)),
        "pool": (str(body.get("pool", "")) or None),
        "folder": (str(body.get("folder", "")) or None),
        "rotate_seconds": float(body.get("rotate_seconds", 3.0)),
        "saved_at": int(time.time()),
    }
    valid_pools = set(BG_POOLS.keys()) | {"compositions"}
    if cleaned["pool"] is not None and cleaned["pool"] not in valid_pools:
        cleaned["pool"] = None

    data = _load_all_settings()
    data[clip_key] = cleaned
    _save_all_settings(data)
    return JsonResponse({"ok": True, "clip": clip_key, "settings": cleaned})


@require_GET
def greenscreen_bg_image(request, key: str):
    pool_root = BG_POOLS.get(key)
    if pool_root is None:
        raise Http404
    rel = request.GET.get("p", "")
    if not rel or ".." in rel:
        raise Http404
    target = (pool_root / rel).resolve()
    try:
        target.relative_to(pool_root.resolve())
    except ValueError:
        raise Http404
    if not target.is_file():
        raise Http404
    ctype = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
    }.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(open(target, "rb"), content_type=ctype)
