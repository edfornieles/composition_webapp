#!/usr/bin/env python3
"""Build (or update) one bucket from a spec.

Reads a BucketSpec, runs each configured source, downloads candidates,
dedups against existing manifest entries, applies quality + aesthetic
filters, writes UUID-named files to the bucket folder, and appends to
the manifest.

Resumable by design: re-run on the same bucket dir to top up to
target_count. Already-fetched URLs / sha256 / phash are skipped.

Usage:
    # First build of a bucket from a spec file:
    python scripts/bucket_build.py --spec scripts/specs/goth_darkforest_landscapes.json

    # Update an existing bucket (reads spec from inside the bucket folder):
    python scripts/bucket_build.py --update /Volumes/Oom/aggregation/got/goth_darkforest_landscapes

    # Specify a different got/ root:
    python scripts/bucket_build.py --spec ... --root /custom/got

Quality filters: minimum long-edge resolution, max aspect ratio, optional
content-type sniff. Aesthetic gating (CLIP cosine vs centroid built from
seed_image_paths or include_terms) is applied if `aesthetic_threshold > 0`
and sentence-transformers/CLIP is available.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Allow `import bucket_spec` from the same scripts dir
sys.path.insert(0, str(Path(__file__).parent))


# Auto-load env from the project's .env files (no python-dotenv dependency).
def _load_dotenv():
    candidates = [
        Path(__file__).parent.parent / "contracts" / ".env",
        Path(__file__).parent.parent / ".env",
    ]
    for p in candidates:
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


_load_dotenv()

from bucket_spec import (
    BucketSpec, ManifestRow,
    GOT_ROOT_DEFAULT,
    write_spec, read_spec, has_spec,
    append_manifest, known_hashes, manifest_count,
    new_uuid_stem,
)
from bucket_sources import fetch_source, IMAGE_EXTS, VIDEO_EXTS

UA = "compositions-bucket-builder/0.1"


# ---------------------------------------------------------------------------
# Optional: perceptual hash (for cross-source dedup of near-duplicates)
# ---------------------------------------------------------------------------

def _phash(image_bytes: bytes) -> str:
    try:
        from PIL import Image
        import imagehash
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        return str(imagehash.phash(im))
    except Exception:
        return ""


def _probe_image(image_bytes: bytes) -> tuple[int, int, str]:
    """Return (width, height, mime). Returns (0,0,'') on failure."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(image_bytes))
        mime = (im.format or "").lower()
        if mime == "jpeg": mime = "image/jpeg"
        elif mime == "png": mime = "image/png"
        elif mime == "webp": mime = "image/webp"
        elif mime == "gif": mime = "image/gif"
        return im.width, im.height, mime
    except Exception:
        return 0, 0, ""


# ---------------------------------------------------------------------------
# Optional: CLIP aesthetic gate
# ---------------------------------------------------------------------------

class AestheticGate:
    """Cosine-similarity gate against a centroid embedding.

    Centroid built from seed_image_paths if any exist, otherwise from
    include_terms. Threshold compared against per-candidate cosine.
    """
    def __init__(self, spec: BucketSpec):
        self.spec = spec
        self.enabled = spec.aesthetic_threshold > 0
        self._model = None
        self._centroid = None

    def _ensure(self):
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            print("  [gate] sentence-transformers not installed; aesthetic gate disabled.", flush=True)
            self.enabled = False
            return
        # Use the multimodal CLIP model so we can compare image and text.
        self._model = SentenceTransformer("clip-ViT-B-32")
        # Build centroid: prefer seed images on disk (canonical bucket aesthetic),
        # then explicit seed_image_paths, then include_terms as fallback.
        anchors = []
        from PIL import Image
        # Auto-discover .seeds/ in the bucket folder
        from bucket_spec import GOT_ROOT_DEFAULT
        seeds_dir = self.spec.bucket_dir(GOT_ROOT_DEFAULT) / ".seeds"
        if seeds_dir.is_dir():
            for p in sorted(seeds_dir.iterdir()):
                if p.name.startswith("._") or p.is_dir():
                    continue
                if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic", ".tiff", ".bmp"}:
                    continue
                try:
                    anchors.append(Image.open(p).convert("RGB"))
                except Exception:
                    continue
        # Explicit absolute paths in spec
        if not anchors and self.spec.seed_image_paths:
            for p in self.spec.seed_image_paths:
                try:
                    anchors.append(Image.open(p).convert("RGB"))
                except Exception:
                    continue
        # Text fallback
        if not anchors and self.spec.include_terms:
            anchors = list(self.spec.include_terms)
            print(f"  [gate] using TEXT centroid from {len(anchors)} terms (no seed images found).", flush=True)
        else:
            print(f"  [gate] using IMAGE centroid from {len(anchors)} seed image(s).", flush=True)
        if not anchors:
            self.enabled = False
            return
        emb = self._model.encode(anchors, convert_to_numpy=True, normalize_embeddings=True)
        import numpy as np
        self._centroid = emb.mean(axis=0)
        n = (self._centroid ** 2).sum() ** 0.5
        if n > 0:
            self._centroid = self._centroid / n

    def score(self, image_bytes: bytes) -> float:
        if not self.enabled:
            return 1.0
        self._ensure()
        if not self.enabled or self._centroid is None:
            return 1.0
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            emb = self._model.encode([im], convert_to_numpy=True, normalize_embeddings=True)[0]
            return float(emb @ self._centroid)
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _http_get_bytes(url: str, timeout: int = 25) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if len(data) < 1024:
                return None
            return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Build orchestrator
