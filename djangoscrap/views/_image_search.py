"""Text-to-image search over OpenCLIP embeddings + named-cluster persistence.

Loads every per-board embeddings npy under chan_corpus/manifests/embeddings/,
concatenates them in memory, then computes cosine similarity against an
on-the-fly text embedding from OpenCLIP's text tower.

The OpenCLIP model is loaded lazily on first call and pinned to module scope
so subsequent searches are cheap (matmul-only on Apple Silicon: ~tens of ms
for ~50k embeddings).

Endpoints:
  GET  /api/image-monitor/search/?q=king+of+the+hill&top=200
        → ranked list of image_keys + thumb_urls + similarities
  POST /api/image-monitor/save-named-cluster/   (name, image_keys[])
        → append-only record in manifests/named_clusters.jsonl
  GET  /api/image-monitor/named-clusters/
        → list saved named clusters with sizes
  GET  /api/image-monitor/named-cluster/?name=...
        → full member list (filtered to allow + thumbnail-exists)
"""
from __future__ import annotations

import json
import os
import re
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


# --- model / embeddings cache ----------------------------------------------

_model_lock = threading.Lock()
_clip_state = {
    "model": None, "tokenizer": None, "device": None, "model_tag": None,
}


def _load_clip_text_encoder(model_name: str = "ViT-B-32",
                            pretrained: str = "laion2b_s34b_b79k"):
    """Lazy-load OpenCLIP and return (encode_text_fn, model_tag)."""
    with _model_lock:
        tag = f"{model_name}_{pretrained}".replace("/", "-")
        if _clip_state["model"] is not None and _clip_state["model_tag"] == tag:
            return _clip_state, tag
        import torch  # type: ignore
        import open_clip  # type: ignore
        device = ("cuda" if torch.cuda.is_available()
                  else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
                  else "cpu")
        model, _, _ = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained, device=device)
        model.eval()
        tokenizer = open_clip.get_tokenizer(model_name)
        _clip_state.update({"model": model, "tokenizer": tokenizer,
                            "device": device, "model_tag": tag, "torch": torch})
        return _clip_state, tag


_emb_lock = threading.Lock()
_emb_cache = {
    "mtime": 0.0, "vectors": None, "ids": [], "id_to_idx": {},
}


def _embeddings_dir(root: Path) -> Path:
    return root / "manifests" / "embeddings"


