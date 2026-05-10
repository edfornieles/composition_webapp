"""ImagePlot 2.2 export helpers for /fit/-style imageboard corpora.

Produces a flat CSV row per usable image, suitable for loading into
Lev Manovich's ImagePlot or any browser-based collection-visualisation tool.

Used by management commands:
    export_fit_imageplot_dataset
    build_fit_imageplot_sample
    import_fit_imageplot_labels

This is for *visual research / clustering*, NOT model training. We err on the
side of including images that pass hard safety blockers (no CP indicator,
no doxxing, no violence-incite, no hate, no ED-glorification).
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import shutil
from pathlib import Path
from typing import Iterable, Iterator

from . import storage

# ── CSV column order — keep stable; ImagePlot reads by header name ───────────
CSV_COLUMNS = [
    "id",
    "board",
    "thread_id",
    "post_id",
    "datetime_utc",
    "local_image_path",
    "local_thumbnail_path",
    "filename_original",
    "ext",
    "width",
    "height",
    "aspect_ratio",
    "file_size",
    "safety_allowed_for_visual_use",
    "contamination_body_shame",
    "contamination_discipline_fetish",
    "contamination_status_anxiety",
    "contamination_supplement_obsession",
    "contamination_injury_fear",
    "contamination_self_loathing",
    "contamination_greentext_energy",
    "fit_tags",
    "text_excerpt_safe",
    "source_bucket",
    "suggested_cluster_label",
    # Reserved for ImagePlot round-tripping — left blank on export, filled by
    # the user inside ImagePlot, then read back by import_fit_imageplot_labels:
    "cluster_label_manual",
    "screen_assignment",
    "composition_bucket",
]

# Hard safety blockers — if ANY of these are true, drop the image from export
HARD_BLOCKERS = (
    "likely_cp_indicator",
    "likely_doxxing",
    "likely_violence_incite",
    "likely_hate",
    "likely_eating_disorder",   # we don't visually surface ED content
)

# Contamination axes that we expose as numeric columns (matches user spec)
CONTAMINATION_KEYS = [
    "body_shame",
    "discipline_fetish",
    "status_anxiety",
    "supplement_obsession",
    "injury_fear",
    "self_loathing",
    "greentext_energy",
]


def is_visually_usable(rec: dict) -> bool:
    """Cheap gate: has an image AND no hard safety blocker."""
    img = rec.get("image")
    if not img or not img.get("tim") or not img.get("ext"):
        return False
    safety = rec.get("safety") or {}
    if any(safety.get(k) for k in HARD_BLOCKERS):
        return False
    return True


def _fmt_dt(unix_ts: int | float | None) -> str:
    if not unix_ts:
        return ""
    try:
        return _dt.datetime.fromtimestamp(int(unix_ts), tz=_dt.timezone.utc).isoformat()
    except Exception:
        return ""


def _suggest_cluster_label(rec: dict) -> str:
    """Lightweight first-guess label. User refines manually inside ImagePlot.

    Priority:
      1. First fit_tag if present
      2. Dominant contamination axis (>= 0.4 threshold)
      3. 'other'
    """
    tags = rec.get("fit_tags") or []
    if tags:
        return str(tags[0])
    contam = rec.get("contamination") or {}
    best_k, best_v = None, 0.0
    for k in CONTAMINATION_KEYS:
        v = float(contam.get(k) or 0.0)
        if v > best_v:
            best_k, best_v = k, v
    if best_k and best_v >= 0.4:
        return f"contam_{best_k}"
    return "other"


def _image_paths(corpus: str, rec: dict) -> tuple[Path | None, Path | None]:
    """Return (full_image_path, thumbnail_path), each None if missing on disk."""
    img = rec.get("image") or {}
    board = rec.get("board") or "fit"
    tim = img.get("tim")
    ext = img.get("ext") or ""
    if not tim:
        return None, None
    root = storage.source_root(corpus)
    full_p = root / "images" / board / f"{tim}{ext}"
    thumb_p = root / "thumbnails" / board / f"{tim}s.jpg"
    return (full_p if full_p.exists() else None,
            thumb_p if thumb_p.exists() else None)


def build_row(rec: dict, corpus: str) -> dict | None:
    """Convert one manifest record → CSV row dict, or None if not usable."""
    if not is_visually_usable(rec):
        return None
    full_p, thumb_p = _image_paths(corpus, rec)
    if full_p is None and thumb_p is None:
        return None  # manifest mentions image but neither file is on disk
    img = rec.get("image") or {}
    contam = rec.get("contamination") or {}
    use_policy = rec.get("use_policy") or {}
    w = int(img.get("w") or 0)
    h = int(img.get("h") or 0)
    aspect = round(w / h, 4) if w and h else 0.0
    safe_text = (rec.get("model_safe_text") or "").replace("\n", " ").strip()
    text_excerpt = safe_text[:200]
    row = {
        "id": rec.get("post_uid") or f"{rec.get('board')}:{rec.get('post_no')}",
        "board": rec.get("board") or "",
        "thread_id": rec.get("thread_no") or "",
        "post_id": rec.get("post_no") or "",
        "datetime_utc": _fmt_dt(rec.get("time_unix")),
        "local_image_path": str(full_p) if full_p else "",
        "local_thumbnail_path": str(thumb_p) if thumb_p else "",
        "filename_original": img.get("filename") or "",
        "ext": img.get("ext") or "",
        "width": w,
        "height": h,
        "aspect_ratio": aspect,
        "file_size": int(img.get("fsize") or 0),
        "safety_allowed_for_visual_use": bool(use_policy.get("allowed_for_retrieval", True)),
        "fit_tags": ";".join(rec.get("fit_tags") or []),
        "text_excerpt_safe": text_excerpt,
        "source_bucket": rec.get("source_key") or "",
        "suggested_cluster_label": _suggest_cluster_label(rec),
        "cluster_label_manual": "",
        "screen_assignment": "",
        "composition_bucket": "",
    }
    for k in CONTAMINATION_KEYS:
        row[f"contamination_{k}"] = float(contam.get(k) or 0.0)
    return row


def iter_rows(corpus: str) -> Iterator[dict]:
    for rec in storage.iter_manifest(corpus):
        row = build_row(rec, corpus)
        if row is not None:
            yield row


def write_csv(rows: Iterable[dict], output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with output.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
            n += 1
    return n


# ── Sample export: copy/symlink images into a sibling images/ dir ────────────

def _materialize_image(src: Path, dst_dir: Path, *, copy: bool) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists() or dst.is_symlink():
        return dst
    if copy:
        shutil.copy2(src, dst)
    else:
        try:
            os.symlink(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    return dst


def write_sample(
    corpus: str,
    output_csv: Path,
    images_dir: Path,
    *,
    sample_size: int,
    image_mode: str = "thumbnails",
    copy_images: bool = False,
    seed: int = 1337,
) -> int:
    """Write a sampled CSV + materialise images alongside it.

    image_mode: 'thumbnails' | 'full' | 'both'
    """
    import random
    rng = random.Random(seed)
    all_rows = list(iter_rows(corpus))
    if not all_rows:
        return 0
    if sample_size > 0 and sample_size < len(all_rows):
        rows = rng.sample(all_rows, sample_size)
    else:
        rows = all_rows

    # Materialise images into images_dir, rewrite paths to be relative to CSV
    out_rows: list[dict] = []
    for r in rows:
        new_full = ""
        new_thumb = ""
        if r.get("local_image_path") and image_mode in ("full", "both"):
            src = Path(r["local_image_path"])
            if src.exists():
                dst = _materialize_image(src, images_dir, copy=copy_images)
                new_full = str(dst.relative_to(output_csv.parent))
        if r.get("local_thumbnail_path") and image_mode in ("thumbnails", "both"):
            src = Path(r["local_thumbnail_path"])
            if src.exists():
                dst = _materialize_image(src, images_dir, copy=copy_images)
                new_thumb = str(dst.relative_to(output_csv.parent))
        r2 = dict(r)
        r2["local_image_path"] = new_full or r["local_image_path"]
        r2["local_thumbnail_path"] = new_thumb or r["local_thumbnail_path"]
        out_rows.append(r2)

    return write_csv(out_rows, output_csv)


# ── Label round-trip from ImagePlot ──────────────────────────────────────────

def import_labels(labels_csv: Path, corpus: str) -> Path:
    """Read a labelled CSV (manual cluster_label_manual / screen_assignment /
    composition_bucket filled in) and write a JSONL of just-the-labels into
    the corpus' manifests/imageplot_labels.jsonl. Returns the output path.
    """
    out = storage.source_root(corpus) / "manifests" / "imageplot_labels.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with labels_csv.open("r", encoding="utf-8") as fin, out.open("w", encoding="utf-8") as fout:
        reader = csv.DictReader(fin)
        for row in reader:
            rid = row.get("id") or ""
            cluster = (row.get("cluster_label_manual") or "").strip()
            screen = (row.get("screen_assignment") or "").strip()
            bucket = (row.get("composition_bucket") or "").strip()
            if not (cluster or screen or bucket):
                continue
            fout.write(json.dumps({
                "id": rid,
                "cluster_label_manual": cluster,
                "screen_assignment": screen,
                "composition_bucket": bucket,
                "post_id": row.get("post_id") or "",
                "thread_id": row.get("thread_id") or "",
            }, ensure_ascii=False) + "\n")
            n += 1
    return out
