"""Ingest many imageboard boards toward an image-count target.

Wraps `chan_ingest.ingest_board` and walks a list of SFW-leaning boards
sequentially. Stops as soon as the cumulative `images_new` count reaches
`--target`, so you can say "give me 10000 images" without picking boards
manually.

The 1 req/sec rate limit is enforced inside fourchan_client, so this
command effectively serialises across boards. Expect throughput on the
order of a few hundred images/min depending on filter strictness.

Boards listed below are chosen for visual-corpus density (lots of OC
images, mostly photographic / illustrative, broadly worksafe). Pass
`--boards a,b,c` to override.

Examples:
    python manage.py ingest_imageboard_all --target 10000 --min-image-dim 350
    python manage.py ingest_imageboard_all --boards fit,ck,p,wg --target 5000
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from djangoscrap.imageboard_ingestion import chan_ingest, config


# Visual-rich, mostly-SFW boards. Order = priority.
DEFAULT_BOARDS = [
    "p",    # photography
    "wg",   # wallpapers / general
    "w",    # anime wallpapers
    "ic",   # artwork / critique
    "fit",  # fitness (already has data, will skip dupes)
    "ck",   # food
    "diy",
    "out",  # outdoors
    "n",    # transportation
    "an",   # animals
    "his",  # history
    "sci",
    "lit",
    "mu",   # music
    "tv",
    "co",   # comics & cartoons
    "v",    # video games (visual-heavy)
    "vp",   # pokemon
    "g",    # technology
    "trv",  # travel
    "fa",   # fashion
    "i",    # oekaki
    "po",   # papercraft / origami
    "toy",
]


class Command(BaseCommand):
    help = "Ingest many imageboard boards until an image-count target is met."

    def add_arguments(self, parser):
        parser.add_argument("--source", default="fourchan", choices=["fourchan"])
        parser.add_argument("--root", default=None,
                            help="Override CHAN_CORPUS_ROOT for this run.")
        parser.add_argument("--boards", default="",
                            help="Comma-separated board slugs (default: built-in list).")
        parser.add_argument("--target", type=int, default=10_000,
                            help="Stop once cumulative new images >= this (0 = no cap).")
        parser.add_argument("--per-board-thread-cap", type=int, default=0,
                            help="Cap threads walked per board (0 = full catalog). "
                                 "Useful to spread coverage across many boards.")
        parser.add_argument("--min-image-dim", type=int, default=350,
                            help="Skip images whose declared min dim is below this.")
        parser.add_argument("--no-thumbnails", dest="download_thumbnails",
                            action="store_false", default=True)
        parser.add_argument("--no-originals", dest="download_originals",
                            action="store_false", default=True)
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        root = config.resolve_root(opts.get("root"))
        boards_arg = (opts.get("boards") or "").strip()
        boards = [b.strip() for b in boards_arg.split(",") if b.strip()] or list(DEFAULT_BOARDS)
        target = int(opts.get("target") or 0)
        per_board_cap = int(opts.get("per_board_thread_cap") or 0)
        min_dim = int(opts.get("min_image_dim") or 0)

        self.stdout.write(self.style.NOTICE(
            f"chan corpus root: {root}\n"
            f"boards: {','.join(boards)}\n"
            f"target images: {target or 'unlimited'} · per-board thread cap: {per_board_cap or 'full catalog'} · "
            f"min_dim: {min_dim}"
        ))

        cumulative = {
            "posts_new": 0, "images_new": 0,
            "images_quarantined": 0, "images_corrupt": 0,
            "images_skipped_too_small": 0, "errors": 0,
        }
        started = time.monotonic()

        for i, board in enumerate(boards, 1):
            if target and cumulative["images_new"] >= target:
                self.stdout.write(self.style.SUCCESS(
                    f"hit target {target} images_new — stopping before /{board}/"))
                break
            remaining = (target - cumulative["images_new"]) if target else None
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n[{i}/{len(boards)}] /{board}/  (have {cumulative['images_new']} new images so far"
                + (f", need {remaining} more" if remaining is not None else "") + ")"
            ))
            config.ensure_layout(root, opts["source"], board)
            try:
                stats = chan_ingest.ingest_board(
                    source=opts["source"],
                    board=board,
                    root=root,
                    limit_threads=per_board_cap,
                    download_originals=bool(opts["download_originals"]),
                    download_thumbnails=bool(opts["download_thumbnails"]),
                    force=bool(opts["force"]),
                    dry_run=bool(opts["dry_run"]),
                    min_image_dim=min_dim,
                    log=self.stdout.write,
                )
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"/{board}/ failed: {e}"))
                cumulative["errors"] += 1
                continue

            for k in cumulative:
                cumulative[k] = cumulative[k] + int(stats.get(k, 0) or 0)

            elapsed = time.monotonic() - started
            rate = cumulative["images_new"] / max(1.0, elapsed)
            self.stdout.write(self.style.SUCCESS(
                f"/{board}/ done · this board images_new={stats.get('images_new', 0)} "
                f"quarantine={stats.get('images_quarantined', 0)} skipped_small={stats.get('images_skipped_too_small', 0)}\n"
                f"cumulative: posts_new={cumulative['posts_new']} images_new={cumulative['images_new']} "
                f"quarantine={cumulative['images_quarantined']} skipped_small={cumulative['images_skipped_too_small']} "
                f"errors={cumulative['errors']} · {rate:.2f} img/s avg over {elapsed:.0f}s"
            ))

        self.stdout.write(self.style.SUCCESS(
            f"\n=== finished sweep ===\n"
            f"  posts_new={cumulative['posts_new']}\n"
            f"  images_new={cumulative['images_new']}\n"
            f"  quarantine={cumulative['images_quarantined']}\n"
            f"  corrupt={cumulative['images_corrupt']}\n"
            f"  skipped_too_small={cumulative['images_skipped_too_small']}\n"
            f"  errors={cumulative['errors']}\n"
            f"  total elapsed: {time.monotonic()-started:.0f}s"
        ))
