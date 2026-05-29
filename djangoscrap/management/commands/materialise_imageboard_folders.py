"""Materialise folders of similar images for art-production workflows.

    python manage.py materialise_imageboard_folders \\
        --root /Volumes/oom/chan_corpus \\
        --source fourchan --board fit \\
        --by openclip_cluster_label_auto \\
        --min-cluster-size 25 \\
        --symlink

    --by options:
        openclip_cluster_id
        openclip_cluster_label_auto
        manual_cluster_label
        composition_bucket
        screen_assignment

    --where filter expression:
        "orientation=portrait,contamination_body_shame>=0.5,cluster_size>=25"
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Iterable, Optional

from django.core.management.base import BaseCommand, CommandError

from djangoscrap.imageboard_ingestion import config


_SAFE = re.compile(r"[^A-Za-z0-9._\-]+")


def _slug(s: str) -> str:
    s = _SAFE.sub("_", str(s)).strip("_")
    return s or "unlabeled"


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


def _parse_where(expr: Optional[str]) -> list[tuple[str, str, str]]:
    """Tiny expression parser. Returns list of (field, op, value)."""
    if not expr:
        return []
    out = []
    for clause in expr.split(","):
        clause = clause.strip()
        if not clause:
            continue
        m = re.match(r"^(\w+)\s*(>=|<=|==|=|>|<|!=)\s*(.*)$", clause)
        if not m:
            continue
        op = m.group(2)
        if op == "=":
            op = "=="
        out.append((m.group(1), op, m.group(3).strip()))
    return out


def _row_passes(row: dict, where: list[tuple[str, str, str]]) -> bool:
    for f, op, v in where:
        x = row.get(f)
        if x is None:
            return False
        try:
            xv = float(x)
            vv = float(v)
            cmp_pair = (xv, vv)
            num = True
        except (TypeError, ValueError):
            num = False
            cmp_pair = (str(x), str(v))
        a, b = cmp_pair
        if op == ">=" and not (a >= b): return False
        elif op == "<=" and not (a <= b): return False
        elif op == ">" and not (a > b): return False
        elif op == "<" and not (a < b): return False
        elif op == "==" and not (a == b): return False
        elif op == "!=" and not (a != b): return False
    return True


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
    help = "Generate folders of similar images (symlinks by default) keyed by cluster/manual label."

    def add_arguments(self, parser):
        parser.add_argument("--root", default=None)
        parser.add_argument("--source", default="fourchan")
        parser.add_argument("--board", required=True)
        parser.add_argument("--by", required=True,
                            choices=["openclip_cluster_id", "openclip_cluster_label_auto",
                                     "manual_cluster_label", "composition_bucket",
                                     "screen_assignment"])
        parser.add_argument("--where", default=None,
                            help="Comma-separated filter expression "
                                 "(e.g. 'orientation=portrait,contamination_body_shame>=0.5').")
        parser.add_argument("--min-cluster-size", type=int, default=1)
        parser.add_argument("--symlink", action="store_true", default=True)
        parser.add_argument("--copy", dest="symlink", action="store_false",
                            help="Copy files instead of symlinking.")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--include-quarantine", action="store_true",
                            help="DANGEROUS: include quarantined items.")

    def handle(self, *args, **opts):
        root = config.resolve_root(opts.get("root"))
        config.ensure_layout(root)
        source = opts["source"]
        board = opts["board"]

        # Build joined-row table in memory (this is the same join as the
        # enriched export, but we only need the columns we filter/group on)
        post_idx = {r.get("post_uid"): r for r in _load_jsonl(config.posts_manifest(root))}
        feat_idx = {r.get("image_key"): r
                    for r in _load_jsonl(config.image_features_path(root).with_suffix(".jsonl"))}
        cm_idx = {r.get("image_key"): r
                  for r in _load_jsonl(config.cluster_membership_path(root).with_suffix(".jsonl"))}
        cluster_label_by_cid = {
            r.get("cluster_id"): r.get("suggested_label_auto") or ""
            for r in _load_jsonl(config.clusters_path(root).with_suffix(".jsonl"))
        }
        manual = {}
        for r in _load_jsonl(config.manifests_dir(root) / "imageplot_labels.jsonl"):
            manual[r.get("id")] = r

        rows: list[dict] = []
        for img in _load_jsonl(config.images_manifest(root)):
            if img.get("source") != source or img.get("board") != board:
                continue
            sd = img.get("safety_decision") or {}
            if sd.get("decision") and sd["decision"] != "allow" and not opts["include_quarantine"]:
                continue
            op = img.get("original_path") or ""
            if not op or not Path(op).exists():
                continue
            post = post_idx.get(img.get("post_uid")) or {}
            feats = feat_idx.get(img.get("image_key")) or {}
            cm = cm_idx.get(img.get("image_key")) or {}
            ml = manual.get(img.get("post_uid")) or manual.get(img.get("image_key")) or {}
            row = {
                "image_key": img.get("image_key"),
                "original_path": op,
                "orientation": feats.get("orientation"),
                "aspect_ratio": feats.get("aspect_ratio") or 0.0,
                "blur_score": feats.get("blur_score") or 0,
                "openclip_cluster_id": cm.get("cluster_id"),
                "openclip_cluster_label_auto": cluster_label_by_cid.get(cm.get("cluster_id"), ""),
                "manual_cluster_label": ml.get("cluster_label_manual", ""),
                "composition_bucket": ml.get("composition_bucket", ""),
                "screen_assignment": ml.get("screen_assignment", ""),
            }
            for k, v in (post.get("contamination") or {}).items():
                row[f"contamination_{k}"] = float(v or 0.0)
            rows.append(row)

        where = _parse_where(opts.get("where"))
        if where:
            rows = [r for r in rows if _row_passes(r, where)]

        # Group
        by_field = opts["by"]
        groups: dict[str, list[dict]] = {}
        for r in rows:
            v = r.get(by_field)
            if v in (None, "", -1, "noise"):
                continue
            groups.setdefault(str(v), []).append(r)

        out_root = config.derived_dir(root, "folders", f"{source}_{board}", by_field)
        if not opts["dry_run"]:
            out_root.mkdir(parents=True, exist_ok=True)

        n_groups = 0
        n_links = 0
        min_size = int(opts["min_cluster_size"])
        for label, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            if len(members) < min_size:
                continue
            folder = out_root / _slug(label)
            n_groups += 1
            self.stdout.write(f"  {label!r}  ({len(members)} images)  → {folder}")
            if opts["dry_run"]:
                continue
            for m in members:
                src = Path(m["original_path"])
                if not src.exists():
                    continue
                _materialise(src, folder, copy=not opts["symlink"])
                n_links += 1
            manifest = {
                "label": label,
                "by": by_field,
                "image_count": len(members),
                "representative_image_keys": [m["image_key"] for m in members[:5]],
                "where": opts.get("where") or "",
                "symlinks": bool(opts["symlink"]),
            }
            (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        self.stdout.write(self.style.SUCCESS(
            f"materialised {n_groups} folder(s), {n_links} link(s) → {out_root}"
        ))
