"""Ingest one imageboard board into the local-first chan corpus.

    python manage.py ingest_imageboard_board \\
        --source fourchan --board fit \\
        --limit-threads 50 \\
        --download-originals --download-thumbnails \\
        --root /Volumes/oom/chan_corpus
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from djangoscrap.imageboard_ingestion import chan_ingest, config


class Command(BaseCommand):
    help = "Ingest a chan board into the local-first chan corpus on /Volumes/oom."

    def add_arguments(self, parser):
        parser.add_argument("--source", default="fourchan",
                            choices=["fourchan"],
                            help="Imageboard source (only fourchan wired up so far).")
        parser.add_argument("--board", required=True,
                            help="Board slug, e.g. fit, ck, p, mu, lit.")
        parser.add_argument("--root", default=None,
                            help="Override CHAN_CORPUS_ROOT for this run.")
        parser.add_argument("--limit-threads", type=int, default=0,
                            help="Cap on threads walked (0 = full catalog).")
        parser.add_argument("--download-originals", action="store_true", default=True)
        parser.add_argument("--no-originals", dest="download_originals", action="store_false")
        parser.add_argument("--download-thumbnails", action="store_true", default=True)
        parser.add_argument("--no-thumbnails", dest="download_thumbnails", action="store_false")
        parser.add_argument("--force", action="store_true",
                            help="Re-download/overwrite even if files already exist.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Walk the API but write nothing to disk.")
        parser.add_argument("--min-image-dim", type=int, default=0,
                            help="Skip images whose declared width OR height is below this "
                                 "(checked before download). 0 = no filter. e.g. 350.")

    def handle(self, *args, **opts):
        root = config.resolve_root(opts.get("root"))
        config.ensure_layout(root, opts["source"], opts["board"])
        self.stdout.write(self.style.NOTICE(f"chan corpus root: {root}"))

        stats = chan_ingest.ingest_board(
            source=opts["source"],
            board=opts["board"],
            root=root,
            limit_threads=int(opts["limit_threads"] or 0),
            download_originals=bool(opts["download_originals"]),
            download_thumbnails=bool(opts["download_thumbnails"]),
            force=bool(opts["force"]),
            dry_run=bool(opts["dry_run"]),
            min_image_dim=int(opts.get("min_image_dim") or 0),
            log=self.stdout.write,
        )
        self.stdout.write(self.style.SUCCESS(
            f"ingest stats: posts_new={stats['posts_new']}  images_new={stats['images_new']}  "
            f"quarantine={stats['images_quarantined']}  corrupt={stats['images_corrupt']}  "
            f"errors={stats['errors']}"
        ))