def _load_all_embeddings(root: Path, model_tag: str = "ViT-B-32_laion2b_s34b_b79k"):
    """Concatenate every per-board npy for the given model tag.

    Cached by directory mtime; reloads when new boards appear or embeddings
    are recomputed."""
    edir = _embeddings_dir(root)
    if not edir.exists():
        return None, [], {}
    npy_files = sorted(edir.glob(f"openclip_{model_tag}_*.npy"))
    if not npy_files:
        return None, [], {}
    sig = max(p.stat().st_mtime for p in npy_files)
    with _emb_lock:
        if _emb_cache["mtime"] == sig and _emb_cache["vectors"] is not None:
            return _emb_cache["vectors"], _emb_cache["ids"], _emb_cache["id_to_idx"]
        vecs = []
        all_ids: list[str] = []
        for npy in npy_files:
            ids_json = npy.with_name(npy.stem + "_ids.json")
            if not ids_json.exists():
                continue
            arr = np.load(npy)
            ids = json.loads(ids_json.read_text(encoding="utf-8"))
            if len(ids) != arr.shape[0]:
                continue
            vecs.append(arr.astype(np.float32))
            all_ids.extend(ids)
        if not vecs:
            return None, [], {}
        V = np.concatenate(vecs, axis=0)
        # L2-normalise once for cosine via dot
        norms = np.linalg.norm(V, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        V = V / norms
        idx = {ik: i for i, ik in enumerate(all_ids)}
        _emb_cache.update({"mtime": sig, "vectors": V, "ids": all_ids, "id_to_idx": idx})
        return V, all_ids, idx


def _excluded_keys(root: Path, mode: str) -> set[str]:
    """Build an exclusion set for searches.

    mode=saved   → every image_key already in any named cluster
    mode=rejected→ every key with effective decision == 'reject'
    mode=decided → 'reject' OR 'later' (anything user has touched)
    mode=both    → saved + rejected
    mode=all     → saved + rejected + later + keep (everything triaged)
    mode=''/none → empty
    """
    if not mode or mode == "none":
        return set()
    out: set[str] = set()
    if mode in ("saved", "both", "all"):
        for rec in _effective_named_clusters(root).values():
            out.update(rec.get("image_keys") or [])
    if mode in ("rejected", "both", "decided", "all"):
        # Avoid circular import — read decisions directly here
        dp = root / "manifests" / "curation_decisions.jsonl"
        latest: dict[str, str] = {}
        for rec in _iter_jsonl(dp):
            ik = rec.get("image_key"); d = rec.get("decision")
            if ik and d:
                latest[ik] = d
        for ik, d in latest.items():
            if mode == "rejected" and d == "reject":
                out.add(ik)
            elif mode == "both" and d == "reject":
                out.add(ik)
            elif mode == "decided" and d in ("reject", "later"):
                out.add(ik)
            elif mode == "all" and d in ("keep", "reject", "later"):
                out.add(ik)
    return out


def _allow_set_with_thumbs(root: Path, want_keys: set[str]) -> dict[str, dict]:
    """Filter to allow-listed keys with on-disk thumbnails, returning meta."""
    out: dict[str, dict] = {}
    for rec in _iter_jsonl(root / "manifests" / "images.jsonl"):
        ik = rec.get("image_key")
        if ik not in want_keys:
            continue
        if (rec.get("safety_decision") or {}).get("decision", "allow") != "allow":
            continue
        thumb = rec.get("thumbnail_path") or ""
        if not thumb or not Path(thumb).exists():
            continue
        out[ik] = rec
    return out


# --- endpoints --------------------------------------------------------------


def _query_vector_from_keys(V: np.ndarray, idx: dict, plus_keys: list[str],
                             minus_keys: list[str] | None = None) -> np.ndarray | None:
    """Average + L2-norm of the plus-keys' vectors, optionally subtract minus."""
    plus = [idx[k] for k in plus_keys if k in idx]
    if not plus:
        return None
    v = V[plus].mean(axis=0)
    if minus_keys:
        m = [idx[k] for k in minus_keys if k in idx]
        if m:
            mvec = V[m].mean(axis=0)
            v = v - 0.5 * mvec
    n = np.linalg.norm(v)
    if n == 0:
        return None
    return (v / n).astype(np.float32)


@require_GET
def image_search_by_image(request):
    """Image-as-query: pass one or more image_keys, optionally subtract a set."""
    plus_raw = request.GET.get("keys") or request.GET.get("key") or ""
    plus_keys = [k.strip() for k in plus_raw.split(",") if k.strip()]
    minus_raw = request.GET.get("minus_keys") or request.GET.get("minus") or ""
    minus_keys = [k.strip() for k in minus_raw.split(",") if k.strip()]
    if not plus_keys:
        return HttpResponseBadRequest("keys required")
    try:
        top = max(1, min(int(request.GET.get("top") or 200), 1000))
    except ValueError:
        top = 200
    try:
        min_sim = float(request.GET.get("min_sim") or 0.0)
    except ValueError:
        min_sim = 0.0

    root = _root()
    V, ids, idx = _load_all_embeddings(root)
    if V is None:
        return JsonResponse({"ok": False, "error": "no embeddings yet"})

    qv = _query_vector_from_keys(V, idx, plus_keys, minus_keys)
    if qv is None:
        return JsonResponse({"ok": False, "error": "none of the keys are in embeddings"})

    sims = V @ qv
    drop = set(plus_keys)  # don't return the query itself
    if top >= sims.shape[0]:
        order = np.argsort(-sims)
    else:
        part = np.argpartition(-sims, top - 1)[:top]
        order = part[np.argsort(-sims[part])]
    ranked = [(ids[i], float(sims[i])) for i in order
              if ids[i] not in drop and float(sims[i]) >= min_sim]
    meta = _allow_set_with_thumbs(root, set(k for k, _ in ranked))
    members = []
    for ik, sim in ranked:
        rec = meta.get(ik)
        if not rec:
            continue
        members.append({
            "image_key": ik, "similarity": round(sim, 4),
            "width": rec.get("width") or 0, "height": rec.get("height") or 0,
            "filename": rec.get("filename_original") or "",
            "thumb_url": "/api/image-monitor/thumb/?key=" + ik,
            "decision": "none",
        })
    label = f"like {len(plus_keys)} image(s)"
    if minus_keys:
        label += f" minus {len(minus_keys)}"
    return JsonResponse({"ok": True, "q": label, "n_searched": len(ids),
                         "n_results": len(members), "members": members,
                         "plus": plus_keys, "minus": minus_keys})


# --- Aesthetic clustering --------------------------------------------------

_aesthetic_lock = threading.Lock()
_aesthetic_cache = {"sig": (0, 0), "labels": {}, "rows": []}


def _aesthetic_features_for_key(rec: dict) -> list[float] | None:
    try:
        feats = [
            float(rec.get("brightness") or 0.0) / 255.0,
            float(rec.get("contrast") or 0.0) / 128.0,
            float(rec.get("saturation") or 0.0) / 100.0,
            float(rec.get("entropy") or 0.0) / 8.0,
            min(1.0, float(rec.get("blur_score") or 0.0) / 200.0),
            float(rec.get("aspect_ratio") or 1.0),
            1.0 if (rec.get("orientation") == "landscape") else 0.0,
        ]
        return feats
    except Exception:
        return None


def _build_aesthetic_clusters(root: Path, *, n_clusters: int = 24) -> dict:
    feats_path = root / "manifests" / "image_features.jsonl"
    img_path = root / "manifests" / "images.jsonl"
    sig = (
        feats_path.stat().st_mtime if feats_path.exists() else 0,
        img_path.stat().st_mtime if img_path.exists() else 0,
    )
    with _aesthetic_lock:
        if _aesthetic_cache["sig"] == sig and _aesthetic_cache["rows"]:
            return {"rows": _aesthetic_cache["rows"], "labels": _aesthetic_cache["labels"]}

        # Filter to allow-listed
        allow: set[str] = set()
        meta_by_key: dict[str, dict] = {}
        for rec in _iter_jsonl(img_path):
            ik = rec.get("image_key")
            if not ik:
                continue
            sd = (rec.get("safety_decision") or {}).get("decision") or "allow"
            if sd != "allow":
                continue
            thumb = rec.get("thumbnail_path") or ""
            if not thumb or not Path(thumb).exists():
                continue
            allow.add(ik); meta_by_key[ik] = rec

        keys: list[str] = []; X: list[list[float]] = []
        for rec in _iter_jsonl(feats_path):
            ik = rec.get("image_key")
            if ik not in allow:
                continue
            f = _aesthetic_features_for_key(rec)
            if f is None:
                continue
            keys.append(ik); X.append(f)
        if len(keys) < n_clusters * 2:
            return {"rows": [], "labels": {}}

        Xa = np.array(X, dtype=np.float32)
        # z-score so palette/contrast aren't dominated by aspect_ratio
        mu = Xa.mean(axis=0); sd = Xa.std(axis=0); sd[sd == 0] = 1.0
        Z = (Xa - mu) / sd

        from sklearn.cluster import KMeans  # type: ignore
        km = KMeans(n_clusters=int(n_clusters), n_init=8, random_state=42).fit(Z)
        labels = km.labels_

        # Build cluster summary rows
        by_cid: dict[int, list[int]] = {}
        for i, c in enumerate(labels):
            by_cid.setdefault(int(c), []).append(i)

        feat_names = ["bright", "contrast", "satur", "entropy", "sharp", "aspect", "landscape"]
        rows = []
        for cid, idxs in sorted(by_cid.items(), key=lambda kv: -len(kv[1])):
            centroid = Z[idxs].mean(axis=0)
            # Pick top-3 distinguishing features (largest |z| against corpus mean=0)
            order = np.argsort(-np.abs(centroid))
            tags = []
            for j in order[:3]:
                direction = "high" if centroid[j] > 0 else "low"
                tags.append(f"{direction}-{feat_names[j]}")
            # Reps = closest to centroid in feature space
            ddist = ((Z[idxs] - centroid) ** 2).sum(axis=1)
            rep_order = list(np.argsort(ddist))[:6]
            rep_keys = [keys[idxs[r]] for r in rep_order]
            rows.append({
                "cluster_id": int(cid), "size": len(idxs),
                "label": "_".join(tags),
                "feature_tags": tags,
                "rep_keys": rep_keys,
            })
        label_map = {keys[i]: int(labels[i]) for i in range(len(keys))}
        _aesthetic_cache["sig"] = sig
        _aesthetic_cache["rows"] = rows
        _aesthetic_cache["labels"] = label_map
        return {"rows": rows, "labels": label_map}


@require_GET
def aesthetic_clusters_index(request):
    try:
        n = max(4, min(int(request.GET.get("n") or 24), 80))
    except ValueError:
        n = 24
    out = _build_aesthetic_clusters(_root(), n_clusters=n)
    return JsonResponse({"ok": True, "clusters": out["rows"][:80]})


@require_GET
def aesthetic_cluster_members(request):
    try:
        cid = int(request.GET.get("cluster_id"))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("cluster_id required")
    out = _build_aesthetic_clusters(_root())
    keys = [k for k, c in out["labels"].items() if c == cid]
    meta = _allow_set_with_thumbs(_root(), set(keys))
    from ._image_curation import _effective_decisions
    decisions = _effective_decisions(_root())
    members = []
    for ik in keys:
        rec = meta.get(ik)
        if not rec:
            continue
        members.append({
            "image_key": ik,
            "width": rec.get("width") or 0, "height": rec.get("height") or 0,
            "filename": rec.get("filename_original") or "",
            "thumb_url": "/api/image-monitor/thumb/?key=" + ik,
            "decision": decisions.get(ik, "none"),
        })
    counts = {"keep": 0, "reject": 0, "none": 0}
    for m in members:
        counts[m["decision"]] = counts.get(m["decision"], 0) + 1
    return JsonResponse({"ok": True, "cluster_id": cid, "size": len(members),
                         "counts": counts, "members": members})


def _encode_text(state, queries: list[str]) -> np.ndarray:
    """Average + L2-norm text-vector for a list of phrases."""
    torch = state["torch"]; tok = state["tokenizer"]; model = state["model"]; dev = state["device"]
    with torch.no_grad():
        t = tok(queries).to(dev)
        v = model.encode_text(t)
        v = v / v.norm(dim=-1, keepdim=True)
        v = v.mean(dim=0)
        v = v / v.norm()
        return v.cpu().numpy().astype(np.float32)


def _parse_query_terms(raw: str) -> tuple[list[str], list[str]]:
    """Split 'a, b, -c, +d' → (positives, negatives). Bare terms are positive."""
    pos: list[str] = []; neg: list[str] = []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) == 1 and not parts[0].startswith(("+", "-")):
        return [parts[0]], []
    for p in parts:
        if p.startswith("-"):
            neg.append(p[1:].strip())
        elif p.startswith("+"):
            pos.append(p[1:].strip())
        else:
            pos.append(p)
    return [p for p in pos if p], [n for n in neg if n]


