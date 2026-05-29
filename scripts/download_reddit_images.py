#!/usr/bin/env python3
"""Download OP images referenced in scraped Reddit JSONL.

Companion to scrape_arctic_shift.py / scrape_reddit_archive.py / scrape_reddit_full.py.
Reads {out}/text/*.jsonl, finds rows with non-empty image_urls (OP posts only),
downloads each URL to {out}/images/{sub}/{post_id}_{idx}.{ext}.

Skips already-downloaded files. Records outcome to {out}/images_log.jsonl
so re-runs are cheap.

Usage:
    python scripts/download_reddit_images.py --out /Volumes/Oom/reddit_women40s_arctic
    python scripts/download_reddit_images.py --out /Volumes/Oom/reddit_women40s_arctic --rps 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UA = "women40s-corpus/0.3 (academic character research)"
TIMEOUT = 25
DEFAULT_EXT = ".jpg"


def download_one(url: str, dest: Path) -> tuple[bool, str]:
    if dest.exists() and dest.stat().st_size > 0:
        return True, "exists"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read()
        if not data:
            return False, "empty"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True, "ok"
    except urllib.error.HTTPError as e:
        return False, f"http_{e.code}"
    except Exception as e:
        return False, f"err_{type(e).__name__}"


def iter_jobs(text_dir: Path, image_dir: Path):
    for shard in sorted(p for p in text_dir.glob("*.jsonl") if not p.name.startswith("._")):
        with shard.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not row.get("is_op"):
                    continue
                urls = row.get("image_urls") or []
                if not urls:
                    continue
                sub = row.get("subreddit") or shard.stem
                post_id = row.get("thread_id") or ""
                if not post_id:
                    continue
                for idx, url in enumerate(urls):
                    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
                    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                        ext = DEFAULT_EXT
                    fname = f"{post_id}_{idx}{ext}"
                    dest = image_dir / sub / fname
                    yield (sub, post_id, idx, url, dest)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="Corpus root (containing text/, images/ written here).")
    ap.add_argument("--rps", type=float, default=2.0, help="Max image downloads per second (across workers).")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="Stop after N downloads (0 = all)")
    args = ap.parse_args()

    out = Path(args.out)
    text_dir = out / "text"
    image_dir = out / "images"
    log_path = out / "images_log.jsonl"

    if not text_dir.exists():
        sys.exit(f"No text dir at {text_dir}")

    image_dir.mkdir(parents=True, exist_ok=True)

    seen_keys: set[str] = set()
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            try:
                e = json.loads(line)
                if e.get("status") in ("ok", "exists"):
                    seen_keys.add(e.get("key", ""))
            except json.JSONDecodeError:
                continue

    log_fh = log_path.open("a", encoding="utf-8")

    gap = (1.0 / args.rps) if args.rps > 0 else 0.0
    last_dispatch = 0.0
    n_ok = n_skip = n_fail = 0

    def submit_throttled(executor, fn, *fnargs):
        nonlocal last_dispatch
        if gap:
            elapsed = time.time() - last_dispatch
            if elapsed < gap:
                time.sleep(gap - elapsed)
        last_dispatch = time.time()
        return executor.submit(fn, *fnargs)

    print(f"Scanning {text_dir} → {image_dir}", flush=True)
    print(f"RPS={args.rps}  workers={args.workers}", flush=True)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = []
        n_dispatched = 0
        for sub, post_id, idx, url, dest in iter_jobs(text_dir, image_dir):
            key = f"{sub}/{post_id}_{idx}"
            if key in seen_keys or (dest.exists() and dest.stat().st_size > 0):
                n_skip += 1
                continue
            fut = submit_throttled(ex, download_one, url, dest)
            futures.append((fut, key, url, dest))
            n_dispatched += 1
            if args.limit and n_dispatched >= args.limit:
                break

            # Drain completed periodically to keep memory bounded
            if len(futures) >= args.workers * 8:
                done = []
                for f, k, u, d in futures:
                    if f.done():
                        ok, status = f.result()
                        log_fh.write(json.dumps({"key": k, "url": u, "status": status}) + "\n")
                        if ok:
                            n_ok += 1
                        else:
                            n_fail += 1
                        done.append((f, k, u, d))
                futures = [t for t in futures if t not in done]

        for fut, key, url, dest in futures:
            ok, status = fut.result()
            log_fh.write(json.dumps({"key": key, "url": url, "status": status}) + "\n")
            if ok:
                n_ok += 1
            else:
                n_fail += 1

    log_fh.close()
    elapsed = int(time.time() - t0)
    print(f"\nDone. ok={n_ok:,}  skip={n_skip:,}  fail={n_fail:,}  elapsed={elapsed}s", flush=True)


if __name__ == "__main__":
    main()
