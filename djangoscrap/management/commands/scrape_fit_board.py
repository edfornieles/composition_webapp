"""Scrape 4chan /fit/ catalog + threads into the corpus manifest.

Usage:
    python manage.py scrape_fit_board \
        --source 4chan --board fit \
        --max-threads 80 --max-posts 15000 \
        --download-media --download-thumbnails \
        --bucket 4chan__fit__body_discipline

The --bucket flag tags the records with a source-bucket label. The on-disk
corpus directory is `corpora/imageboards/<corpus>/` (default 'fourchan_fit'),
keyed by --corpus.

Idempotent: posts already present in the manifest (by post_uid) are skipped.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from djangoscrap.imageboard_ingestion import (
    adapters,
    fourchan_client as api,
    storage,
)

DEFAULT_CORPUS = "fourchan_fit"
DEFAULT_BUCKET = "4chan__fit__body_discipline"


class Command(BaseCommand):
    help = "Scrape 4chan /fit/ posts into the imageboard corpus manifest."

    def add_arguments(self, parser):
        parser.add_argument("--source", default="4chan")
        parser.add_argument("--board", default="fit")
        parser.add_argument("--corpus", default=DEFAULT_CORPUS,
                            help="Corpus directory under corpora/imageboards/")
        parser.add_argument("--bucket", default=DEFAULT_BUCKET,
                            help="Bucket label written into each record (source_key).")
        parser.add_argument("--max-threads", type=int, default=80)
        parser.add_argument("--max-posts", type=int, default=15000,
                            help="Hard cap across all threads in this run.")
        parser.add_argument("--max-posts-per-thread", type=int, default=300)
        parser.add_argument("--download-media", action="store_true")
        parser.add_argument("--download-thumbnails", action="store_true")
        parser.add_argument("--text-only", action="store_true",
                            help="Skip all media downloads (overrides thumbnail/full flags).")
        parser.add_argument("--thumbnails-only", action="store_true",
                            help="Equivalent to --download-thumbnails without --download-media.")
        parser.add_argument("--corpus-root", default=None,
                            help="Override IMAGEBOARD_CORPUS_ROOT for this run "
                                 "(e.g. /Volumes/oom/imageboard_corpora).")
        parser.add_argument("--force-low-disk", action="store_true",
                            help="Skip the 50GB free-space check before media scrape.")

    def handle(self, *args, **opts):
        if opts["source"] != "4chan":
            raise CommandError("Only --source 4chan is implemented in this version.")
        board = opts["board"]
        corpus = opts["corpus"]
        bucket = opts["bucket"]
        max_threads = int(opts["max_threads"])
        max_posts = int(opts["max_posts"])
        max_per_thread = int(opts["max_posts_per_thread"])
        text_only = bool(opts.get("text_only"))
        thumbs_only = bool(opts.get("thumbnails_only"))
        do_full = bool(opts["download_media"]) and not text_only
        do_thumbs = (bool(opts["download_thumbnails"]) or thumbs_only or do_full) and not text_only

        if opts.get("corpus_root"):
            storage.set_corpus_root(opts["corpus_root"])

        # Sanity-check the corpus root before we start hammering the network.
        root = storage.CORPUS_ROOT
        self.stdout.write(f"Corpus root: {root}")
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise CommandError(f"cannot create corpus root {root}: {e}")

        try:
            from django.conf import settings as _s
            base_dir = Path(getattr(_s, "BASE_DIR", Path.cwd())).resolve()
            if str(root.resolve()).startswith(str(base_dir)):
                self.stderr.write(self.style.WARNING(
                    f"WARNING: corpus root is inside the git repo ({root}). "
                    f"Use --corpus-root /Volumes/oom/imageboard_corpora to keep "
                    f"scraped data off-repo."
                ))
        except Exception:
            pass

        if do_full and not opts.get("force_low_disk"):
            try:
                free_gb = shutil.disk_usage(root).free / (1024 ** 3)
                if free_gb < 50.0:
                    raise CommandError(
                        f"Only {free_gb:.1f}GB free on {root}; full media scrape "
                        f"refused. Pass --force-low-disk to override or use "
                        f"--thumbnails-only / --text-only."
                    )
            except FileNotFoundError:
                pass

        storage.ensure_layout(corpus)
        seen = storage.load_seen_post_ids(corpus)
        self.stdout.write(f"Manifest already contains {len(seen)} posts. Fetching catalog…")

        try:
            pages, _ = api.fetch_catalog(board)
        except api.FourChanError as e:
            raise CommandError(f"catalog fetch failed: {e}")

        threads = list(api.iter_catalog_threads(pages))
        threads.sort(key=lambda pt: pt[1].get("replies", 0), reverse=True)
        threads = threads[:max_threads]
        self.stdout.write(f"Selected {len(threads)} threads (top by replies). "
                          f"Hard cap: {max_posts} new posts.")

        new_posts = 0
        new_images = 0
        for _, thread_summary in threads:
            if new_posts >= max_posts:
                break
            thread_no = int(thread_summary.get("no") or 0)
            if not thread_no:
                continue
            try:
                payload, _ = api.fetch_thread(board, thread_no)
            except api.FourChanError as e:
                self.stderr.write(f"thread {thread_no}: {e}")
                continue

            storage.save_raw_thread(corpus, board, thread_no, payload)

            posts = (payload.get("posts") or [])[:max_per_thread]
            for i, post in enumerate(posts):
                if new_posts >= max_posts:
                    break
                rec = adapters.build_record(
                    source_key=bucket, board=board, thread_no=thread_no,
                    post=post, is_op=(i == 0),
                )
                if rec is None:
                    continue
                if rec["post_uid"] in seen:
                    continue
                # Hard-block at ingest: never even keep the record for the worst stuff
                policy = rec.get("use_policy") or {}
                if not policy.get("allowed_for_profile", True):
                    # Track only an opaque count of excluded posts; do not store text.
                    continue
                storage.append_manifest(corpus, rec)
                seen.add(rec["post_uid"])
                new_posts += 1
                if rec["plain_text"]:
                    storage.save_post_text(corpus, board, thread_no, rec["post_no"], rec["plain_text"])

                if rec.get("image"):
                    img = rec["image"]
                    try:
                        if do_thumbs:
                            thumb_bytes = api.fetch_image(board, img["tim"], img["ext"], thumbnail=True)
                            storage.save_image(corpus, board, img["tim"], img["ext"], thumb_bytes, thumbnail=True)
                            new_images += 1
                        if do_full:
                            full_bytes = api.fetch_image(board, img["tim"], img["ext"], thumbnail=False)
                            storage.save_image(corpus, board, img["tim"], img["ext"], full_bytes, thumbnail=False)
                            new_images += 1
                    except api.FourChanError as e:
                        self.stderr.write(f"image {img['tim']}{img['ext']}: {e}")

            self.stdout.write(f"  thread {thread_no}: ingested through post #{posts[-1].get('no') if posts else '-'} "
                              f"(running total: {new_posts})")

        self.stdout.write(self.style.SUCCESS(
            f"Done. New posts: {new_posts}. New image files: {new_images}. "
            f"Manifest: {storage.manifest_path(corpus)}"
        ))
