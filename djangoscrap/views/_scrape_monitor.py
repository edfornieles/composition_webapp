"""Live progress visualiser for the imageboard text scrapers.

Reads the per-corpus state files and JSONL shard line counts, computes
throughput from a small in-memory history, returns JSON for the admin page.
"""
from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

import random

CORPUS_ROOT = Path("/Volumes/oom/imageboard_corpora")
SAMPLE_STRIDE = 1000  # show one post per N
MAX_SAMPLES = 12

CORPORA = [
    {"key": "desu", "label": "desuarchive", "dir": "imageboard_text_desu", "state": "desu_state.json"},
    {"key": "fourplebs", "label": "archive.4plebs", "dir": "imageboard_text_4plebs", "state": "fourplebs_state.json"},
    {"key": "b4k", "label": "arch.b4k.dev", "dir": "imageboard_text_b4k", "state": "b4k_state.json"},
]

TARGETS = [1_000_000, 5_000_000, 10_000_000]

_history: dict[str, deque] = {}
_HISTORY_LEN = 60  # last ~60 samples


def _count_jsonl_lines(d: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    if not d.exists():
        return out
    for p in d.rglob("*.jsonl"):
        if p.name.startswith("._"):
            continue
        try:
            with p.open("rb") as fh:
                count = sum(1 for _ in fh)
            out[p.stem] = count
        except OSError:
            continue
    return out


def _read_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _push_history(key: str, total: int) -> tuple[float, float]:
    """Returns (posts_per_sec_recent, posts_per_sec_long)."""
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


@require_GET
def scrape_monitor_page(request):
    return render(request, "admin/scrape_monitor.html", {"targets": TARGETS})


def _sample_posts() -> list[dict]:
    candidates: list[Path] = []
    for c in CORPORA:
        text_dir = CORPUS_ROOT / c["dir"] / "text"
        if not text_dir.exists():
            continue
        for p in text_dir.rglob("*.jsonl"):
            if p.name.startswith("._"):
                continue
            try:
                if p.stat().st_size > 0:
                    candidates.append(p)
            except OSError:
                continue
    if not candidates:
        return []
    out: list[dict] = []
    random.shuffle(candidates)
    for shard in candidates:
        if len(out) >= MAX_SAMPLES:
            break
        try:
            with shard.open("r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        if not lines:
            continue
        idxs = list(range(0, len(lines), SAMPLE_STRIDE)) or [0]
        random.shuffle(idxs)
        for idx in idxs[:3]:
            try:
                rec = json.loads(lines[idx])
            except json.JSONDecodeError:
                continue
            comment = (rec.get("comment") or "").strip()
            if len(comment) < 12:
                continue
            out.append({
                "board": rec.get("board"),
                "thread_num": rec.get("thread_num"),
                "post_num": rec.get("post_num"),
                "is_op": rec.get("is_op"),
                "subject": (rec.get("subject") or "")[:80],
                "comment": comment[:600],
                "shard": shard.parent.parent.name,
            })
            if len(out) >= MAX_SAMPLES:
                break
    return out


@require_GET
def scrape_monitor_samples(request):
    return JsonResponse({"samples": _sample_posts()})


@require_GET
def scrape_monitor_data(request):
    grand_total = 0
    sources = []
    for c in CORPORA:
        base = CORPUS_ROOT / c["dir"]
        text_dir = base / "text"
        sources_dir = base / "sources"
        boards = _count_jsonl_lines(text_dir)
        total = sum(boards.values())
        grand_total += total

        state = _read_state(sources_dir / c["state"])
        bstate = (state.get("boards") or {})
        stats = state.get("stats") or {}

        rps_recent, rps_long = _push_history(c["key"], total)

        board_rows = []
        for board, count in sorted(boards.items(), key=lambda kv: -kv[1]):
            bs = bstate.get(board) or {}
            board_rows.append({
                "board": board,
                "posts": count,
                "last_page": bs.get("last_page") or 0,
                "threads_done": len(bs.get("processed_threads") or []),
            })

        sources.append({
            "key": c["key"],
            "label": c["label"],
            "total_posts": total,
            "rps_recent": round(rps_recent, 2),
            "rps_long": round(rps_long, 2),
            "errors": stats.get("errors") or 0,
            "elapsed_seconds": stats.get("elapsed_seconds") or 0,
            "boards": board_rows,
        })

    rps_recent_total, rps_long_total = _push_history("__grand__", grand_total)

    targets = []
    for t in TARGETS:
        remaining = max(0, t - grand_total)
        eta_seconds = (remaining / rps_long_total) if rps_long_total > 0 else None
        targets.append({
            "target": t,
            "pct": round(min(100.0, 100.0 * grand_total / t), 2),
            "eta_seconds": int(eta_seconds) if eta_seconds is not None else None,
        })

    return JsonResponse({
        "grand_total": grand_total,
        "rps_recent": round(rps_recent_total, 2),
        "rps_long": round(rps_long_total, 2),
        "sources": sources,
        "targets": targets,
        "ts": int(time.time()),
    })
