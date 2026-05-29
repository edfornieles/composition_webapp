"""Export the /fit/ corpus manifest as a flat CSV for ImagePlot 2.2.

One row per usable (image-bearing, safety-passing) post. CSV is suitable for
loading directly into Lev Manovich's ImagePlot, or any Bokeh / browser-based
image-collection visualiser that consumes a CSV manifest.

Example:
    python manage.py export_fit_imageplot_dataset \\
        --corpus fourchan_fit \\
        --corpus-root "/Volumes/oom/imageboard_corpora" \\
        --output "/Volumes/oom/imageboard_corpora/fourchan_fit/imageplot_exports/fit_imageplot.csv" \\
        --image-mode thumbnails

This is for visual-research / clustering, NOT for model training.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from djangoscrap.imageboard_ingestion import imageplot_export, storage

DEFAULT_CORPUS = "fourchan_fit"


class Command(BaseCommand):
    help = "Export an ImagePlot-compatible CSV from a /fit/-style imageboard manifest."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", default=DEFAULT_CORPUS,
                            help="Corpus key (default: fourchan_fit)")
        parser.add_argument("--corpus-root", default=None,
                            help="Override IMAGEBOARD_CORPUS_ROOT for this run.")
        parser.add_argument("--output", required=True,
                            help="Path to write the CSV file.")
        parser.add_argument("--image-mode",
                            choices=["thumbnails", "full", "both"],
                            default="thumbnails",
                            help="Which path columns to populate / verify.")
        parser.add_argument("--limit", type=int, default=0,
                            help="Optional cap on rows for quick smoke tests (0 = all).")

    def handle(self, *args, **opts):
        if opts.get("corpus_root"):
            storage.set_corpus_root(opts["corpus_root"])
        corpus = opts["corpus"]
        output = Path(opts["output"]).expanduser()
        image_mode = opts["image_mode"]
        limit = int(opts.get("limit") or 0)

        manifest = storage.manifest_path(corpus)
        if not manifest.exists():
            raise CommandError(f"Manifest not found: {manifest}")

        def _stream():
            n = 0
            for row in imageplot_export.iter_rows(corpus):
                # Honour image_mode by clearing the unwanted column for cleanliness;
                # the file references on disk are still under the corpus root.
                if image_mode == "thumbnails":
                    row["local_image_path"] = ""
                elif image_mode == "full":
                    row["local_thumbnail_path"] = ""
                yield row
                n += 1
                if limit and n >= limit:
                    return

        written = imageplot_export.write_csv(_stream(), output)
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {written} rows → {output}"
        ))
        self.stdout.write(
            f"  corpus       : {corpus}\n"
            f"  corpus_root  : {storage.CORPUS_ROOT}\n"
            f"  image_mode   : {image_mode}\n"
            f"  manifest_in  : {manifest}\n"
        )
