"""Build the retrieval index over the corpus."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from djangoscrap.imageboard_ingestion import retrieval, storage

DEFAULT_CORPUS = "fourchan_fit"


class Command(BaseCommand):
    help = "Build and persist the local retrieval index."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", default=DEFAULT_CORPUS)
        parser.add_argument("--mode", choices=["clean", "contaminated_art"], default="contaminated_art")
        parser.add_argument("--backend", choices=["tfidf"], default="tfidf",
                            help="Only TF-IDF is implemented in this version.")
        parser.add_argument("--corpus-root", default=None)

    def handle(self, *args, **opts):
        if opts.get("corpus_root"):
            storage.set_corpus_root(opts["corpus_root"])
        ret = retrieval.build_retrieval(opts["corpus"], mode=opts["mode"])
        out = retrieval.save(ret, opts["corpus"])
        self.stdout.write(self.style.SUCCESS(
            f"Retrieval index built (mode={opts['mode']}): {ret.n_chunks} chunks → {out}"
        ))
