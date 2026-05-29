"""Compute OpenCLIP image embeddings for all allowed images in the corpus.

    python manage.py embed_imageboard_images \\
        --root /Volumes/oom/chan_corpus \\
        --source fourchan --board fit \\
        --model openclip --batch-size 32
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from djangoscrap.imageboard_ingestion import config, quality


class Command(BaseCommand):
    help = "Compute local OpenCLIP image embeddings (CPU or GPU)."

    def add_arguments(self, parser):
        parser.add_argument("--root", default=None)
        parser.add_argument("--source", default="fourchan")
        parser.add_argument("--board", required=True)
        parser.add_argument("--model", default="ViT-B-32",
                            help="OpenCLIP model name (default ViT-B-32).")
        parser.add_argument("--pretrained", default="laion2b_s34b_b79k",
                            help="OpenCLIP pretrained tag.")
        parser.add_argument("--batch-size", type=int, default=32)
        parser.add_argument("--device", default=None,
                            help="cpu | cuda | mps. Auto-detected if omitted.")
        parser.add_argument("--force", action="store_true",
                            help="Re-embed images already present in the saved npy/ids.")

    def handle(self, *args, **opts):
        root = config.resolve_root(opts.get("root"))
        config.ensure_layout(root)
        try:
            import torch  # type: ignore
            import open_clip  # type: ignore
            import numpy as np  # type: ignore
            from PIL import Image  # type: ignore
        except ImportError as e:
            raise CommandError(
                f"missing dep: {e.name}. install with: "
                f"pip install open_clip_torch torch numpy Pillow"
            )

        device = opts["device"] or ("cuda" if torch.cuda.is_available()
                                    else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
                                    else "cpu")
        if device == "cpu":
            self.stdout.write(self.style.WARNING("running on CPU — slow; consider mps/cuda"))

        model, _, preprocess = open_clip.create_model_and_transforms(
            opts["model"], pretrained=opts["pretrained"], device=device,
        )
        model.eval()
        model_tag = f"{opts['model']}_{opts['pretrained']}".replace("/", "-")

        npy_path = config.embeddings_npy(root, model_tag, opts["source"], opts["board"])
        ids_path = config.embeddings_ids(root, model_tag, opts["source"], opts["board"])
        npy_path.parent.mkdir(parents=True, exist_ok=True)

        # Resume support
        existing_ids: list[str] = []
        existing_arr = None
        if not opts["force"] and npy_path.exists() and ids_path.exists():
            existing_arr = np.load(npy_path)
            existing_ids = json.loads(ids_path.read_text(encoding="utf-8"))
            self.stdout.write(f"  resume: {len(existing_ids)} already-embedded")

        existing_set = set(existing_ids)
        todo: list[tuple[str, Path]] = []
        for rec in quality.iter_allowed_images(root, source=opts["source"], board=opts["board"]):
            if rec["image_key"] in existing_set:
                continue
            op = Path(rec["original_path"])
            if op.exists():
                todo.append((rec["image_key"], op))

        if not todo:
            self.stdout.write(self.style.SUCCESS("nothing new to embed"))
            return

        self.stdout.write(f"  embedding {len(todo)} image(s) on {device}…")
        out_ids: list[str] = list(existing_ids)
        out_chunks = [existing_arr] if existing_arr is not None else []
        bs = int(opts["batch_size"])

        with torch.no_grad():
            for i in range(0, len(todo), bs):
                batch = todo[i:i + bs]
                tensors = []
                ok_keys = []
                for k, p in batch:
                    try:
                        with Image.open(p) as im:
                            t = preprocess(im.convert("RGB"))
                            tensors.append(t)
                            ok_keys.append(k)
                    except Exception as e:
                        self.stdout.write(f"    skip {k}: {type(e).__name__}: {e}")
                if not tensors:
                    continue
                stack = torch.stack(tensors).to(device)
                feats = model.encode_image(stack)
                feats = feats / feats.norm(dim=-1, keepdim=True)
                out_chunks.append(feats.cpu().numpy())
                out_ids.extend(ok_keys)
                if (i // bs) % 5 == 0:
                    self.stdout.write(f"    {i+len(batch)}/{len(todo)}")

        arr = np.concatenate(out_chunks, axis=0) if len(out_chunks) > 1 else out_chunks[0]
        np.save(npy_path, arr)
        ids_path.write_text(json.dumps(out_ids), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"  saved {arr.shape} → {npy_path}  ({len(out_ids)} ids)"
        ))
