"""Live progress visualiser for the local-first imageboard image pipeline.

Reads the chan-corpus manifests and on-disk byte counts, computes throughput
from a small in-memory history, returns JSON for the admin page. Mirrors
_scrape_monitor.py for the text scrape but speaks the image pipeline's
schema (images.jsonl + cluster_membership + clusters + originals/thumbnails
on disk).
"""
from __future__ import annotations

import json
import os
import random
import time
from collections import Counter, deque
from pathlib import Path

import subprocess
import sys
import threading

from django.http import JsonResponse, HttpResponseNotFound, FileResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

# Default location, overridable by env / settings
_DEFAULT_ROOT = Path(os.environ.get("CHAN_CORPUS_ROOT", "/Volumes/Oom/chan_corpus"))

TARGETS = [1_000, 5_000, 10_000, 50_000]
_HISTORY_LEN = 60
_history: dict[str, deque] = {}


def _root() -> Path:
    return _DEFAULT_ROOT


def _count_jsonl(p: Path) -> int:
    if not p.exists():
        return 0
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _iter_jsonl(p: Path):
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


def _push_history(key: str, total: int) -> tuple[float, float]:
    h = _history.setdefault(key, deque(maxlen=_HISTORY_LEN))
    now = time.time()
    h.append((now, total))
    if len(h) < 2:
        return (0.0, 0.0)
    recent = list(h)[-6:]
    long = list(h)
    rps_recent = (recent[-1][1] - recent[0][1]) / max(1.0, recent[-1][0] - recent[0][0])
    rps_long = (long[-1][1] - long[0][1]) / max(1.0, long[-1][0] - long[0][0])
    return (max(0.0, rps_recent), max(0.0, rps_long))


def _dir_size_and_count(d: Path) -> tuple[int, int]:
    if not d.exists():
        return 0, 0
    total = 0
    n = 0
    for p in d.rglob("*"):
        if p.is_file() and not p.name.startswith("._"):
            try:
                total += p.stat().st_size
                n += 1
            except OSError:
                continue
    return total, n


def _per_board_breakdown(images_jsonl: Path) -> list[dict]:
    """One row per (source, board) seen in images.jsonl."""
    by_key: dict[tuple[str, str], dict] = {}
    for rec in _iter_jsonl(images_jsonl):
        src = rec.get("source") or ""
        brd = rec.get("board") or ""
        key = (src, brd)
        slot = by_key.setdefault(key, {
            "source": src, "board": brd,
            "images": 0, "allow": 0, "quarantine": 0, "delete": 0,
            "bytes_recorded": 0,
        })
        slot["images"] += 1
        decision = (rec.get("safety_decision") or {}).get("decision") or "allow"
        if decision == "allow":
            slot["allow"] += 1
        elif decision == "delete":
            slot["delete"] += 1
        else:
            slot["quarantine"] += 1
        slot["bytes_recorded"] += int(rec.get("fsize_remote") or 0)
    return sorted(by_key.values(), key=lambda r: -r["images"])


@require_GET
def image_scrape_monitor_page(request):
    return render(request, "admin/image_scrape_monitor.html", {"targets": TARGETS})


