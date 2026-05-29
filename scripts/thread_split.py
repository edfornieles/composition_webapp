#!/usr/bin/env python3
"""Thread-level train/valid split for chan SFT data.

The default 95/5 random line split LEAKS: replies in train can quote OPs
in valid. Per chan_anon framing memory, validation must be at THREAD level,
not row level — every post belonging to a held-out thread goes to valid;
no thread is split across train and valid.

Reads SFT JSONL where each row has metadata.thread_num (or metadata.source).
Writes train.jsonl and valid.jsonl into the same directory.

Usage:
    python scripts/thread_split.py \\
        --in   /Volumes/Oom/imageboard_corpora/chan_sft/chan_sft.jsonl \\
        --out-dir /Volumes/Oom/imageboard_corpora/chan_sft/ \\
        --valid-fraction 0.05 \\
        --seed 42
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path


def thread_key(row: dict) -> str:
    md = row.get("metadata") or {}
    src = md.get("source") or ""
    board = md.get("board") or ""
    tnum = md.get("thread_num") or md.get("thread_id") or ""
    return f"{src}:{board}:{tnum}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--valid-fraction", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    in_path = Path(args.in_path)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Pass 1: collect thread keys
    thread_rows: dict[str, list[int]] = defaultdict(list)
    rows: list[dict] = []
    with in_path.open("r", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            tk = thread_key(row)
            thread_rows[tk].append(len(rows))
            rows.append(row)

    keys = list(thread_rows.keys())
    rng = random.Random(args.seed)
    rng.shuffle(keys)

    n_total_rows = len(rows)
    target_valid = int(n_total_rows * args.valid_fraction)
    valid_keys: set[str] = set()
    rolling = 0
    for k in keys:
        if rolling >= target_valid:
            break
        valid_keys.add(k)
        rolling += len(thread_rows[k])

    train_path = out_dir / "train.jsonl"
    valid_path = out_dir / "valid.jsonl"
    n_train = n_valid = 0
    with train_path.open("w", encoding="utf-8") as ft, valid_path.open("w", encoding="utf-8") as fv:
        for row in rows:
            tk = thread_key(row)
            line = json.dumps(row, ensure_ascii=False) + "\n"
            if tk in valid_keys:
                fv.write(line); n_valid += 1
            else:
                ft.write(line); n_train += 1

    manifest = {
        "input": str(in_path),
        "input_sha1_size": hashlib.sha1(str(in_path.stat().st_size).encode()).hexdigest()[:12],
        "rows_total": n_total_rows,
        "rows_train": n_train,
        "rows_valid": n_valid,
        "valid_fraction_actual": round(n_valid / max(n_total_rows, 1), 4),
        "threads_total": len(thread_rows),
        "threads_valid": len(valid_keys),
        "seed": args.seed,
    }
    (out_dir / "split_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
