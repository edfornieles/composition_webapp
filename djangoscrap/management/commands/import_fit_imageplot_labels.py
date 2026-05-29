"""Import manual ImagePlot cluster/screen/bucket labels back into the corpus.

After clustering inside ImagePlot, save the labelled CSV (with
`cluster_label_manual`, `screen_assignment`, `composition_bucket` columns
filled in) and feed it back through this command. The labels are written as
JSONL to:

    <corpus_root>/<corpus>/manifests/imageplot_labels.jsonl

Downstream code (composition source-bucket assignment, screen scheduling, AI
thought-context selection) can then join this label file against the
manifest by `id` / `post_id` / `thread_id`.

Example:
    python manage.py import_fit_imageplot_labels \\
        --labels-csv "/Volumes/oom/imageboard_corpora/fourchan_fit/imageplot_exports/fit_imageplot_labelled.csv" \\
        --corpus fourchan_fit \\
        --corpus-root "/Volumes/oom/imageboard_corpora"
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from djangoscrap.imageboard_ingestion import imageplot_export, storage

DEFAULT_CORPUS = "fourchan_fit"


class Command(BaseCommand):
    help = "Import manual ImagePlot labels into the corpus' label JSONL."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", default=DEFAULT_CORPUS)
        parser.add_argument("--corpus-root", default=None)
        parser.add_argument("--labels-csv", required=True,
                            help="Path to the labelled CSV exported from ImagePlot.")

    def handle(self, *args, **opts):
        if opts.get("corpus_root"):
            storage.set_corpus_root(opts["corpus_root"])
        corpus = opts["corpus"]
        labels_csv = Path(opts["labels_csv"]).expanduser()
        if not labels_csv.exists():
            raise CommandError(f"Labels CSV not found: {labels_csv}")

        out = imageplot_export.import_labels(labels_csv, corpus)
        # Count lines for the user
        n = sum(1 for _ in out.open("r", encoding="utf-8"))
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {n} label record{'s' if n != 1 else ''} → {out}"
        ))
