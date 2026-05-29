"""Build the TF-IDF index over the model-safe corpus."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from djangoscrap.imageboard_ingestion import embeddings

DEFAULT_SOURCE_KEY = "4chan__fit__body_discipline"


class Command(BaseCommand):
    help = "Build and persist the TF-IDF retrieval index from the manifest."

    def add_arguments(self, parser):
        parser.add_argument("--source-key", default=DEFAULT_SOURCE_KEY)

    def handle(self, *args, **opts):
        idx = embeddings.build_index(opts["source_key"])
        out = embeddings.save_index(idx, opts["source_key"])
        self.stdout.write(self.style.SUCCESS(
            f"Indexed {idx.n_docs} documents → {out}"
        ))
