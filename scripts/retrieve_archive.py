#!/usr/bin/env python3
"""Query the chan archive retrieval index for runtime context packets.

Used at runtime to fetch a small set of cleaned, board/era/tier/style-filtered
archive examples that the model can use as in-context grounding. Per the
chan_anon framing memory, retrieval is more important than more SFT — it's
how board specificity, era specificity, and topic specificity stay outside
the weights.

Library + CLI. Composes with character_memory.py and the runtime context
builder.

Usage (CLI):
    python scripts/retrieve_archive.py \\
        --index /Volumes/Oom/chan_corpus/index/pol \\
        --query "ignored her message after a bad gym session" \\
        --board fit --year 2018 --max-tier T1_hostile \\
        --style-tags confession --k 5

Usage (library):
    from retrieve_archive import ArchiveIndex
    idx = ArchiveIndex(Path("/Volumes/Oom/chan_corpus/index/pol"))
    hits = idx.search(
        query="ignored her message",
        board="fit", year=2018, max_tier="T1_hostile",
        style_tags=["confession"], k=5,
    )
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import numpy as np
except ImportError:
    sys.exit("Missing dependency: pip install numpy")

TIER_ORDER = ["T0_clean", "T1_hostile", "T2_identity_attack", "T3_severe"]


@dataclass
class Hit:
    score: float
    row_id: int
    board: str
    thread_num: int
    post_num: int
    year: int | None
    toxicity_tier: str
    thread_type: str
    style_tags: list[str]
    is_op: bool
    has_image: bool
    image_key: str | None
    comment: str


class ArchiveIndex:
    def __init__(self, index_dir: Path):
        self.index_dir = Path(index_dir)
        manifest = json.loads((self.index_dir / "MANIFEST.json").read_text())
        self.dim = manifest["dimensions"]
        self.model_name = manifest["model"]
        self._matrix: np.ndarray | None = None
        self._db_path = self.index_dir / "metadata.sqlite"
        self._encoder = None

    def _load(self):
        if self._matrix is None:
            self._matrix = np.load(self.index_dir / "vectors.f32.npy")
        if self._encoder is None:
            from sentence_transformers import SentenceTransformer
            self._encoder = SentenceTransformer(self.model_name)
        return self._matrix, self._encoder

    def search(
        self,
        query: str,
        *,
        k: int = 5,
        board: str | None = None,
        year: int | None = None,
        year_range: tuple[int, int] | None = None,
        max_tier: str = "T1_hostile",
        style_tags: list[str] | None = None,
        thread_type: str | None = None,
        min_char_len: int = 30,
        is_op: bool | None = None,
        candidates: int = 2000,
    ) -> list[Hit]:
        """Return top-k cosine-similarity hits subject to metadata filters.

        Strategy: filter candidates in SQLite first, then rank by cosine
        similarity in NumPy on the candidate row_ids. This avoids brute-forcing
        the full matrix when we're board/era-restricted.
        """
        matrix, encoder = self._load()
        max_tier_idx = TIER_ORDER.index(max_tier)
        allowed_tiers = TIER_ORDER[: max_tier_idx + 1]

        # Build SQL filter
        clauses, params = [], []
        clauses.append(f"toxicity_tier IN ({','.join('?'*len(allowed_tiers))})")
        params.extend(allowed_tiers)
        if board:
            clauses.append("board = ?"); params.append(board)
        if year is not None:
            clauses.append("year = ?"); params.append(year)
        elif year_range is not None:
            clauses.append("year BETWEEN ? AND ?"); params.extend(year_range)
        if thread_type:
            clauses.append("thread_type = ?"); params.append(thread_type)
        if min_char_len:
            clauses.append("char_len >= ?"); params.append(min_char_len)
        if is_op is True:
            clauses.append("is_op = 1")
        elif is_op is False:
            clauses.append("is_op = 0")

        sql = f"SELECT row_id, board, thread_num, post_num, year, toxicity_tier, thread_type, " \
              f"style_tags, is_op, has_image, image_key, comment FROM posts WHERE {' AND '.join(clauses)} " \
              f"LIMIT ?"
        params.append(int(candidates))

        conn = sqlite3.connect(self._db_path)
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        conn.close()

        if not rows:
            return []

        # style_tags filter (post-SQL since tags is JSON)
        if style_tags:
            style_set = set(style_tags)
            rows = [r for r in rows if style_set & set(json.loads(r[7] or "[]"))]
            if not rows:
                return []

        row_ids = np.array([r[0] for r in rows], dtype=np.int64)
        candidate_vectors = matrix[row_ids]
        q_vec = encoder.encode([query], convert_to_numpy=True, normalize_embeddings=True).astype(np.float32)[0]

        sims = candidate_vectors @ q_vec  # cosine since normalized
        top_idx = np.argsort(-sims)[:k]

        hits: list[Hit] = []
        for ti in top_idx:
            r = rows[int(ti)]
            hits.append(Hit(
                score=float(sims[ti]),
                row_id=r[0], board=r[1], thread_num=r[2], post_num=r[3], year=r[4],
                toxicity_tier=r[5], thread_type=r[6], style_tags=json.loads(r[7] or "[]"),
                is_op=bool(r[8]), has_image=bool(r[9]), image_key=r[10], comment=r[11],
            ))
        return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True, help="Path to index dir.")
    ap.add_argument("--query", required=True)
    ap.add_argument("--board")
    ap.add_argument("--year", type=int)
    ap.add_argument("--max-tier", default="T1_hostile",
                    choices=["T0_clean", "T1_hostile", "T2_identity_attack", "T3_severe"])
    ap.add_argument("--thread-type")
    ap.add_argument("--style-tags", help="Comma-separated style tags any-of filter.")
    ap.add_argument("--is-op", choices=["true", "false"])
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    idx = ArchiveIndex(Path(args.index))
    is_op = None if args.is_op is None else (args.is_op == "true")
    hits = idx.search(
        query=args.query,
        k=args.k, board=args.board, year=args.year,
        max_tier=args.max_tier, thread_type=args.thread_type,
        style_tags=args.style_tags.split(",") if args.style_tags else None,
        is_op=is_op,
    )
    for h in hits:
        snippet = h.comment.replace("\n", " ")[:200]
        print(f"[{h.score:.3f}] /{h.board}/ {h.year} {h.toxicity_tier} {h.style_tags}")
        print(f"  {snippet}")
        print()


if __name__ == "__main__":
    main()
