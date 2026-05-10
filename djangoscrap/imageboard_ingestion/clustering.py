"""UMAP + HDBSCAN/KMeans clustering of OpenCLIP image embeddings.

Auto-suggests cluster labels by mining captions and fit_tags inside each
cluster. Pure-stdlib + scikit-learn fallback if umap-learn / hdbscan aren't
installed.
"""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path
from typing import Optional

from . import config


_STOP = set("""a an the and or of to in on at for with from by is was are were be
been being it its this that these those i you he she they we your my our his her their
""".split())


def _load_embeddings(root: Path, model_tag: str, source: str, board: str):
    import numpy as np  # type: ignore
    npy = config.embeddings_npy(root, model_tag, source, board)
    ids = config.embeddings_ids(root, model_tag, source, board)
    if not npy.exists() or not ids.exists():
        raise FileNotFoundError(
            f"embeddings missing: {npy} / {ids}. Run embed_imageboard_images first."
        )
    arr = np.load(npy)
    image_keys = json.loads(ids.read_text(encoding="utf-8"))
    return arr, image_keys


def _load_captions(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    p = config.captions_manifest(root)
    if not p.exists():
        return out
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                out[rec["image_key"]] = rec.get("caption") or ""
            except Exception:
                pass
    return out


def _load_post_text_by_image_key(root: Path) -> dict[str, str]:
    """Map image_key → joined post text, via post_uid linkage in images.jsonl."""
    post_text: dict[str, str] = {}
    pp = config.posts_manifest(root)
    if pp.exists():
        with pp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    puid = rec.get("post_uid") or ""
                    txt = rec.get("plain_text") or rec.get("subject") or ""
                    if puid and txt:
                        post_text[puid] = txt
                except Exception:
                    pass
    by_image: dict[str, str] = {}
    ip = config.images_manifest(root)
    if ip.exists():
        with ip.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ik = rec.get("image_key") or ""
                    puid = rec.get("post_uid") or ""
                    if ik and puid in post_text:
                        by_image[ik] = post_text[puid]
                except Exception:
                    pass
    return by_image


def _load_image_meta(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    p = config.images_manifest(root)
    if not p.exists():
        return out
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                out[rec.get("image_key", "")] = rec
            except Exception:
                pass
    return out


def cluster(
    *,
    root: Path,
    source: str,
    board: str,
    model_tag: str,
    method: str = "hdbscan",
    n_clusters: int = 50,
    use_umap: bool = True,
    umap_n_components: int = 2,
    umap_n_neighbors: int = 15,
    umap_min_dist: float = 0.1,
    min_cluster_size: int = 25,
    log=print,
) -> dict:
    import numpy as np  # type: ignore

    arr, ids = _load_embeddings(root, model_tag, source, board)
    log(f"[cluster] loaded {arr.shape} embeddings, {len(ids)} ids")

    # 2D projection — always compute even if not used as cluster space, for ImagePlot
    proj_xy = None
    try:
        import umap  # type: ignore
        log("[cluster] running UMAP 2D projection…")
        reducer = umap.UMAP(n_components=2, n_neighbors=umap_n_neighbors,
                            min_dist=umap_min_dist, random_state=42)
        proj_xy = reducer.fit_transform(arr)
    except ImportError:
        log("[cluster] umap-learn not installed — falling back to PCA-2 for projection.")
        from sklearn.decomposition import PCA  # type: ignore
        proj_xy = PCA(n_components=2, random_state=42).fit_transform(arr)

    cluster_space = arr
    if use_umap:
        try:
            import umap  # type: ignore
            log(f"[cluster] running UMAP {umap_n_components}D for cluster space…")
            cluster_space = umap.UMAP(n_components=umap_n_components,
                                      n_neighbors=umap_n_neighbors,
                                      min_dist=umap_min_dist,
                                      random_state=42).fit_transform(arr)
        except ImportError:
            log("[cluster] umap missing — clustering on raw CLIP embeddings.")

    if method == "hdbscan":
        try:
            import hdbscan  # type: ignore
            clusterer = hdbscan.HDBSCAN(min_cluster_size=int(min_cluster_size),
                                        metric="euclidean")
            labels = clusterer.fit_predict(cluster_space)
            log(f"[cluster] HDBSCAN: {len(set(labels)) - (1 if -1 in labels else 0)} clusters, "
                f"{(labels == -1).sum()} noise")
        except ImportError:
            log("[cluster] hdbscan not installed — falling back to KMeans.")
            method = "kmeans"

    if method == "kmeans":
        from sklearn.cluster import KMeans  # type: ignore
        log(f"[cluster] KMeans k={n_clusters}")
        labels = KMeans(n_clusters=int(n_clusters), n_init=10, random_state=42).fit_predict(cluster_space)

    captions = _load_captions(root)
    meta = _load_image_meta(root)
    post_text_by_key = _load_post_text_by_image_key(root)

    # Pre-pass: corpus-wide word frequency for TF-IDF-style scoring.
    # We weight cluster-specific words against the corpus average so
    # generic high-frequency words ("have", "like") drop out of labels.
    corpus_word_counts: collections.Counter = collections.Counter()
    corpus_doc_counts: collections.Counter = collections.Counter()
    for ik in ids:
        toks = set(re.findall(r"[a-z]{4,}", (post_text_by_key.get(ik) or "").lower()))
        toks |= set(re.findall(r"[a-z]{3,}", (captions.get(ik) or "").lower()))
        toks = {t for t in toks if t not in _STOP and len(t) <= 16}
        for t in toks:
            corpus_doc_counts[t] += 1
            corpus_word_counts[t] += 1
    n_docs_total = max(1, len(ids))

    cluster_rows: list[dict] = []
    membership_rows: list[dict] = []
    by_cluster: dict[int, list[int]] = {}
    for i, c in enumerate(labels):
        by_cluster.setdefault(int(c), []).append(i)

    for c, idxs in sorted(by_cluster.items()):
        keys = [ids[i] for i in idxs]
        cap_words: collections.Counter = collections.Counter()
        tag_counter: collections.Counter = collections.Counter()
        boards: collections.Counter = collections.Counter()
        text_word_counter: collections.Counter = collections.Counter()
        for k in keys:
            for w in re.findall(r"[a-z]{3,}", (captions.get(k) or "").lower()):
                if w not in _STOP:
                    cap_words[w] += 1
            # Mine post text as a fallback / supplement to captions
            for w in re.findall(r"[a-z]{4,}", (post_text_by_key.get(k) or "").lower()):
                if w not in _STOP and len(w) <= 16:
                    text_word_counter[w] += 1
            m = meta.get(k) or {}
            for t in (m.get("fit_tags") or []):
                tag_counter[t] += 1
            boards[m.get("board") or ""] += 1
        top_words = [w for w, _ in cap_words.most_common(8)]
        top_tags = [t for t, _ in tag_counter.most_common(3)]
        # TF-IDF-ish scoring: words that are over-represented in this cluster
        # vs. the corpus baseline. min_count=2 inside the cluster avoids
        # label noise from one-off tokens.
        cluster_size = max(1, len(idxs))
        scored: list[tuple[float, str]] = []
        for w, n in text_word_counter.items():
            if n < 2:
                continue
            in_cluster_freq = n / cluster_size
            global_freq = corpus_word_counts.get(w, 0) / n_docs_total
            if global_freq <= 0:
                continue
            # Higher = more distinctive to this cluster
            ratio = in_cluster_freq / global_freq
            if ratio > 1.0:
                scored.append((ratio * (n ** 0.5), w))
        scored.sort(reverse=True)
        top_text_words = [w for _, w in scored[:6]]
        # Combine all label sources and pick distinguishing tokens
        label_pool = top_tags[:1] + top_text_words[:2] + top_words[:1]
        seen = set(); uniq = []
        for w in label_pool:
            if w and w not in seen:
                seen.add(w); uniq.append(w)
        suggested = "_".join(uniq[:3]) if uniq else f"cluster_{c}"
        if c == -1:
            suggested = "noise"

        # Pick a representative — first 5 by index distance to centroid
        if proj_xy is not None and len(idxs) > 0:
            import numpy as _np
            centroid = proj_xy[idxs].mean(axis=0)
            dists = ((proj_xy[idxs] - centroid) ** 2).sum(axis=1)
            order = list(_np.argsort(dists))
            rep = [ids[idxs[o]] for o in order[:5]]
        else:
            rep = keys[:5]

        cluster_rows.append({
            "cluster_id": int(c),
            "method": method,
            "size": len(keys),
            "suggested_label_auto": suggested,
            "top_caption_words": top_words,
            "top_text_words": top_text_words,
            "top_fit_tags": top_tags,
            "top_boards": list(boards.keys())[:3],
            "representative_image_keys": rep,
        })
        for i in idxs:
            membership_rows.append({
                "image_key": ids[i],
                "cluster_id": int(c),
                "umap_x": float(proj_xy[i][0]) if proj_xy is not None else 0.0,
                "umap_y": float(proj_xy[i][1]) if proj_xy is not None else 0.0,
            })

    # Write parquet (and JSONL fallbacks)
    def _write(rows, pq_path):
        pq_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import pyarrow as pa  # type: ignore
            import pyarrow.parquet as pq  # type: ignore
            cols = {k: [r.get(k) for r in rows]
                    for k in sorted({k for r in rows for k in r.keys()})}
            pq.write_table(pa.Table.from_pydict(cols), pq_path)
        except Exception:
            pass
        with pq_path.with_suffix(".jsonl").open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    _write(cluster_rows, config.clusters_path(root))
    _write(membership_rows, config.cluster_membership_path(root))
    proj_rows = [
        {"image_key": ids[i], "umap_x": float(proj_xy[i][0]), "umap_y": float(proj_xy[i][1])}
        for i in range(len(ids))
    ]
    _write(proj_rows, config.umap_projection_path(root))

    log(f"[cluster] wrote {len(cluster_rows)} clusters, {len(membership_rows)} memberships")
    return {"clusters": len(cluster_rows), "members": len(membership_rows), "method": method}
