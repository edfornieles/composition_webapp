"""Generate a sample /fit/ be-me thought end-to-end."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from djangoscrap.imageboard_ingestion import local_fit_model, storage, thought_grounding

DEFAULT_CORPUS = "fourchan_fit"


class Command(BaseCommand):
    help = "Generate a /fit/ be-me thought and print it with grounding debug."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", default=DEFAULT_CORPUS)
        parser.add_argument("--query", default=None,
                            help="Free-form query line (overrides scenario state).")
        parser.add_argument("--screen", default=None,
                            help="Screen preset id (e.g. 7_night_kitchen).")
        parser.add_argument("--mode", choices=["clean", "contaminated_art"], default="contaminated_art")
        parser.add_argument("--contamination-intensity", type=float, default=0.75)
        parser.add_argument("--require-grounding", action="store_true", default=True)
        parser.add_argument("--no-require-grounding", dest="require_grounding", action="store_false")
        parser.add_argument("--runtime", default=None,
                            help="Override FIT_LOCAL_RUNTIME (mlx/ollama/llamacpp/template).")
        parser.add_argument("--debug", action="store_true")
        parser.add_argument("--corpus-root", default=None)

    def handle(self, *args, **opts):
        if opts.get("corpus_root"):
            storage.set_corpus_root(opts["corpus_root"])
        ctx = None
        if opts["query"]:
            ctx = {"object": opts["query"]}
        result = thought_grounding.generate_fit_be_me_thought(
            current_context=ctx,
            source_key=opts["corpus"],
            screen_id=opts["screen"],
            n_fragments=8,
            contamination_intensity=float(opts["contamination_intensity"]),
            require_grounding=bool(opts["require_grounding"]),
            runtime=opts["runtime"],
        )

        runtime_used = result.get("runtime") or local_fit_model.active_runtime()
        self.stdout.write("Persona: /fit/ — be me body discipline")
        self.stdout.write(
            f"Grounding: {result['grounding_status']}, "
            f"{len(result.get('internal_grounding') or [])} fragments"
        )
        self.stdout.write(f"Runtime: {runtime_used}")
        self.stdout.write("")
        if result["thought"]:
            self.stdout.write("Thought:")
            self.stdout.write(result["thought"])
        else:
            self.stdout.write(self.style.WARNING(
                f"No thought produced (status={result['grounding_status']}). "
                f"flags={result.get('safety_flags')}"
            ))

        self.stdout.write("\nState:")
        for k, v in (result.get("persona_state") or {}).items():
            self.stdout.write(f"  {k}: {v}")

        if opts["debug"]:
            self.stdout.write("\nGrounding debug:")
            for f in (result.get("internal_grounding") or [])[:8]:
                excerpt = (f.get("safe_excerpt") or "")[:160]
                self.stdout.write(f"  [{f.get('chunk_id')}] sim={f.get('sim')} :: {excerpt}")
            self.stdout.write("\nFull result:")
            self.stdout.write(json.dumps({**result, "internal_grounding": [
                {"chunk_id": f.get("chunk_id"), "sim": f.get("sim"),
                 "safe_excerpt": (f.get("safe_excerpt") or "")[:160]}
                for f in (result.get("internal_grounding") or [])
            ]}, indent=2, ensure_ascii=False))
