"""Copy selected images to chan_corpus/downloads/<name>/ or stream a zip.

Public endpoints:
  POST /api/img/download/   {name, image_keys[], format=originals|thumbs}
        → copies the files; returns {ok, name, n_copied, path}
  GET  /api/img/download-zip/?name=...&keys=k1,k2
        → streams a zip of those files

The destination folder under downloads/ is treated as a flat materialised
selection — overwriting copies a fresh set, no manifest log.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Iterator

from django.http import (FileResponse, HttpResponse, HttpResponseBadRequest,
                          JsonResponse)
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST


_DEFAULT_ROOT = Path(os.environ.get("CHAN_CORPUS_ROOT", "/Volumes/Oom/chan_corpus"))


def _root() -> Path:
    return _DEFAULT_ROOT


def _safe_name(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_\- ]+", "", name).strip()
    s = re.sub(r"\s+", "_", s)
    return s[:80]


def _iter_jsonl(p: Path) -> Iterator[dict]:
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _resolve_paths(root: Path, want_keys: set[str], use_full: bool
                   ) -> dict[str, Path]:
    """Map image_key → absolute on-disk path (allow-listed only)."""
    out: dict[str, Path] = {}
    for rec in _iter_jsonl(root / "manifests" / "images.jsonl"):
        ik = rec.get("image_key")
        if ik not in want_keys:
            continue
        if (rec.get("safety_decision") or {}).get("decision", "allow") != "allow":
            continue
        if use_full:
            p = rec.get("original_path") or rec.get("file_path") or rec.get("thumbnail_path") or ""
        else:
            p = rec.get("thumbnail_path") or rec.get("original_path") or ""
        if not p:
            continue
        path = Path(p)
        if path.exists() and path.is_file():
            out[ik] = path
    return out


def _within_root(p: Path, root: Path) -> bool:
    rp = str(p.resolve()).casefold()
    rr = str(root.resolve()).casefold()
    return rp == rr or rp.startswith(rr + os.sep)


@csrf_exempt
@require_POST
def download_to_folder(request):
    name = _safe_name(request.POST.get("name") or "")
    if not name:
        return HttpResponseBadRequest("name required")
    keys = request.POST.getlist("image_keys[]") or request.POST.getlist("image_keys")
    if not keys:
        raw = request.POST.get("image_keys") or ""
        keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        return HttpResponseBadRequest("image_keys required")
    fmt = (request.POST.get("format") or "originals").lower()
    use_full = fmt != "thumbs"

    root = _root()
    paths = _resolve_paths(root, set(keys), use_full=use_full)
    dest = root / "downloads" / name
    dest.mkdir(parents=True, exist_ok=True)

    n_copied = 0
    skipped: list[str] = []
    for ik in keys:
        src = paths.get(ik)
        if not src:
            skipped.append(ik)
            continue
        if not _within_root(src, root):
            skipped.append(ik)
            continue
        # File name: <board>_<image_key_tail><ext>
        ext = src.suffix or ".jpg"
        # image_key looks like "fourchan:p:1777..."
        parts = ik.split(":")
        board = parts[1] if len(parts) >= 2 else "x"
        tail = parts[-1]
        out_name = f"{board}_{tail}{ext}"
        try:
            shutil.copy2(src, dest / out_name)
            n_copied += 1
        except OSError:
            skipped.append(ik)

    return JsonResponse({
        "ok": True, "name": name, "n_copied": n_copied,
        "n_requested": len(keys), "n_skipped": len(skipped),
        "format": "originals" if use_full else "thumbs",
        "path": str(dest),
    })


@require_GET
def download_zip(request):
    name = _safe_name(request.GET.get("name") or "selection")
    keys_raw = request.GET.get("keys") or ""
    keys = [k.strip() for k in keys_raw.split(",") if k.strip()]
    if not keys:
        return HttpResponseBadRequest("keys required")
    fmt = (request.GET.get("format") or "originals").lower()
    use_full = fmt != "thumbs"

    paths = _resolve_paths(_root(), set(keys), use_full=use_full)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for ik in keys:
            src = paths.get(ik)
            if not src or not _within_root(src, _root()):
                continue
            parts = ik.split(":")
            board = parts[1] if len(parts) >= 2 else "x"
            tail = parts[-1]
            ext = src.suffix or ".jpg"
            zf.write(src, arcname=f"{name}/{board}_{tail}{ext}")
    buf.seek(0)
    resp = HttpResponse(buf.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="{name}.zip"'
    return resp


@require_GET
def downloads_index(request):
    """List existing folders under downloads/ for the recent-folders bar."""
    base = _root() / "downloads"
    rows = []
    if base.exists():
        for d in sorted(base.iterdir(), key=lambda p: -p.stat().st_mtime):
            if not d.is_dir():
                continue
            try:
                n = sum(1 for _ in d.iterdir() if _.is_file())
            except OSError:
                n = 0
            rows.append({"name": d.name, "n_files": n,
                          "modified": int(d.stat().st_mtime)})
    return JsonResponse({"ok": True, "folders": rows[:60]})
