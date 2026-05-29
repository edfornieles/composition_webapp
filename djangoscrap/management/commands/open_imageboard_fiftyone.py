"""Load the local chan corpus into FiftyOne and launch the app.

    python manage.py open_imageboard_fiftyone \\
        --root /Volumes/oom/chan_corpus \\
        --source fourchan --board fit \\
        --dataset-name fourchan_fit
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from djangoscrap.imageboard_ingestion import config


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


class Command(BaseCommand):
    help = "Open the corpus in FiftyOne (local browser-based image dataset viewer)."

    def add_arguments(self, parser):
        parser.add_argument("--root", default=None)
        parser.add_argument("--source", default="fourchan")
        parser.add_argument("--board", required=True)
        parser.add_argument("--dataset-name", default=None,
                            help="Default: <source>_<board>.")
        parser.add_argument("--no-launch", action="store_true",
                            help="Build dataset only; don't open the FiftyOne app.")
        parser.add_argument("--persist", action="store_true",
                            help="Persist dataset between runs (default: ephemeral).")
        parser.add_argument("--include-quarantine", action="store_true",
                            help="DANGEROUS: include quarantined entries.")

    def handle(self, *args, **opts):
        try:
            import fiftyone as fo  # type: ignore
        except ImportError:
            raise CommandError(
                "fiftyone not installed. pip install fiftyone"
            )

        root = config.resolve_root(opts.get("root"))
        config.ensure_layout(root)
        source = opts["source"]
        board = opts["board"]
        ds_name = opts.get("dataset_name") or f"{source}_{board}"

        if fo.dataset_exists(ds_name):
            self.stdout.write(f"  loading existing dataset {ds_name!r}")
            ds = fo.load_dataset(ds_name)
            ds.delete()
        ds = fo.Dataset(ds_name, persistent=bool(opts["persist"]))

        # Index post records for joining metadata
        post_idx = {}
        for r in _load_jsonl(config.posts_manifest(root)):
            puid = r.get("post_uid")
            if puid:
                post_idx[puid] = r
        feat_idx = {r.get("image_key"): r
                    for r in _load_jsonl(config.image_features_path(root).with_suffix(".jsonl"))}
        cap_idx = {r.get("image_key"): r
                   for r in _load_jsonl(config.captions_manifest(root))}
        cm_idx = {r.get("image_key"): r
                  for r in _load_jsonl(config.cluster_membership_path(root).with_suffix(".jsonl"))}
        cluster_label_by_cid = {
            r.get("cluster_id"): r.get("suggested_label_auto") or ""
            for r in _load_jsonl(config.clusters_path(root).with_suffix(".jsonl"))
        }

        n_added = 0
        n_skipped = 0
        for img in _load_jsonl(config.images_manifest(root)):
            if img.get("source") != source or img.get("board") != board:
                continue
            sd = img.get("safety_decision") or {}
            if sd.get("decision") and sd["decision"] != "allow" and not opts["include_quarantine"]:
                n_skipped += 1
                continue
            op = img.get("original_path") or ""
            if not op or not Path(op).exists():
                n_skipped += 1
                continue
            puid = img.get("post_uid") or ""
            post = post_idx.get(puid) or {}
            feats = feat_idx.get(img.get("image_key")) or {}
            cap = cap_idx.get(img.get("image_key")) or {}
            cm = cm_idx.get(img.get("image_key")) or {}

            sample = fo.Sample(filepath=op)
            sample["board"] = img.get("board") or ""
            sample["thread_id"] = int(img.get("thread_id") or 0)
            sample["post_id"] = int(img.get("post_id") or 0)
            sample["width"] = int(img.get("width") or 0)
            sample["height"] = int(img.get("height") or 0)
            sample["aspect_ratio"] = float(feats.get("aspect_ratio") or 0.0)
            sample["orientation"] = feats.get("orientation") or "unknown"
            sample["blur_score"] = float(feats.get("blur_score") or 0)
            sample["brightness"] = float(feats.get("brightness") or 0)
            sample["saturation"] = float(feats.get("saturation") or 0)
            sample["contrast"] = float(feats.get("contrast") or 0)
            sample["entropy"] = float(feats.get("entropy") or 0)
            sample["sha256"] = img.get("sha256") or ""
            sample["phash"] = feats.get("phash") or ""
            sample["caption"] = cap.get("caption") or ""
            sample["caption_model"] = cap.get("caption_model") or ""
            sample["openclip_cluster_id"] = int(cm.get("cluster_id") or -2)
            sample["openclip_cluster_label_auto"] = cluster_label_by_cid.get(cm.get("cluster_id"), "")
            sample["openclip_umap_x"] = float(cm.get("umap_x") or 0.0)
            sample["openclip_umap_y"] = float(cm.get("umap_y") or 0.0)
            sample["fit_tags"] = post.get("fit_tags") or []
            sample["text_excerpt"] = (post.get("plain_text") or "")[:300]
            for k, v in (post.get("contamination") or {}).items():
                sample[f"contamination_{k}"] = float(v or 0.0)
            sample["safety_decision"] = sd.get("decision") or "allow"
            sample["safety_reasons"] = sd.get("reason_codes") or []
            ds.add_sample(sample)
            n_added += 1

        self.stdout.write(self.style.SUCCESS(
            f"FiftyOne dataset {ds_name!r}: added {n_added}, skipped {n_skipped}"
        ))
        if not opts["no_launch"]:
            self.stdout.write("launching FiftyOne app — Ctrl-C in this terminal to stop")
            session = fo.launch_app(ds)
            session.wait()
