#!/usr/bin/env python3
"""Re-score and re-gate an existing bucket against new seed images.

Reads images from {bucket}/.seeds/, builds a CLIP centroid from them,
scores every file in the bucket against the centroid, updates manifest's
aesthetic_score field, and optionally moves low-scoring files to
{bucket}/.rejected/.

This is the active-learning step. After a text-anchored first pull,
drop 3–10 reference images into .seeds/, run this to see which existing
files match the aesthetic and which drift.

Usage:
    # Drop reference images into the .seeds/ folder, then:
    python scripts/bucket_regate.py --bucket /Volumes/Oom/aggregation/got/dark_wooded_forest

    # With a custom threshold (default uses spec.aesthetic_threshold):
    python scripts/bucket_regate.py --bucket ... --threshold 0.27 --move-rejected

The CLIP model used is clip-ViT-B-32 (image+text, normalized embeddings).
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bucket_spec import read_spec, write_spec, GOT_ROOT_DEFAULT

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic", ".tiff", ".bmp"}


def load_clip():
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        sys.exit("pip install sentence-transformers")
    return SentenceTransformer("clip-ViT-B-32")


def build_centroid(model, seed_dir: Path):
    from PIL import Image
    import numpy as np
    seeds = sorted(p for p in seed_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith("._"))
    if not seeds:
        return None, []
    images = []
    for p in seeds:
        try:
            images.append(Image.open(p).convert("RGB"))
        except Exception:
            continue
    if not images:
        return None, []
    emb = model.encode(images, convert_to_numpy=True, normalize_embeddings=True)
    centroid = emb.mean(axis=0)
    n = (centroid ** 2).sum() ** 0.5
    if n > 0:
        centroid = centroid / n
    return centroid, [p.name for p in seeds]


def score_image(model, centroid, path: Path) -> float:
    from PIL import Image
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return 0.0
    emb = model.encode([im], convert_to_numpy=True, normalize_embeddings=True)[0]
    return float(emb @ centroid)


def load_manifest(bucket_dir: Path) -> list[dict]:
    rows = []
    p = bucket_dir / ".manifest.jsonl"
    if not p.exists():
        return rows
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def write_manifest(bucket_dir: Path, rows: list[dict]) -> None:
    p = bucket_dir / ".manifest.jsonl"
    if not rows:
        # Refuse to truncate. If we have no rows, something is wrong upstream
        # (no files matched manifest entries) — keep the existing manifest intact.
        print(f"  [safe] refusing to overwrite manifest with empty rows; existing manifest preserved.", flush=True)
        return
    # Backup before overwrite (atomic-ish): write to .tmp, rename.
    if p.exists():
        backup = bucket_dir / f".manifest.jsonl.bak.{int(time.time())}"
        shutil.copy2(p, backup)
    tmp = bucket_dir / ".manifest.jsonl.tmp"
    with tmp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", required=True, help="Path to bucket directory.")
    ap.add_argument("--threshold", type=float, default=None,
                    help="Cosine threshold. Defaults to spec.aesthetic_threshold or 0.25.")
    ap.add_argument("--move-rejected", action="store_true",
                    help="Move files scoring below threshold to .rejected/ inside the bucket.")
    ap.add_argument("--save-threshold", action="store_true",
                    help="Save chosen threshold back to spec.aesthetic_threshold.")
    args = ap.parse_args()

    bdir = Path(args.bucket)
    seeds_dir = bdir / ".seeds"
    if not seeds_dir.is_dir():
        sys.exit(f"No .seeds/ in {bdir}. Drop 3-10 reference images there first.")

    spec = None
    spec_path = bdir / ".spec.json"
    if spec_path.exists():
        try:
            spec = read_spec(bdir)
        except Exception:
            pass

    threshold = args.threshold
    if threshold is None and spec is not None:
        threshold = spec.aesthetic_threshold or 0.25
    if threshold is None:
        threshold = 0.25

    print(f"Bucket:    {bdir}", flush=True)
    print(f"Threshold: {threshold}", flush=True)

    print("Loading CLIP model (first run takes ~20s)...", flush=True)
    model = load_clip()

    centroid, seed_names = build_centroid(model, seeds_dir)
    if centroid is None:
        sys.exit(f"No valid images in {seeds_dir}.")
    print(f"Centroid built from {len(seed_names)} seed image(s): {seed_names}", flush=True)

    rows = load_manifest(bdir)
    if not rows:
        sys.exit(f"No manifest in {bdir}.")

    rejected_dir = bdir / ".rejected"
    moved = 0
    kept = 0
    n_scored = 0
    score_buckets = {"<.10": 0, ".10-.15": 0, ".15-.20": 0, ".20-.25": 0, ".25-.30": 0, ".30+": 0}

    new_rows = []
    for row in rows:
        fname = row.get("filename") or ""
        path = bdir / fname
        if not path.exists():
            continue
        score = score_image(model, centroid, path)
        row["aesthetic_score"] = round(float(score), 4)
        row["regated_at"] = int(time.time())
        n_scored += 1

        if score < 0.10: score_buckets["<.10"] += 1
        elif score < 0.15: score_buckets[".10-.15"] += 1
        elif score < 0.20: score_buckets[".15-.20"] += 1
        elif score < 0.25: score_buckets[".20-.25"] += 1
        elif score < 0.30: score_buckets[".25-.30"] += 1
        else: score_buckets[".30+"] += 1

        if score < threshold and args.move_rejected:
            rejected_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(rejected_dir / fname))
            row["rejected"] = True
            moved += 1
        else:
            kept += 1
        new_rows.append(row)

    write_manifest(bdir, new_rows)

    if args.save_threshold and spec is not None:
        spec.aesthetic_threshold = threshold
        write_spec(spec, root=bdir.parent)

    print(f"\nScored: {n_scored}", flush=True)
    print("Distribution:", flush=True)
    for k, v in score_buckets.items():
        bar = "█" * min(v, 60)
        print(f"  {k:10s} {v:5d}  {bar}", flush=True)
    print(f"\nThreshold {threshold}: kept={kept}  moved-to-rejected={moved}", flush=True)
    if args.move_rejected:
        print(f"  rejected files in: {rejected_dir}", flush=True)
    else:
        print(f"  (re-run with --move-rejected to actually move below-threshold files)", flush=True)


if __name__ == "__main__":
    main()
