"""Django management command: eval the imageboard LM voice.

Thin wrapper around djangoscrap.imageboard_ingestion.eval_fit_voice.run_eval.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand

from djangoscrap.imageboard_ingestion import eval_fit_voice


class Command(BaseCommand):
    help = "Run the imageboard LM eval: side-by-side template/base/LoRA outputs."

    def add_arguments(self, parser):
        parser.add_argument("--out-dir", type=Path, default=None,
                            help="Output dir; default var/evals/eval_<ts>.")
        parser.add_argument("--backends", type=str, default="",
                            help="Comma-separated subset (template,mlx,ollama,llamacpp).")
        parser.add_argument("--skip-chat", action="store_true",
                            help="Skip chat cases (faster smoke).")
        parser.add_argument("--persona-slug", type=str,
                            default=eval_fit_voice.DEFAULT_FIT_PERSONA_SLUG)
        parser.add_argument("--source-key", type=str, default="fourchan_fit")

    def handle(self, *args, **opts):
        backends_filter = None
        if opts.get("backends"):
            backends_filter = [b.strip() for b in opts["backends"].split(",") if b.strip()]

        summary = eval_fit_voice.run_eval(
            out_dir=opts.get("out_dir"),
            backends_filter=backends_filter,
            skip_chat=bool(opts.get("skip_chat")),
            persona_slug=opts.get("persona_slug"),
            source_key=opts.get("source_key"),
        )

        self.stdout.write(self.style.SUCCESS(f"Eval finished at {summary['started_at']}"))
        self.stdout.write(f"  out_dir:   {summary['out_dir']}")
        self.stdout.write(f"  rows:      {summary['row_count']}")
        self.stdout.write(f"  available: {', '.join(summary['available_backends']) or 'none'}")
        if summary["unavailable_backends"]:
            self.stdout.write(f"  missing:   {', '.join(summary['unavailable_backends'])}")
        if summary["errors"]:
            self.stdout.write(self.style.WARNING(f"  errors:    {len(summary['errors'])}"))
        self.stdout.write(f"  report:    {summary['report_path']}")
