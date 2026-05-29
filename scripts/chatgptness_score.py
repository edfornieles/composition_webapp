"""Heuristic ChatGPT-ness detector for chan_anon eval.

Scores 0..1 — higher means MORE ChatGPT-y (worse). Used as a fast pre-screen
during smoke / real training runs so we don't need to wait for full manual
eval to know if voice is collapsing.

Detects:
  - assistant openers ("As an AI", "I'm happy to", "Certainly!", "Of course!")
  - hedging stock phrases ("It's important to remember", "While X, it's also")
  - "great question" / "absolutely" / "definitely" markers
  - bulleted list usage in unexpected contexts
  - balanced both-sides framing
  - therapy language ("I understand you're feeling", "Have you tried...")
  - excessive politeness, summaries before reply
  - "happy to help" / "let me know if"

Per chan_anon framing memory, this is one of the bars: outputs that read
as ChatGPT-flavored fail the artwork test. Run this on every batch of
generations during smoke evaluation.

Usage:
    from chatgptness_score import score_text, score_jsonl

    score = score_text("Certainly! It's important to remember that...")
    # score → ~0.85

    python scripts/chatgptness_score.py --in generations.jsonl --field output
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Phrase → weight (0..1 contribution per occurrence, capped overall).
# Tuned by eye; iterate empirically.
PHRASE_WEIGHTS = [
    (r"\bas an ai\b", 1.00),
    (r"\bi'?m an ai\b", 1.00),
    (r"\bi cannot (?:and will not|provide|generate|assist|help with)", 0.95),
    (r"\bi (?:can'?t|will not) (?:provide|generate|create|write|assist)", 0.85),
    (r"\bcertainly!?", 0.55),
    (r"^of course[!,]", 0.55),
    (r"^absolutely[!,]", 0.55),
    (r"\bgreat question\b", 0.95),
    (r"\bhappy to help\b", 0.85),
    (r"\blet me know if\b", 0.55),
    (r"\bi'?d be happy to\b", 0.85),
    (r"\bit'?s important to (?:remember|note|consider|recognize)", 0.85),
    (r"\bit'?s worth noting\b", 0.55),
    (r"\bthat (?:said|being said), it'?s", 0.55),
    (r"\bwhile (?:it'?s|.{0,30}), it'?s also\b", 0.55),
    (r"\bon (?:one|the other) hand\b", 0.30),
    (r"\bin conclusion\b", 0.55),
    (r"\bi understand (?:that )?you'?re feeling\b", 0.95),
    (r"\bhave you (?:tried|considered)\b", 0.55),
    (r"\bit sounds like you'?re\b", 0.85),
    (r"\bi hear (?:that )?you\b", 0.85),
    (r"\bthat must be (?:hard|difficult|frustrating)\b", 0.85),
    (r"\bthis is a complex (?:issue|topic|question)\b", 0.55),
    (r"\bthere'?s no (?:one[- ]size|single) (?:right )?answer\b", 0.55),
    (r"\bremember[,]? (?:everyone|it'?s ok|that)\b", 0.55),
    (r"\bself[- ]care\b", 0.30),
    (r"\bseek (?:professional )?help\b", 0.55),
    (r"\bspeak (?:with|to) a (?:professional|therapist|doctor)\b", 0.85),
    (r"\bto sum (?:it )?up\b", 0.55),
    (r"\bin summary\b", 0.55),
    (r"\boverall[,]?", 0.30),
]

# Compiled patterns
_PATTERNS = [(re.compile(p, re.IGNORECASE | re.MULTILINE), w) for p, w in PHRASE_WEIGHTS]

# Bulleted list pattern (3+ bullets in a row)
_BULLETS_RE = re.compile(r"(?:^[\s]*[-•*]\s+\S.*\n){3,}", re.MULTILINE)
_NUMBERED_RE = re.compile(r"(?:^[\s]*\d+[.)]\s+\S.*\n){3,}", re.MULTILINE)

# "Summary of the thread before reply" — opens by restating the prompt
_SUMMARY_OPEN_RE = re.compile(
    r"^\s*(it (?:seems|sounds|appears) (?:like )?(?:you|the)|you (?:are|seem to be) asking|"
    r"based on (?:what|your)|so it (?:sounds|seems))",
    re.IGNORECASE,
)


def score_text(text: str) -> dict:
    """Return {'score': 0..1, 'hits': [...]}."""
    if not text:
        return {"score": 0.0, "hits": []}
    weight_sum = 0.0
    hits = []
    for pat, w in _PATTERNS:
        m = pat.search(text)
        if m:
            weight_sum += w
            hits.append({"pattern": pat.pattern, "weight": w, "snippet": m.group(0)[:60]})

    if _BULLETS_RE.search(text):
        weight_sum += 0.7
        hits.append({"pattern": "bulleted_list_3plus", "weight": 0.7})
    if _NUMBERED_RE.search(text):
        weight_sum += 0.7
        hits.append({"pattern": "numbered_list_3plus", "weight": 0.7})
    if _SUMMARY_OPEN_RE.match(text.strip()):
        weight_sum += 0.55
        hits.append({"pattern": "summary_opener", "weight": 0.55})

    # Saturation curve so a single very strong hit doesn't max out
    score = 1.0 - (1.0 / (1.0 + weight_sum))
    return {"score": round(score, 3), "hits": hits}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="in_path", required=True, help="JSONL file of generations.")
    ap.add_argument("--field", default="output", help="JSON field containing the generated text.")
    ap.add_argument("--threshold", type=float, default=0.5, help="Flag if >= threshold.")
    ap.add_argument("--max-show-hits", type=int, default=3)
    args = ap.parse_args()

    n = flagged = 0
    total_score = 0.0
    with open(args.in_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = row.get(args.field) or ""
            res = score_text(text)
            n += 1
            total_score += res["score"]
            if res["score"] >= args.threshold:
                flagged += 1
                print(f"[{res['score']:.2f}] {text[:120]!r}")
                for h in res["hits"][: args.max_show_hits]:
                    print(f"     hit: {h.get('pattern')} (w={h.get('weight')})")
    if n:
        avg = total_score / n
        print(f"\nN={n}  flagged={flagged} ({flagged/n:.0%})  avg_chatgptness={avg:.3f}")


if __name__ == "__main__":
    main()