# ---------------------------------------------------------------------------

def build_bucket(spec: BucketSpec, root: Path, *, dry_run: bool = False) -> dict:
    bucket_dir = spec.bucket_dir(root)
    bucket_dir.mkdir(parents=True, exist_ok=True)
    write_spec(spec, root)

    existing_sha, existing_phash = known_hashes(bucket_dir)
    seen_urls: set[str] = set()
    for row in manifest_rows(bucket_dir):
        seen_urls.add(row.get("source_url") or "")
        seen_urls.add(row.get("file_uuid") or "")

    have_now = manifest_count(bucket_dir)
    need = max(spec.target_count - have_now, 0)
    print(f"\nBucket {spec.name}: have {have_now:,}, target {spec.target_count:,}, need {need:,}", flush=True)
    if need == 0:
        return {"added": 0, "skipped": 0, "rejected": 0}

    gate = AestheticGate(spec)
    n_added = 0
    n_skip = 0
    n_reject = 0

    for src in spec.sources:
        if n_added >= need:
            break
        print(f"  [source] {src.type}  params={list(src.params.keys())}", flush=True)
        try:
            iterator = fetch_source(src.type, src.params, allow_video=spec.allow_video)
        except ValueError as e:
            print(f"    skipping: {e}", flush=True)
            continue
        for cand in iterator:
            if n_added >= need:
                break
            url = cand.get("url") or ""
            if not url or url in seen_urls:
                n_skip += 1
                continue
            seen_urls.add(url)

            # extension policy
            ext = (cand.get("ext_hint") or os.path.splitext(url)[1] or ".jpg").lower()
            if ext not in IMAGE_EXTS and not (spec.allow_video and ext in VIDEO_EXTS):
                n_reject += 1
                continue

            if dry_run:
                print(f"    DRY {url}", flush=True)
                n_added += 1
                continue

            data = _http_get_bytes(url)
            if not data:
                n_reject += 1
                continue

            sha = hashlib.sha256(data).hexdigest()
            if sha in existing_sha:
                n_skip += 1
                continue

            # quality + size gate
            w = h = 0
            mime = ""
            phash_hex = ""
            if ext in IMAGE_EXTS:
                w, h, mime = _probe_image(data)
                if w and h:
                    long_edge = max(w, h)
                    if long_edge < spec.min_resolution:
                        n_reject += 1
                        continue
                    aspect = max(w, h) / max(min(w, h), 1)
                    if aspect > spec.max_aspect_ratio:
                        n_reject += 1
                        continue
                phash_hex = _phash(data)
                if phash_hex and phash_hex in existing_phash:
                    n_skip += 1
                    continue

            # aesthetic gate
            score = 1.0
            if gate.enabled and ext in IMAGE_EXTS:
                score = gate.score(data)
                if score < spec.aesthetic_threshold:
                    n_reject += 1
                    continue

            uuid_stem = new_uuid_stem()
            fname = f"{uuid_stem}{ext}"
            out_path = bucket_dir / fname
            out_path.write_bytes(data)

            row = ManifestRow(
                file_uuid=uuid_stem, filename=fname,
                source_type=cand.get("source_type", src.type),
                source_url=cand.get("source_url") or url,
                source_subkey=cand.get("source_subkey", ""),
                sha256=sha, phash=phash_hex,
                width=w, height=h, bytes=len(data), mime=mime,
                fetched_at=int(time.time()),
                aesthetic_score=round(score, 4),
                notes=cand.get("title", "")[:240],
            )
            append_manifest(bucket_dir, row)
            existing_sha.add(sha)
            if phash_hex:
                existing_phash.add(phash_hex)
            n_added += 1
            if n_added % 25 == 0:
                print(f"    +{n_added}  (skip {n_skip}, reject {n_reject})", flush=True)

    spec.last_run_at = int(time.time())
    write_spec(spec, root)
    return {"added": n_added, "skipped": n_skip, "rejected": n_reject}


def manifest_rows(bucket_dir: Path):
    from bucket_spec import read_manifest
    yield from read_manifest(bucket_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--spec", help="Path to a spec JSON to build into a bucket.")
    g.add_argument("--update", help="Path to an existing bucket dir to top up.")
    ap.add_argument("--root", default=str(GOT_ROOT_DEFAULT))
    ap.add_argument("--dry-run", action="store_true",
                    help="Enumerate candidates without downloading.")
    args = ap.parse_args()

    root = Path(args.root)
    if args.spec:
        spec = BucketSpec.from_dict(json.loads(Path(args.spec).read_text()))
    else:
        bdir = Path(args.update)
        if not has_spec(bdir):
            sys.exit(f"No .spec.json in {bdir}; can't update.")
        spec = read_spec(bdir)

    stats = build_bucket(spec, root, dry_run=args.dry_run)
    print(f"\nDone. {json.dumps(stats)}", flush=True)


if __name__ == "__main__":
    main()
