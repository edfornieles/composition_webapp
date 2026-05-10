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
                            help="Comma-separated subset (template,mlx,mlx_no_adapter,ollama,llamacpp).")
        parser.add_argument("--mlx-no-adapter", action="store_true",
                            help="Force-include mlx_no_adapter virtual backend.")
        parser.add_argument("--skip-chat", action="store_true",
                            help="Skip chat cases (faster smoke).")
        parser.add_argument("--debug-prompts", action="store_true",
                            help="Save full prompt/fragments/retry chain per row to <out>/debug/.")
        parser.add_argument("--temperature", type=str, default="",
                            help="Comma-separated sweep temperatures (e.g. 0.55,0.68,0.85).")
        parser.add_argument("--top-p", type=str, default="",
                            help="Comma-separated sweep top_p values (e.g. 0.82,0.88,0.95).")
        parser.add_argument("--sweep-cases", type=str, default="",
                            help="Comma-separated case ids to sweep on.")
        parser.add_argument("--persona-slug", type=str,
                            default=eval_fit_voice.DEFAULT_FIT_PERSONA_SLUG)
        parser.add_argument("--source-key", type=str, default="fourchan_fit")

    def handle(self, *args, **opts):
        backends_filter = None
        if opts.get("backends"):
            backends_filter = [b.strip() for b in opts["backends"].split(",") if b.strip()]
        elif opts.get("mlx_no_adapter"):
            backends_filter = ["template", "mlx", "mlx_no_adapter", "ollama", "llamacpp"]

        sweep_cases = None
        if opts.get("sweep_cases"):
            sweep_cases = [c.strip() for c in opts["sweep_cases"].split(",") if c.strip()]

        summary = eval_fit_voice.run_eval(
            out_dir=opts.get("out_dir"),
            backends_filter=backends_filter,
            skip_chat=bool(opts.get("skip_chat")),
            persona_slug=opts.get("persona_slug"),
            source_key=opts.get("source_key"),
            debug_prompts=bool(opts.get("debug_prompts")),
            sweep_temperatures=eval_fit_voice._parse_floats(opts.get("temperature") or "") or None,
            sweep_top_p=eval_fit_voice._parse_floats(opts.get("top_p") or "") or None,
            sweep_cases=sweep_cases,
        )

        self.stdout.write(self.style.SUCCESS(f"Eval finished at {summary['started_at']}"))
        self.stdout.write(f"  out_dir:   {summary['out_dir']}")
        self.stdout.write(f"  rows:      {summary['row_count']} (main={summary['main_row_count']} sweep={summary['sweep_row_count']})")
        self.stdout.write(f"  available: {', '.join(summary['available_backends']) or 'none'}")
        if summary["unavailable_backends"]:
            self.stdout.write(f"  missing:   {', '.join(summary['unavailable_backends'])}")
        if summary["errors"]:
            self.stdout.write(self.style.WARNING(f"  errors:    {len(summary['errors'])}"))
        self.stdout.write(f"  report:    {summary['report_path']}")
        self.stdout.write(f"  diagnosis: {summary['diagnosis_path']}")
        if summary.get("debug_dir"):
            self.stdout.write(f"  debug:     {summary['debug_dir']}")
