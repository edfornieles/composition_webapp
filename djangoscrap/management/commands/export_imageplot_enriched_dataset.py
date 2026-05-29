"""Export an enriched ImagePlot CSV (and optional materialised package).

Joins:
  - images.jsonl manifest (geometry, paths, safety_decision)
  - posts.jsonl manifest (text excerpts, fit_tags, contamination axes)
  - image_features.parquet/jsonl (phash, blur, brightness, etc.)
  - duplicates.parquet/jsonl (duplicate_group_id, near_duplicate_group_id)
  - cluster_membership (umap_x, umap_y, cluster_id)
  - clusters (suggested_label_auto)
  - captions
  - imageplot_labels.jsonl (manual_cluster_label, screen_assignment, composition_bucket)

Strictly excludes anything whose safety_decision != 'allow'. Existing
export_fit_imageplot_dataset / build_fit_imageplot_sample stay intact.

    python manage.py export_imageplot_enriched_dataset \\
        --root /Volumes/oom/chan_corpus \\
        --source fourchan --board fit \\
        --output /Volumes/oom/chan_corpus/derived/imageplot_exports/fit_enriched.csv \\
        --sample-size 5000 \\
        --materialise-symlinks
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import random
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand

from djangoscrap.imageboard_ingestion import config, imageplot_export

# Existing CSV columns + new enrichment columns (additive, kept in order)
ENRICHED_EXTRA_COLUMNS = [
    "original_path",
    "thumbnail_path",
    "safe_for_preview",
    "sha256",
    "phash",
    "duplicate_group_id",
    "near_duplicate_group_id",
    "blur_score",
    "brightness",
    "saturation",
    "contrast",
    "entropy",
    "openclip_umap_x",
    "openclip_umap_y",
    "openclip_cluster_id",
    "openclip_cluster_label_auto",
    "caption",
    "caption_model",
    "manual_cluster_label",
    "aesthetic_bucket",
    "generated_folder_path",
]

ENRICHED_COLUMNS = imageplot_export.CSV_COLUMNS + [
    c for c in ENRICHED_EXTRA_COLUMNS if c not in imageplot_export.CSV_COLUMNS
]


def _load_jsonl(p: Path):
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


def _index_by(rows, key):
    return {r.get(key): r for r in rows if r.get(key) is not None}


def _index_dup_groups(p: Path) -> tuple[dict[str, str], dict[str, str]]:
    exact: dict[str, str] = {}
    near: dict[str, str] = {}
    if not p.exists():
        return exact, near
    for rec in _load_jsonl(p):
        kind = rec.get("kind")
        gid = rec.get("duplicate_group_id") or rec.get("near_duplicate_group_id") or ""
        for k in rec.get("image_keys") or []:
            if kind == "exact":
                exact[k] = gid
            elif kind == "near":
                near[k] = gid
    return exact, near


def _index_clusters(root: Path):
    cluster_rows = list(_load_jsonl(config.clusters_path(root).with_suffix(".jsonl")))
    members = list(_load_jsonl(config.cluster_membership_path(root).with_suffix(".jsonl")))
    label_by_cid = {r["cluster_id"]: r.get("suggested_label_auto") or "" for r in cluster_rows}
    return label_by_cid, _index_by(members, "image_key")


def _index_labels(root: Path) -> dict[str, dict]:
    p = config.manifests_dir(root) / "imageplot_labels.jsonl"
    out: dict[str, dict] = {}
    for rec in _load_jsonl(p):
        rid = rec.get("id") or ""
        if rid:
            out[rid] = rec
    return out


def _fmt_dt(unix_ts) -> str:
    if not unix_ts:
        return ""
    try:
        return _dt.datetime.fromtimestamp(int(unix_ts), tz=_dt.timezone.utc).isoformat()
    except Exception:
        return ""


def _materialise(src: Path, dst_dir: Path, *, copy: bool) -> Path:
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


class Command(BaseCommand):
    help = "Export an enriched ImagePlot CSV joining all corpus enrichment layers."

    def add_arguments(self, parser):
        parser.add_argument("--root", default=None)
        parser.add_argument("--source", default="fourchan")
        parser.add_argument("--board", default=None,
                            help="Optional board filter.")
        parser.add_argument("--output", required=True)
        parser.add_argument("--sample-size", type=int, default=0,
                            help="0 = export everything allowed. Otherwise random sample.")
        parser.add_argument("--seed", type=int, default=1337)
        parser.add_argument("--image-mode",
                            choices=["thumbnails", "full", "both"], default="thumbnails")
        parser.add_argument("--materialise-symlinks", action="store_true",
                            help="Also create images/ folder next to the CSV (symlinks).")
        parser.add_argument("--copy-images", action="store_true",
                            help="Use copies instead of symlinks when materialising.")
        parser.add_argument("--include-quarantine", action="store_true",
                            help="DANGEROUS: include quarantined entries (still no thumbnails).")

    def handle(self, *args, **opts):
        root = config.resolve_root(opts.get("root"))
        config.ensure_layout(root)
        source = opts["source"]
        board = opts.get("board")
        output = Path(opts["output"]).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)

        self.stdout.write(self.style.NOTICE(f"chan corpus root: {root}"))

        # Load all the indices once
        post_idx = _index_by(_load_jsonl(config.posts_manifest(root)), "post_uid")
        feat_idx = _index_by(_load_jsonl(config.image_features_path(root).with_suffix(".jsonl")),
                              "image_key")
        exact_dup, near_dup = _index_dup_groups(config.duplicates_path(root).with_suffix(".jsonl"))
        cluster_label_by_cid, member_by_key = _index_clusters(root)
        captions = {r["image_key"]: r for r in _load_jsonl(config.captions_manifest(root))}
        manual_labels = _index_labels(root)

        # Build rows
        rows: list[dict] = []
        for img in _load_jsonl(config.images_manifest(root)):
            if source and img.get("source") != source:
                continue
            if board and img.get("board") != board:
                continue
            sd = img.get("safety_decision") or {}
            if sd.get("decision") and sd["decision"] != "allow" and not opts["include_quarantine"]:
                continue
            puid = img.get("post_uid") or ""
            post = post_idx.get(puid) or {}
            feats = feat_idx.get(img.get("image_key")) or {}
            cm = member_by_key.get(img.get("image_key")) or {}
            cap = captions.get(img.get("image_key")) or {}
            ml = manual_labels.get(puid) or manual_labels.get(img.get("image_key")) or {}

            contam = (post.get("contamination") or {})
            row = {
                "id": puid or img.get("image_key") or "",
                "board": img.get("board") or "",
                "thread_id": img.get("thread_id") or "",
                "post_id": img.get("post_id") or "",
                "datetime_utc": _fmt_dt(post.get("time_unix")),
                "local_image_path": img.get("original_path") or "",
                "local_thumbnail_path": img.get("thumbnail_path") or "",
                "filename_original": img.get("filename_original") or "",
                "ext": img.get("ext") or "",
                "width": int(img.get("width") or feats.get("width") or 0),
                "height": int(img.get("height") or feats.get("height") or 0),
                "aspect_ratio": float(feats.get("aspect_ratio") or 0.0),
                "file_size": int(img.get("fsize_remote") or 0),
                "safety_allowed_for_visual_use": sd.get("decision", "allow") == "allow",
                "fit_tags": ";".join(post.get("fit_tags") or []),
                "text_excerpt_safe": (post.get("model_safe_text") or post.get("plain_text") or "")[:200].replace("\n", " "),
                "source_bucket": post.get("source_key") or img.get("source") or "",
                "suggested_cluster_label": cluster_label_by_cid.get(cm.get("cluster_id"), ""),
                # Round-trip slots
                "cluster_label_manual": ml.get("cluster_label_manual", ""),
                "screen_assignment": ml.get("screen_assignment", ""),
                "composition_bucket": ml.get("composition_bucket", ""),
                # Enrichment extras
                "original_path": img.get("original_path") or "",
                "thumbnail_path": img.get("thumbnail_path") or "",
                "safe_for_preview": sd.get("decision", "allow") == "allow",
                "sha256": img.get("sha256") or "",
                "phash": feats.get("phash") or "",
                "duplicate_group_id": exact_dup.get(img.get("image_key"), ""),
                "near_duplicate_group_id": near_dup.get(img.get("image_key"), ""),
                "blur_score": feats.get("blur_score") or 0,
                "brightness": feats.get("brightness") or 0,
                "saturation": feats.get("saturation") or 0,
                "contrast": feats.get("contrast") or 0,
                "entropy": feats.get("entropy") or 0,
                "openclip_umap_x": cm.get("umap_x") or 0.0,
                "openclip_umap_y": cm.get("umap_y") or 0.0,
                "openclip_cluster_id": cm.get("cluster_id") if cm.get("cluster_id") is not None else "",
                "openclip_cluster_label_auto": cluster_label_by_cid.get(cm.get("cluster_id"), ""),
                "caption": cap.get("caption", ""),
                "caption_model": cap.get("caption_model", ""),
                "manual_cluster_label": ml.get("cluster_label_manual", ""),
                "aesthetic_bucket": ml.get("aesthetic_bucket", ""),
                "generated_folder_path": "",
            }
            # Carry contamination axes
            for k in imageplot_export.CONTAMINATION_KEYS:
                row[f"contamination_{k}"] = float(contam.get(k) or 0.0)
            rows.append(row)

        if opts["sample_size"] and 0 < opts["sample_size"] < len(rows):
            rng = random.Random(int(opts["seed"]))
            rows = rng.sample(rows, int(opts["sample_size"]))

        # Materialise images alongside CSV?
        if opts["materialise_symlinks"] or opts["copy_images"]:
            images_dir = output.parent / "images"
            for r in rows:
                for col in (("local_image_path", "original_path") if opts["image_mode"] in ("full", "both") else ()):
                    src = Path(r[col]) if r.get(col) else None
                    if src and src.exists():
                        dst = _materialise(src, images_dir, copy=opts["copy_images"])
                        rel = str(dst.relative_to(output.parent))
                        r[col] = rel
                if opts["image_mode"] in ("thumbnails", "both"):
                    for col in ("local_thumbnail_path", "thumbnail_path"):
                        src = Path(r[col]) if r.get(col) else None
                        if src and src.exists():
                            dst = _materialise(src, images_dir, copy=opts["copy_images"])
                            r[col] = str(dst.relative_to(output.parent))

        # Honour image_mode display (clear unwanted column for cleanliness)
        if opts["image_mode"] == "thumbnails":
            for r in rows:
                r["local_image_path"] = ""
        elif opts["image_mode"] == "full":
            for r in rows:
                r["local_thumbnail_path"] = ""

        with output.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=ENRICHED_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        self.stdout.write(self.style.SUCCESS(
            f"wrote {len(rows)} enriched rows → {output}"
        ))