@require_GET
def image_search_refine(request):
    """Re-rank a given set of image_keys by similarity to a new text query.

    Used to drill down inside an existing search: keeps the candidate set
    fixed, sorts by relevance to the sub-concept. Optional min_sim filter.
    """
    sub_q = (request.GET.get("q") or "").strip()
    keys_raw = request.GET.get("keys") or ""
    keys = [k.strip() for k in keys_raw.split(",") if k.strip()]
    if not sub_q:
        return HttpResponseBadRequest("q required")
    if not keys:
        return HttpResponseBadRequest("keys required")
    try:
        min_sim = float(request.GET.get("min_sim") or 0.20)
    except ValueError:
        min_sim = 0.20
    # Default behaviour is now FILTER (drop below threshold). Pass mode=rerank
    # to keep all candidates and merely re-order them.
    mode = (request.GET.get("mode") or request.GET.get("filter") or "filter").lower()
    keep_only = mode != "rerank"
    strict_and = (request.GET.get("strict_and") or "").lower() in ("1", "true", "yes")
    exclude_mode = (request.GET.get("exclude") or "").lower()

    root = _root()
    V, ids, idx = _load_all_embeddings(root)
    if V is None:
        return JsonResponse({"ok": False, "error": "no embeddings yet"})
    try:
        state, _ = _load_clip_text_encoder()
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"clip load failed: {e}"})

    pos, neg = _parse_query_terms(sub_q)
    if not pos:
        return HttpResponseBadRequest("at least one positive term required")

    excluded = _excluded_keys(root, exclude_mode)
    keys_in = [k for k in keys if k in idx and k not in excluded]
    if not keys_in:
        return JsonResponse({"ok": False, "error": "no candidate keys after exclude"})
    sub_idx = np.array([idx[k] for k in keys_in], dtype=np.int64)

    if strict_and and len(pos) > 1:
        per = []
        for term in pos:
            tv = _encode_text(state, [term])
            if neg:
                nv = _encode_text(state, neg)
                tv = tv - 0.5 * nv
                nrm = np.linalg.norm(tv)
                if nrm > 0:
                    tv = tv / nrm
            per.append(V[sub_idx] @ tv)
        S = np.stack(per, axis=0)
        sims = S.min(axis=0)
        per_pass = (S >= min_sim).all(axis=0)
        pairs = [(keys_in[i], float(sims[i])) for i in range(len(keys_in))
                 if (not keep_only) or per_pass[i]]
    else:
        qv = _encode_text(state, pos)
        if neg:
            nv = _encode_text(state, neg)
            qv = qv - 0.5 * nv
            nrm = np.linalg.norm(qv)
            if nrm > 0:
                qv = qv / nrm
        sims = V[sub_idx] @ qv
        pairs = list(zip(keys_in, [float(s) for s in sims]))
        if keep_only:
            pairs = [p for p in pairs if p[1] >= min_sim]
    pairs.sort(key=lambda p: -p[1])

    meta = _allow_set_with_thumbs(root, set(k for k, _ in pairs))
    members = []
    for ik, sim in pairs:
        rec = meta.get(ik)
        if not rec:
            continue
        members.append({
            "image_key": ik, "similarity": round(sim, 4),
            "width": rec.get("width") or 0, "height": rec.get("height") or 0,
            "filename": rec.get("filename_original") or "",
            "thumb_url": "/api/image-monitor/thumb/?key=" + ik,
            "decision": "none",
        })
    return JsonResponse({"ok": True, "q": sub_q, "n_input": len(keys),
                         "n_results": len(members), "min_sim": min_sim,
                         "filtered": keep_only, "strict_and": strict_and,
                         "exclude": exclude_mode, "n_excluded": len(excluded),
                         "members": members})


