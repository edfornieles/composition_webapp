"""Generate local image captions (BLIP / BLIP-2).

    python manage.py caption_imageboard_images \\
        --root /Volumes/oom/chan_corpus \\
        --source fourchan --board fit \\
        --model blip --batch-size 8
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from djangoscrap.imageboard_ingestion import config, quality


class Command(BaseCommand):
    help = "Caption all allowed images using a local HuggingFace captioning model."

    def add_arguments(self, parser):
        parser.add_argument("--root", default=None)
        parser.add_argument("--source", default="fourchan")
        parser.add_argument("--board", required=True)
        parser.add_argument("--model", default="blip",
                            choices=["blip", "blip2"],
                            help="blip = Salesforce/blip-image-captioning-base (~1GB).")
        parser.add_argument("--batch-size", type=int, default=8)
        parser.add_argument("--device", default=None)
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--max-images", type=int, default=0,
                            help="Cap on number of new captions (0 = all).")

    def handle(self, *args, **opts):
        root = config.resolve_root(opts.get("root"))
        config.ensure_layout(root)
        try:
            import torch  # type: ignore
            from PIL import Image  # type: ignore
            from transformers import (BlipProcessor, BlipForConditionalGeneration,  # type: ignore
                                       Blip2Processor, Blip2ForConditionalGeneration)
        except ImportError as e:
            raise CommandError(
                f"missing dep: {e.name}. install with: "
                f"pip install transformers torch Pillow"
            )

        device = opts["device"] or ("cuda" if torch.cuda.is_available()
                                    else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
                                    else "cpu")
        if opts["model"] == "blip":
            model_id = "Salesforce/blip-image-captioning-base"
            processor = BlipProcessor.from_pretrained(model_id)
            model = BlipForConditionalGeneration.from_pretrained(model_id).to(device).eval()
            caption_model = f"blip:{model_id}"
        else:
            model_id = "Salesforce/blip2-opt-2.7b"
            processor = Blip2Processor.from_pretrained(model_id)
            model = Blip2ForConditionalGeneration.from_pretrained(model_id).to(device).eval()
            caption_model = f"blip2:{model_id}"

        # Load existing captions for resume
        cap_path = config.captions_manifest(root)
        existing: dict[str, dict] = {}
        if cap_path.exists() and not opts["force"]:
            with cap_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        existing[rec["image_key"]] = rec
                    except Exception:
                        pass
        self.stdout.write(f"  resume: {len(existing)} existing captions")

        todo: list[tuple[str, Path]] = []
        for rec in quality.iter_allowed_images(root, source=opts["source"], board=opts["board"]):
            if rec["image_key"] in existing:
                continue
            op = Path(rec["original_path"])
            if op.exists():
                todo.append((rec["image_key"], op))
        if opts["max_images"]:
            todo = todo[: int(opts["max_images"])]
        if not todo:
            self.stdout.write(self.style.SUCCESS("nothing new to caption"))
            return
        self.stdout.write(f"  captioning {len(todo)} image(s) on {device} with {caption_model}…")

        bs = int(opts["batch_size"])
        with cap_path.open("a", encoding="utf-8") as out_f:
            for i in range(0, len(todo), bs):
                batch = todo[i:i + bs]
                imgs = []
                keys = []
                for k, p in batch:
                    try:
                        imgs.append(Image.open(p).convert("RGB"))
                        keys.append(k)
                    except Exception as e:
                        self.stdout.write(f"    skip {k}: {type(e).__name__}: {e}")
                if not imgs:
                    continue
                inputs = processor(imgs, return_tensors="pt").to(device)
                import torch as _t
                with _t.no_grad():
                    out_ids = model.generate(**inputs, max_new_tokens=40)
                texts = processor.batch_decode(out_ids, skip_special_tokens=True)
                for k, t in zip(keys, texts):
                    out_f.write(json.dumps({
                        "image_key": k,
                        "caption": t.strip(),
                        "caption_model": caption_model,
                        "caption_created_at": int(time.time()),
                    }, ensure_ascii=False) + "\n")
                if (i // bs) % 5 == 0:
                    self.stdout.write(f"    {i+len(batch)}/{len(todo)}")

        # Try parquet sibling
        try:
            import pyarrow as pa  # type: ignore
            import pyarrow.parquet as pq  # type: ignore
            rows = []
            with cap_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
            if rows:
                cols = {k: [r.get(k) for r in rows] for k in sorted({k for r in rows for k in r.keys()})}
                pq.write_table(pa.Table.from_pydict(cols), cap_path.with_suffix(".parquet"))
                self.stdout.write(f"  parquet sibling: {cap_path.with_suffix('.parquet')}")
        except Exception:
            pass

        self.stdout.write(self.style.SUCCESS(f"captions appended → {cap_path}"))
