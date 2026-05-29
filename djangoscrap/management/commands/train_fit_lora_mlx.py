"""Run an MLX-LM LoRA fine-tune of the be-me dataset.

This command shells out to `mlx_lm.lora` (the standard MLX-LM training entry).
If MLX-LM is not installed, it prints clear install instructions and writes a
stub training report describing what *would* run. The pipeline does NOT crash
if MLX is unavailable — the rest of the system still works in template mode.
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from django.core.management.base import BaseCommand

from djangoscrap.imageboard_ingestion import storage

DEFAULT_CORPUS = "fourchan_fit"
DEFAULT_BASE_MODEL = "mlx-community/Qwen2.5-3B-Instruct-4bit"


class Command(BaseCommand):
    help = "Fine-tune a /fit/ LoRA adapter via MLX-LM (Apple Silicon)."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", default=DEFAULT_CORPUS)
        parser.add_argument("--dataset", required=True,
                            help="Path to the SFT JSONL dataset.")
        parser.add_argument("--base-model", default=DEFAULT_BASE_MODEL)
        parser.add_argument("--output", default=None,
                            help="Adapter output dir. Default: corpora/.../models/fit_be_me_lora")
        parser.add_argument("--iters", type=int, default=800)
        parser.add_argument("--batch-size", type=int, default=1)
        parser.add_argument("--learning-rate", type=float, default=2e-5)
        parser.add_argument("--lora-rank", type=int, default=16)
        parser.add_argument("--dry-run", action="store_true",
                            help="Print the command and exit without training.")
        parser.add_argument("--corpus-root", default=None)

    def handle(self, *args, **opts):
        if opts.get("corpus_root"):
            storage.set_corpus_root(opts["corpus_root"])
        dataset = Path(opts["dataset"]).resolve()
        if not dataset.exists():
            self.stderr.write(self.style.ERROR(f"Dataset not found: {dataset}"))
            sys.exit(2)

        out_dir = Path(opts["output"]) if opts["output"] else storage.model_dir(opts["corpus"])
        out_dir.mkdir(parents=True, exist_ok=True)

        is_apple_silicon = (platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64"))
        if not is_apple_silicon:
            self.stderr.write(self.style.WARNING(
                "MLX requires Apple Silicon. On Intel Mac / Linux, train on a "
                "cloud GPU and import the adapter manually. Skipping."
            ))
            self._write_report(out_dir, opts, status="skipped_not_apple_silicon")
            return

        # MLX-LM exposes its trainer as `python -m mlx_lm.lora`.
        cmd = [
            sys.executable, "-m", "mlx_lm.lora",
            "--train",
            "--model", str(opts["base_model"]),
            "--data", str(dataset.parent),  # mlx_lm.lora expects a directory with train/valid jsonl
            "--iters", str(opts["iters"]),
            "--batch-size", str(opts["batch_size"]),
            "--learning-rate", str(opts["learning_rate"]),
            "--num-layers", str(opts["lora_rank"]),
            "--adapter-path", str(out_dir),
        ]

        # mlx_lm.lora wants train.jsonl / valid.jsonl in --data dir. Stage them.
        self._stage_dataset(dataset, dataset.parent)

        self.stdout.write(self.style.NOTICE("Train command:"))
        self.stdout.write("  " + " ".join(cmd))

        if opts["dry_run"]:
            self._write_report(out_dir, opts, status="dry_run", cmd=cmd)
            return

        try:
            import mlx_lm  # noqa: F401
        except Exception as e:
            self.stderr.write(self.style.WARNING(
                f"mlx_lm not installed ({e}). Install with:\n"
                f"  pip install mlx-lm\n"
                f"Then re-run this command. Writing stub report and exiting."
            ))
            self._write_report(out_dir, opts, status="mlx_lm_not_installed", cmd=cmd)
            return

        t0 = time.time()
        try:
            res = subprocess.run(cmd, check=False)
            status = "ok" if res.returncode == 0 else f"failed:exit={res.returncode}"
        except Exception as e:
            status = f"exception:{e}"
        self._write_report(out_dir, opts, status=status, cmd=cmd, seconds=time.time() - t0)
        self.stdout.write(self.style.SUCCESS(f"Training run complete (status={status}). Adapter dir: {out_dir}"))

    @staticmethod
    def _stage_dataset(dataset: Path, dest: Path) -> None:
        """mlx_lm.lora expects {train,valid}.jsonl inside --data dir."""
        train_dst = dest / "train.jsonl"
        valid_dst = dest / "valid.jsonl"
        if train_dst.exists() and valid_dst.exists():
            return
        lines = dataset.read_text(encoding="utf-8").splitlines()
        n = len(lines)
        cut = max(1, int(n * 0.95))
        train_dst.write_text("\n".join(lines[:cut]) + ("\n" if lines[:cut] else ""), encoding="utf-8")
        valid_dst.write_text("\n".join(lines[cut:]) + ("\n" if lines[cut:] else ""), encoding="utf-8")

    @staticmethod
    def _write_report(out_dir: Path, opts: dict, *, status: str, cmd=None, seconds: float | None = None) -> None:
        report = {
            "status": status,
            "base_model": opts["base_model"],
            "iters": opts["iters"],
            "batch_size": opts["batch_size"],
            "learning_rate": opts["learning_rate"],
            "lora_rank": opts["lora_rank"],
            "dataset": str(opts["dataset"]),
            "output_dir": str(out_dir),
            "cmd": list(cmd) if cmd else None,
            "seconds": seconds,
        }
        (out_dir / "training_report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
