"""Backfill historical /fit/ images from desuarchive.

Walks the FoolFuuka index API page-by-page (each page = ~15 threads, recent
first) and saves thumbnails (or full images, optionally) into the same corpus
layout used by the live 4chan scraper.

Resumable: writes a state file with the last page processed and a set of
already-saved thread nums. Dedups by checking whether the destination file
already exists, so re-running is cheap.

Example:

    python manage.py scrape_fit_archive \
        --corpus-root /Volumes/oom/imageboard_corpora \
        --pages 200 --rps 0.7 --max-images 10000 --thumbnails

Defaults are intentionally conservative.
"""
from __future__ import annotations

import json
import time
import urllib.error
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from djangoscrap.imageboard_ingestion import desuarchive_client, storage

DEFAULT_CORPUS = "fourchan_fit"
DEFAULT_BOARD = "fit"


class Command(BaseCommand):
    help = "Backfill /fit/ historical thumbnails (and optionally full images) from desuarchive."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", default=DEFAULT_CORPUS)
        parser.add_argument("--board", default=DEFAULT_BOARD)
        parser.add_argument("--corpus-root", default=None)
        parser.add_argument("--start-page", type=int, default=1)
        parser.add_argument("--pages", type=int, default=50,
                            help="Number of index pages to walk from --start-page.")
        parser.add_argument("--max-images", type=int, default=10000,
                            help="Stop once this many new images are saved.")
        parser.add_argument("--max-thread-posts", type=int, default=400,
                            help="Skip threads with more replies than this (rare giants).")
        parser.add_argument("--rps", type=float, default=0.7,
                            help="Max requests per second (combined index/thread/image).")
        parser.add_argument("--thumbnails", action="store_true", default=True,
                            help="Download thumbnails (default).")
        parser.add_argument("--full-images", action="store_true",
                            help="Also download full-size images (much larger).")
        parser.add_argument("--state-name", default="archive_state.json",
                            help="Filename inside the corpus root for resumable state.")
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **opts):
        if opts.get("corpus_root"):
            storage.set_corpus_root(opts["corpus_root"])
        desuarchive_client.set_rate(opts["rps"])

        corpus = opts["corpus"]
        board = opts["board"]
        storage.ensure_layout(corpus)
        thumb_root = storage.source_root(corpus) / "thumbnails" / board
        thumb_root.mkdir(parents=True, exist_ok=True)
        img_root = storage.source_root(corpus) / "images" / board
        img_root.mkdir(parents=True, exist_ok=True)

        state_path: Path = storage.source_root(corpus) / opts["state_name"]
        state = {"processed_threads": [], "last_page": 0, "stats": {}}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        processed = set(state.get("processed_threads") or [])

        max_images = int(opts["max_images"])
        thumbnails = bool(opts["thumbnails"])
        full_images = bool(opts["full_images"])
        if not thumbnails and not full_images:
            raise CommandError("Pass --thumbnails or --full-images (or both).")

        new_images = 0
        skipped_existing = 0
        errors = 0
        threads_seen = 0
        threads_processed = 0
        start_page = int(opts["start_page"])
        end_page = start_page + int(opts["pages"])

        t0 = time.time()
        self.stdout.write(self.style.NOTICE(
            f"Backfilling /{board}/ from desuarchive: pages {start_page}..{end_page-1}, "
            f"target ≤{max_images} new images, rps={opts['rps']}"
        ))
        self.stdout.write(f"  thumbnails dir: {thumb_root}")
        if full_images:
            self.stdout.write(f"  images dir: {img_root}")

        try:
            for page in range(start_page, end_page):
                if new_images >= max_images:
                    break
                try:
                    index = desuarchive_client.fetch_index(board, page)
                except Exception as e:
                    self.stderr.write(self.style.WARNING(f"page {page} index error: {e}; skipping page"))
                    errors += 1
                    continue

                thread_nums = list(desuarchive_client.iter_thread_nums(index))
                threads_seen += len(thread_nums)
                if not opts["quiet"]:
                    self.stdout.write(f"page {page}: {len(thread_nums)} threads")

                for tnum in thread_nums:
                    if new_images >= max_images:
                        break
                    if tnum in processed:
                        continue
                    try:
                        thread = desuarchive_client.fetch_thread(board, tnum)
                    except urllib.error.HTTPError as e:
                        if e.code == 404:
                            processed.add(tnum)
                            continue
                        self.stderr.write(self.style.WARNING(f"thread {tnum} http {e.code}"))
                        errors += 1
                        continue
                    except Exception as e:
                        self.stderr.write(self.style.WARNING(f"thread {tnum} fetch error: {e}"))
                        errors += 1
                        continue

                    posts_with_media = list(desuarchive_client.iter_posts_with_media(thread))
                    if len(posts_with_media) > opts["max_thread_posts"]:
                        posts_with_media = posts_with_media[:opts["max_thread_posts"]]

                    for post in posts_with_media:
                        if new_images >= max_images:
                            break
                        media_orig = post.get("media_orig") or ""
                        preview_orig = post.get("preview_orig") or ""
                        if thumbnails and post.get("thumb_link") and preview_orig:
                            dest = thumb_root / preview_orig
                            if dest.exists():
                                skipped_existing += 1
                            else:
                                try:
                                    data = desuarchive_client.fetch_bytes(post["thumb_link"])
                                    dest.write_bytes(data)
                                    new_images += 1
                                except Exception as e:
                                    errors += 1
                                    if not opts["quiet"]:
                                        self.stderr.write(self.style.WARNING(
                                            f"thumb {preview_orig} error: {e}"))
                        if full_images and post.get("media_link") and media_orig:
                            dest = img_root / media_orig
                            if dest.exists():
                                skipped_existing += 1
                            else:
                                try:
                                    data = desuarchive_client.fetch_bytes(post["media_link"])
                                    dest.write_bytes(data)
                                    new_images += 1
                                except Exception as e:
                                    errors += 1

                    processed.add(tnum)
                    threads_processed += 1

                    if threads_processed % 10 == 0:
                        self._save_state(state_path, processed, page, new_images,
                                         skipped_existing, errors, t0,
                                         threads_seen, threads_processed)
                        if not opts["quiet"]:
                            self.stdout.write(
                                f"  progress: page={page} threads={threads_processed} "
                                f"new_images={new_images} skipped={skipped_existing} "
                                f"errors={errors} elapsed={int(time.time()-t0)}s"
                            )

                state["last_page"] = page
                self._save_state(state_path, processed, page, new_images,
                                 skipped_existing, errors, t0,
                                 threads_seen, threads_processed)

        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\ninterrupted; saving state."))
        finally:
            self._save_state(state_path, processed, state.get("last_page", 0),
                             new_images, skipped_existing, errors, t0,
                             threads_seen, threads_processed)

        elapsed = int(time.time() - t0)
        self.stdout.write(self.style.SUCCESS(
            f"\nDone. new_images={new_images} skipped_existing={skipped_existing} "
            f"errors={errors} threads={threads_processed} elapsed={elapsed}s "
            f"(state: {state_path})"
        ))

    @staticmethod
    def _save_state(path: Path, processed: set, last_page: int, new_images: int,
                    skipped: int, errors: int, t0: float,
                    threads_seen: int, threads_processed: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "processed_threads": sorted(processed),
            "last_page": last_page,
            "stats": {
                "new_images": new_images,
                "skipped_existing": skipped,
                "errors": errors,
                "elapsed_seconds": int(time.time() - t0),
                "threads_seen": threads_seen,
                "threads_processed": threads_processed,
            },
        }, indent=2), encoding="utf-8")
