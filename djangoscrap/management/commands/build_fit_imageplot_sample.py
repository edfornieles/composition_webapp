"""Build a sampled ImagePlot dataset for the /fit/ corpus.

Writes a CSV with N randomly-sampled rows alongside an `images/` directory
containing symlinks (or copies) to the referenced files, so the dataset can
be loaded into ImagePlot or shipped to a remote workstation as one folder.

Example:
    python manage.py build_fit_imageplot_sample \\
        --corpus fourchan_fit \\
        --sample-size 1000 \\
        --image-mode thumbnails \\
        --corpus-root "/Volumes/oom/imageboard_corpora"

Outputs to:
    <corpus_root>/<corpus>/imageplot_exports/
        fit_imageplot_sample.csv
        images/
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from djangoscrap.imageboard_ingestion import imageplot_export, storage

DEFAULT_CORPUS = "fourchan_fit"


class Command(BaseCommand):
    help = "Build a sampled ImagePlot CSV + images/ dir for the /fit/ corpus."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", default=DEFAULT_CORPUS)
        parser.add_argument("--corpus-root", default=None)
        parser.add_argument("--sample-size", type=int, default=1000,
                            help="Number of rows to sample (0 = all usable).")
        parser.add_argument("--image-mode",
                            choices=["thumbnails", "full", "both"],
                            default="thumbnails")
        parser.add_argument("--copy-images", action="store_true",
                            help="Copy image files instead of symlinking (for tools "
                                 "that don't follow symlinks, or for transporting off-disk).")
        parser.add_argument("--output-dir", default=None,
                            help="Override output directory "
                                 "(defaults to <corpus_root>/<corpus>/imageplot_exports/).")
        parser.add_argument("--seed", type=int, default=1337)

    def handle(self, *args, **opts):
        if opts.get("corpus_root"):
            storage.set_corpus_root(opts["corpus_root"])
        corpus = opts["corpus"]

        manifest = storage.manifest_path(corpus)
        if not manifest.exists():
            raise CommandError(f"Manifest not found: {manifest}")

        out_dir = (Path(opts["output_dir"]).expanduser()
                   if opts.get("output_dir")
                   else storage.source_root(corpus) / "imageplot_exports")
        out_dir.mkdir(parents=True, exist_ok=True)
        output_csv = out_dir / "fit_imageplot_sample.csv"
        images_dir = out_dir / "images"

        n = imageplot_export.write_sample(
            corpus,
            output_csv=output_csv,
            images_dir=images_dir,
            sample_size=int(opts["sample_size"] or 0),
            image_mode=opts["image_mode"],
            copy_images=bool(opts["copy_images"]),
            seed=int(opts["seed"]),
        )
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {n} rows → {output_csv}"
        ))
        self.stdout.write(
            f"  images_dir   : {images_dir}\n"
            f"  image_mode   : {opts['image_mode']}\n"
            f"  copy_images  : {bool(opts['copy_images'])}\n"
            f"  sample_size  : {opts['sample_size']}\n"
        )
