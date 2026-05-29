"""
Pad images in the white_bg source folders to 16:10 landscape with a white
background, without cropping. Writes results to sibling `<source>_landscape/`
folders so originals are untouched.

Usage:
    DB_ENGINE=sqlite python3 scripts/pad_white_bg_to_landscape.py [--folder GRID_ID]

Default folder: 6909cd01d3.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow running from any cwd by ensuring the project root is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")
django.setup()

from django.conf import settings  # noqa: E402
from PIL import Image  # noqa: E402

# Register AVIF read support (no-op if files are jpg/png/webp only).
try:
    import pillow_avif  # noqa: F401
except ImportError:
    pass

from djangoscrap.models import Composition  # noqa: E402


TARGET_W, TARGET_H = 16, 10  # aspect ratio
WHITE_RGB = (255, 255, 255)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".avif"}


def detect_edge_color(im: Image.Image) -> tuple[int, int, int]:
    """Sample the 1-pixel border of the image and return its median RGB.

    For product photography on a solid background, the image edges are almost
    always background, so the median is a robust estimate of the fill color.
    """
    rgb = im.convert("RGB")
    w, h = rgb.size
    # Collect all border pixels: top row, bottom row, left col, right col.
    samples: list[tuple[int, int, int]] = []
    samples.extend(rgb.crop((0, 0, w, 1)).getdata())          # top
    samples.extend(rgb.crop((0, h - 1, w, h)).getdata())      # bottom
    samples.extend(rgb.crop((0, 0, 1, h)).getdata())          # left
    samples.extend(rgb.crop((w - 1, 0, w, h)).getdata())      # right
    # Median per channel — resistant to a few outlier pixels (e.g. logos).
    n = len(samples)
    r_vals = sorted(p[0] for p in samples)
    g_vals = sorted(p[1] for p in samples)
    b_vals = sorted(p[2] for p in samples)
    return (r_vals[n // 2], g_vals[n // 2], b_vals[n // 2])


def discover_white_bg_sources(grid_id: str) -> list[str]:
    sources: set[str] = set()
    for comp in Composition.objects.filter(grid_id=grid_id):
        bgs = comp.background_sources or []
        if isinstance(bgs, str):
            bgs = [bgs]
        for name in bgs:
            if "white_bg" in (name or ""):
                sources.add(name)
    return sorted(sources)


def pad_to_landscape(src: Path, dst: Path, bg_mode: str = "white") -> str:
    """Pad src -> dst to 16:10. bg_mode: 'white' or 'auto' (sample image edges)."""
    with Image.open(src) as im:
        im.load()
        w, h = im.size
        # Compute canvas: new_w/new_h = 16/10, new_w >= w, new_h >= h.
        # If original is too tall for ratio, extend width. Otherwise extend height.
        if w * TARGET_H < h * TARGET_W:
            new_w = -(-h * TARGET_W // TARGET_H)  # ceil division
            new_h = h
        else:
            new_w = w
            new_h = -(-w * TARGET_H // TARGET_W)

        fill_color = WHITE_RGB if bg_mode == "white" else detect_edge_color(im)

        if (new_w, new_h) == (w, h):
            canvas = im.convert("RGB") if im.mode != "RGB" else im.copy()
        else:
            canvas = Image.new("RGB", (new_w, new_h), fill_color)
            paste_x = (new_w - w) // 2
            paste_y = (new_h - h) // 2
            if im.mode == "RGBA":
                # Composite over fill using alpha as mask.
                canvas.paste(im, (paste_x, paste_y), im.split()[-1])
            else:
                canvas.paste(im.convert("RGB"), (paste_x, paste_y))

        dst.parent.mkdir(parents=True, exist_ok=True)
        # AVIF/etc fall back to JPEG output; reads of AVIF require pillow-avif-plugin.
        ext = src.suffix.lower()
        if ext in {".jpg", ".jpeg"}:
            canvas.save(dst, format="JPEG", quality=92, optimize=True)
        elif ext == ".png":
            canvas.save(dst, format="PNG", optimize=True)
        elif ext == ".webp":
            canvas.save(dst, format="WEBP", quality=92, method=4)
        elif ext == ".avif":
            # Most browsers and our composition layer handle JPG fine; AVIF write
            # in PIL needs a plugin. Persist as JPG with a .jpg extension instead.
            dst = dst.with_suffix(".jpg")
            canvas.save(dst, format="JPEG", quality=92, optimize=True)
        else:
            canvas.save(dst)

    suffix = f"  bg=rgb{fill_color}" if bg_mode != "white" else ""
    return f"{w}x{h} -> {new_w}x{new_h}{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", default=None, help="Composition grid_id (discovers all white_bg sources in the folder)")
    parser.add_argument("--source", action="append", default=[], help="Process a specific source folder by name (repeatable)")
    parser.add_argument("--bg", choices=["white", "auto"], default="white",
                        help="Fill color: 'white' (default) or 'auto' (sample image edges)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(settings.LOCAL_SOURCES_ROOT)
    if args.source:
        sources = list(args.source)
    else:
        grid_id = args.folder or "6909cd01d3"
        sources = discover_white_bg_sources(grid_id)
    if not sources:
        target = args.source or args.folder or "6909cd01d3"
        print(f"No sources to process (target: {target})")
        return 1

    print(f"Found {len(sources)} source(s) under {root}:")
    for s in sources:
        print(f"  - {s}")
    print()

    total_done = 0
    total_skipped = 0
    total_failed = 0
    for source in sources:
        src_dir = root / source
        dst_dir = root / f"{source}_landscape"
        if not src_dir.is_dir():
            print(f"[MISS] {src_dir} does not exist; skipping")
            continue
        files = [
            f for f in src_dir.iterdir()
            if f.is_file()
            and f.suffix.lower() in IMAGE_SUFFIXES
            and not f.name.startswith("._")
        ]
        print(f"[{source}] {len(files)} images -> {dst_dir.name}/")
        for src in files:
            dst = dst_dir / src.name
            if dst.exists():
                total_skipped += 1
                continue
            if args.dry_run:
                total_done += 1
                continue
            try:
                msg = pad_to_landscape(src, dst, bg_mode=args.bg)
                total_done += 1
                if total_done % 25 == 0:
                    print(f"  ... {total_done} done ({src.name}: {msg})")
            except Exception as exc:
                total_failed += 1
                print(f"  [FAIL] {src.name}: {exc}")
        print()

    print(f"Done. processed={total_done}  skipped_existing={total_skipped}  failed={total_failed}")
    return 0 if total_failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
