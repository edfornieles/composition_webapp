"""
Purge "tombstone" ingestion items — database rows whose on-disk file is gone.

Context:
    The cheap AI-QC path in the campaign cycle deletes the downloaded file from
    disk when it rejects an item (see ``_run_campaign_cycle`` in ``views.py``),
    but leaves the ``IngestionItem`` row in place so the log of what was tried
    is preserved. After a few nights of aggregation this produces batches with
    thousands of rows that render as broken thumbnails in the grid (e.g. batch
    141 with 1177 rows, most of them ghosts of QC-rejected files).

What this command does:
    For every ``IngestionItem`` whose ``decision == 'rejected'`` **and** whose
    ``file_path`` no longer resolves to a real file under ``MEDIA_ROOT``, delete
    the database row. Accepted / pending items and rejects whose file is still
    on disk are never touched.

Options:
    --batch <id>   restrict the purge to a single batch
    --dry-run      list what would be deleted without writing anything
    --limit <n>    cap the number of rows deleted in one pass (safety net)
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from djangoscrap.models import IngestionBatch, IngestionItem
from djangoscrap.views import INGESTION_MEDIA_ROOT


class Command(BaseCommand):
    help = "Delete IngestionItem rows whose on-disk file is missing (QC tombstones)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch",
            type=int,
            default=None,
            help="Only purge items inside this batch id.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without touching the database.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after inspecting this many rows (0 = no limit).",
        )
        parser.add_argument(
            "--include-non-rejected",
            action="store_true",
            help=(
                "Also purge pending / accepted rows whose file is missing. "
                "Normally we only purge rejects so we don't silently lose "
                "legitimate items that are temporarily unreadable."
            ),
        )

    def handle(self, *args, **opts):
        batch_id = opts.get("batch")
        dry_run = bool(opts.get("dry_run"))
        limit = int(opts.get("limit") or 0)
        include_non_rejected = bool(opts.get("include_non_rejected"))

        qs = IngestionItem.objects.all()
        if batch_id is not None:
            if not IngestionBatch.objects.filter(id=batch_id).exists():
                raise CommandError(f"Batch {batch_id} does not exist.")
            qs = qs.filter(batch_id=batch_id)
        if not include_non_rejected:
            qs = qs.filter(decision="rejected")
        qs = qs.only("id", "batch_id", "file_path", "decision").order_by("id")

        scanned = 0
        missing_ids: list[int] = []
        per_batch_missing: dict[int, int] = {}
        for item in qs.iterator(chunk_size=500):
            scanned += 1
            rel = (item.file_path or "").strip().replace("\\", "/")
            if not rel:
                missing_ids.append(item.id)
                per_batch_missing[item.batch_id] = per_batch_missing.get(item.batch_id, 0) + 1
            else:
                full = INGESTION_MEDIA_ROOT / rel
                if not full.exists():
                    missing_ids.append(item.id)
                    per_batch_missing[item.batch_id] = per_batch_missing.get(item.batch_id, 0) + 1
            if limit and scanned >= limit:
                break

        total_missing = len(missing_ids)
        self.stdout.write(
            f"Scanned {scanned} row(s); {total_missing} have no file on disk."
        )
        if per_batch_missing:
            self.stdout.write("Per-batch tombstone counts:")
            for bid, n in sorted(per_batch_missing.items()):
                self.stdout.write(f"  batch {bid}: {n}")

        if not total_missing:
            return

        if dry_run:
            self.stdout.write("Dry run — no rows deleted.")
            return

        deleted_total = 0
        for i in range(0, len(missing_ids), 500):
            chunk = missing_ids[i : i + 500]
            n, _ = IngestionItem.objects.filter(id__in=chunk).delete()
            deleted_total += n
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted_total} tombstone row(s)."))