@require_GET
def image_scrape_monitor_data(request):
    root = _root()
    manifests = root / "manifests"
    images_jsonl = manifests / "images.jsonl"
    posts_jsonl = manifests / "posts.jsonl"
    captions_jsonl = manifests / "captions.jsonl"
    image_features = manifests / "image_features.jsonl"
    duplicates = manifests / "duplicates.jsonl"
    cluster_membership = manifests / "cluster_membership.jsonl"
    clusters = manifests / "clusters.jsonl"

    total_images = _count_jsonl(images_jsonl)
    total_posts = _count_jsonl(posts_jsonl)
    total_captions = _count_jsonl(captions_jsonl)
    total_features = _count_jsonl(image_features)
    total_dupgroups = _count_jsonl(duplicates)
    total_members = _count_jsonl(cluster_membership)

    rps_recent, rps_long = _push_history("images_total", total_images)
    p_rps_recent, p_rps_long = _push_history("posts_total", total_posts)

    targets = []
    for t in TARGETS:
        pct = 100.0 * total_images / t if t else 0.0
        eta = None
        if rps_long > 0 and total_images < t:
            eta = int((t - total_images) / rps_long)
        targets.append({"target": t, "pct": min(100.0, pct), "eta_seconds": eta})

    # Originals / thumbnails / quarantine on-disk sizes
    originals_root = root / "raw"   # raw/<source>/<board>/originals/...
    quarantine_root = root / "quarantine"
    bytes_originals, n_originals = _dir_size_and_count(originals_root)
    bytes_quarantine, n_quarantine = _dir_size_and_count(quarantine_root)

    boards = _per_board_breakdown(images_jsonl)

    # Cluster summary
    cluster_rows = list(_iter_jsonl(clusters))
    cluster_rows.sort(key=lambda r: -int(r.get("size") or 0))
    top_clusters = [
        {
            "cluster_id": r.get("cluster_id"),
            "size": int(r.get("size") or 0),
            "label": r.get("suggested_label_auto") or "",
            "top_words": (r.get("top_caption_words") or [])[:4],
            "top_tags": (r.get("top_fit_tags") or [])[:3],
            "rep_keys": (r.get("representative_image_keys") or [])[:4],
        }
        for r in cluster_rows[:24] if int(r.get("cluster_id") or -1) != -1
    ]
    n_clusters = sum(1 for r in cluster_rows if int(r.get("cluster_id") or -1) != -1)
    n_noise = sum(int(r.get("size") or 0)
                  for r in cluster_rows if int(r.get("cluster_id") or -1) == -1)

    return JsonResponse({
        "ts": time.time(),
        "root": str(root),
        "totals": {
            "images": total_images,
            "posts": total_posts,
            "captions": total_captions,
            "features": total_features,
            "duplicate_groups": total_dupgroups,
            "cluster_members": total_members,
            "n_clusters": n_clusters,
            "n_noise": n_noise,
            "bytes_originals": bytes_originals,
            "n_files_originals": n_originals,
            "bytes_quarantine": bytes_quarantine,
            "n_files_quarantine": n_quarantine,
        },
        "rates": {
            "images_recent": rps_recent, "images_long": rps_long,
            "posts_recent": p_rps_recent, "posts_long": p_rps_long,
        },
        "targets": targets,
        "boards": boards,
        "clusters": top_clusters,
    })


@require_GET
def image_scrape_monitor_samples(request):
    """Return up to 12 random recent allow-listed images for the gallery."""
    root = _root()
    images_jsonl = root / "manifests" / "images.jsonl"
    rows = []
    for rec in _iter_jsonl(images_jsonl):
        if (rec.get("safety_decision") or {}).get("decision", "allow") != "allow":
            continue
        thumb = rec.get("thumbnail_path") or ""
        if not thumb or not Path(thumb).exists():
            continue
        rows.append(rec)
    if not rows:
        return JsonResponse({"samples": []})
    sample = random.sample(rows, min(12, len(rows)))
    out = []
    for r in sample:
        out.append({
            "image_key": r.get("image_key"),
            "board": r.get("board"),
            "thread_id": r.get("thread_id"),
            "post_id": r.get("post_id"),
            "width": r.get("width"),
            "height": r.get("height"),
            "filename": r.get("filename_original") or "",
            "thumb_url": "/api/image-monitor/thumb/?key=" + (r.get("image_key") or ""),
        })
    return JsonResponse({"samples": out})


# In-process state for background recompute
_recompute_state = {"running": False, "started_at": None, "ended_at": None,
                    "stage": "idle", "log": "", "error": ""}
_recompute_lock = threading.Lock()


def _discover_boards(root: Path, source: str) -> list[str]:
    """Boards with raw images on disk for the given source."""
    raw = root / "raw" / source
    if not raw.exists():
        return []
    return sorted([p.name for p in raw.iterdir() if p.is_dir()])