@require_GET
def image_search(request):
    q = (request.GET.get("q") or "").strip()
    if not q:
        return HttpResponseBadRequest("q required")
    try:
        top = max(1, min(int(request.GET.get("top") or 200), 1000))
    except ValueError:
        top = 200
    try:
        min_sim = float(request.GET.get("min_sim") or 0.18)
    except ValueError:
        min_sim = 0.18

    root = _root()
    V, ids, _ = _load_all_embeddings(root)
    if V is None:
        return JsonResponse({"ok": False, "error": "no embeddings yet — run embed_imageboard_images first"})

    try:
        state, _ = _load_clip_text_encoder()
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"clip load failed: {e}"})

    pos, neg = _parse_query_terms(q)
    if not pos:
        return HttpResponseBadRequest("at least one positive term required")
    strict_and = (request.GET.get("strict_and") or "").lower() in ("1", "true", "yes")
    exclude_mode = (request.GET.get("exclude") or "").lower()
    excluded = _excluded_keys(root, exclude_mode)
    t0 = time.monotonic()

    if strict_and and len(pos) > 1:
        # Score each positive term independently, intersect by min_sim,
        # rank by min-across-terms (the weakest match wins, so all terms hold).
        per_sims = []
        for term in pos:
            tv = _encode_text(state, [term])
            if neg:
                nv = _encode_text(state, neg)
                tv = tv - 0.5 * nv
                nrm = np.linalg.norm(tv)
                if nrm > 0:
                    tv = tv / nrm
            per_sims.append(V @ tv)
        S = np.stack(per_sims, axis=0)  # (T, N)
        sims = S.min(axis=0)            # AND ⇒ weakest term
        # All terms must independently exceed min_sim
        mask = (S >= min_sim).all(axis=0)
    else:
        tv_np = _encode_text(state, pos)
        if neg:
            nv = _encode_text(state, neg)
            tv_np = tv_np - 0.5 * nv
            nrm = np.linalg.norm(tv_np)
            if nrm > 0:
                tv_np = tv_np / nrm
        sims = V @ tv_np
        mask = sims >= min_sim

    if top >= sims.shape[0]:
        order = np.argsort(-sims)
    else:
        part = np.argpartition(-sims, top - 1)[:top]
        order = part[np.argsort(-sims[part])]
    ranked_keys = [ids[i] for i in order
                   if mask[i] and ids[i] not in excluded]
    sims_for = {ids[i]: float(sims[i]) for i in order}

    meta = _allow_set_with_thumbs(root, set(ranked_keys))
    members = []
    for ik in ranked_keys:
        rec = meta.get(ik)
        if not rec:
            continue
        members.append({
            "image_key": ik,
            "similarity": round(sims_for[ik], 4),
            "width": rec.get("width") or 0,
            "height": rec.get("height") or 0,
            "filename": rec.get("filename_original") or "",
            "thumb_url": "/api/image-monitor/thumb/?key=" + ik,
            "decision": "none",
        })
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return JsonResponse({
        "ok": True, "q": q, "n_searched": len(ids),
        "n_results": len(members), "min_sim": min_sim,
        "strict_and": strict_and, "exclude": exclude_mode,
        "n_excluded": len(excluded),
        "elapsed_ms": elapsed_ms, "members": members,
    })


