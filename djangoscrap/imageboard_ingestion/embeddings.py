"""Lightweight TF-IDF retrieval over the model-safe corpus.

Pure-stdlib implementation so the pipeline works without numpy/scipy/sklearn.
Index is pickled to corpora/.../embeddings/fit_tfidf.pkl.

Optional backends (sentence-transformers, OpenAI) are deliberately not wired
here — TF-IDF is enough to demonstrate grounding, and avoids surprise costs.
"""
from __future__ import annotations

import math
import pickle
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from . import storage

_TOKEN_RE = re.compile(r"[a-z0-9']+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in", "on",
    "at", "for", "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "i", "you",
    "he", "she", "it", "we", "they", "me", "my", "your", "his", "her", "their",
    "this", "that", "these", "those", "so", "not", "no", "yes", "just", "like",
    "what", "why", "how", "when", "where", "who", "whom", "which", "ll", "ve",
    "re", "s", "t", "d", "m", "o",
}


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    toks = _TOKEN_RE.findall(text.lower())
    return [t for t in toks if t not in _STOPWORDS and len(t) > 1]


class TfIdfIndex:
    """Minimal TF-IDF index. Documents are post records (uid + safe text)."""

    def __init__(self) -> None:
        self.docs: list[dict] = []        # [{uid, text, tokens, tf}]
        self.df: Counter = Counter()      # token -> doc count
        self.idf: dict[str, float] = {}
        self.norms: list[float] = []      # per-doc L2 norm of tf-idf vector

    @property
    def n_docs(self) -> int:
        return len(self.docs)

    def add(self, uid: str, text: str, *, extra: dict | None = None) -> None:
        toks = tokenize(text)
        if not toks:
            return
        tf: Counter = Counter(toks)
        self.docs.append({"uid": uid, "text": text, "tf": tf, "extra": extra or {}})
        for term in tf:
            self.df[term] += 1

    def finalize(self) -> None:
        n = max(1, len(self.docs))
        self.idf = {term: math.log((n + 1) / (df + 1)) + 1.0 for term, df in self.df.items()}
        self.norms = []
        for doc in self.docs:
            sq = 0.0
            for term, c in doc["tf"].items():
                w = (1.0 + math.log(c)) * self.idf.get(term, 0.0)
                sq += w * w
            self.norms.append(math.sqrt(sq) or 1.0)

    def _query_vec(self, query: str) -> dict[str, float]:
        toks = tokenize(query)
        tf = Counter(toks)
        return {t: (1.0 + math.log(c)) * self.idf.get(t, 0.0) for t, c in tf.items()}

    def search(self, query: str, *, top_k: int = 8) -> list[tuple[float, dict]]:
        if not self.docs:
            return []
        qvec = self._query_vec(query)
        if not qvec:
            return []
        qnorm = math.sqrt(sum(v * v for v in qvec.values())) or 1.0
        scored: list[tuple[float, dict]] = []
        for i, doc in enumerate(self.docs):
            dot = 0.0
            for term, qw in qvec.items():
                c = doc["tf"].get(term)
                if not c:
                    continue
                dw = (1.0 + math.log(c)) * self.idf.get(term, 0.0)
                dot += qw * dw
            if dot <= 0:
                continue
            sim = dot / (self.norms[i] * qnorm)
            scored.append((sim, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]


def build_index(source_key: str = "4chan__fit__body_discipline") -> TfIdfIndex:
    idx = TfIdfIndex()
    for rec in storage.iter_manifest(source_key):
        text = rec.get("model_safe_text") or ""
        if not text or len(text) < 8:
            continue
        idx.add(
            rec["post_uid"],
            text,
            extra={
                "fit_tags": rec.get("fit_tags") or [],
                "thread_no": rec.get("thread_no"),
                "post_no": rec.get("post_no"),
            },
        )
    idx.finalize()
    return idx


def save_index(idx: TfIdfIndex, source_key: str = "4chan__fit__body_discipline") -> Path:
    storage.ensure_layout(source_key)
    out = storage.embeddings_path(source_key)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        pickle.dump(idx, f)
    return out


def load_index(source_key: str = "4chan__fit__body_discipline") -> TfIdfIndex | None:
    path = storage.embeddings_path(source_key)
    if not path.exists():
        return None
    with path.open("rb") as f:
        return pickle.load(f)
