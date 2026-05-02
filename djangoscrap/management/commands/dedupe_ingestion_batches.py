"""
Batch-wide perceptual-hash dedupe for ingestion rows.

Why this exists
===============
The campaign cycle's on-the-fly dedupe only looks one row at a time. Over
weeks of overnight aggregation, a single underlying image ends up stored
many times across batches — e.g. the same "blue fur" photo mirrored on
``i.pinimg.com``, ``letsystatic.com``, ``akl.ostkcdn.com`` and
``m.media-amazon.com`` all pass the per-item guard because each mirror
serves slightly different bytes. The result is batches where the grid is
dominated by near-identical tiles.

The codebase already has ``_dedupe_batch_visual_crops``, which is the
backend of the "Reject similar / duplicate crops" button on the batch
page. It clusters images in a single batch by letterbox-normalised
perceptual hash (the more robust variant that ignores letterbox crops
and moderate re-encodes) and marks the smaller copies as rejected,
keeping the largest one in each cluster as the canonical record.

This command wraps that function so it runs across *every* batch (or a
specific one), and adds an optional "collapse" pass that hard-deletes
rejected rows which are now just near-duplicates of another rejected
row in the same batch. Rejects are the vast majority of what still
clutters the grid (the campaign filter never deletes them), so
collapsing them is the fastest way to make batches readable again.

Modes
-----
* ``--mode mark`` (default)
    Run ``_dedupe_batch_visual_crops`` on pending + accepted items. Near
    duplicates get their ``decision`` flipped to ``rejected`` with a
    ``dedupe_note`` explaining why. No files are deleted, no rejected
    rows are touched. Safe and non-destructive.

* ``--mode collapse``
    Same as ``mark``, then additionally cluster *rejected* items by
    letterbox phash within each batch. For every cluster with more than
    one rejected row, keep the largest-pixel row (canonical) and
    hard-delete the rest (DB row + on-disk file). This is what actually
    shrinks the grid back to something sensible.

Options
-------
``--batch N``         restrict to one batch
``--hamming N``       Hamming-distance threshold (default 14, matches the UI slider)
``--dry-run``         report what would change without writing
``--delete-files``    when mode=collapse, also remove the rejected duplicates' files
                      from disk (default: yes). Pass ``--no-delete-files`` to keep
                      the files around even after their DB row is gone.
"""

from __future__ import annotations

import argparse

from django.core.management.base import BaseCommand, CommandError
from django.db.models import F

from djangoscrap.models import IngestionBatch, IngestionItem
from djangoscrap.views import (
    INGESTION_MEDIA_ROOT,
    _dedupe_batch_visual_crops,
    _dedupe_phash_for_item,
    _hamming_distance_hex,
)