def _named_clusters_path(root: Path) -> Path:
    return root / "manifests" / "named_clusters.jsonl"


def _safe_name(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_\- ]+", "", name).strip()
    s = re.sub(r"\s+", "_", s)
    return s[:80]


@csrf_exempt
@require_POST
def save_named_cluster(request):
    """Save or append to a named cluster.

    mode=overwrite (default): replace the cluster's keys with the supplied set.
    mode=append:               union with whatever was previously stored.
    mode=remove:               subtract the supplied set from the existing cluster.
    """
    name = _safe_name(request.POST.get("name") or "")
    if not name:
        return HttpResponseBadRequest("name required")
    keys = request.POST.getlist("image_keys[]") or request.POST.getlist("image_keys")
    if not keys:
        raw = request.POST.get("image_keys") or ""
        keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        return HttpResponseBadRequest("image_keys required")
    mode = (request.POST.get("mode") or "overwrite").lower()
    if mode not in ("overwrite", "append", "remove"):
        return HttpResponseBadRequest("mode must be overwrite|append|remove")
    query = request.POST.get("query") or ""

    existing = _effective_named_clusters(_root()).get(name)
    if mode == "overwrite" or not existing:
        merged = list(dict.fromkeys(keys))  # dedupe preserve order
    elif mode == "append":
        merged = list(dict.fromkeys((existing.get("image_keys") or []) + keys))
    else:  # remove
        drop = set(keys)
        merged = [k for k in (existing.get("image_keys") or []) if k not in drop]

    rec = {
        "name": name,
        "query": query or (existing.get("query") if existing else ""),
        "image_keys": merged,
        "size": len(merged), "at_unix": int(time.time()),
        "mode": mode, "delta": len(keys),
    }
    p = _named_clusters_path(_root())
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return JsonResponse({"ok": True, "name": name, "size": len(merged),
                         "mode": mode, "delta": len(keys)})


