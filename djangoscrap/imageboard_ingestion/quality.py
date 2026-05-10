"""Image-quality + duplicate analysis for the local chan corpus.

Pure-Pillow features for the baseline path; fastdup is opportunistically
loaded for near-duplicate / outlier analysis when present.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterable, Iterator, Optional

from . import config


def _iter_images_manifest(root: Path) -> Iterator[dict]:
    p = config.images_manifest(root)
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


def _orientation(w: int, h: int) -> str:
    if not w or not h:
        return "unknown"
    r = w / h
    if r < 0.85:
        return "portrait"
    if r > 1.6:
        return "wide"
    if r > 1.15:
        return "landscape"
    return "square"


def compute_features(path: Path) -> dict:
    """Pillow-driven baseline features. Returns {} on failure."""
    try:
        from PIL import Image, ImageStat  # type: ignore
        import imagehash  # type: ignore
    except ImportError as e:
        return {"feature_error": f"missing_dep:{e.name}"}

    out: dict = {}
    try:
        with Image.open(path) as im:
            im.load()
            w, h = im.size
            out["width"] = w
            out["height"] = h
            out["aspect_ratio"] = round(w / h, 4) if h else 0.0
            out["orientation"] = _orientation(w, h)
            rgb = im.convert("RGB")
            stat = ImageStat.Stat(rgb)
            r, g, b = stat.mean
            out["brightness"] = round(0.299 * r + 0.587 * g + 0.114 * b, 2)
            mr, mg, mb = stat.stddev
            out["contrast"] = round((mr + mg + mb) / 3.0, 2)
            # Saturation via HSV mean S
            hsv = im.convert("HSV")
            _, s, _ = ImageStat.Stat(hsv).mean
            out["saturation"] = round(s, 2)
            # Entropy
            try:
                out["entropy"] = round(rgb.entropy(), 4)
            except Exception:
                out["entropy"] = 0.0
            # Blur score: variance of Laplacian-ish via gradient stddev
            gray = im.convert("L")
            gstat = ImageStat.Stat(gray)
            out["blur_score"] = round(gstat.stddev[0], 2)  # higher = sharper
            # Perceptual hashes
            out["phash"] = str(imagehash.phash(rgb))
            out["dhash"] = str(imagehash.dhash(rgb))
    except Exception as e:
        out["feature_error"] = f"{type(e).__name__}:{e}"
    return out


def iter_allowed_images(root: Path, source: Optional[str] = None, board: Optional[str] = None) -> Iterator[dict]:
    """Yield image manifest rows whose safety_decision allows preview."""
    for rec in _iter_images_manifest(root):
        if source and rec.get("source") != source:
            continue
        if board and rec.get("board") != board:
            continue
        sd = rec.get("safety_decision") or {}
        if sd.get("decision") and sd["decision"] != "allow":
            continue
        if not rec.get("original_path"):
            continue
        yield rec


def build_features_table(root: Path, source: Optional[str] = None, board: Optional[str] = None) -> list[dict]:
    rows: list[dict] = []
    for rec in iter_allowed_images(root, source=source, board=board):
        op = Path(rec["original_path"])
        if not op.exists():
            continue
        feats = compute_features(op)
        rows.append({
            "image_key": rec["image_key"],
            "source": rec["source"],
            "board": rec["board"],
            "thread_id": rec["thread_id"],
            "post_id": rec["post_id"],
            "original_path": rec["original_path"],
            "sha256": rec.get("sha256", ""),
            **feats,
        })
    return rows


def find_exact_duplicates(rows: Iterable[dict]) -> list[dict]:
    """Group rows by sha256."""
    by: dict[str, list[dict]] = {}
    for r in rows:
        h = r.get("sha256")
        if not h:
            continue
        by.setdefault(h, []).append(r)
    out: list[dict] = []
    for sha, group in by.items():
        if len(group) < 2:
            continue
        ids = [g["image_key"] for g in group]
        out.append({"duplicate_group_id": sha[:12], "sha256": sha, "size": len(group), "image_keys": ids})
    return out


def find_near_duplicates(rows: Iterable[dict], *, hamming_threshold: int = 4) -> list[dict]:
    """Cluster by phash within hamming_threshold using union-find."""
    rows = [r for r in rows if r.get("phash")]
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def hd(h1: str, h2: str) -> int:
        try:
            return bin(int(h1, 16) ^ int(h2, 16)).count("1")
        except Exception:
            return 64
    n = len(rows)
    for i in range(n):
        for j in range(i + 1, n):
            if hd(rows[i]["phash"], rows[j]["phash"]) <= hamming_threshold:
                union(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out: list[dict] = []
    for root_id, members in groups.items():
        if len(members) < 2:
            continue
        ids = [rows[m]["image_key"] for m in members]
        out.append({
            "near_duplicate_group_id": f"phash_{rows[root_id]['phash'][:8]}",
            "size": len(members),
            "image_keys": ids,
        })
    return out


def write_parquet(rows: list[dict], path: Path) -> Optional[Path]:
    if not rows:
        return None
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except Exception:
        return None
    keys = sorted({k for r in rows for k in r.keys()})
    cols = {k: [r.get(k) for r in rows] for k in keys}
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pydict(cols), path)
    return path


def write_jsonl(rows: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


# ── Optional fastdup integration ────────────────────────────────────────────

def run_fastdup(root: Path, source: str, board: str, log=print) -> Optional[Path]:
    try:
        import fastdup  # type: ignore
    except ImportError:
        log("[quality] fastdup not installed — skipping near-dup/outlier deep analysis. "
            "pip install fastdup to enable.")
        return None
    work = config.derived_dir(root, "fastdup", f"{source}_{board}")
    work.mkdir(parents=True, exist_ok=True)
    image_paths = []
    for rec in iter_allowed_images(root, source=source, board=board):
        op = Path(rec["original_path"])
        if op.exists():
            image_paths.append(str(op))
    if not image_paths:
        log("[quality] no allowed images to feed fastdup")
        return None
    fd = fastdup.create(work_dir=str(work), input_dir=str(work / "_input_list.txt"))
    list_path = work / "_input_list.txt"
    list_path.write_text("\n".join(image_paths), encoding="utf-8")
    fd.run(image_paths)
    return work
