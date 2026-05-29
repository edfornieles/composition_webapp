"""Text-only multi-board imageboard scraper (desuarchive / FoolFuuka).

Walks many boards in round-robin, fetches threads via the FoolFuuka JSON API,
strips post text only (no media downloads), and appends to per-board JSONL
shards. Designed to run in parallel with the image-focused scrape_fit_archive
command without colliding on rate limits.

Resumable: writes a state file with per-board last_page and the set of
processed thread nums.

Example:

    python manage.py scrape_imageboards_text \\
        --corpus-root /Volumes/oom/imageboard_corpora \\
        --boards pol,r9k,lit,sci,fit,x,mu,tv,v,vg,biz \\
        --pages 1000 --rps 1.4

Excludes 8chan/8kun (legal hazard) and /b/ (illegal-content risk) by default.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from djangoscrap.imageboard_ingestion import desuarchive_client, storage

DEFAULT_CORPUS = "imageboard_text"
DEFAULT_HOST = "https://desuarchive.org"
# desuarchive's actual board coverage (verified). Other boards (/pol/, /sci/,
# /v/, /biz/, /jp/) live on different archives — handled by separate runs.
DESUARCHIVE_BOARDS = (
    "a", "aco", "an", "c", "cgl", "ck", "co", "fit", "g", "his", "int",
    "k", "lit", "m", "mlp", "mlpol", "mu", "q", "qa", "r9k", "soc",
    "sp", "tg", "trv", "tv", "vip", "vp", "vt", "wsg", "x", "y",
)
ARCHIVE_4PLEBS_BOARDS = ("adv", "f", "hr", "o", "pol", "s4s", "x")
WAROSU_BOARDS = ("g", "lit", "sci", "jp", "biz")
DEFAULT_BOARDS = DESUARCHIVE_BOARDS

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+\n")


def _clean_comment(html_text: str) -> str:
    if not html_text:
        return ""
    text = html_text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return _WS_RE.sub("\n", text).strip()


def _iter_all_posts(thread: dict):
    if not thread:
        return
    for outer_key, payload in thread.items():
        op = payload.get("op") or {}
        if op:
            yield op, True
        for post in (payload.get("posts") or {}).values():
            if post:
                yield post, False


class Command(BaseCommand):
    help = "Text-only multi-board scraper for FoolFuuka archives (desuarchive)."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", default=DEFAULT_CORPUS)
        parser.add_argument("--corpus-root", default=None)
        parser.add_argument("--host", default=DEFAULT_HOST,
                            help="FoolFuuka archive host (e.g. https://desuarchive.org or https://archive.4plebs.org).")
        parser.add_argument("--boards", default=",".join(DEFAULT_BOARDS),
                            help="Comma-separated board codes (without slashes).")
        parser.add_argument("--start-page", type=int, default=1)
        parser.add_argument("--pages", type=int, default=1000,
                            help="Pages to walk per board.")
        parser.add_argument("--max-posts", type=int, default=2_000_000,
                            help="Stop once this many posts have been written across all boards.")
        parser.add_argument("--rps", type=float, default=1.4,
                            help="Max requests per second (text-only is cheap, can go faster than image scrape).")
        parser.add_argument("--state-name", default="text_scrape_state.json")
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **opts):
        if opts.get("corpus_root"):
            storage.set_corpus_root(opts["corpus_root"])
        desuarchive_client.set_rate(opts["rps"])

        corpus = opts["corpus"]
        storage.ensure_layout(corpus)

        boards = [b.strip() for b in opts["boards"].split(",") if b.strip()]
        if not boards:
            raise CommandError("No boards specified.")
        if "b" in boards or "8kun" in boards or "8chan" in boards:
            raise CommandError("/b/, 8chan, and 8kun are excluded for legal/content-risk reasons.")

        text_root: Path = storage.source_root(corpus) / "text"
        text_root.mkdir(parents=True, exist_ok=True)
        state_path: Path = storage.source_root(corpus) / opts["state_name"]

        state = {"boards": {}, "stats": {}}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        for b in boards:
            state["boards"].setdefault(b, {"processed_threads": [], "last_page": 0, "posts_written": 0})

        max_posts = int(opts["max_posts"])
        start_page = int(opts["start_page"])
        end_page = start_page + int(opts["pages"])

        total_posts = sum((state["boards"][b].get("posts_written") or 0) for b in boards)
        errors = 0
        threads_processed = 0
        t0 = time.time()

        self.stdout.write(self.style.NOTICE(
            f"Text scrape: corpus={corpus} boards={boards} pages={start_page}..{end_page-1} "
            f"max_posts={max_posts} rps={opts['rps']}"
        ))
        self.stdout.write(f"  text dir: {text_root}")
        self.stdout.write(f"  state: {state_path}")

        try:
            for page in range(start_page, end_page):
                if total_posts >= max_posts:
                    break
                for board in boards:
                    if total_posts >= max_posts:
                        break
                    bstate = state["boards"][board]
                    processed = set(bstate.get("processed_threads") or [])
                    if page <= int(bstate.get("last_page") or 0):
                        continue
                    try:
                        index = desuarchive_client.fetch_index(board, page, host=opts["host"])
                    except urllib.error.HTTPError as e:
                        if e.code == 404:
                            if not opts["quiet"]:
                                self.stderr.write(self.style.WARNING(f"/{board}/ page {page} 404; skipping board"))
                            bstate["last_page"] = page
                            continue
                        self.stderr.write(self.style.WARNING(f"/{board}/ page {page} http {e.code}; skipping page"))
                        errors += 1
                        continue
                    except Exception as e:
                        self.stderr.write(self.style.WARNING(f"/{board}/ page {page} index error: {e}; skipping"))
                        errors += 1
                        continue

                    thread_nums = list(desuarchive_client.iter_thread_nums(index))
                    if not opts["quiet"]:
                        self.stdout.write(f"/{board}/ page {page}: {len(thread_nums)} threads")

                    shard = text_root / f"{board}.jsonl"
                    with shard.open("a", encoding="utf-8") as fh:
                        for tnum in thread_nums:
                            if total_posts >= max_posts:
                                break
                            if tnum in processed:
                                continue
                            try:
                                thread = desuarchive_client.fetch_thread(board, tnum, host=opts["host"])
                            except urllib.error.HTTPError as e:
                                if e.code == 404:
                                    processed.add(tnum)
                                    continue
                                errors += 1
                                continue
                            except Exception:
                                errors += 1
                                continue

                            written_here = 0
                            for post, is_op in _iter_all_posts(thread):
                                comment = _clean_comment(post.get("comment") or "")
                                if not comment:
                                    continue
                                fh.write(json.dumps({
                                    "board": board,
                                    "thread_num": tnum,
                                    "post_num": post.get("num"),
                                    "timestamp": post.get("timestamp"),
                                    "is_op": is_op,
                                    "name": post.get("name"),
                                    "trip": post.get("trip"),
                                    "subject": post.get("title") or post.get("subject"),
                                    "comment": comment,
                                }, ensure_ascii=False) + "\n")
                                written_here += 1

                            processed.add(tnum)
                            threads_processed += 1
                            total_posts += written_here
                            bstate["posts_written"] = (bstate.get("posts_written") or 0) + written_here

                            if threads_processed % 25 == 0:
                                bstate["processed_threads"] = sorted(processed)
                                bstate["last_page"] = page
                                self._save_state(state_path, state, total_posts, errors, t0, threads_processed)
                                if not opts["quiet"]:
                                    self.stdout.write(
                                        f"  progress: total_posts={total_posts} threads={threads_processed} "
                                        f"errors={errors} elapsed={int(time.time()-t0)}s"
                                    )

                    bstate["processed_threads"] = sorted(processed)
                    bstate["last_page"] = page
                    self._save_state(state_path, state, total_posts, errors, t0, threads_processed)

        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\ninterrupted; saving state."))
        finally:
            self._save_state(state_path, state, total_posts, errors, t0, threads_processed)

        elapsed = int(time.time() - t0)
        self.stdout.write(self.style.SUCCESS(
            f"\nDone. total_posts={total_posts} threads={threads_processed} "
            f"errors={errors} elapsed={elapsed}s (state: {state_path})"
        ))

    @staticmethod
    def _save_state(path: Path, state: dict, total_posts: int, errors: int,
                    t0: float, threads_processed: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        state["stats"] = {
            "total_posts": total_posts,
            "errors": errors,
            "elapsed_seconds": int(time.time() - t0),
            "threads_processed": threads_processed,
        }
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
