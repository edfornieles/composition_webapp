"""
Build /fit/ SFT dataset in the rich RAG-grounded format.

Output format per example:
  system:    FitAnon persona + safety bounds
  user:      MODE + BOARD + THREAD_CONTEXT + RETRIEVED_POSTS + TASK
  assistant: the actual post text (cleaned)

Mode is assigned heuristically from post content.
RETRIEVED_POSTS are sampled from other threads with similar mode (stand-in
for real BM25/CLIP retrieval at inference time).

Usage:
  python build_fit_sft.py \
      --src /Volumes/Oom/imageboard_corpora/imageboard_text_desu/text/fit.jsonl \
      --out /Volumes/Oom/aggregation/composition_webapp-main/training/fit_lora/data/fit_sft.jsonl \
      --target 10000 \
      --seed 42
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
from collections import defaultdict
from typing import Optional

# ---------------------------------------------------------------------------
# Mode assignment
# ---------------------------------------------------------------------------

MODES: dict[str, list[str]] = {
    # Must look like a >be me narrative greentext, not just a >>quote reply.
    "inner_monologue": [
        r"^>be me",
        r"^>mfw",
        r"^>tfw",
        r"^>implying",
        r"(?:^>\w.+\n){2,}",   # 2+ consecutive greentext lines
        r"\bfeels (?:bad|good|weird|horrible)\b",
    ],
    "confession": [
        r"i used to",
        r"i admit",
        r"truth is",
        r"i was embarrass",
        r"\bashamed\b",
        r"i’m guilty",
        r"secret(?:ly)?",
        r"never told",
    ],
    "body_judgement": [
        r"\bnatty\b",
        r"\brate (?:my|this|him|his)\b",
        r"\b\d+/10\b",
        r"\bdyel\b",
        r"\bbodyfat\b",
        r"\bbf%\b",
        r"what does /fit/ think of (?:my|this)\b",
        r"\bphysique\b.{0,40}\brate\b",
    ],
    "hope": [
        r"\bone day (?:i|i’ll|i will)\b",
        r"\bsomeday\b",
        r"\bmy goal is\b",
        r"\bwhen i (?:finally|eventually|get|reach|hit)\b",
        r"\bwill be\b.{0,40}\b(?:jacked|swole|lean|big|strong)\b",
        r"i(?:’m| am) going to (?:make it|be|become)",
    ],
    "self_attack": [
        r"i(?:’m| am) (?:such a |a )?(?:fat|lazy|failure|loser|weak|pathetic|disgusting)",
        r"\bi suck\b",
        r"\bpathetic\b",
        r"i hate myself",
        r"i(?:’m| am) so (?:fat|weak|small|pathetic|ugly)",
        r"what(?:’s| is) wrong with me",
    ],
    "ritual_instruction": [
        r"\b\d+x\d+\b",   # sets x reps notation
        r"\b(?:sets?|reps?) of\b",
        r"\bprotein (?:intake|goal|shake|powder)\b",
        r"\beat \d+ grams?\b",
        r"\btake (?:creatine|pre|caffeine)\b",
        r"\b(?:starting strength|stronglifts|ppl|nsuns|gzclp)\b",
        r"\b(?:a|b|c) day\b.{0,20}\b(?:squat|bench|dead)\b",
    ],
    "product_review": [
        r"\bpreworkout\b",
        r"\bprotein powder\b",
        r"\btastes? (?:like|good|bad|great|horrible|disgusting)\b",
        r"\bi(?:’d| would) recommend\b",
        r"\bworth (?:the|it|buying)\b",
        r"\b(?:bought|tried) (?:this|a) (?:brand|tub|bag|jug)\b",
    ],
    "image_caption": [
        r"^(?:this|these|look at this|check this|lmao look)",
        r"\bOP(?:’s| is| looks?)\b",
        r"the (?:guy|dude|person|man) in (?:the|this)\b",
        r"\bwhat am i looking at\b",
        r"\bwtf is (?:this|that|wrong with)\b",
    ],
}

MODE_TASK: dict[str, str] = {
    "inner_monologue": "Produce one short inner thought in the voice of someone from /fit/.",
    "confession": "Produce one candid confession or admission.",
    "body_judgement": "Produce one blunt body or physique judgement.",
    "hope": "Produce one aspiration or future-self statement.",
    "self_attack": "Produce one moment of self-criticism or frustration.",
    "ritual_instruction": "Produce one practical instruction or routine tip.",
    "product_review": "Produce one short product or supplement take.",
    "image_caption": "Produce one dry image-reaction or caption.",
    "reply_like_thread": "Produce one typical /fit/ reply.",
}

_COMPILED = {
    mode: [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in pats]
    for mode, pats in MODES.items()
}


def assign_mode(text: str) -> str:
    for mode, pats in _COMPILED.items():
        for pat in pats:
            if pat.search(text):
                return mode
    return "reply_like_thread"


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

_QUOTE_RE = re.compile(r">>\d+\n?")
_GREENTEXT_RE = re.compile(r"^>", re.MULTILINE)
_HTML_RE = re.compile(r"<[^>]+>")
_MULTI_NL = re.compile(r"\n{3,}")


def clean_comment(text: str) -> str:
    text = _HTML_RE.sub("", text)
    text = _QUOTE_RE.sub("", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Thread reconstruction
# ---------------------------------------------------------------------------

def group_threads(src: str) -> dict[int, list[dict]]:
    threads: dict[int, list[dict]] = defaultdict(list)
    with open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            threads[row["thread_num"]].append(row)
    # sort each thread by post_num
    for posts in threads.values():
        posts.sort(key=lambda p: int(p["post_num"]))
    return threads


# ---------------------------------------------------------------------------
# Example builder
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an anonymous poster on 4chan /fit/. "
    "Crude, cynical, dry. Replies are 1–3 lines, never polished. "
    "Use > to start a greentext line. No name, no preamble, no disclaimers. "
    "You are not a helpful assistant. You are an anon."
)


def fmt_post(post: dict, label: str = "") -> str:
    subject = post.get("subject") or ""
    comment = clean_comment(post.get("comment") or "")
    parts = []
    if label:
        parts.append(f"[{label}]")
    if subject:
        parts.append(f'"{subject}"')
    parts.append(comment[:300])
    return " ".join(parts)


def build_example(
    thread_posts: list[dict],
    reply_idx: int,
    retrieved: list[dict],
    mode: str,
) -> Optional[dict]:
    target = thread_posts[reply_idx]
    comment = clean_comment(target.get("comment") or "")
    if len(comment) < 60:
        return None

    # thread context: OP + up to 3 posts before target
    context_posts = thread_posts[:reply_idx]
    op = context_posts[0] if context_posts else target
    prev_replies = context_posts[1:][-3:]

    ctx_lines = [fmt_post(op, "OP")]
    for i, p in enumerate(prev_replies, 1):
        ctx_lines.append(fmt_post(p, f"Reply {i}"))
    thread_context = "\n".join(ctx_lines)

    # retrieved posts
    ret_lines = []
    for i, p in enumerate(retrieved[:6], 1):
        ret_lines.append(fmt_post(p, f"Post {chr(64+i)}"))
    retrieved_block = "\n".join(ret_lines) if ret_lines else "(none)"

    user_content = (
        f"MODE: {mode}\n"
        f"BOARD: /fit/\n\n"
        f"THREAD_CONTEXT:\n{thread_context}\n\n"
        f"RETRIEVED_POSTS:\n{retrieved_block}\n\n"
        f"TASK: {MODE_TASK[mode]}"
    )

    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": comment},
        ]
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--target", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-reply-len", type=int, default=80)
    args = parser.parse_args()

    random.seed(args.seed)

    print(f"Loading {args.src}...")
    threads = group_threads(args.src)
    print(f"  {len(threads):,} threads loaded")

    # Build a pool of posts per mode for retrieval
    mode_pool: dict[str, list[dict]] = defaultdict(list)
    all_posts: list[dict] = []
    for posts in threads.values():
        for p in posts:
            if len((p.get("comment") or "")) >= args.min_reply_len:
                mode = assign_mode(p["comment"])
                mode_pool[mode].append(p)
                all_posts.append(p)

    print(f"  Mode distribution:")
    for m, pool in sorted(mode_pool.items()):
        print(f"    {m}: {len(pool):,}")

    # Cap per mode so reply_like_thread (the catch-all) doesn't dominate.
    # Give minority modes 2x their natural share, cap majority modes.
    n_modes = len(MODES) + 1  # +1 for reply_like_thread
    soft_cap = max(500, args.target // n_modes * 2)
    mode_counts_live: dict[str, int] = defaultdict(int)

    examples: list[dict] = []
    thread_ids = list(threads.keys())
    random.shuffle(thread_ids)

    for tid in thread_ids:
        if len(examples) >= args.target:
            break
        posts = threads[tid]
        if len(posts) < 3:
            continue
        for idx in range(1, len(posts)):
            p = posts[idx]
            comment = p.get("comment") or ""
            if len(comment) < args.min_reply_len:
                continue
            mode = assign_mode(comment)
            # skip if this mode is already over-represented
            if mode_counts_live[mode] >= soft_cap:
                continue
            # retrieve from pool excluding this thread
            pool = [x for x in mode_pool[mode] if x["thread_num"] != tid]
            if len(pool) < 3:
                pool = [x for x in all_posts if x["thread_num"] != tid]
            retrieved = random.sample(pool, min(6, len(pool)))
            ex = build_example(posts, idx, retrieved, mode)
            if ex is None:
                continue
            examples.append(ex)
            mode_counts_live[mode] += 1
            if len(examples) >= args.target:
                break

    print(f"\nBuilt {len(examples):,} examples")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    print(f"Saved → {args.out}")

    # Mode breakdown of final set
    mode_counts: dict[str, int] = defaultdict(int)
    for ex in examples:
        user_content = ex["messages"][1]["content"]
        m = re.search(r"^MODE: (.+)$", user_content, re.MULTILINE)
        if m:
            mode_counts[m.group(1)] += 1
    print("Final mode breakdown:")
    for mode, cnt in sorted(mode_counts.items(), key=lambda x: -x[1]):
        print(f"  {mode}: {cnt:,} ({100*cnt/len(examples):.1f}%)")


if __name__ == "__main__":
    main()
