#!/usr/bin/env python3
"""Comprehensive Reddit scraper for women-40s voice + image corpus.

Uses Reddit's public JSON API — no API key required.
Captures text posts, comments, and images (OP images downloaded to disk).
Resumable: skips already-processed threads via state.json.

Usage:
    python scripts/scrape_reddit_full.py \\
        --out /Volumes/Oom/reddit_women40s \\
        --pages 50 --rps 0.5 --images

Output layout:
    {out}/
      text/{subreddit}.jsonl   — posts + comments, one per line
      images/{subreddit}/      — downloaded OP images
      state.json               — resumption state

Post JSONL schema:
    {
      "subreddit": str,
      "thread_id": str,
      "post_id": str,
      "timestamp": int,
      "is_op": bool,
      "subject": str,          # OP title only
      "comment": str,          # body text
      "score": int,
      "source": "reddit",
      "has_image": bool,
      "image_keys": [str],     # relative paths under images/
      "image_urls": [str],     # original URLs
    }
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

TARGET_SUBREDDITS = [
    # Core: hormones, aging, health
    "Menopause",
    "Perimenopause",
    "Hysterectomy",
    "WomensHealth",
    # Generational identity
    "GenX",
    "AskWomenOver40",
    "AskWomenOver30",
    "AskWomen",
    # Parenting & family
    "breakingmom",
    "SAHM",
    "Mommit",
    "workingmoms",
    "SingleMoms",
    "EmptyNesters",
    "stepparents",
    # Relationships & emotional life
    "relationship_advice",
    "Divorce",
    "survivinginfidelity",
    "DeadBedrooms",
    "dating_over_40",
    "widows",
    "raisedbynarcissists",
    "narcissisticabuse",
    "CPTSD",
    # Body & fitness
    "xxfitness",
    "loseit",
    "progresspics",
    "femalefashionadvice",
    # Identity & society
    "TwoXChromosomes",
    "childfree",
    "AITA",
    "TrueOffMyChest",
    # Mental health & candour
    "offmychest",
    "Fibromyalgia",
]

UA = "women40s-corpus/0.2 (academic character research; contact via github)"
_LAST = 0.0
_RPS = 0.5

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _throttle():
    global _LAST
    gap = 1.0 / _RPS
    delta = time.time() - _LAST
    if delta < gap:
        time.sleep(gap - delta)
    _LAST = time.time()


def _get(url: str, timeout: float = 30.0, retries: int = 3) -> dict | list:
    for attempt in range(retries):
        _throttle()
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 90 * (2 ** attempt)
                print(f"  429 rate-limit — sleeping {wait}s", flush=True)
                time.sleep(wait)
                if attempt == retries - 1:
                    raise
            elif e.code == 403:
                raise
            else:
                raise
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(5)
    raise RuntimeError("unreachable")


def _clean(text: str) -> str:
    if not text or text in ("[removed]", "[deleted]"):
        return ""
    return text.strip()


def _extract_image_urls(d: dict) -> list[str]:
    """Extract direct image URLs from a Reddit post data dict."""
    urls: list[str] = []

    # Direct image link
    url = d.get("url") or ""
    if url:
        from urllib.parse import urlparse
        path = urlparse(url).path
        ext = os.path.splitext(path)[1].lower()
        if ext in IMAGE_EXTS or "i.redd.it" in url:
            urls.append(url)

    # Gallery posts
    if d.get("is_gallery") and d.get("media_metadata"):
        for item in d["media_metadata"].values():
            if item.get("status") == "valid" and item.get("s"):
                src = item["s"].get("u") or item["s"].get("gif") or ""
                if src:
                    urls.append(src.replace("&amp;", "&"))

    # Preview image (fallback, lower quality)
    if not urls:
        preview = d.get("preview") or {}
        images = preview.get("images") or []
        if images:
            src = (images[0].get("source") or {}).get("url") or ""
            if src:
                urls.append(src.replace("&amp;", "&"))

    return urls


def _download_image(url: str, dest: Path) -> bool:
    if dest.exists():
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            dest.write_bytes(resp.read())
        return True
    except Exception:
        return False


def fetch_listing(subreddit: str, sort: str, after: str = "", limit: int = 100) -> dict:
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}&raw_json=1"
    if after:
        url += f"&after={after}"
    return _get(url)


def fetch_comments(subreddit: str, thread_id: str) -> list[dict]:
    url = f"https://www.reddit.com/r/{subreddit}/comments/{thread_id}.json?raw_json=1&limit=500"
    data = _get(url)
    if not isinstance(data, list) or len(data) < 2:
        return []
    return _flatten_comments(data[1].get("data", {}).get("children", []))


def _flatten_comments(nodes: list) -> list[dict]:
    out = []
    for node in nodes:
        kind = node.get("kind")
        d = node.get("data") or {}
        if kind == "t1":
            out.append(d)
            replies = d.get("replies")
            if isinstance(replies, dict):
                children = replies.get("data", {}).get("children", [])
                out.extend(_flatten_comments(children))
    return out


def _write(fh, row: dict):
    fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def scrape_subreddit(
    subreddit: str,
    text_dir: Path,
    image_dir: Path,
    state: dict,
    pages: int,
    download_images: bool,
    quiet: bool,
) -> int:
    shard = text_dir / f"{subreddit.lower()}.jsonl"
    img_sub_dir = image_dir / subreddit.lower()
    sub_state = state.setdefault(subreddit, {
        "processed_threads": [],
        "posts_written": 0,
    })
    processed = set(sub_state.get("processed_threads") or [])
    written = 0

    if download_images:
        img_sub_dir.mkdir(parents=True, exist_ok=True)

    sorts = ["hot", "top", "new"]

    with shard.open("a", encoding="utf-8") as fh:
        for sort in sorts:
            after = ""
            for page in range(pages):
                try:
                    listing = fetch_listing(subreddit, sort, after=after)
                except Exception as e:
                    if not quiet:
                        print(f"  [{subreddit}/{sort} p{page}] error: {e}", flush=True)
                    break

                children = listing.get("data", {}).get("children", [])
                if not children:
                    break

                for child in children:
                    d = child.get("data") or {}
                    thread_id = d.get("id")
                    if not thread_id or thread_id in processed:
                        continue

                    title = _clean(d.get("title") or "")
                    selftext = _clean(d.get("selftext") or "")
                    if not title:
                        processed.add(thread_id)
                        continue

                    op_body = f"{title}\n\n{selftext}".strip() if selftext else title

                    # Images from OP
                    image_urls = _extract_image_urls(d)
                    image_keys: list[str] = []
                    if download_images and image_urls:
                        for idx, img_url in enumerate(image_urls):
                            from urllib.parse import urlparse
                            ext = os.path.splitext(urlparse(img_url).path)[1].lower() or ".jpg"
                            fname = f"{thread_id}_{idx}{ext}"
                            dest = img_sub_dir / fname
                            if _download_image(img_url, dest):
                                image_keys.append(f"{subreddit.lower()}/{fname}")

                    _write(fh, {
                        "subreddit": subreddit.lower(),
                        "thread_id": thread_id,
                        "post_id": d.get("name") or f"t3_{thread_id}",
                        "timestamp": int(d.get("created_utc") or 0),
                        "is_op": True,
                        "subject": title,
                        "comment": op_body,
                        "score": int(d.get("score") or 0),
                        "source": "reddit",
                        "has_image": bool(image_urls),
                        "image_keys": image_keys,
                        "image_urls": image_urls,
                    })
                    written += 1

                    # Comments
                    try:
                        comments = fetch_comments(subreddit, thread_id)
                        for c in comments:
                            body = _clean(c.get("body") or "")
                            if not body:
                                continue
                            _write(fh, {
                                "subreddit": subreddit.lower(),
                                "thread_id": thread_id,
                                "post_id": c.get("name") or c.get("id") or "",
                                "timestamp": int(c.get("created_utc") or 0),
                                "is_op": False,
                                "subject": "",
                                "comment": body,
                                "score": int(c.get("score") or 0),
                                "source": "reddit",
                                "has_image": False,
                                "image_keys": [],
                                "image_urls": [],
                            })
                            written += 1
                    except Exception as e:
                        if not quiet:
                            print(f"    comment fetch error {thread_id}: {e}", flush=True)

                    processed.add(thread_id)
                    fh.flush()

                after = listing.get("data", {}).get("after") or ""
                if not after:
                    break

                if not quiet and (page + 1) % 5 == 0:
                    print(f"  r/{subreddit} {sort} p{page+1}  written={written}", flush=True)

    sub_state["processed_threads"] = sorted(processed)
    sub_state["posts_written"] = (sub_state.get("posts_written") or 0) + written
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="/Volumes/Oom/reddit_women40s", help="Output root directory.")
    parser.add_argument("--subreddits", default=",".join(TARGET_SUBREDDITS),
                        help="Comma-separated subreddit names.")
    parser.add_argument("--pages", type=int, default=50,
                        help="Listing pages per sort per subreddit (100 threads/page).")
    parser.add_argument("--rps", type=float, default=0.5,
                        help="Max requests per second.")
    parser.add_argument("--images", action="store_true",
                        help="Download OP images to images/ subdirectory.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    global _RPS
    _RPS = args.rps

    out = Path(args.out)
    text_dir = out / "text"
    image_dir = out / "images"
    text_dir.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"

    state: dict = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            pass

    subreddits = [s.strip() for s in args.subreddits.split(",") if s.strip()]
    total = 0
    t0 = time.time()

    print(f"Output: {out}", flush=True)
    print(f"Subreddits ({len(subreddits)}): {', '.join(subreddits)}", flush=True)
    print(f"Images: {'yes' if args.images else 'no'}", flush=True)
    print(flush=True)

    for sub in subreddits:
        if not args.quiet:
            print(f"r/{sub}", flush=True)
        try:
            n = scrape_subreddit(
                sub, text_dir, image_dir, state,
                pages=args.pages,
                download_images=args.images,
                quiet=args.quiet,
            )
            total += n
            if not args.quiet:
                print(f"  → {n:,} posts written", flush=True)
        except KeyboardInterrupt:
            print("\nInterrupted — saving state.", flush=True)
            break
        except Exception as e:
            print(f"  ERROR: {e}", flush=True)
        finally:
            state_path.write_text(json.dumps(state, indent=2))

    elapsed = int(time.time() - t0)
    print(f"\nDone. total={total:,}  elapsed={elapsed}s", flush=True)
    state_path.write_text(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