def _run_recompute(root: Path, source: str, board: str,
                   min_cluster_size: int = 5):
    """Run quality → embed → cluster in sequence, capturing stage and log.

    board="all" or "*" iterates every board found under raw/<source>/.
    """
    global _recompute_state
    base = [sys.executable, "manage.py"]
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "djangoscrap.settings"
    env["CHAN_CORPUS_ROOT"] = str(root)

    if board in ("all", "*"):
        boards = _discover_boards(root, source) or ["fit"]
    else:
        boards = [board]

    log_chunks: list[str] = []
    try:
        for b in boards:
            sequence = [
                ("quality", base + ["analyze_image_quality", "--source", source,
                                    "--board", b, "--root", str(root)]),
                ("embed",   base + ["embed_imageboard_images", "--source", source,
                                    "--board", b, "--root", str(root)]),
                ("cluster", base + ["cluster_imageboard_images", "--source", source,
                                    "--board", b, "--method", "hdbscan",
                                    "--min-cluster-size", str(min_cluster_size),
                                    "--root", str(root)]),
            ]
            for stage, cmd in sequence:
                with _recompute_lock:
                    _recompute_state["stage"] = f"{b}:{stage}"
                cwd = Path(__file__).resolve().parents[2]
                proc = subprocess.run(cmd, cwd=str(cwd), env=env,
                                      capture_output=True, text=True, timeout=3600)
                log_chunks.append(f"=== {b}:{stage} (rc={proc.returncode}) ===\n"
                                  f"{proc.stdout[-600:]}\n{proc.stderr[-300:]}")
                if proc.returncode != 0:
                    with _recompute_lock:
                        _recompute_state["error"] = f"{b}:{stage} failed (rc={proc.returncode})"
                    # don't abort entire multi-board run on one failure
                    break
    except Exception as e:
        with _recompute_lock:
            _recompute_state["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _recompute_lock:
            _recompute_state["running"] = False
            _recompute_state["ended_at"] = time.time()
            _recompute_state["stage"] = "done"
            _recompute_state["log"] = "\n\n".join(log_chunks)[-6000:]


@csrf_exempt
@require_POST
def image_scrape_monitor_recompute(request):
    """Kick off quality + embed + cluster in a background thread."""
    global _recompute_state
    with _recompute_lock:
        if _recompute_state["running"]:
            return JsonResponse({"ok": False, "msg": "already running",
                                  "state": _recompute_state})
        _recompute_state.update({
            "running": True, "started_at": time.time(),
            "ended_at": None, "stage": "starting",
            "log": "", "error": "",
        })
    source = request.POST.get("source") or "fourchan"
    board = request.POST.get("board") or "fit"
    try:
        min_cs = int(request.POST.get("min_cluster_size") or 5)
    except ValueError:
        min_cs = 5
    threading.Thread(
        target=_run_recompute,
        args=(_root(), source, board, min_cs),
        daemon=True,
    ).start()
    return JsonResponse({"ok": True, "msg": "started",
                          "state": _recompute_state})


@require_GET
def image_scrape_monitor_recompute_status(request):
    return JsonResponse({"state": _recompute_state})


@require_GET
def image_scrape_monitor_thumb(request):
    """Stream a corpus thumbnail by image_key.

    Restricted to thumbnails for allow-listed images. Resolves path via the
    images manifest so we never serve quarantined bytes."""
    key = request.GET.get("key") or ""
    if not key or "/" in key:
        return HttpResponseNotFound()
    root = _root()
    images_jsonl = root / "manifests" / "images.jsonl"
    for rec in _iter_jsonl(images_jsonl):
        if rec.get("image_key") != key:
            continue
        if (rec.get("safety_decision") or {}).get("decision", "allow") != "allow":
            return HttpResponseNotFound()
        want_full = request.GET.get("full") in ("1", "true", "yes")
        path_str = ""
        if want_full:
            path_str = rec.get("original_path") or rec.get("file_path") or ""
        if not path_str:
            path_str = rec.get("thumbnail_path") or ""
        if not path_str:
            return HttpResponseNotFound()
        p = Path(path_str)
        if not p.exists() or not p.is_file():
            return HttpResponseNotFound()
        # Make sure we only serve files inside the chan_corpus root.
        # macOS is case-insensitive but pathlib's relative_to is case-sensitive,
        # so compare case-folded strings to tolerate /Volumes/Oom vs /Volumes/oom.
        try:
            rp = str(p.resolve()).casefold()
            rr = str(root.resolve()).casefold()
            if not (rp == rr or rp.startswith(rr + os.sep)):
                return HttpResponseNotFound()
        except (OSError, ValueError):
            return HttpResponseNotFound()
        return FileResponse(p.open("rb"), content_type="image/jpeg")
    return HttpResponseNotFound()
