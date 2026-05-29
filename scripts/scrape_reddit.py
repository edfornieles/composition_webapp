#!/usr/bin/env python3
"""Reddit scraper for building a women-40s voice corpus.

Uses Reddit's public JSON API — no API key required.
Fetches threads (hot + top + new) from target subreddits, stores posts as JSONL.
Output is compatible with build_reddit_sft.py.

Usage:
    python scripts/scrape_reddit.py \\
        --out /Volumes/oom/imageboard_corpora/reddit_women40s \\
        --subreddits menopause,perimenopause,GenX,AskWomenOver40,breakingmom \\
        --pages 50 --rps 1.0

Output layout:
    {out}/
      text/{subreddit}.jsonl     — one post/comment per line
      state.json                 — resumption state

Each JSONL row:
    {
      "subreddit": "menopause",
      "thread_id": "abc123",
      "post_id": "def456",
      "timestamp": 1234567890,
      "is_op": true,
      "subject": "thread title",
      "comment": "post body",
      "score": 42,
      "source": "reddit"
    }
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

TARGET_SUBREDDITS = [
    # High signal
    "Menopause",
    "Perimenopause",
    "GenX",
    "AskWomenOver40",
    "breakingmom",
    "SAHM",
    "Mommit",
    # High volume
    "TwoXChromosomes",
    "AITA",
    "relationship_advice",
    "Divorce",
    "raisedbynarcissists",
    # Niche
    "dating_over_40",
    "childfree",
    "xxfitness",
]

UA = "chan-corpus-research/0.1 (academic art project; contact via github)"
_LAST = 0.0
_RPS = 1.0


def _throttle():
    global _LAST
    gap = 1.0 / _RPS
    delta = time.time() - _LAST
    if delta < gap:
        time.sleep(gap - delta)
    _LAST = time.time()


def _get(url: str, timeout: float = 30.0) -> dict:
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
            time.sleep(60)
            raise
        raise


def _clean(text: str) -> str:
    if not text or text in ("[removed]", "[deleted]"):
        return ""
    return text.strip()


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


def _flatten_comments(nodes: list, depth: int = 0) -> list[dict]:
    out = []
    for node in nodes:
        kind = node.get("kind")
        d = node.get("data") or {}
        if kind == "t1":
            out.append(d)
            replies = d.get("replies")
            if isinstance(replies, dict):
                children = replies.get("data", {}).get("children", [])
                out.extend(_flatten_comments(children, depth + 1))
    return out


def _write(fh, row: dict):
    fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def scrape_subreddit(
    subreddit: str,
    text_dir: Path,
    state: dict,
    pages: int,
    quiet: bool,
) -> int:
    shard = text_dir / f"{subreddit.lower()}.jsonl"
    sub_state = state.setdefault(subreddit, {
        "processed_threads": [],
        "posts_written": 0,
    })
    processed = set(sub_state.get("processed_threads") or [])
    written = 0

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

                    # Write OP
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
                    })
                    written += 1

                    # Fetch and write comments
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
                            })
                            written += 1
                    except Exception as e:
                        if not quiet:
                            print(f"    comment fetch failed {thread_id}: {e}", flush=True)

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
    parser.add_argument("--out", required=True, help="Output directory.")
    parser.add_argument("--subreddits", default=",".join(TARGET_SUBREDDITS),
                        help="Comma-separated subreddit names.")
    parser.add_argument("--pages", type=int, default=50,
                        help="Listing pages per subreddit per sort (hot/top/new). Each page ~100 threads.")
    parser.add_argument("--rps", type=float, default=0.8,
                        help="Max requests per second.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    global _RPS
    _RPS = args.rps

    out = Path(args.out)
    text_dir = out / "text"
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

    for sub in subreddits:
        if not args.quiet:
            print(f"\nr/{sub}", flush=True)
        try:
            n = scrape_subreddit(sub, text_dir, state, pages=args.pages, quiet=args.quiet)
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
