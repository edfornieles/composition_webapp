"""
Compute and store CLIP ViT-B/32 embeddings for ingestion items that don't have
one yet.

Why this exists
===============
The semantic-dedupe path in ``_detect_duplicate`` compares a newly-downloaded
image's CLIP embedding against embeddings already stored on older items in the
same batch. Items scraped before this field existed have ``clip_embedding=""``,
which means the first night after the feature ships, CLIP has nothing to
compare against and dedupe silently degrades back to "phash only".

This command walks every image IngestionItem whose ``clip_embedding`` is empty
and whose file is still on disk, encodes it, and stores the vector. It's safe
to re-run — already-populated rows are skipped.

Options
-------
``--batch N``   restrict to one batch
``--limit N``   stop after writing N embeddings (useful for overnight-safe runs)
``--dry-run``   count how many rows would be updated, write nothing
``--decision``  comma-separated list of decisions to include (default: all)

Usage examples
--------------
    # Backfill the biggest / noisiest batch first:
    python manage.py backfill_clip_embeddings --batch 141

    # Backfill everywhere, at most 2000 rows:
    python manage.py backfill_clip_embeddings --limit 2000

    # Just report what's missing:
    python manage.py backfill_clip_embeddings --dry-run
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from djangoscrap.models import IngestionBatch, IngestionItem
from djangoscrap.views import (
    INGESTION_MEDIA_ROOT,
    _clip_embed_encode,
    _clip_embed_path,
    _clip_get_model,
)


class Command(BaseCommand):
    help = "Compute CLIP ViT-B/32 embeddings for image IngestionItems that are missing one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch",
            type=int,
            default=None,
            help="Only backfill items inside this batch id.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after encoding this many new embeddings (0 = no limit).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count what's missing; do not encode or write.",
        )
        parser.add_argument(
            "--decision",
            type=str,
            default="",
            help=(
                "Comma-separated decisions to include (pending, accepted, rejected). "
                "Default includes all three. Useful if you only care about keeping the "
                "accepted canon indexed."
            ),
        )

    def handle(self, *args, **opts):
        batch_id = opts.get("batch")
        limit = int(opts.get("limit") or 0)
        dry_run = bool(opts.get("dry_run"))
        decision_csv = (opts.get("decision") or "").strip()
        decisions = [d.strip() for d in decision_csv.split(",") if d.strip()]

        qs = IngestionItem.objects.filter(media_kind="image", clip_embedding="")
        if batch_id is not None:
            if not IngestionBatch.objects.filter(id=batch_id).exists():
                raise CommandError(f"Batch {batch_id} does not exist.")
            qs = qs.filter(batch_id=batch_id)
        if decisions:
            qs = qs.filter(decision__in=decisions)
        qs = qs.only("id", "batch_id", "file_path", "decision").order_by("-id")

        total = qs.count()
        self.stdout.write(
            f"{total} image row(s) without a CLIP embedding"
            + (f" in batch {batch_id}" if batch_id is not None else "")
            + "."
        )
        if dry_run or total == 0:
            return

        # Preflight: make sure CLIP actually loads before we iterate. Avoids a
        # partial run where every item fails for the same reason.
        model = _clip_get_model()
        if model is None:
            raise CommandError(
                "CLIP is unavailable in this environment — install "
                "sentence-transformers and torch, then re-run."
            )

        encoded = 0
        missing_file = 0
        failed = 0
        started = time.time()
        # Iterate in chunks so we don't hold one giant cursor open while writing.
        for item in qs.iterator(chunk_size=200):
            rel = (item.file_path or "").strip().replace("\\", "/")
            if not rel:
                missing_file += 1
                continue
            full = INGESTION_MEDIA_ROOT / rel
            if not full.is_file():
                missing_file += 1
                continue
            vec = _clip_embed_path(full)
            if vec is None:
                failed += 1
                continue
            blob = _clip_embed_encode(vec)
            if not blob:
                failed += 1
                continue
            IngestionItem.objects.filter(id=item.id).update(clip_embedding=blob)
            encoded += 1
            if limit and encoded >= limit:
                break
            if encoded % 50 == 0:
                elapsed = time.time() - started
                rate = encoded / elapsed if elapsed > 0 else 0.0
                self.stdout.write(
                    f"  … {encoded} encoded ({rate:.1f}/s, missing_file={missing_file}, failed={failed})"
                )

        elapsed = time.time() - started
        self.stdout.write(
            self.style.SUCCESS(
                f"Encoded {encoded} embedding(s) in {elapsed:.1f}s "
                f"(missing_file={missing_file}, failed={failed})."
            )
        )
