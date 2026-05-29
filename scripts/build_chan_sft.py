#!/usr/bin/env python3
"""Build a cross-board chan SFT training dataset from scraped post corpora.

Reads post JSONL files from one or more corpus text directories, reconstructs
threads, filters for quality target posts, and emits (thread-context, reply)
training pairs in OpenAI chat format.

Usage:
    python scripts/build_chan_sft.py \\
        --corpus-dirs /Volumes/oom/imageboard_corpora/imageboard_text_desu/text \\
                      /Volumes/oom/imageboard_corpora/imageboard_text_b4k/text \\
                      /Volumes/oom/chan_corpus/manifests \\
        --out /Volumes/oom/imageboard_corpora/chan_sft/chan_sft.jsonl \\
        --min-reply-len 80 \\
        --context-posts 4 \\
        --max-rows 500000

Re-runnable: skips post_nums already written to the output file.
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

# ---------------------------------------------------------------------------
# Board descriptions — used in system prompts
# ---------------------------------------------------------------------------

BOARD_DESCRIPTIONS: dict[str, str] = {
    "a": "anime and manga",
    "aco": "adult western animation and comics (18+)",
    "an": "animals and nature",
    "b": "random",
    "biz": "business and finance",
    "c": "cute anime pictures",
    "cgl": "cosplay and fashion",
    "ck": "food and cooking",
    "co": "comics and cartoons",
    "diy": "do it yourself — crafts, building, repairs",
    "fa": "fashion",
    "fit": "health, fitness, and body transformation",
    "g": "technology",
    "his": "history and humanities",
    "hr": "high resolution images",
    "i": "oekaki — drawings and sketches",
    "ic": "artwork and critique",
    "int": "international culture and language",
    "jp": "otaku culture",
    "k": "weapons — firearms, knives, military",
    "lit": "literature, books, and writing",
    "m": "mecha and robots",
    "mlp": "my little pony",
    "mlpol": "my little pony and politics",
    "mu": "music — albums, artists, culture",
    "n": "transportation — trains, planes, infrastructure",
    "o": "auto — cars, motorcycles, driving",
    "out": "outdoors — camping, hiking, nature",
    "p": "photography",
    "po": "papercraft and origami",
    "pol": "politics and world events",
    "q": "4chan meta and feedback",
    "qa": "question and answer",
    "r9k": "robot9001 — social isolation and neet culture",
    "s4s": "shit 4chan says — absurdist shitposting",
    "sci": "science and mathematics",
    "soc": "socialising and self-improvement",
    "sp": "sports",
    "tg": "traditional games — tabletop RPGs, wargaming, card games",
    "toy": "toys and collectibles",
    "trv": "travel",
    "tv": "television and film",
    "v": "video games",
    "vg": "video game generals — long-running game threads",
    "vm": "multiplayer video games",
    "vmg": "mobile games",
    "vp": "pokemon",
    "vrpg": "role-playing video games",
    "vst": "strategy and simulation games",
    "vt": "virtual youtubers",
    "w": "anime wallpapers",
    "wg": "wallpapers and background art",
    "wsg": "worksafe gif and webm",
    "x": "paranormal",
    "y": "yaoi",
    "vip": "very important posts",
    "adv": "advice",
    "f": "flash and games",
}

EXCLUDED_BOARDS = {"b", "mlpol"}  # /b/ legal risk; /mlpol/ hate-content density

# ---------------------------------------------------------------------------
# Quality filters
# ---------------------------------------------------------------------------

_QUOTE_ONLY_RE = re.compile(r"^(>>\d+\s*\n?)+$")
_PURE_REACT = re.compile(
    r"^\s*(this|based|kek|lol|same|cringe|cope|seethe|rent free|have sex|go outside"
    r"|touch grass|yikes|ok boomer|based and .*pilled|checked|trips? wasted"
    r"|nice|true|agreed|literally this|literally me|underrated|saved)\s*\.?\s*$",
    re.IGNORECASE,
)
_ONLY_GREENTEXT_QUOTES = re.compile(r"^(>[^\n]{0,80}\n?)+$")
_URL_ONLY_RE = re.compile(r"^\s*(https?://\S+\s*)+$")
_TAG_RE = re.compile(r"<[^>]+>")
_REPLY_LINK_RE = re.compile(r"^>>\d+\s*", re.MULTILINE)


def _is_quality_reply(comment: str, min_len: int) -> bool:
    c = comment.strip()
    if len(c) < min_len:
        return False
    if _QUOTE_ONLY_RE.match(c):
        return False
    if _PURE_REACT.match(c):
        return False
    if _URL_ONLY_RE.match(c):
        return False
    # Strip reply links and check if anything real remains
    stripped = _REPLY_LINK_RE.sub("", c).strip()
    if len(stripped) < min_len // 2:
        return False
    return True


def _clean_comment(text: str) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub("", text)
    text = text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")
    text = text.replace("&quot;", '"').replace("&#039;", "'").replace("&#39;", "'")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------------------
# Safety filter (inline — no Django dependency)
# ---------------------------------------------------------------------------

_MINOR_RE = re.compile(
    r"\b(?:loli|shota|cp|child porn|preteen|underage|11yo|12yo|13yo|14yo|15yo|16yo|17yo)\b",
    re.IGNORECASE,
)
_SEX_RE = re.compile(
    r"\b(?:porn|nude|naked|cum|penis|vagina|masturbat|nsfw)\w*\b",
    re.IGNORECASE,
)
_DOXX_RE = re.compile(
    r"\b(?:his|her|their)\s+(?:real\s+name|address|workplace|employer)\s+is\b",
    re.IGNORECASE,
)
_HATE_RE = re.compile(
    r"\bn[i1]gg[e3]r\b|\bk[i1]k[e3]\b|\bch[i1]nk\b|\bsp[i1]c\b|\bg[o0][o0]k\b",
    re.IGNORECASE,
)


def _is_safe(text: str) -> bool:
    if _DOXX_RE.search(text):
        return False
    if _MINOR_RE.search(text) and _SEX_RE.search(text):
        return False
    if _HATE_RE.search(text):
        return False
    return True


# ---------------------------------------------------------------------------
# Thread reconstruction and pair generation
# ---------------------------------------------------------------------------

def _format_post_for_context(post: dict, idx: int) -> str:
    comment = _clean_comment(post.get("comment") or "")
    subject = (post.get("subject") or "").strip()
    label = "OP" if post.get("is_op") else f"Reply {idx}"
    if subject:
        return f"[{label}] {subject}\n{comment}"
    return f"[{label}] {comment}"


def _build_user_message(context_posts: list[dict], board: str) -> str:
    parts = ["Thread:"]
    for i, post in enumerate(context_posts):
        parts.append(_format_post_for_context(post, i))
    parts.append("\nWrite one reply continuing this thread.")
    return "\n\n".join(parts)


def _build_op_user_message(op: dict, board: str) -> str:
    subject = (op.get("subject") or "").strip()
    if subject:
        return f"Start a new thread on /{board}/ about: {subject}"
    return f"Start a new thread on /{board}/."


def _system_prompt(board: str) -> str:
    desc = BOARD_DESCRIPTIONS.get(board, board)
    return (
        f"You are an anonymous poster on /{board}/ — {desc}. "
        f"You write in the direct, unfiltered style of imageboard culture: "
        f"opinionated, blunt, sometimes crude, but genuine. "
        f"You are not an assistant. You do not explain yourself unless pressed. "
        f"You have a perspective and you hold it."
    )


def iter_thread_pairs(
    posts: list[dict],
    *,
    board: str,
    source: str,
    context_size: int = 4,
    min_reply_len: int = 80,
) -> Iterator[dict]:
    """Given a sorted list of posts in one thread, yield training pairs.

    Yields two kinds of examples:
    - OP examples: no prior context, user asks to start a thread, assistant is the OP post.
    - Reply examples: thread context → next post.
    """
    if not posts:
        return

    # OP as a training target
    op = posts[0]
    op_comment = _clean_comment(op.get("comment") or "")
    if len(op_comment) >= min_reply_len and _is_safe(op_comment):
        yield {
            "messages": [
                {"role": "system", "content": _system_prompt(board)},
                {"role": "user", "content": _build_op_user_message(op, board)},
                {"role": "assistant", "content": op_comment},
            ],
            "metadata": {
                "board": board,
                "source": source,
                "thread_num": op.get("thread_num"),
                "post_num": str(op.get("post_num") or ""),
                "timestamp": op.get("timestamp"),
                "kind": "op",
                "has_image": op.get("has_image", False),
                "image_key": op.get("image_key"),
            },
        }

    # Reply examples
    for i in range(1, len(posts)):
        target = posts[i]
        comment = _clean_comment(target.get("comment") or "")
        if not _is_quality_reply(comment, min_reply_len):
            continue
        if not _is_safe(comment):
            continue

        ctx_posts = posts[max(0, i - context_size):i]
        user_msg = _build_user_message(ctx_posts, board)

        yield {
            "messages": [
                {"role": "system", "content": _system_prompt(board)},
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": comment},
            ],
            "metadata": {
                "board": board,
                "source": source,
                "thread_num": target.get("thread_num"),
                "post_num": str(target.get("post_num") or ""),
                "timestamp": target.get("timestamp"),
                "kind": "reply",
                "has_image": target.get("has_image", False),
                "image_key": target.get("image_key"),
            },
        }


# ---------------------------------------------------------------------------
# Corpus readers
# ---------------------------------------------------------------------------

def _source_name(corpus_dir: Path) -> str:
    name = corpus_dir.parent.name
    for suffix in ("imageboard_text_", "imageboard_"):
        if name.startswith(suffix):
            return name[len(suffix):]
    return name


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


def iter_board_threads(
    board_file: Path,
    board: str,
    source: str,
    excluded_boards: set[str],
) -> Iterator[tuple[str, list[dict]]]:
    """Stream posts from one board file, yield (thread_key, sorted_posts) per thread."""
    if board in excluded_boards:
        return

    threads: dict[int, list[dict]] = defaultdict(list)
    for post in iter_jsonl(board_file):
        tnum = post.get("thread_num")
        if tnum is None:
            continue
        threads[int(tnum)].append(post)

    for tnum, posts in threads.items():
        posts.sort(key=lambda p: (p.get("timestamp") or 0, int(str(p.get("post_num") or 0) or 0)))
        # Ensure OP is first
        ops = [p for p in posts if p.get("is_op")]
        replies = [p for p in posts if not p.get("is_op")]
        if not ops:
            continue
        yield f"{source}:{board}:{tnum}", ops[:1] + replies


# ---------------------------------------------------------------------------
# Live chan corpus reader (different layout)
# ---------------------------------------------------------------------------

def load_image_index(images_manifest: Path) -> dict[str, str]:
    """Build post_uid → image_key lookup from the chan images manifest."""
    idx: dict[str, str] = {}
    if not images_manifest.exists():
        return idx
    for row in iter_jsonl(images_manifest):
        uid = row.get("post_uid")
        key = row.get("image_key")
        if uid and key:
            idx[uid] = key
    return idx


def iter_live_chan_threads(
    posts_manifest: Path,
    image_index: dict[str, str] | None = None,
) -> Iterator[tuple[str, list[dict]]]:
    """Read the live chan scrape posts.jsonl and yield per-thread sorted post lists."""
    threads: dict[str, list[dict]] = defaultdict(list)
    source = "fourchan"
    img_idx = image_index or {}

    for post in iter_jsonl(posts_manifest):
        board = post.get("board", "")
        if board in EXCLUDED_BOARDS:
            continue
        thread_id = post.get("thread_id")
        if thread_id is None:
            continue
        key = f"{source}:{board}:{thread_id}"
        post_uid = post.get("post_uid") or f"{source}:{board}:{post.get('post_id')}"
        image_key = img_idx.get(post_uid)
        threads[key].append({
            "board": board,
            "thread_num": thread_id,
            "post_num": post.get("post_id"),
            "timestamp": post.get("time_unix"),
            "is_op": post.get("post_id") == post.get("thread_id"),
            "subject": post.get("subject") or "",
            "comment": post.get("plain_text") or "",
            "source": source,
            "has_image": image_key is not None,
            "image_key": image_key,
        })

    for key, posts in threads.items():
        posts.sort(key=lambda p: (p.get("timestamp") or 0, int(str(p.get("post_num") or 0) or 0)))
        ops = [p for p in posts if p.get("is_op")]
        replies = [p for p in posts if not p.get("is_op")]
        if not ops:
            continue
        yield key, ops[:1] + replies


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _row_id(row: dict) -> str:
    m = row["metadata"]
    return f"{m['source']}:{m['board']}:{m['thread_num']}:{m['post_num']}"


def _load_seen(out_path: Path) -> set[str]:
    seen: set[str] = set()
    if not out_path.exists():
        return seen
    for row in iter_jsonl(out_path):
        m = row.get("metadata") or {}
        rid = f"{m.get('source')}:{m.get('board')}:{m.get('thread_num')}:{m.get('post_num')}"
        seen.add(rid)
    return seen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus-dirs", nargs="+", required=True,
                        help="Paths to text/ directories (or posts.jsonl for live chan scrape).")
    parser.add_argument("--out", required=True, help="Output JSONL path.")
    parser.add_argument("--min-reply-len", type=int, default=80,
                        help="Minimum character length for a reply to be used as a training target.")
    parser.add_argument("--context-posts", type=int, default=4,
                        help="Number of prior posts to include as context (max).")
    parser.add_argument("--max-rows", type=int, default=0,
                        help="Stop after this many rows (0 = no limit).")
    parser.add_argument("--exclude-boards", default="",
                        help="Comma-separated additional boards to exclude.")
    parser.add_argument("--boards", default="",
                        help="Only include these boards (comma-separated). Default: all.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    excluded = set(EXCLUDED_BOARDS)
    if args.exclude_boards:
        excluded.update(b.strip() for b in args.exclude_boards.split(",") if b.strip())
    only_boards: set[str] = set()
    if args.boards:
        only_boards = {b.strip() for b in args.boards.split(",") if b.strip()}

    seen = _load_seen(out_path)
    if seen and not args.quiet:
        print(f"Resuming: {len(seen):,} rows already written.", flush=True)

    # Load image index for live chan posts (post_uid → image_key)
    images_manifest = Path("/Volumes/Oom/chan_corpus/manifests/images.jsonl")
    if not args.quiet:
        print(f"Loading image index from {images_manifest}…", flush=True)
    image_index = load_image_index(images_manifest)
    if not args.quiet:
        print(f"  {len(image_index):,} image→post links loaded.", flush=True)

    t0 = time.time()
    written = 0
    skipped_seen = 0
    skipped_quality = 0
    threads_processed = 0

    def _log(force=False):
        if args.quiet and not force:
            return
        elapsed = int(time.time() - t0)
        print(
            f"  threads={threads_processed:,}  written={written:,}  "
            f"skipped_seen={skipped_seen:,}  skipped_quality={skipped_quality:,}  "
            f"elapsed={elapsed}s",
            flush=True,
        )

    with out_path.open("a", encoding="utf-8") as fh:

        for corpus_dir_str in args.corpus_dirs:
            corpus_dir = Path(corpus_dir_str)

            # Detect live chan scrape (posts.jsonl directly) vs text/ directory
            if corpus_dir.suffix == ".jsonl":
                if not args.quiet:
                    print(f"\n[live chan] {corpus_dir}", flush=True)
                thread_iter = iter_live_chan_threads(corpus_dir, image_index=image_index)
                for thread_key, posts in thread_iter:
                    if not posts:
                        continue
                    board = posts[0].get("board", "")
                    if board in excluded:
                        continue
                    if only_boards and board not in only_boards:
                        continue
                    threads_processed += 1
                    for row in iter_thread_pairs(
                        posts,
                        board=board,
                        source="fourchan",
                        context_size=args.context_posts,
                        min_reply_len=args.min_reply_len,
                    ):
                        rid = _row_id(row)
                        if rid in seen:
                            skipped_seen += 1
                            continue
                        seen.add(rid)
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                        written += 1
                        if args.max_rows and written >= args.max_rows:
                            _log(force=True)
                            return
                    if threads_processed % 500 == 0:
                        _log()
                continue

            # Text directory: one .jsonl per board
            if not corpus_dir.is_dir():
                print(f"Warning: {corpus_dir} is not a directory, skipping.", file=sys.stderr)
                continue

            source = _source_name(corpus_dir)
            board_files = sorted(corpus_dir.glob("*.jsonl"))
            if not args.quiet:
                print(f"\n[{source}] {corpus_dir}  ({len(board_files)} board files)", flush=True)

            for board_file in board_files:
                board = board_file.stem
                if board in excluded:
                    continue
                if only_boards and board not in only_boards:
                    continue
                if not args.quiet:
                    print(f"  /{board}/", end=" ", flush=True)

                board_written = 0
                for thread_key, posts in iter_board_threads(board_file, board, source, excluded):
                    threads_processed += 1
                    for row in iter_thread_pairs(
                        posts,
                        board=board,
                        source=source,
                        context_size=args.context_posts,
                        min_reply_len=args.min_reply_len,
                    ):
                        rid = _row_id(row)
                        if rid in seen:
                            skipped_seen += 1
                            continue
                        seen.add(rid)
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                        written += 1
                        board_written += 1
                        if args.max_rows and written >= args.max_rows:
                            _log(force=True)
                            return
                    if threads_processed % 500 == 0:
                        _log()

                if not args.quiet:
                    print(f"→ {board_written:,} rows", flush=True)

    _log(force=True)
    print(f"\nDone. Output: {out_path}", flush=True)


if __name__ == "__main__":
    main()
