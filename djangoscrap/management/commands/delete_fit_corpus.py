"""Delete the on-disk imageboard corpus for a given corpus directory."""
from __future__ import annotations

import shutil

from django.core.management.base import BaseCommand

from djangoscrap.imageboard_ingestion import storage

DEFAULT_CORPUS = "fourchan_fit"


class Command(BaseCommand):
    help = "Delete the corpus directory. Dry-run by default; --confirm to proceed."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", default=DEFAULT_CORPUS)
        parser.add_argument("--confirm", action="store_true")
        parser.add_argument("--corpus-root", default=None)

    def handle(self, *args, **opts):
        if opts.get("corpus_root"):
            storage.set_corpus_root(opts["corpus_root"])
        root = storage.source_root(opts["corpus"])
        if not root.exists():
            self.stdout.write(f"Nothing to delete: {root} does not exist.")
            return
        if not opts["confirm"]:
            self.stdout.write(f"Would delete: {root}")
            self.stdout.write("Re-run with --confirm to proceed.")
            return
        shutil.rmtree(root)
        self.stdout.write(self.style.SUCCESS(f"Deleted: {root}"))