def _effective_named_clusters(root: Path) -> dict[str, dict]:
    """Latest record per name wins."""
    out: dict[str, dict] = {}
    for rec in _iter_jsonl(_named_clusters_path(root)):
        n = rec.get("name")
        if n:
            out[n] = rec
    return out


@require_GET
def search_index_stats(request):
    """How big is the searchable index? Per-board breakdown."""
    root = _root()
    V, ids, _ = _load_all_embeddings(root)
    n_total = 0 if V is None else int(V.shape[0])
    by_board: dict[str, int] = {}
    for k in ids:
        # image_key looks like "fourchan:p:..."
        parts = k.split(":")
        b = parts[1] if len(parts) >= 2 else "?"
        by_board[b] = by_board.get(b, 0) + 1
    saved = _excluded_keys(root, "saved")
    rejected = _excluded_keys(root, "rejected")
    return JsonResponse({
        "ok": True, "n_indexed": n_total,
        "by_board": [{"board": b, "n": n}
                     for b, n in sorted(by_board.items(), key=lambda kv: -kv[1])],
        "n_saved": len(saved), "n_rejected": len(rejected),
    })


@require_GET
def named_clusters_index(request):
    nc = _effective_named_clusters(_root())
    rows = sorted(nc.values(), key=lambda r: -int(r.get("at_unix") or 0))
    return JsonResponse({
        "clusters": [{"name": r["name"], "size": r.get("size") or len(r.get("image_keys", [])),
                      "query": r.get("query", ""), "at_unix": r.get("at_unix")}
                     for r in rows]
    })


@require_GET
def named_cluster_detail(request):
    name = _safe_name(request.GET.get("name") or "")
    if not name:
        return HttpResponseBadRequest("name required")
    nc = _effective_named_clusters(_root())
    rec = nc.get(name)
    if not rec:
        return JsonResponse({"ok": False, "error": "not found"}, status=404)
    keys = rec.get("image_keys", [])
    meta = _allow_set_with_thumbs(_root(), set(keys))
    # Pull current decisions
    from ._image_curation import _effective_decisions
    decisions = _effective_decisions(_root())
    members = []
    for ik in keys:
        m = meta.get(ik)
        if not m:
            continue
        members.append({
            "image_key": ik,
            "width": m.get("width") or 0, "height": m.get("height") or 0,
            "filename": m.get("filename_original") or "",
            "thumb_url": "/api/image-monitor/thumb/?key=" + ik,
            "decision": decisions.get(ik, "none"),
        })
    return JsonResponse({"ok": True, "name": name, "query": rec.get("query", ""),
                         "size": len(members), "members": members})
