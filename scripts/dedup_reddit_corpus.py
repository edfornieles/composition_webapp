#!/usr/bin/env python3
"""Merge multiple Reddit corpus output trees and dedup by post_id.

Reads text/*.jsonl from one or more --inputs roots, dedups across them by
post_id (keeping the row with the highest score, then the longest comment
body as a tiebreaker), and writes merged shards to {out}/text/{sub}.jsonl.

Usage:
    python scripts/dedup_reddit_corpus.py \\
        --inputs /Volumes/Oom/reddit_women40s \\
                 /Volumes/Oom/reddit_women40s_arctic \\
                 /Volumes/Oom/imageboard_corpora/reddit_women40s \\
        --out    /Volumes/Oom/reddit_women40s_merged

Reports per-source contribution counts and total dedup ratio.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def row_key(row: dict) -> str | None:
    pid = row.get("post_id") or ""
    if pid:
        return pid
    tid = row.get("thread_id") or ""
    if tid and row.get("is_op"):
        return f"t3_{tid}"
    return None


def better(existing: dict, candidate: dict) -> bool:
    """Return True if candidate should replace existing."""
    e_score = int(existing.get("score") or 0)
    c_score = int(candidate.get("score") or 0)
    if c_score > e_score:
        return True
    if c_score < e_score:
        return False
    e_len = len(existing.get("comment") or "")
    c_len = len(candidate.get("comment") or "")
    if c_len > e_len:
        return True
    # Prefer a row that has image_keys filled in
    if not existing.get("image_keys") and candidate.get("image_keys"):
        return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inputs", nargs="+", required=True, help="Corpus root dirs (each containing text/).")
    ap.add_argument("--out", required=True, help="Merged output root.")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    inputs = [Path(p) for p in args.inputs]
    for p in inputs:
        if not (p / "text").is_dir():
            sys.exit(f"Missing text/ under {p}")

    out = Path(args.out)
    out_text = out / "text"
    out_text.mkdir(parents=True, exist_ok=True)

    # subreddit -> post_id -> (source_idx, row)
    merged: dict[str, dict[str, tuple[int, dict]]] = defaultdict(dict)
    src_seen = [0] * len(inputs)
    src_kept = [0] * len(inputs)
    src_replaced = [0] * len(inputs)
    skipped_no_key = 0

    for src_idx, root in enumerate(inputs):
        text_dir = root / "text"
        shard_files = sorted(p for p in text_dir.glob("*.jsonl") if not p.name.startswith("._"))
        if not args.quiet:
            print(f"[{src_idx}] {root}: {len(shard_files)} shard(s)", flush=True)
        for shard in shard_files:
            sub = shard.stem
            sub_map = merged[sub]
            with shard.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    src_seen[src_idx] += 1
                    key = row_key(row)
                    if not key:
                        skipped_no_key += 1
                        continue
                    cur = sub_map.get(key)
                    if cur is None:
                        sub_map[key] = (src_idx, row)
                        src_kept[src_idx] += 1
                    elif better(cur[1], row):
                        sub_map[key] = (src_idx, row)
                        src_replaced[src_idx] += 1

    total_in = sum(src_seen)
    total_out = sum(len(m) for m in merged.values())

    if not args.quiet:
        print(flush=True)
        print(f"Sources:", flush=True)
        for i, root in enumerate(inputs):
            print(f"  [{i}] seen={src_seen[i]:>10,}  kept={src_kept[i]:>10,}  replaced={src_replaced[i]:>8,}  {root}", flush=True)
        print(f"Skipped (no post_id): {skipped_no_key:,}", flush=True)
        print(f"Total in:  {total_in:,}", flush=True)
        print(f"Total out: {total_out:,}  (dedup ratio: {(total_in - total_out) / max(total_in, 1):.1%})", flush=True)
        print(flush=True)

    for sub in sorted(merged):
        rows = [r for _, r in sorted(merged[sub].values(), key=lambda x: int(x[1].get("timestamp") or 0))]
        out_path = out_text / f"{sub}.jsonl"
        with out_path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        if not args.quiet:
            print(f"  {sub:30s}  {len(rows):>9,} rows  →  {out_path.name}", flush=True)

    print(f"\nDone. {total_out:,} unique rows across {len(merged)} subreddits.", flush=True)


if __name__ == "__main__":
    main()
