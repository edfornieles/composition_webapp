"""Retrieval index over the /fit/ corpus.

Wraps the TF-IDF index from `embeddings.py` with chunk records that the
grounding/dataset layers expect. Each retrieved chunk carries:
  - safe_excerpt        (always model_safe_text; safe to show in operator UI)
  - contaminated_excerpt (only if the post is allowed_for_retrieval)
  - tags
  - contamination
  - use_policy

Posts that are NOT allowed_for_retrieval (doxxing, raw slurs, sourcing) are
omitted entirely from the index. Hard-blocked posts cannot leak through.
"""
from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

from . import embeddings, storage


def _chunk_id(rec: dict) -> str:
    return f"fit_{rec.get('thread_no', 0)}_{rec.get('post_no', 0)}"


def _to_chunk(rec: dict) -> dict:
    return {
        "chunk_id": _chunk_id(rec),
        "post_id": rec.get("post_uid"),
        "thread_id": rec.get("thread_no"),
        "post_no": rec.get("post_no"),
        "safe_excerpt": (rec.get("model_safe_text") or "")[:600],
        "contaminated_excerpt": (rec.get("contaminated_text") or "")[:600],
        "greentext_lines": (rec.get("greentext_lines") or [])[:8],
        "tags": rec.get("fit_tags") or [],
        "contamination": rec.get("contamination") or {},
        "use_policy": rec.get("use_policy") or {},
    }


class FitRetrievalIndex:
    """A bundle of (TF-IDF index, chunk records) saved together."""

    def __init__(self, idx: embeddings.TfIdfIndex, chunks: list[dict]):
        self.idx = idx
        # uid -> chunk
        self.by_uid: dict[str, dict] = {c["post_id"]: c for c in chunks}

    @property
    def n_chunks(self) -> int:
        return len(self.by_uid)

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        mode: str = "contaminated_art",
        min_contamination: dict[str, float] | None = None,
    ) -> list[dict]:
        if not query:
            return []
        results: list[dict] = []
        seen: set[str] = set()
        for sim, doc in self.idx.search(query, top_k=top_k * 4):
            uid = doc.get("uid")
            if not uid or uid in seen:
                continue
            chunk = self.by_uid.get(uid)
            if chunk is None:
                continue
            if mode == "contaminated_art" and not chunk["use_policy"].get("allowed_for_retrieval", False):
                continue
            if min_contamination:
                cs = chunk["contamination"] or {}
                if any(cs.get(k, 0.0) < float(v) for k, v in min_contamination.items()):
                    continue
            seen.add(uid)
            results.append({"sim": round(float(sim), 4), **chunk})
            if len(results) >= top_k:
                break
        return results

    def search_components(
        self,
        components: dict[str, str],
        *,
        per_component_k: dict[str, int] | None = None,
        default_k: int = 4,
        final_k: int = 8,
        mode: str = "contaminated_art",
        rrf_k0: int = 60,
        recent_uids: set[str] | tuple[str, ...] | None = None,
        recent_penalty: float = 0.4,
    ) -> list[dict]:
        """Component-based retrieval with reciprocal-rank fusion.

        Each component is run as its own query and the top-N results merged
        across components by RRF. Each returned fragment carries:
          - source_components : list of component names that surfaced it
          - rrf_score         : the fused score
        plus the same chunk fields as `search`.

        `recent_uids` are downranked (multiplied by `recent_penalty`) but not
        excluded — sometimes the same fragment is genuinely relevant again, but
        we prefer fresh ones first. Set `recent_penalty=0.0` to hard-mask.
        """
        per_component_k = per_component_k or {}
        recent = set(recent_uids or ())

        # uid -> {chunk, score, sources}
        fused: dict[str, dict] = {}
        for component, query in components.items():
            if not query:
                continue
            k = int(per_component_k.get(component, default_k))
            hits = self.search(query, top_k=k, mode=mode)
            for rank, hit in enumerate(hits):
                uid = hit.get("post_id")
                if not uid:
                    continue
                # Reciprocal rank fusion: 1 / (k0 + rank). k0 dampens top-1
                # dominance so multiple components can contribute.
                contribution = 1.0 / (rrf_k0 + rank + 1)
                if uid in recent and recent_penalty != 1.0:
                    contribution *= max(0.0, recent_penalty)
                if uid in fused:
                    fused[uid]["rrf_score"] += contribution
                    if component not in fused[uid]["source_components"]:
                        fused[uid]["source_components"].append(component)
                else:
                    fused[uid] = {
                        "chunk": hit,
                        "rrf_score": contribution,
                        "source_components": [component],
                    }

        # Sort by score, drop the zero-penalty entries that hard-masked out.
        ranked = sorted(
            (f for f in fused.values() if f["rrf_score"] > 0.0),
            key=lambda f: f["rrf_score"],
            reverse=True,
        )

        out: list[dict] = []
        for f in ranked[:final_k]:
            chunk = dict(f["chunk"])
            chunk["rrf_score"] = round(f["rrf_score"], 6)
            chunk["source_components"] = list(f["source_components"])
            out.append(chunk)
        return out


def build_retrieval(source_key: str = "fourchan_fit", *, mode: str = "contaminated_art") -> FitRetrievalIndex:
    idx = embeddings.TfIdfIndex()
    chunks: list[dict] = []
    for rec in storage.iter_manifest(source_key):
        policy = rec.get("use_policy") or {}
        if not policy.get("allowed_for_retrieval", False):
            continue
        text_for_index = rec.get("contaminated_text") if mode == "contaminated_art" else rec.get("model_safe_text")
        if not text_for_index or len(text_for_index) < 8:
            continue
        idx.add(rec["post_uid"], text_for_index, extra={"thread_no": rec.get("thread_no")})
        chunks.append(_to_chunk(rec))
    idx.finalize()
    return FitRetrievalIndex(idx, chunks)


def save(retrieval: FitRetrievalIndex, source_key: str = "fourchan_fit") -> Path:
    storage.ensure_layout(source_key)
    out = storage.retrieval_path(source_key)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        pickle.dump(retrieval, f)
    return out


def load(source_key: str = "fourchan_fit") -> FitRetrievalIndex | None:
    path = storage.retrieval_path(source_key)
    if not path.exists():
        return None
    with path.open("rb") as f:
        obj: Any = pickle.load(f)
    return obj
