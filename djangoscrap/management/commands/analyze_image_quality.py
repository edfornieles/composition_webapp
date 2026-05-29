"""Compute quality features + dedupe groups across the local chan corpus.

    python manage.py analyze_image_quality \\
        --root /Volumes/oom/chan_corpus \\
        --source fourchan --board fit
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from djangoscrap.imageboard_ingestion import config, quality


class Command(BaseCommand):
    help = "Compute Pillow-driven image features + sha256 / pHash duplicate groups."

    def add_arguments(self, parser):
        parser.add_argument("--root", default=None)
        parser.add_argument("--source", default="fourchan")
        parser.add_argument("--board", default=None,
                            help="Optional board filter (default: all boards in manifest).")
        parser.add_argument("--phash-hamming", type=int, default=4,
                            help="Max hamming distance for near-dup group (default 4).")
        parser.add_argument("--run-fastdup", action="store_true",
                            help="Also invoke fastdup if installed.")

    def handle(self, *args, **opts):
        root = config.resolve_root(opts.get("root"))
        config.ensure_layout(root)
        source = opts["source"]
        board = opts.get("board")

        self.stdout.write(self.style.NOTICE(f"chan corpus root: {root}"))
        self.stdout.write(f"  computing features for {source}/{board or '<all boards>'}…")
        rows = quality.build_features_table(root, source=source, board=board)
        if not rows:
            self.stdout.write(self.style.WARNING(
                "no allowed images found — run ingest_imageboard_board first, or check safety filtering."))
            return

        feat_pq = config.image_features_path(root)
        wrote_pq = quality.write_parquet(rows, feat_pq)
        feat_jsonl = feat_pq.with_suffix(".jsonl")
        quality.write_jsonl(rows, feat_jsonl)
        self.stdout.write(self.style.SUCCESS(
            f"  features for {len(rows)} image(s) → {feat_jsonl}"
            + (f"  (+ {wrote_pq})" if wrote_pq else "  (parquet skipped: pyarrow missing)")
        ))

        exact = quality.find_exact_duplicates(rows)
        near = quality.find_near_duplicates(rows, hamming_threshold=int(opts["phash_hamming"]))
        dup_rows = (
            [{**g, "kind": "exact"} for g in exact]
            + [{**g, "kind": "near"} for g in near]
        )
        dup_pq = config.duplicates_path(root)
        wrote_dup_pq = quality.write_parquet(dup_rows, dup_pq)
        quality.write_jsonl(dup_rows, dup_pq.with_suffix(".jsonl"))
        self.stdout.write(self.style.SUCCESS(
            f"  duplicate groups: exact={len(exact)} near={len(near)}  → {dup_pq.with_suffix('.jsonl')}"
            + (f"  (+ {wrote_dup_pq})" if wrote_dup_pq else "")
        ))

        if opts["run_fastdup"]:
            quality.run_fastdup(root, source, board or "all", log=self.stdout.write)
