"""Curation / triage backend for the image-scrape monitor.

Append-only decision log: one JSONL line per click. Effective decision per
image_key is the latest line. Keeps a fast in-memory index that's rebuilt
from disk lazily.

A *reject* doesn't touch any bytes — only marks the image_key in the log.
A separate "apply rejections" pass (not implemented here) can later move
files into derived/rejected/ or unlink them with confirmation.

Endpoints:
  GET  /api/image-monitor/cluster/?cluster_id=N
        → list of members of cluster N with current decisions
  POST /api/image-monitor/decide/   (image_keys[]=…&decision=keep|reject|none)
        → log per-image decisions
  POST /api/image-monitor/decide-cluster/ (cluster_id=N&decision=…)
        → mass apply to every member of that cluster
  GET  /api/image-monitor/decision-counts/
        → totals across keep/reject/none for the dashboard

Decisions are scoped to the chan_corpus root (CHAN_CORPUS_ROOT).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Iterator

from django.http import JsonResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST


_DEFAULT_ROOT = Path(os.environ.get("CHAN_CORPUS_ROOT", "/Volumes/Oom/chan_corpus"))
_VALID = {"keep", "reject", "none", "later"}
_lock = threading.Lock()


def _root() -> Path:
    return _DEFAULT_ROOT


def _decisions_path(root: Path) -> Path:
    return root / "manifests" / "curation_decisions.jsonl"


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


def _effective_decisions(root: Path) -> dict[str, str]:
    """Last write wins."""
    out: dict[str, str] = {}
    for rec in _iter_jsonl(_decisions_path(root)):
        ik = rec.get("image_key")
        d = rec.get("decision")
        if ik and d in _VALID:
            out[ik] = d
    return out


def _append(root: Path, records: list[dict]):
    p = _decisions_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with p.open("a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _members_for_cluster(root: Path, cluster_id: int) -> list[dict]:
    """Read cluster_membership + image manifest + decisions to build a card list."""
    cm_path = root / "manifests" / "cluster_membership.jsonl"
    keys_in_cluster: list[str] = []
    for rec in _iter_jsonl(cm_path):
        if int(rec.get("cluster_id", -99)) == int(cluster_id):
            ik = rec.get("image_key")
            if ik:
                keys_in_cluster.append(ik)
    if not keys_in_cluster:
        return []
    keyset = set(keys_in_cluster)

    img_idx: dict[str, dict] = {}
    for rec in _iter_jsonl(root / "manifests" / "images.jsonl"):
        ik = rec.get("image_key")
        if ik in keyset:
            img_idx[ik] = rec

    decisions = _effective_decisions(root)
    out: list[dict] = []
    for ik in keys_in_cluster:
        rec = img_idx.get(ik) or {}
        sd = (rec.get("safety_decision") or {}).get("decision") or "allow"
        if sd != "allow":
            continue  # never expose quarantined items
        thumb = rec.get("thumbnail_path") or ""
        if not thumb or not Path(thumb).exists():
            continue
        out.append({
            "image_key": ik,
            "width": rec.get("width") or 0,
            "height": rec.get("height") or 0,
            "filename": rec.get("filename_original") or "",
            "thumb_url": "/api/image-monitor/thumb/?key=" + ik,
            "decision": decisions.get(ik, "none"),
        })
    return out


@require_GET
def image_curation_cluster(request):
    raw = request.GET.get("cluster_id")
    try:
        cid = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return HttpResponseBadRequest("cluster_id must be an int")
    if cid is None:
        return HttpResponseBadRequest("cluster_id required")
    members = _members_for_cluster(_root(), cid)
    counts = {"keep": 0, "reject": 0, "none": 0, "later": 0}
    for m in members:
        counts[m["decision"]] = counts.get(m["decision"], 0) + 1
    return JsonResponse({
        "cluster_id": cid,
        "size": len(members),
        "counts": counts,
        "members": members,
    })


def _decide(records_in: list[tuple[str, str, str]]):
    """records_in: list of (image_key, decision, scope)."""
    now = int(time.time())
    out = [{
        "image_key": ik, "decision": d, "scope": s, "at_unix": now,
    } for (ik, d, s) in records_in if ik and d in _VALID]
    if out:
        _append(_root(), out)
    return out


@csrf_exempt
@require_POST
def image_curation_decide(request):
    decision = request.POST.get("decision") or ""
    if decision not in _VALID:
        return HttpResponseBadRequest(f"decision must be one of {_VALID}")
    keys = request.POST.getlist("image_keys[]") or request.POST.getlist("image_keys")
    if not keys:
        # also accept comma-separated single field
        raw = request.POST.get("image_keys") or ""
        keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        return HttpResponseBadRequest("image_keys required")
    written = _decide([(k, decision, "image") for k in keys])
    return JsonResponse({"ok": True, "written": len(written), "decision": decision})


@csrf_exempt
@require_POST
def image_curation_decide_cluster(request):
    decision = request.POST.get("decision") or ""
    if decision not in _VALID:
        return HttpResponseBadRequest(f"decision must be one of {_VALID}")
    raw = request.POST.get("cluster_id")
    try:
        cid = int(raw)
    except (TypeError, ValueError):
        return HttpResponseBadRequest("cluster_id required")
    members = _members_for_cluster(_root(), cid)
    written = _decide([(m["image_key"], decision, "cluster") for m in members])
    return JsonResponse({"ok": True, "cluster_id": cid, "written": len(written),
                          "decision": decision, "size": len(members)})


@require_GET
def image_curation_counts(request):
    decisions = _effective_decisions(_root())
    counts = {"keep": 0, "reject": 0, "none": 0, "later": 0}
    for d in decisions.values():
        counts[d] = counts.get(d, 0) + 1
    return JsonResponse({"counts": counts, "total_decisions": len(decisions)})
