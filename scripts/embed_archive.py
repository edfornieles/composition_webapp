#!/usr/bin/env python3
"""Embed labeled chan corpus posts for archive retrieval.

Reads labeled JSONL (output of label_post_types.py — has style_tags,
thread_type, toxicity.tier on top of the chan post schema) and writes:

    {out}/
      vectors.f32.npy        — float32 (N, D) embedding matrix
      metadata.sqlite        — per-row metadata + foreign key to vector row
      MANIFEST.json          — model name, dimensions, source files,
                               build time, post-filter count, hash

The retriever reads both files. Filtering at retrieval time uses the
metadata DB; vector search is brute-force NumPy for now (works fine up to
a few million rows on Apple Silicon; swap in faiss when needed).

Posts are excluded from the index if any of:
    - toxicity.tier in {T2_identity_attack, T3_severe} (safety)
    - char_len < 30 (no signal)
    - "pure_react" in style_tags (one-word reaction noise)
    - empty comment after cleanup

Usage:
    pip install sentence-transformers numpy

    python scripts/embed_archive.py \\
        --in-dir /Volumes/Oom/chan_corpus/raw/pol/jsonl_labeled \\
        --out    /Volumes/Oom/chan_corpus/index/pol \\
        --model sentence-transformers/all-MiniLM-L6-v2 \\
        --batch 256
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

try:
    import numpy as np
except ImportError:
    sys.exit("Missing dependency: pip install numpy")

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    sys.exit("Missing dependency: pip install sentence-transformers")

EXCLUDE_TIERS = {"T2_identity_attack", "T3_severe"}
EXCLUDE_TAGS = {"pure_react"}
MIN_LEN = 30


SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    row_id          INTEGER PRIMARY KEY,
    board           TEXT NOT NULL,
    thread_num      INTEGER,
    post_num        INTEGER,
    timestamp       INTEGER,
    year            INTEGER,
    is_op           INTEGER,
    char_len        INTEGER,
    toxicity_tier   TEXT,
    thread_type     TEXT,
    style_tags      TEXT,
    source          TEXT,
    has_image       INTEGER,
    image_key       TEXT,
    comment         TEXT
);
CREATE INDEX IF NOT EXISTS idx_board    ON posts(board);
CREATE INDEX IF NOT EXISTS idx_year     ON posts(year);
CREATE INDEX IF NOT EXISTS idx_tier     ON posts(toxicity_tier);
CREATE INDEX IF NOT EXISTS idx_ttype    ON posts(thread_type);
CREATE INDEX IF NOT EXISTS idx_thread   ON posts(thread_num);
"""


def iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def post_eligible(post: dict) -> bool:
    if (post.get("char_len") or 0) < MIN_LEN:
        return False
    tier = (post.get("toxicity") or {}).get("tier")
    if tier in EXCLUDE_TIERS:
        return False
    tags = set(post.get("style_tags") or [])
    if tags & EXCLUDE_TAGS:
        return False
    if not (post.get("comment") or "").strip():
        return False
    return True


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--in", dest="in_path", help="Single labeled JSONL.")
    g.add_argument("--in-dir", dest="in_dir", help="Directory of labeled *.jsonl.")
    ap.add_argument("--out", required=True, help="Output dir for vectors.npy + metadata.sqlite.")
    ap.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--limit", type=int, default=0, help="Cap rows (debug).")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.in_path:
        files = [Path(args.in_path)]
    else:
        files = sorted(p for p in Path(args.in_dir).glob("*.jsonl") if not p.name.startswith("._"))
    if not files:
        sys.exit("No input files found.")

    db_path = out / "metadata.sqlite"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    print(f"Loading model {args.model}...", flush=True)
    model = SentenceTransformer(args.model)
    dim = model.get_sentence_embedding_dimension()
    print(f"  dim={dim}", flush=True)

    vectors: list[np.ndarray] = []
    rows: list[tuple] = []
    n_seen = n_kept = 0
    t0 = time.time()

    def flush_batch(batch_texts, batch_meta):
        emb = model.encode(batch_texts, batch_size=args.batch, show_progress_bar=False,
                           convert_to_numpy=True, normalize_embeddings=True)
        vectors.append(emb.astype(np.float32))
        rows.extend(batch_meta)

    batch_texts: list[str] = []
    batch_meta: list[tuple] = []
    for fpath in files:
        print(f"[{fpath.name}]", flush=True)
        for post in iter_jsonl(fpath):
            n_seen += 1
            if not post_eligible(post):
                continue
            n_kept += 1
            row_id = n_kept - 1
            batch_texts.append(post.get("comment") or "")
            batch_meta.append((
                row_id,
                post.get("board") or "",
                int(post.get("thread_num") or 0),
                int(post.get("post_num") or 0),
                int(post.get("timestamp") or 0),
                int(post.get("year") or 0) if post.get("year") else None,
                1 if post.get("is_op") else 0,
                int(post.get("char_len") or 0),
                (post.get("toxicity") or {}).get("tier") or "T0_clean",
                post.get("thread_type") or "",
                json.dumps(post.get("style_tags") or [], ensure_ascii=False),
                post.get("source") or "",
                1 if post.get("has_image") else 0,
                post.get("image_key"),
                (post.get("comment") or "")[:8000],  # cap stored comment
            ))
            if len(batch_texts) >= args.batch:
                flush_batch(batch_texts, batch_meta)
                batch_texts.clear(); batch_meta.clear()
            if args.limit and n_kept >= args.limit:
                break
        if args.limit and n_kept >= args.limit:
            break
    if batch_texts:
        flush_batch(batch_texts, batch_meta)

    print(f"  embedding {n_kept:,} of {n_seen:,} rows", flush=True)

    if not vectors:
        sys.exit("No eligible rows. Aborting.")

    matrix = np.concatenate(vectors, axis=0)
    np.save(out / "vectors.f32.npy", matrix)

    conn.executemany(
        "INSERT INTO posts (row_id, board, thread_num, post_num, timestamp, year, is_op, "
        "char_len, toxicity_tier, thread_type, style_tags, source, has_image, image_key, comment) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()

    manifest = {
        "model": args.model,
        "dimensions": dim,
        "rows_seen": n_seen,
        "rows_kept": n_kept,
        "matrix_shape": list(matrix.shape),
        "matrix_dtype": str(matrix.dtype),
        "input_files": [str(f) for f in files],
        "input_hashes": {f.name: hashlib.sha1(str(f.stat().st_size).encode()).hexdigest()[:12] for f in files},
        "built_at": int(time.time()),
        "elapsed_sec": int(time.time() - t0),
        "exclude_tiers": sorted(EXCLUDE_TIERS),
        "exclude_tags": sorted(EXCLUDE_TAGS),
        "min_char_len": MIN_LEN,
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nDone. {n_kept:,} rows  → {out}", flush=True)


if __name__ == "__main__":
    main()