class Command(BaseCommand):
    help = (
        "Run phash-based dedupe across every ingestion batch. Marks near-duplicate "
        "pending/accepted items as rejected; optionally collapses near-duplicate "
        "rejected rows down to one canonical record per cluster."
    )

    def add_arguments(self, parser: argparse.ArgumentParser):
        parser.add_argument(
            "--batch",
            type=int,
            default=None,
            help="Only dedupe items inside this batch id.",
        )
        parser.add_argument(
            "--hamming",
            type=int,
            default=14,
            help="Hamming distance threshold for near-duplicate clustering (default: 14).",
        )
        parser.add_argument(
            "--mode",
            choices=("mark", "collapse"),
            default="collapse",
            help=(
                "mark: only flip near-dupe pending/accepted rows to rejected. "
                "collapse: also hard-delete near-dupe rejected rows (keeping the "
                "largest one in each cluster). Default: collapse."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )
        parser.add_argument(
            "--delete-files",
            dest="delete_files",
            action="store_true",
            default=True,
            help="When collapsing rejects, remove their on-disk files too (default).",
        )
        parser.add_argument(
            "--no-delete-files",
            dest="delete_files",
            action="store_false",
            help="Keep the collapsed rejects' files on disk.",
        )

    def handle(self, *args, **opts):
        batch_id = opts.get("batch")
        hamming = int(opts.get("hamming") or 14)
        mode = opts.get("mode") or "collapse"
        dry_run = bool(opts.get("dry_run"))
        delete_files = bool(opts.get("delete_files", True))

        batches = IngestionBatch.objects.all().order_by("id")
        if batch_id is not None:
            batches = batches.filter(id=batch_id)
            if not batches.exists():
                raise CommandError(f"Batch {batch_id} does not exist.")

        grand_marked = 0
        grand_collapsed = 0
        grand_files_removed = 0

        for batch in batches:
            total_before = batch.items.count()
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    f"Batch {batch.id} — {total_before} rows "
                    f"(dest: {batch.destination_source_name or '—'})"
                )
            )
            marked = self._mark_phase(batch, hamming, dry_run)
            grand_marked += marked
            self.stdout.write(f"  mark:     {marked} pending/accepted row(s) flagged as near-dupe rejects")

            if mode == "collapse":
                collapsed, files_removed = self._collapse_phase(
                    batch, hamming, dry_run, delete_files
                )
                grand_collapsed += collapsed
                grand_files_removed += files_removed
                self.stdout.write(
                    f"  collapse: {collapsed} rejected row(s) removed, "
                    f"{files_removed} file(s) deleted from disk"
                )

        tail = "(dry run — no writes)" if dry_run else "done"
        self.stdout.write(self.style.SUCCESS(
            f"Total: marked={grand_marked}, collapsed={grand_collapsed}, "
            f"files_removed={grand_files_removed} {tail}"
        ))

    # ------------------------------------------------------------------ #
    #  Phase 1: mark pending/accepted near-dupes as rejected              #
    # ------------------------------------------------------------------ #
    def _mark_phase(self, batch: IngestionBatch, hamming: int, dry_run: bool) -> int:
        if dry_run:
            # Dry-run counts without mutating: just run the same clustering logic
            # but without the .save() calls. We re-implement it inline rather
            # than branching the shared helper, because the helper writes
            # decisions as it goes.
            base = batch.items.filter(
                media_kind="image", decision__in=["pending", "accepted"]
            ).annotate(px=F("width") * F("height")).order_by("-px", "id")
            kept: list[tuple[int, str]] = []
            would_reject = 0
            for item in base:
                ph = _dedupe_phash_for_item(item)
                if not ph:
                    continue
                if any(_hamming_distance_hex(ph, kph) <= hamming for _, kph in kept):
                    would_reject += 1
                else:
                    kept.append((item.id, ph))
            return would_reject
        # Live run — use the existing helper so the notes match the UI path.
        result = _dedupe_batch_visual_crops(batch, max_hamming=hamming)
        return int(result.get("rejected", 0))

    # ------------------------------------------------------------------ #
    #  Phase 2: collapse rejected near-dupes down to one per cluster      #
    # ------------------------------------------------------------------ #
    def _collapse_phase(
        self,
        batch: IngestionBatch,
        hamming: int,
        dry_run: bool,
        delete_files: bool,
    ) -> tuple[int, int]:
        """
        Collapse rejected rows that are near-duplicates of anything already in
        the batch (accepted, pending, or a larger reject).

        We seed the ``kept`` cluster-set with phashes of accepted/pending rows
        FIRST — otherwise a ``rejected`` row that was merely a duplicate of an
        existing ``accepted`` row stays in the grid as clutter, which is the
        exact pattern that made batch 141 look repetitive even after the first
        collapse pass (the rejected copy never matched another reject so it
        was retained). Only rejected rows are ever deleted; accepted/pending
        rows are untouchable here.
        """
        # Step 1: seed kept[] with accepted/pending phashes so rejected
        # near-duplicates of them get collapsed as well.
        anchor_base = batch.items.filter(
            media_kind="image", decision__in=["accepted", "pending"]
        )
        kept: list[tuple[int, str]] = []
        for item in anchor_base.only("id", "file_path", "media_kind"):
            ph = _dedupe_phash_for_item(item)
            if ph:
                kept.append((item.id, ph))

        # Step 2: iterate rejected rows largest-first and drop anything that
        # lands within `hamming` of an already-kept phash.
        base = (
            batch.items.filter(media_kind="image", decision="rejected")
            .annotate(px=F("width") * F("height"))
            .order_by("-px", "id")
        )
        to_drop: list[IngestionItem] = []
        for item in base:
            ph = _dedupe_phash_for_item(item)
            if not ph:
                continue
            if any(_hamming_distance_hex(ph, kph) <= hamming for _, kph in kept):
                to_drop.append(item)
            else:
                kept.append((item.id, ph))

        if dry_run:
            # Report what would be deleted; don't touch disk or DB.
            would_remove_files = 0
            if delete_files:
                for item in to_drop:
                    rel = (item.file_path or "").strip().replace("\\", "/")
                    if rel:
                        full = INGESTION_MEDIA_ROOT / rel
                        if full.exists():
                            would_remove_files += 1
            return len(to_drop), would_remove_files

        files_removed = 0
        if delete_files:
            for item in to_drop:
                rel = (item.file_path or "").strip().replace("\\", "/")
                if not rel:
                    continue
                full = INGESTION_MEDIA_ROOT / rel
                try:
                    if full.exists():
                        full.unlink()
                        files_removed += 1
                except Exception as exc:
                    self.stderr.write(
                        f"  ! failed to delete {full}: {exc}"
                    )

        doomed_ids = [item.id for item in to_drop]
        # Chunk the delete so we never send a huge IN (...) to the DB.
        collapsed = 0
        for i in range(0, len(doomed_ids), 500):
            chunk = doomed_ids[i : i + 500]
            n, _ = IngestionItem.objects.filter(id__in=chunk).delete()
            collapsed += n
        return collapsed, files_removed
