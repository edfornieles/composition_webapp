"""Active-learning P(keep) ranker over CLIP embeddings + decision log.

Trains a logistic regression on the user's keep/reject decisions, scores
every embedding in the corpus, returns the score map. Cached by
(decision-log mtime + embeddings mtime). Re-fits when either advances.

Endpoints:
  POST /api/image-monitor/ranker/refit/
        → fit on current decisions, return summary (n_keep, n_reject, val_acc)
  GET  /api/image-monitor/ranker/scores/?keys=k1,k2,...
        → P(keep) for the requested keys (subset query)
  GET  /api/image-monitor/ranker/state/
        → status: trained?, counts, val_acc, sample size

The classifier is a small numpy-only logistic regression with L2, gradient
descent. ~50ms to fit on 50k embeddings × 512 dims with a few hundred
labels — fast enough to refit every time the user lands on the page.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Iterator

import numpy as np  # type: ignore
from django.http import JsonResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST


_DEFAULT_ROOT = Path(os.environ.get("CHAN_CORPUS_ROOT", "/Volumes/Oom/chan_corpus"))


def _root() -> Path:
    return _DEFAULT_ROOT


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


_lock = threading.Lock()
_state = {
    "fit_at": 0.0, "decisions_mtime": 0.0, "embeddings_sig": 0.0,
    "weights": None, "bias": 0.0,
    "scores": {},  # image_key -> float
    "n_keep": 0, "n_reject": 0, "val_acc": None,
    "n_scored": 0, "error": None,
}


def _decisions_mtime(root: Path) -> float:
    p = root / "manifests" / "curation_decisions.jsonl"
    return p.stat().st_mtime if p.exists() else 0.0


def _logit_fit(X: np.ndarray, y: np.ndarray, *, l2: float = 1.0,
               lr: float = 0.5, steps: int = 400):
    """Tiny LR with full-batch GD. Returns (w, b)."""
    n, d = X.shape
    w = np.zeros(d, dtype=np.float32)
    b = 0.0
    for _ in range(steps):
        z = X @ w + b
        # numerically stable sigmoid
        p = np.where(z >= 0, 1.0 / (1.0 + np.exp(-z)), np.exp(z) / (1.0 + np.exp(z)))
        diff = (p - y).astype(np.float32)
        gw = (X.T @ diff) / n + (l2 / n) * w
        gb = float(diff.mean())
        w -= lr * gw
        b -= lr * gb
    return w, b


def _logit_predict(X: np.ndarray, w: np.ndarray, b: float) -> np.ndarray:
    z = X @ w + b
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _refit(root: Path) -> dict:
    """Fit logistic regression on all keep/reject pairs in the decision log."""
    from ._image_search import _load_all_embeddings  # reuse cache
    V, ids, idx = _load_all_embeddings(root)
    if V is None:
        raise RuntimeError("no embeddings on disk yet")

    # Latest decision wins per image_key
    eff: dict[str, str] = {}
    for rec in _iter_jsonl(root / "manifests" / "curation_decisions.jsonl"):
        ik = rec.get("image_key"); d = rec.get("decision")
        if ik and d in ("keep", "reject"):
            eff[ik] = d

    keep_idx = [idx[k] for k, d in eff.items() if d == "keep" and k in idx]
    rej_idx = [idx[k] for k, d in eff.items() if d == "reject" and k in idx]
    n_keep, n_reject = len(keep_idx), len(rej_idx)
    if n_keep < 5 or n_reject < 5:
        raise RuntimeError(f"need at least 5 keeps and 5 rejects (have {n_keep} / {n_reject})")

    X_pos = V[keep_idx]; X_neg = V[rej_idx]
    X = np.concatenate([X_pos, X_neg], axis=0).astype(np.float32)
    y = np.concatenate([np.ones(n_keep), np.zeros(n_reject)]).astype(np.float32)

    # Shuffle + 80/20 split for a quick val_acc sanity number
    rng = np.random.default_rng(42)
    perm = rng.permutation(len(y))
    X, y = X[perm], y[perm]
    n_tr = int(len(y) * 0.8)
    Xtr, ytr = X[:n_tr], y[:n_tr]
    Xv, yv = X[n_tr:], y[n_tr:]

    w, b = _logit_fit(Xtr, ytr)
    val_acc = None
    if len(yv) > 0:
        pv = _logit_predict(Xv, w, b)
        val_acc = float(((pv >= 0.5).astype(np.float32) == yv).mean())

    # Score the entire corpus in one matmul
    scores_arr = _logit_predict(V, w, b)
    scores = {ids[i]: float(scores_arr[i]) for i in range(len(ids))}

    return {
        "weights": w, "bias": float(b),
        "scores": scores, "n_keep": n_keep, "n_reject": n_reject,
        "val_acc": val_acc, "n_scored": len(ids),
    }


def _maybe_refit(root: Path, force: bool = False) -> None:
    from ._image_search import _emb_cache
    cur_dec = _decisions_mtime(root)
    cur_emb = _emb_cache.get("mtime", 0.0)
    with _lock:
        stale = force or (cur_dec != _state["decisions_mtime"]) or (cur_emb != _state["embeddings_sig"])
        if not stale and _state["weights"] is not None:
            return
        try:
            res = _refit(root)
            _state.update(res)
            _state.update({"decisions_mtime": cur_dec, "embeddings_sig": cur_emb,
                           "fit_at": time.time(), "error": None})
        except Exception as e:
            _state["error"] = f"{type(e).__name__}: {e}"


@csrf_exempt
@require_POST
def ranker_refit(request):
    _maybe_refit(_root(), force=True)
    return JsonResponse({
        "ok": _state["error"] is None,
        "error": _state["error"],
        "n_keep": _state["n_keep"], "n_reject": _state["n_reject"],
        "val_acc": _state["val_acc"], "n_scored": _state["n_scored"],
        "fit_at": _state["fit_at"],
    })


@require_GET
def ranker_state(request):
    _maybe_refit(_root())
    return JsonResponse({
        "trained": _state["weights"] is not None,
        "n_keep": _state["n_keep"], "n_reject": _state["n_reject"],
        "val_acc": _state["val_acc"], "n_scored": _state["n_scored"],
        "fit_at": _state["fit_at"], "error": _state["error"],
    })


@require_GET
def ranker_scores(request):
    _maybe_refit(_root())
    raw = request.GET.get("keys") or ""
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        return JsonResponse({"scores": {}})
    return JsonResponse({"scores": {k: _state["scores"].get(k) for k in keys},
                         "trained": _state["weights"] is not None})
