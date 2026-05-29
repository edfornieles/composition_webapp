#!/usr/bin/env python3
"""Build SFT training data from scraped Reddit posts.

Reads JSONL files produced by scrape_reddit.py, reconstructs threads,
filters for quality, and emits (thread-context, reply) pairs in the same
chat format as build_chan_sft.py.

Usage:
    python scripts/build_reddit_sft.py \\
        --corpus-dir /Volumes/oom/imageboard_corpora/reddit_women40s/text \\
        --out /Volumes/oom/imageboard_corpora/reddit_women40s/reddit_sft.jsonl \\
        --min-reply-len 80

Re-runnable: skips post_ids already written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterator

SUBREDDIT_DESCRIPTIONS: dict[str, str] = {
    "menopause": "Menopause — women sharing candid experiences of perimenopause and menopause",
    "perimenopause": "Perimenopause — the transition years before menopause",
    "genx": "GenX — the generation born 1965–1980, now in their 40s and 50s",
    "askwomenover40": "AskWomenOver40 — questions and answers from women in their 40s and beyond",
    "breakingmom": "breakingmom — a space for mothers to vent honestly about the hard parts of parenting",
    "sahm": "SAHM — stay-at-home mothers sharing the reality of domestic life",
    "mommit": "Mommit — honest parenting talk, the good and the exhausting",
    "twoxchromosomes": "TwoXChromosomes — women's perspectives on life, relationships, and society",
    "aita": "AITA — real-life conflict stories asking 'am I the asshole?'",
    "relationship_advice": "relationship_advice — people working through relationship problems",
    "divorce": "Divorce — navigating the end of a marriage",
    "raisedbynarcissists": "raisedbynarcissists — survivors of narcissistic family dynamics",
    "dating_over_40": "dating_over_40 — dating life in your 40s and beyond",
    "childfree": "childfree — people who have chosen not to have children",
    "xxfitness": "xxfitness — fitness and health from a women's perspective",
}

# ---------------------------------------------------------------------------
# Quality filters
# ---------------------------------------------------------------------------

_DELETED = re.compile(r"^\[(?:removed|deleted)\]$", re.IGNORECASE)
_PURE_REACT = re.compile(
    r"^\s*(this|same|lol|haha|omg|wow|exactly|yes|no|agree|disagree|absolutely|totally|"
    r"thanks|thank you|thank you so much|❤️|💯|🙌|👏|😂|😭|💀)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_URL_ONLY = re.compile(r"^\s*(https?://\S+\s*)+$")


def _is_quality(comment: str, min_len: int) -> bool:
    c = comment.strip()
    if len(c) < min_len:
        return False
    if _DELETED.match(c):
        return False
    if _PURE_REACT.match(c):
        return False
    if _URL_ONLY.match(c):
        return False
    return True


def _clean(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Safety (inline, no Django)
# ---------------------------------------------------------------------------

_MINOR_RE = re.compile(r"\b(?:cp|child porn|preteen|underage)\b", re.IGNORECASE)
_SEX_RE = re.compile(r"\b(?:porn|nude|naked)\w*\b", re.IGNORECASE)
_DOXX_RE = re.compile(r"\b(?:his|her|their)\s+(?:real\s+name|address|workplace)\s+is\b", re.IGNORECASE)


def _is_safe(text: str) -> bool:
    if _DOXX_RE.search(text):
        return False
    if _MINOR_RE.search(text) and _SEX_RE.search(text):
        return False
    return True


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _system_prompt(subreddit: str) -> str:
    desc = SUBREDDIT_DESCRIPTIONS.get(subreddit.lower(), f"r/{subreddit}")
    return (
        f"You are a user on r/{subreddit} — {desc}. "
        f"You write honestly and directly about your experience. "
        f"You are not performing positivity. You are not an assistant. "
        f"You have opinions, feelings, and a life behind your words."
    )


def _format_post(post: dict, idx: int) -> str:
    comment = _clean(post.get("comment") or "")
    subject = (post.get("subject") or "").strip()
    label = "OP" if post.get("is_op") else f"Reply {idx}"
    if subject and post.get("is_op"):
        return f"[{label}] {subject}\n{comment}"
    return f"[{label}] {comment}"


def _build_user_msg(context: list[dict], subreddit: str) -> str:
    parts = ["Thread:"]
    for i, post in enumerate(context):
        parts.append(_format_post(post, i))
    parts.append("\nWrite one reply continuing this thread.")
    return "\n\n".join(parts)


def _build_op_user_msg(op: dict, subreddit: str) -> str:
    subject = (op.get("subject") or "").strip()
    if subject:
        return f"Start a new post on r/{subreddit} about: {subject}"
    return f"Start a new post on r/{subreddit}."


# ---------------------------------------------------------------------------
# Thread pair generation
# ---------------------------------------------------------------------------

def iter_thread_pairs(
    posts: list[dict],
    *,
    subreddit: str,
    context_size: int = 4,
    min_reply_len: int = 80,
) -> Iterator[dict]:
    if not posts:
        return

    op = posts[0]
    op_body = _clean(op.get("comment") or "")
    if len(op_body) >= min_reply_len and _is_safe(op_body):
        yield {
            "messages": [
                {"role": "system", "content": _system_prompt(subreddit)},
                {"role": "user", "content": _build_op_user_msg(op, subreddit)},
                {"role": "assistant", "content": op_body},
            ],
            "metadata": {
                "subreddit": subreddit,
                "source": "reddit",
                "thread_id": op.get("thread_id"),
                "post_id": str(op.get("post_id") or ""),
                "timestamp": op.get("timestamp"),
                "score": op.get("score", 0),
                "kind": "op",
            },
        }

    for i in range(1, len(posts)):
        target = posts[i]
        comment = _clean(target.get("comment") or "")
        if not _is_quality(comment, min_reply_len):
            continue
        if not _is_safe(comment):
            continue

        ctx = posts[max(0, i - context_size):i]
        yield {
            "messages": [
                {"role": "system", "content": _system_prompt(subreddit)},
                {"role": "user", "content": _build_user_msg(ctx, subreddit)},
                {"role": "assistant", "content": comment},
            ],
            "metadata": {
                "subreddit": subreddit,
                "source": "reddit",
                "thread_id": target.get("thread_id"),
                "post_id": str(target.get("post_id") or ""),
                "timestamp": target.get("timestamp"),
                "score": target.get("score", 0),
                "kind": "reply",
            },
        }


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

def iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def iter_subreddit_threads(shard: Path, subreddit: str) -> Iterator[tuple[str, list[dict]]]:
    threads: dict[str, list[dict]] = defaultdict(list)
    for post in iter_jsonl(shard):
        tid = post.get("thread_id")
        if not tid:
            continue
        threads[tid].append(post)
    for tid, posts in threads.items():
        posts.sort(key=lambda p: (p.get("timestamp") or 0))
        ops = [p for p in posts if p.get("is_op")]
        replies = [p for p in posts if not p.get("is_op")]
        if not ops:
            continue
        yield f"reddit:{subreddit}:{tid}", ops[:1] + replies


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _row_id(row: dict) -> str:
    m = row["metadata"]
    return f"reddit:{m.get('subreddit')}:{m.get('thread_id')}:{m.get('post_id')}"


def _load_seen(out_path: Path) -> set[str]:
    seen: set[str] = set()
    if not out_path.exists():
        return seen
    for row in iter_jsonl(out_path):
        m = row.get("metadata") or {}
        seen.add(f"reddit:{m.get('subreddit')}:{m.get('thread_id')}:{m.get('post_id')}")
    return seen


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus-dir", required=True, help="Path to reddit text/ directory.")
    parser.add_argument("--out", required=True, help="Output JSONL path.")
    parser.add_argument("--min-reply-len", type=int, default=80)
    parser.add_argument("--context-posts", type=int, default=4)
    parser.add_argument("--max-rows", type=int, default=0)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    corpus_dir = Path(args.corpus_dir)

    seen = _load_seen(out_path)
    if seen and not args.quiet:
        print(f"Resuming: {len(seen):,} rows already written.", flush=True)

    t0 = time.time()
    written = 0
    threads_processed = 0

    shards = sorted(corpus_dir.glob("*.jsonl"))
    if not args.quiet:
        print(f"Found {len(shards)} subreddit files in {corpus_dir}", flush=True)

    with out_path.open("a", encoding="utf-8") as fh:
        for shard in shards:
            subreddit = shard.stem
            if not args.quiet:
                print(f"  r/{subreddit}", end=" ", flush=True)
            sub_written = 0
            for _, posts in iter_subreddit_threads(shard, subreddit):
                threads_processed += 1
                for row in iter_thread_pairs(
                    posts,
                    subreddit=subreddit,
                    context_size=args.context_posts,
                    min_reply_len=args.min_reply_len,
                ):
                    rid = _row_id(row)
                    if rid in seen:
                        continue
                    seen.add(rid)
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    written += 1
                    sub_written += 1
                    if args.max_rows and written >= args.max_rows:
                        print(f"→ {sub_written:,} rows", flush=True)
                        print(f"\nDone (max-rows hit). total={written:,}", flush=True)
                        return
            if not args.quiet:
                print(f"→ {sub_written:,} rows", flush=True)

    elapsed = int(time.time() - t0)
    print(f"\nDone. written={written:,}  threads={threads_processed:,}  elapsed={elapsed}s", flush=True)


if __name__ == "__main__":
    main()
