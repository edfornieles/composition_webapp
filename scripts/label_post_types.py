#!/usr/bin/env python3
"""Heuristic post-type labeler for chan corpus posts.

Detects and labels archive-native task modes WITHOUT synthesis. Per the
chan_anon framing memory, the heuristic pass is responsible for ~80–90% of
the first task-mode batch. The teacher-model pass handles only non-native
psyche modes (inner_monologue, dream_fragment, etc.) — and even there it
*transforms* real archive material rather than inventing thread replies.

Modes detected here (multi-label — a post can be several at once):
    thread_reply, op_post, greentext, confession, advice, insult,
    derailment, pile_on, correction, status_performance, argument_reply

Plus a single thread_type per thread:
    confession_thread, advice_thread, argument_thread, status_thread,
    flame_war, general_chat

Input:  JSONL in the schema produced by convert_pol_dataset.py
        (or any flat per-post JSONL with: thread_num, post_num, is_op,
         comment, reply_to (optional), quotes_count (optional), etc.)
Output: same JSONL with two new fields:
            "style_tags":   [str, ...]
            "thread_type":  str

Usage:
    python scripts/label_post_types.py \\
        --in  /Volumes/Oom/chan_corpus/raw/pol/jsonl/pol-2018-06.jsonl \\
        --out /Volumes/Oom/chan_corpus/raw/pol/jsonl_labeled/pol-2018-06.jsonl

    # Process a whole dir:
    python scripts/label_post_types.py \\
        --in-dir  /Volumes/Oom/chan_corpus/raw/pol/jsonl \\
        --out-dir /Volumes/Oom/chan_corpus/raw/pol/jsonl_labeled
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Lexicons — kept simple/regex-based on purpose. Iterate empirically.
# ---------------------------------------------------------------------------

_FIRST_PERSON_RE = re.compile(r"\b(i|i'm|im|i've|ive|i'd|id|me|my|myself|mine)\b", re.IGNORECASE)

_VULNERABILITY_RE = re.compile(
    r"\b(asham(?:ed|e)|embarrass|lonely|alone|depress|anxious|anxiety|worthless"
    r"|hate myself|hate my|kissless|virgin|ugly|fat|weak|failure|gave up"
    r"|never had|cant get|can'?t get|terrified|scared|afraid|shame|broken"
    r"|hopeless|empty|numb|crying|cried|cry|tears|hate myself|hate my body"
    r"|insecure|inferior|invisible|nobody|unloved|unwanted|disgusting"
    r"|panic|despair|trapped|stuck|drowning|breaking down|no friends"
    r"|never been|never kissed|never had a|cant talk|can'?t talk)\b",
    re.IGNORECASE,
)

_IMPERATIVE_OPENERS_RE = re.compile(
    r"^\s*(do|don'?t|stop|start|try|use|eat|lift|run|drop|ditch|get|just|learn"
    r"|read|watch|buy|sell|avoid|ignore|focus|train|sleep|hydrate|stretch|cook"
    r"|fast|cut|bulk|squat|bench|deadlift|push|pull|switch|kill|delete|uninstall"
    r"|install|use|run|build|write|practice|pick|grab|take|leave|forget|listen)\b",
    re.IGNORECASE | re.MULTILINE,
)

_INSULT_RE = re.compile(
    r"\b(cope|seethe|mald|ngmi|midwit|dyel|manlet|incel|redditor|normalfag"
    r"|larper|kissless|ratio'?d?|cringe|yikes|ok boomer|touch grass|have sex"
    r"|rent free|dilate|kek|lmao|lol|based|cuck|simp|soyboy|chud|tranny|troon)\b",
    re.IGNORECASE,
)

_CORRECTION_OPENERS_RE = re.compile(
    r"^\s*(actually[,.]?|no[,.]|wrong[.,]|you mean|incorrect|not quite|nope|"
    r"\*[a-z]+|technically|fwiw|um actually)",
    re.IGNORECASE | re.MULTILINE,
)

_ARGUMENT_MARKERS_RE = re.compile(
    r"\b(wrong|no\b|but\b|actually|the (?:issue|problem|reason)|"
    r"what (?:you|youre|you'?re) missing|you don'?t understand|"
    r"this is why|the truth is|in fact|fact is)\b",
    re.IGNORECASE,
)

_STATUS_NUMBERS_RE = re.compile(
    r"\b(?:bench|squat|deadlift|press|ohp|dl|bw|bf|body fat|body[\s-]?weight"
    r"|height|weight|run|mile|km|reps?)\D{0,8}(\d{2,4})", re.IGNORECASE,
)
_STATUS_FLEX_RE = re.compile(
    r"\b(trust me i|as someone who|i'?ve been (?:doing|lifting|training|coding"
    r"|writing|reading) (?:this|for) \d+|(?:\d+) (?:years?|yrs?) experience"
    r"|i make (?:six|seven|\$?\d+k)|i'?m a \w+ at|i work at|phd in|been doing this for"
    r"|im built|im jacked|im natty)\b",
    re.IGNORECASE,
)

_THANKS_REACT_RE = re.compile(
    r"^\s*(this|based|kek|lol|same|cringe|cope|seethe|nice|true|agreed|"
    r"literally this|literally me|underrated|saved|fpbp)\s*\.?\s*$",
    re.IGNORECASE,
)


def _extract_quotes(comment: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r">>(\d+)", comment)]


def _greentext_lines(comment: str) -> int:
    return sum(1 for line in comment.split("\n")
               if line.startswith(">") and not line.startswith(">>"))


def _word_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z']{4,}", text.lower()))


# ---------------------------------------------------------------------------
# Per-post detectors (return list of style tags)
# ---------------------------------------------------------------------------

def detect_post_features(post: dict) -> list[str]:
    tags: list[str] = []
    comment = post.get("comment") or ""
    is_op = bool(post.get("is_op"))
    char_len = post.get("char_len") or len(comment)
    quotes = _extract_quotes(comment)
    quotes_count = post.get("quotes_count")
    if quotes_count is None:
        quotes_count = len(quotes)
    has_quote = quotes_count > 0

    # Structural
    if is_op:
        tags.append("op_post")
    else:
        if has_quote:
            tags.append("thread_reply")

    # Greentext
    gt_lines = post.get("greentext_lines")
    if gt_lines is None:
        gt_lines = _greentext_lines(comment)
    if gt_lines >= 2:
        tags.append("greentext")

    # Pure react / one-liner — cheap reject for SFT later, useful tag now
    if _THANKS_REACT_RE.match(comment.strip()):
        tags.append("pure_react")
        return tags  # nothing else interesting

    # Confession (long, first-person, vulnerable)
    fp_hits = len(_FIRST_PERSON_RE.findall(comment))
    vuln_hits = len(_VULNERABILITY_RE.findall(comment))
    if char_len >= 200 and fp_hits >= 2 and vuln_hits >= 1:
        tags.append("confession")

    # Advice (imperative, has reply, practical)
    if has_quote and 30 <= char_len <= 600 and _IMPERATIVE_OPENERS_RE.search(comment):
        tags.append("advice")

    # Insult (short, has insult lex)
    insult_hits = len(_INSULT_RE.findall(comment))
    if insult_hits >= 1 and char_len < 200:
        tags.append("insult")

    # Correction (opens with correction marker, has quote)
    if has_quote and _CORRECTION_OPENERS_RE.match(comment.strip()):
        tags.append("correction")

    # Status performance (numbers/flex with first-person)
    if (fp_hits >= 1) and (
        _STATUS_NUMBERS_RE.search(comment) or _STATUS_FLEX_RE.search(comment)
    ):
        tags.append("status_performance")

    # Argument reply (has quote, length >80, adversarial markers, NOT just insult)
    if has_quote and char_len > 80 and _ARGUMENT_MARKERS_RE.search(comment):
        if "insult" not in tags or len(comment) > 150:
            tags.append("argument_reply")

    return tags


# ---------------------------------------------------------------------------
# Thread-level detectors (require thread context)
# ---------------------------------------------------------------------------

def label_thread(posts: list[dict]) -> tuple[list[dict], str]:
    """Apply thread-level annotations + return inferred thread_type.

    Mutates each post dict to add 'style_tags'. Returns (posts, thread_type).
    """
    # Sort by timestamp + post_num
    posts = sorted(posts, key=lambda p: (p.get("timestamp") or 0, int(p.get("post_num") or 0)))

    # Pass 1: per-post style tags
    for p in posts:
        p["style_tags"] = detect_post_features(p)

    # Pass 2: pile_on detection
    # A reply piles on if its target post has 2+ prior negative replies (insult/argument_reply)
    # AND the current reply itself is insult/argument_reply.
    target_negative_count: dict[int, int] = defaultdict(int)
    for p in posts:
        tags = p["style_tags"]
        if not p.get("is_op"):
            quotes = _extract_quotes(p.get("comment") or "")
            target = p.get("reply_to") or (quotes[0] if quotes else None)
            if target is not None:
                if target_negative_count[int(target)] >= 2 and (
                    "insult" in tags or "argument_reply" in tags
                ):
                    p["style_tags"].append("pile_on")
                if "insult" in tags or "argument_reply" in tags:
                    target_negative_count[int(target)] += 1

    # Pass 3: derailment — replies with no quote that introduce
    # word-set sharply different from OP and immediate neighbors. Cheap proxy.
    # Skip greentext (stylistic indirection often paraphrases rather than echoes).
    if posts:
        op_words = _word_set(posts[0].get("comment") or "")
        for p in posts[1:]:
            if p.get("is_op"):
                continue
            if "greentext" in p["style_tags"]:
                continue
            comment = p.get("comment") or ""
            quotes = _extract_quotes(comment)
            has_quote = bool(quotes) or bool(p.get("reply_to"))
            if has_quote:
                continue
            words = _word_set(comment)
            if len(words) >= 8 and op_words and len(words & op_words) <= 1:
                p["style_tags"].append("derailment")

    # Thread type — aggregate
    tag_counter: Counter = Counter()
    for p in posts:
        for t in p["style_tags"]:
            tag_counter[t] += 1
    n = max(len(posts), 1)
    op_tags = set(posts[0]["style_tags"]) if posts else set()

    def share(t: str) -> float:
        return tag_counter[t] / n

    if "confession" in op_tags:
        thread_type = "confession_thread"
    elif "status_performance" in op_tags or share("status_performance") > 0.20:
        thread_type = "status_thread"
    elif share("argument_reply") + share("insult") > 0.40 and tag_counter["pile_on"] >= 1:
        thread_type = "flame_war"
    elif share("argument_reply") > 0.25:
        thread_type = "argument_thread"
    elif share("advice") > 0.20:
        thread_type = "advice_thread"
    else:
        thread_type = "general_chat"

    for p in posts:
        p["thread_type"] = thread_type
    return posts, thread_type


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def process_file(in_path: Path, out_path: Path) -> dict:
    threads: dict[int, list[dict]] = defaultdict(list)
    n_in = 0
    for post in iter_jsonl(in_path):
        n_in += 1
        tnum = post.get("thread_num")
        if tnum is None:
            continue
        threads[int(tnum)].append(post)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tag_totals: Counter = Counter()
    type_totals: Counter = Counter()
    n_out = 0
    with out_path.open("w", encoding="utf-8") as out_fh:
        for tnum, posts in threads.items():
            posts, ttype = label_thread(posts)
            type_totals[ttype] += 1
            for p in posts:
                for t in p["style_tags"]:
                    tag_totals[t] += 1
                out_fh.write(json.dumps(p, ensure_ascii=False) + "\n")
                n_out += 1

    return {
        "in_rows": n_in, "out_rows": n_out, "threads": len(threads),
        "tag_totals": dict(tag_totals), "thread_type_totals": dict(type_totals),
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--in", dest="in_path", help="Input JSONL file.")
    g.add_argument("--in-dir", dest="in_dir", help="Input directory (process all *.jsonl).")
    ap.add_argument("--out", dest="out_path", help="Output JSONL file (with --in).")
    ap.add_argument("--out-dir", dest="out_dir",
                    help="Output directory (with --in-dir; mirrors input filenames).")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.in_path:
        if not args.out_path:
            sys.exit("--out is required with --in")
        stats = process_file(Path(args.in_path), Path(args.out_path))
        if not args.quiet:
            print(json.dumps(stats, indent=2))
        return

    in_dir = Path(args.in_dir)
    out_dir = Path(args.out_dir or (in_dir.parent / (in_dir.name + "_labeled")))
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in in_dir.glob("*.jsonl") if not p.name.startswith("._"))
    grand_tags: Counter = Counter()
    grand_types: Counter = Counter()
    grand_in = grand_out = grand_threads = 0
    for f in files:
        out_f = out_dir / f.name
        stats = process_file(f, out_f)
        grand_in += stats["in_rows"]
        grand_out += stats["out_rows"]
        grand_threads += stats["threads"]
        for k, v in stats["tag_totals"].items():
            grand_tags[k] += v
        for k, v in stats["thread_type_totals"].items():
            grand_types[k] += v
        if not args.quiet:
            print(f"[{f.name}] threads={stats['threads']:,} rows={stats['out_rows']:,}", flush=True)

    if not args.quiet:
        print(json.dumps({
            "files": len(files), "in_rows": grand_in, "out_rows": grand_out,
            "threads": grand_threads,
            "tag_totals": dict(grand_tags),
            "thread_type_totals": dict(grand_types),
        }, indent=2))


if __name__ == "__main__":
    main()
