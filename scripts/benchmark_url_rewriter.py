#!/usr/bin/env python
"""
Benchmark for the ingestion URL rewriter.

Runs a representative mixed-URL corpus through the rewriter with and
without the JSON overlay, so we can quantify Phase 1A's impact. The
corpus is synthetic but each URL is a shape we actually see in the
wild (Yandex/Google results, Pinterest pins, Tumblr tag pages,
Instagram CDN, etc.).

Exits 0 in all cases — this is an informational benchmark, not a gate.
Use ``scripts/run_tests.sh`` for gating.

Usage:
    .venv/bin/python scripts/benchmark_url_rewriter.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure project root on sys.path and Django settings configured.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")

from djangoscrap import ingestion_url_rewriter as rw


# Representative corpus — host shapes drawn from batches 1/26/141 plus
# the maxurl project's common-case list. Each line is: raw URL.
CORPUS = [
    # Pinterest thumbnails (very high frequency)
    "https://i.pinimg.com/236x/ab/cd/ef/abc.jpg",
    "https://i.pinimg.com/474x/11/22/33/def.jpg",
    "https://i.pinimg.com/564x/aa/bb/cc/ghi.jpg",
    "https://i.pinimg.com/736x/dd/ee/ff/jkl.jpg",
    # Tumblr (frequent)
    "https://64.media.tumblr.com/abc/s540x810/hash.jpg",
    "https://64.media.tumblr.com/def/s250x400/hash2.jpg",
    "https://64.media.tumblr.com/ghi/s1280/hash3.jpg",  # no match (different shape)
    # Flickr
    "https://live.staticflickr.com/65535/51234_abcdef_m.jpg",
    "https://live.staticflickr.com/65535/51235_abcdef_q.jpg",
    # Wikimedia
    "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/X.jpg/320px-X.jpg",
    "https://upload.wikimedia.org/wikipedia/en/thumb/c/cd/Y.jpg/800px-Y.jpg",
    # Reddit preview
    "https://preview.redd.it/abc.jpg?auto=webp&s=deadbeef",
    # Imgur thumb
    "https://i.imgur.com/abcDEF1b.jpg",
    "https://i.imgur.com/ZYXWVUTs.png",
    # Blogger/Blogspot
    "https://1.bp.blogspot.com/path/s400/image.jpg",
    "https://2.bp.blogspot.com/nothing-to-rewrite/image.jpg",
    # Twitter pbs
    "https://pbs.twimg.com/media/AbCdEf.jpg:large",
    "https://pbs.twimg.com/media/ZzYyXx?format=jpg&name=medium",
    # Facebook fbcdn
    "https://scontent.fbcdn.net/v/t1.0-9/p200x200/12345_foo_n.jpg",
    # Instagram cdninstagram
    "https://scontent.cdninstagram.com/v/t1/s150x150/12345_n.jpg",
    # Shopify
    "https://cdn.shopify.com/s/files/1/0000/0000/products/shirt_small.jpg",
    # Amazon
    "https://images-amazon.com/images/I/foo._SX500_.jpg",
    "https://m.media-amazon.com/images/I/bar._AC_UL480_.jpg",
    # Artstation
    "https://cdnb.artstation.com/p/assets/images/images/000/00/large/foo.jpg",
    # Wix
    "https://static.wixstatic.com/media/abc/v1/fill/w_300,h_200,q_90/photo.jpg",
    # Pixiv
    "https://i.pximg.net/c/360x360_70/img-master/img/2024/01/01/12/00/00/12345_p0_master1200.jpg",
    # Stock watermark hosts (should be dropped)
    "https://www.shutterstock.com/image-photo/foo",
    "https://www.gettyimages.com/detail/photo/bar",
    "https://www.freepik.com/free-photo/baz",
    "https://www.canstockphoto.com/image/12345",
    "https://www.rawpixel.com/image/xyz",
    "https://stocksnap.io/photo/abc",
    # Squarespace
    "https://images.squarespace-cdn.com/content/v1/abc/def.jpg?format=500w",
    # WP Photon
    "https://i0.wp.com/example.com/foo.jpg?resize=300,200",
    # Etsy
    "https://i.etsystatic.com/123/r/il/ab/9876/il_340x270.12345_xyz.jpg",
    # Dribbble
    "https://cdn.dribbble.com/userupload/123/shot_1x.png",
    # 500px
    "https://drscdn.500px.org/photo/12345/v2?webp=1&w=440&h=660",
    # Unsplash
    "https://images.unsplash.com/photo-12345?w=400&q=60",
    # Weibo
    "https://wx1.sinaimg.cn/bmiddle/abcd1234.jpg",
    # postimg / ibb
    "https://i.postimg.cc/thumb/abc/photo.jpg",
    "https://i.ibb.co/abc/foo.th.jpg",
    # Pass-through (no known rule)
    "https://random-blog.example.com/photo.jpg",
    "https://blog.somewhere.net/gallery/2024/image.png",
    "https://cdn.unknown.host/a/b/c.jpg",
]


def run(use_overlay: bool) -> dict:
    if use_overlay:
        os.environ.pop("INGESTION_SITE_RULES", None)  # use default path lookup
        rw.reload_rules()
    else:
        # To disable the overlay we need to bypass _json_rule_candidates'
        # fallback-to-project-root logic. Point the env var at an empty
        # overlay file AND temporarily stash the project-root file during
        # the reload.
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        empty = Path(tmpdir) / "empty.json"
        empty.write_text('{"ignore": [], "upgrade": []}')
        os.environ["INGESTION_SITE_RULES"] = str(empty)
        project_overlay = Path(__file__).resolve().parents[1] / "ingestion_site_rules.json"
        stashed = None
        if project_overlay.exists():
            stashed = project_overlay.with_suffix(".json.benchbak")
            shutil.move(str(project_overlay), str(stashed))
        try:
            rw.reload_rules()
            stats = rw.RewriteStats()
            out = rw.rewrite_candidate_urls(CORPUS, stats=stats)
            return {
                "out_len": len(out),
                "in_len": len(CORPUS),
                "upgraded": stats.upgraded,
                "dropped": stats.dropped,
                "top_rules": sorted(stats.per_rule.items(), key=lambda kv: -kv[1])[:12],
                "summary": stats.as_summary(),
            }
        finally:
            if stashed and stashed.exists():
                shutil.move(str(stashed), str(project_overlay))
            shutil.rmtree(tmpdir, ignore_errors=True)
            os.environ.pop("INGESTION_SITE_RULES", None)
            rw.reload_rules()

    stats = rw.RewriteStats()
    out = rw.rewrite_candidate_urls(CORPUS, stats=stats)
    return {
        "out_len": len(out),
        "in_len": len(CORPUS),
        "upgraded": stats.upgraded,
        "dropped": stats.dropped,
        "top_rules": sorted(stats.per_rule.items(), key=lambda kv: -kv[1])[:12],
        "summary": stats.as_summary(),
    }


def main() -> int:
    print(f"Corpus size: {len(CORPUS)} URLs\n")

    print("=== Baseline (built-in rules only, no overlay) ===")
    baseline = run(use_overlay=False)
    print(f"  out_len: {baseline['out_len']}  upgraded: {baseline['upgraded']}  dropped: {baseline['dropped']}")
    print(f"  summary: {baseline['summary']}")
    print(f"  top rules: {baseline['top_rules']}\n")

    print("=== With project overlay (Phase 1A) ===")
    overlay = run(use_overlay=True)
    print(f"  out_len: {overlay['out_len']}  upgraded: {overlay['upgraded']}  dropped: {overlay['dropped']}")
    print(f"  summary: {overlay['summary']}")
    print(f"  top rules: {overlay['top_rules']}\n")

    delta_upgraded = overlay["upgraded"] - baseline["upgraded"]
    delta_dropped = overlay["dropped"] - baseline["dropped"]
    print("=== Delta ===")
    print(f"  +{delta_upgraded} upgrades")
    print(f"  +{delta_dropped} drops (watermark hosts filtered before download)")
    unchanged = len(CORPUS) - overlay["upgraded"] - overlay["dropped"]
    coverage_pct = 100.0 * (overlay["upgraded"] + overlay["dropped"]) / len(CORPUS)
    print(f"  coverage: {coverage_pct:.1f}% of corpus handled by a rule ({unchanged} passthrough)")

    # Restore default rule state.
    os.environ.pop("INGESTION_SITE_RULES", None)
    rw.reload_rules()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
