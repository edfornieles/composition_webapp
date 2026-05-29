"""Aggregate fit_profile.json from the manifest's safe/contaminated posts."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from djangoscrap.imageboard_ingestion import profile_builder, storage

DEFAULT_CORPUS = "fourchan_fit"


class Command(BaseCommand):
    help = "Aggregate fit_profile from the manifest. Modes: clean, contaminated_art."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", default=DEFAULT_CORPUS)
        parser.add_argument("--mode", choices=["clean", "contaminated_art"], default="clean")
        parser.add_argument("--corpus-root", default=None)

    def handle(self, *args, **opts):
        if opts.get("corpus_root"):
            storage.set_corpus_root(opts["corpus_root"])
        out = profile_builder.write_profile(opts["corpus"], mode=opts["mode"])
        self.stdout.write(self.style.SUCCESS(f"Wrote profile ({opts['mode']}): {out}"))
