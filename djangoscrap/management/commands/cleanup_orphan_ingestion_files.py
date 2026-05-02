"""
Clean orphan files under ``MEDIA_ROOT/ingestion_items/``.

Context
-------
Every file in the ingestion staging area is *supposed* to be referenced by
exactly one ``IngestionItem.file_path`` (stored as ``<batch_id>/<sha>.<ext>``).
In practice crashes mid-download, interrupted campaign cycles, and legacy
rows that were deleted before the bulk-delete endpoint unlinked disk files
leave behind files that no DB row points at. They're invisible to the UI
but slowly consume disk.

Companion to ``purge_ingestion_tombstones`` which handles the opposite
direction (DB rows whose file is gone). Together they keep disk and DB in
sync.

What this does
--------------
For every regular file under ``INGESTION_MEDIA_ROOT`` whose relative path
(``<batch_id>/<sha>.<ext>``) does NOT appear in the set of currently
referenced ``IngestionItem.file_path`` values, delete the file. By default
the command is a dry run — pass ``--apply`` to actually delete. Nothing in
the database is ever touched.

Options
-------
``--batch <id>``   restrict to one batch subdirectory
``--apply``        actually unlink files (default: dry-run)
``--limit <n>``    stop after N orphans have been removed (safety net)
``--min-age <h>``  skip files younger than N hours (protect an in-flight run)
"""

from __future__ import annotations

import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from djangoscrap.models import IngestionBatch, IngestionItem

# Mirror djangoscrap.views.INGESTION_MEDIA_ROOT without importing views.py
# (which pulls in video_processing -> ffmpeg, a heavy optional dependency
# that isn't required for pure file/DB cleanup).
INGESTION_MEDIA_ROOT = Path(settings.MEDIA_ROOT) / "ingestion_items"


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class Command(BaseCommand):
    help = (
        "Delete files under MEDIA_ROOT/ingestion_items/ that no IngestionItem "
        "row references (orphan thumbnails). Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch",
            type=int,
            default=None,
            help="Only scan this batch's subdirectory (e.g. --batch 10).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete files. Without this flag the command reports what would happen.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after unlinking N orphans (0 = no limit). Useful for testing in batches.",
        )
        parser.add_argument(
            "--min-age",
            type=float,
            default=1.0,
            help=(
                "Skip files modified within the last N hours (default 1.0). "
                "Protects files that an in-flight campaign cycle just downloaded "
                "but hasn't created a DB row for yet."
            ),
        )

    def handle(self, *args, **opts):
        batch_id = opts.get("batch")
        apply_changes = bool(opts.get("apply"))
        limit = int(opts.get("limit") or 0)
        min_age_hours = float(opts.get("min_age") or 0.0)
        min_age_seconds = max(0.0, min_age_hours) * 3600.0
        now = time.time()

        root: Path = INGESTION_MEDIA_ROOT
        if not root.exists():
            self.stdout.write(f"Nothing to scan — {root} does not exist.")
            return

        scan_dirs: list[Path]
        if batch_id is not None:
            if not IngestionBatch.objects.filter(id=batch_id).exists():
                raise CommandError(f"Batch {batch_id} does not exist.")
            target = root / str(batch_id)
            if not target.exists():
                self.stdout.write(f"No staging dir for batch {batch_id} at {target}.")
                return
            scan_dirs = [target]
        else:
            scan_dirs = [p for p in root.iterdir() if p.is_dir()]

        # Build the "known" set — every file_path currently referenced by a row.
        # Scoped to the same batch subset we're scanning so we don't hold the
        # whole table in RAM for a targeted cleanup.
        known_rel: set[str] = set()
        row_qs = IngestionItem.objects.only("batch_id", "file_path")
        if batch_id is not None:
            row_qs = row_qs.filter(batch_id=batch_id)
        for it in row_qs.iterator(chunk_size=2000):
            rel = (it.file_path or "").strip().replace("\\", "/")
            if rel:
                known_rel.add(rel)

        self.stdout.write(
            f"Scanning {len(scan_dirs)} batch dir(s) under {root} "
            f"against {len(known_rel)} referenced file_path value(s)."
        )
        if min_age_seconds > 0:
            self.stdout.write(
                f"Skipping files modified within the last {min_age_hours:g}h."
            )

        orphan_count = 0
        orphan_bytes = 0
        skipped_recent = 0
        deleted_count = 0
        deleted_bytes = 0
        per_batch: dict[str, int] = {}

        root_resolved = root.resolve()

        for batch_dir in sorted(scan_dirs):
            for path in sorted(batch_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(root).as_posix()
                if rel in known_rel:
                    continue

                try:
                    # Defence in depth: never touch anything resolving outside
                    # INGESTION_MEDIA_ROOT even if a symlink snuck in.
                    path.resolve().relative_to(root_resolved)
                except ValueError:
                    continue

                try:
                    stat = path.stat()
                except OSError:
                    continue

                if min_age_seconds > 0 and (now - stat.st_mtime) < min_age_seconds:
                    skipped_recent += 1
                    continue

                orphan_count += 1
                orphan_bytes += stat.st_size
                per_batch[batch_dir.name] = per_batch.get(batch_dir.name, 0) + 1

                if apply_changes:
                    try:
                        path.unlink()
                    except OSError as exc:
                        self.stderr.write(f"  unlink failed: {rel}: {exc}")
                        continue
                    deleted_count += 1
                    deleted_bytes += stat.st_size
                    if limit and deleted_count >= limit:
                        self.stdout.write(f"Reached --limit {limit}, stopping.")
                        break
                else:
                    self.stdout.write(f"  orphan: {rel} ({_format_bytes(stat.st_size)})")
                    if limit and orphan_count >= limit:
                        self.stdout.write(f"Reached --limit {limit}, stopping scan.")
                        break
            else:
                continue
            break  # inner loop hit --limit; propagate break to outer loop

        if per_batch:
            self.stdout.write("Per-batch orphan counts:")
            for name, n in sorted(per_batch.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 1 << 30):
                self.stdout.write(f"  batch {name}: {n}")

        if skipped_recent:
            self.stdout.write(
                f"Skipped {skipped_recent} file(s) younger than --min-age "
                f"(protecting any in-flight campaign downloads)."
            )

        if apply_changes:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Deleted {deleted_count} orphan file(s), freed {_format_bytes(deleted_bytes)}."
                )
            )
        else:
            self.stdout.write(
                f"Dry run: found {orphan_count} orphan file(s) "
                f"totalling {_format_bytes(orphan_bytes)}. "
                "Re-run with --apply to delete."
            )
