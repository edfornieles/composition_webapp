"""Build the SFT training dataset (be-me thoughts)."""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from djangoscrap.imageboard_ingestion import profile_builder, storage, training_dataset

DEFAULT_CORPUS = "fourchan_fit"


class Command(BaseCommand):
    help = "Build a SFT JSONL training dataset for the /fit/ be-me LoRA."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", default=DEFAULT_CORPUS)
        parser.add_argument("--profile", choices=["clean", "contaminated"], default="contaminated",
                            help="Which profile JSON to use as state seed pool.")
        parser.add_argument("--examples", type=int, default=10000)
        parser.add_argument("--mode", choices=["templates", "teacher_model"], default="templates")
        parser.add_argument("--output", default=None,
                            help="Output JSONL path (default under corpus training/).")
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--corpus-root", default=None)

    def handle(self, *args, **opts):
        if opts.get("corpus_root"):
            storage.set_corpus_root(opts["corpus_root"])
        # Load profile if available; else build from manifest.
        profile_name = "fit_profile_contaminated.json" if opts["profile"] == "contaminated" else "fit_profile.json"
        profile_path = storage.profile_path(opts["corpus"], profile_name)
        if profile_path.exists():
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        else:
            profile = profile_builder.build_profile(
                opts["corpus"],
                mode="contaminated_art" if opts["profile"] == "contaminated" else "clean",
            )

        out_path = Path(opts["output"]) if opts["output"] else None
        out = training_dataset.write_dataset(
            source_key=opts["corpus"],
            n_examples=int(opts["examples"]),
            mode=opts["mode"],
            out_path=out_path,
            profile=profile,
        )
        self.stdout.write(self.style.SUCCESS(f"Wrote training dataset → {out}"))
