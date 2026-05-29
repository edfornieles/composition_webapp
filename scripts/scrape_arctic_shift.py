#!/usr/bin/env python3
"""Reddit corpus builder via Arctic Shift HTTP API (post-Pushshift archive).

Pulls the full historical archive of target subreddits from
https://arctic-shift.photon-reddit.com — no torrents, no Reddit rate limits.
Writes JSONL in the same schema as scrape_reddit_full.py so output drops
into the same directory tree (text/{sub}.jsonl).

Usage:
    python scripts/scrape_arctic_shift.py \\
        --out /Volumes/Oom/reddit_women40s \\
        --since 2018-01-01

    # Specific subreddits:
    python scripts/scrape_arctic_shift.py --out /tmp/r --subreddits Menopause,GenX

Resumable: state.json tracks last-seen created_utc per (subreddit, kind).

Output layout:
    {out}/
      text/{subreddit}.jsonl
      images/{subreddit}/...      (if --images)
      arctic_state.json
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
from datetime import datetime, timezone
from pathlib import Path

TARGET_SUBREDDITS = [
    "Menopause", "Perimenopause", "Hysterectomy", "WomensHealth",
    "GenX", "AskWomenOver40", "AskWomenOver30", "AskWomen",
    "breakingmom", "SAHM", "Mommit", "workingmoms", "SingleMoms",
    "EmptyNesters", "stepparents",
    "relationship_advice", "Divorce", "survivinginfidelity",
    "DeadBedrooms", "dating_over_40", "widows",
    "raisedbynarcissists", "narcissisticabuse", "CPTSD",
    "xxfitness", "loseit", "progresspics", "femalefashionadvice",
    "TwoXChromosomes", "childfree", "AITA", "TrueOffMyChest",
    "offmychest", "Fibromyalgia",
]

API_BASE = "https://arctic-shift.photon-reddit.com/api"
UA = "women40s-corpus/0.3 (academic character research)"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_LAST_REQ = 0.0
_RPS = 1.5  # arctic-shift tolerates more than reddit; stay polite


def parse_date(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def throttle():
    global _LAST_REQ
    gap = 1.0 / _RPS
    delta = time.time() - _LAST_REQ
    if delta < gap:
        time.sleep(gap - delta)
    _LAST_REQ = time.time()


def api_get(path: str, params: dict, retries: int = 5) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{API_BASE}{path}?{qs}"
    backoff = 5
    for attempt in range(retries):
        throttle()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = backoff * (2 ** attempt)
                print(f"    429 — sleeping {wait}s", flush=True)
                time.sleep(wait)
                continue
            if e.code in (500, 502, 503, 504):
                wait = backoff * (attempt + 1)
                print(f"    {e.code} — retry in {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            wait = backoff * (attempt + 1)
            print(f"    network error ({e}) — retry in {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"api_get failed after {retries} retries: {url}")


def clean(text) -> str:
    if not text or text in ("[removed]", "[deleted]"):
        return ""
    return str(text).strip()


def extract_image_urls(d: dict) -> list[str]:
    urls: list[str] = []
    url = d.get("url") or d.get("url_overridden_by_dest") or ""
    if url:
        path = urllib.parse.urlparse(url).path
        ext = os.path.splitext(path)[1].lower()
        if ext in IMAGE_EXTS or "i.redd.it" in url:
            urls.append(url)
    if d.get("is_gallery") and d.get("media_metadata"):
        for item in (d["media_metadata"] or {}).values():
            if item.get("status") == "valid" and item.get("s"):
                src = item["s"].get("u") or item["s"].get("gif") or ""
                if src:
                    urls.append(src.replace("&amp;", "&"))
    if not urls:
        preview = d.get("preview") or {}
        images = preview.get("images") or []
        if images:
            src = (images[0].get("source") or {}).get("url") or ""
            if src:
                urls.append(src.replace("&amp;", "&"))
    return urls


def download_image(url: str, dest: Path) -> bool:
    if dest.exists():
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            dest.write_bytes(resp.read())
        return True
    except Exception:
        return False


def post_to_row(d: dict, image_keys: list[str], image_urls: list[str]) -> dict:
    title = clean(d.get("title"))
    selftext = clean(d.get("selftext"))
    op_body = f"{title}\n\n{selftext}".strip() if selftext else title
    return {
        "subreddit": (d.get("subreddit") or "").lower(),
        "thread_id": d.get("id") or "",
        "post_id": d.get("name") or f"t3_{d.get('id') or ''}",
        "timestamp": int(d.get("created_utc") or 0),
        "is_op": True,
        "subject": title,
        "comment": op_body,
        "score": int(d.get("score") or 0),
        "source": "reddit",
        "has_image": bool(image_urls),
        "image_keys": image_keys,
        "image_urls": image_urls,
    }


def comment_to_row(d: dict) -> dict:
    body = clean(d.get("body"))
    if not body:
        return {}
    link_id = d.get("link_id") or ""
    thread_id = link_id[3:] if link_id.startswith("t3_") else link_id
    return {
        "subreddit": (d.get("subreddit") or "").lower(),
        "thread_id": thread_id,
        "post_id": d.get("name") or d.get("id") or "",
        "timestamp": int(d.get("created_utc") or 0),
        "is_op": False,
        "subject": "",
        "comment": body,
        "score": int(d.get("score") or 0),
        "source": "reddit",
        "has_image": False,
        "image_keys": [],
        "image_urls": [],
    }


def scrape_kind(
    sub: str,
    kind: str,
    text_dir: Path,
    image_dir: Path,
    state: dict,
    since_ts: int,
    until_ts: int,
    download_images: bool,
    limit: int,
) -> int:
    """kind in {'posts','comments'}. Returns rows written."""
    state_key = f"{sub.lower()}::{kind}"
    cursor = max(int(state.get(state_key, 0) or 0), since_ts)
    shard = (text_dir / f"{sub.lower()}.jsonl").open("a", encoding="utf-8")
    written = 0
    page_size = 100
    path = "/posts/search" if kind == "posts" else "/comments/search"

    img_sub_dir = image_dir / sub.lower() if download_images else None
    if img_sub_dir:
        img_sub_dir.mkdir(parents=True, exist_ok=True)

    while True:
        params = {
            "subreddit": sub,
            "limit": page_size,
            "sort": "asc",
            "after": cursor,
        }
        if until_ts:
            params["before"] = until_ts
        try:
            payload = api_get(path, params)
        except Exception as e:
            print(f"    [{sub}/{kind}] fatal: {e}", flush=True)
            break

        items = payload.get("data") or []
        if not items:
            break

        max_ts = cursor
        for d in items:
            ts = int(d.get("created_utc") or 0)
            if ts <= cursor:
                continue
            max_ts = max(max_ts, ts)
            if kind == "posts":
                image_urls = extract_image_urls(d)
                image_keys: list[str] = []
                if download_images and image_urls and img_sub_dir is not None:
                    for idx, img_url in enumerate(image_urls):
                        ext = os.path.splitext(urllib.parse.urlparse(img_url).path)[1].lower() or ".jpg"
                        fname = f"{d.get('id')}_{idx}{ext}"
                        dest = img_sub_dir / fname
                        if download_image(img_url, dest):
                            image_keys.append(f"{sub.lower()}/{fname}")
                row = post_to_row(d, image_keys, image_urls)
                if not row["thread_id"] or not row["subject"]:
                    continue
            else:
                row = comment_to_row(d)
                if not row:
                    continue

            shard.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1

        if max_ts == cursor:  # no progress — avoid infinite loop
            cursor += 1
        else:
            cursor = max_ts
        state[state_key] = cursor
        shard.flush()
        if limit and written >= limit:
            break
        if len(items) < page_size:
            break

    shard.close()
    state[state_key] = cursor
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="/Volumes/Oom/reddit_women40s")
    ap.add_argument("--subreddits", default=",".join(TARGET_SUBREDDITS))
    ap.add_argument("--since", default="", help="YYYY-MM-DD lower bound on created_utc")
    ap.add_argument("--until", default="", help="YYYY-MM-DD upper bound on created_utc")
    ap.add_argument("--images", action="store_true", help="Download OP images")
    ap.add_argument("--limit-per-kind", type=int, default=0,
                    help="Stop after N rows per (sub, kind). 0 = no cap.")
    ap.add_argument("--rps", type=float, default=1.5, help="Max API requests per second")
    ap.add_argument("--posts-only", action="store_true")
    ap.add_argument("--comments-only", action="store_true")
    args = ap.parse_args()

    global _RPS
    _RPS = args.rps

    out = Path(args.out)
    text_dir = out / "text"
    image_dir = out / "images"
    text_dir.mkdir(parents=True, exist_ok=True)
    state_path = out / "arctic_state.json"

    state: dict = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            pass

    subs = [s.strip() for s in args.subreddits.split(",") if s.strip()]
    since_ts = parse_date(args.since) if args.since else 0
    until_ts = parse_date(args.until) if args.until else 0

    kinds: list[str] = []
    if not args.comments_only:
        kinds.append("posts")
    if not args.posts_only:
        kinds.append("comments")

    print(f"Output:     {out}", flush=True)
    print(f"Subreddits: {len(subs)}", flush=True)
    print(f"Window:     {args.since or 'beginning'} → {args.until or 'now'}", flush=True)
    print(f"Kinds:      {', '.join(kinds)}", flush=True)
    print(f"Images:     {'yes' if args.images else 'no'}", flush=True)
    print(f"RPS:        {args.rps}", flush=True)
    print(flush=True)

    grand = 0
    t0 = time.time()
    try:
        for sub in subs:
            print(f"r/{sub}", flush=True)
            for kind in kinds:
                try:
                    n = scrape_kind(
                        sub, kind, text_dir, image_dir, state,
                        since_ts=since_ts, until_ts=until_ts,
                        download_images=args.images and kind == "posts",
                        limit=args.limit_per_kind,
                    )
                    grand += n
                    print(f"  {kind:9s} +{n:,} rows  cursor={state.get(f'{sub.lower()}::{kind}', 0)}", flush=True)
                except Exception as e:
                    print(f"  {kind:9s} ERROR {e}", flush=True)
                finally:
                    state_path.write_text(json.dumps(state, indent=2))
    except KeyboardInterrupt:
        print("\nInterrupted — state saved.", flush=True)

    state_path.write_text(json.dumps(state, indent=2))
    elapsed = int(time.time() - t0)
    print(f"\nDone. total_rows={grand:,}  elapsed={elapsed}s", flush=True)


if __name__ == "__main__":
    main()
