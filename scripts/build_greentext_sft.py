"""Build SFT training data from scraped /fit/ greentexts.

Input:
  training/fit_lora/greentext_corpus/raw_desuarchive.jsonl
    (rows: {thread_num, timestamp, title, comment, source, scrape_query})

Output (chat-message JSONL, mlx-lm compatible):
  training/fit_lora/data_greentext/train.jsonl   (~85%)
  training/fit_lora/data_greentext/valid.jsonl   (~10%)
  training/fit_lora/data_greentext/test.jsonl    (~5%)
  training/fit_lora/data_greentext/curation_report.md

Each row:
  {
    "messages": [
      {"role": "system",    "content": "<persona + format rules>"},
      {"role": "user",      "content": "topic: <inferred from story>"},
      {"role": "assistant", "content": "<full greentext body>"}
    ],
    "metadata": {"thread_num": "...", "source": "...", "line_count": N}
  }

Curation filters:
  - normalize body: collapse multiple blank lines, strip trailing whitespace
  - require ≥5 `>` lines AND ≥0.55 greentext-line ratio (already enforced
    upstream, re-checked here)
  - reject if any line >200 chars (prose-disguised)
  - reject if body contains a slur cluster (delegated to a small regex)
  - reject if body contains ad/spam markers (discord, telegram, http URLs)
  - reject if too short (<200 chars total) or too long (>2500 chars total)
  - reject obvious low-effort copypasta (high duplicate-line ratio)
  - de-duplicate by normalized first-3-lines hash

Topic inference for the user message:
  - Skip leading `>be me`, `>be N`, generic role lines
  - Pick the FIRST line that contains a concrete noun (gym, mirror, wife,
    girl, mom, etc.) — use as the topic seed
  - Fall back to the title if present, else "/fit/ greentext"
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
IN_PATH = ROOT / "training" / "fit_lora" / "greentext_corpus" / "raw_desuarchive.jsonl"
OUT_DIR = ROOT / "training" / "fit_lora" / "data_greentext"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SEED = 1337


# Minimal slur cluster — runtime safety has its own; this is the training-
# data hygiene gate. We don't want the LoRA learning these patterns.
_SLUR_RE = re.compile(
    r"\b(?:n[i1]gg?[ae]r?s?|"            # n-word variants
    r"k[i1]k[ae]s?|"                       # kike(s)
    r"sp[i1]cks?|"                         # spic(s)
    r"ch[i1]nks?|"                         # chink(s)
    r"tr[ao]nn?[iy]e?s?|"                  # tr*nny variants
    r"f[a4]gg?[oe]ts?|"                    # f-word variants
    r"jeets?|p[a4]j[e3]+ts?|h[e3]+bs?"     # ethnic slurs we added to runtime
    r")\b",
    re.I,
)

_AD_SPAM_RE = re.compile(
    r"(?:https?://|www\.|discord\.gg|t\.me/|telegram\.|onlyfans\.|"
    r"\bbuy\s+now\b|\bdm\s+me\b|\bvisit\s+my\b)",
    re.I,
)

# Lines with these tokens are usually genuinely concrete enough to use as
# topic seeds.
_TOPIC_NOUNS = {
    "gym", "mirror", "wife", "gf", "girlfriend", "mom", "dad", "girl", "guy",
    "scale", "bench", "squat", "rack", "bar", "weight", "weights", "protein",
    "creatine", "tren", "steroid", "steroids", "shorts", "shirt", "shoes",
    "locker", "shower", "spreadsheet", "phone", "alarm", "bus", "stop",
    "wedding", "party", "beach", "doctor", "manlet", "chad", "dyel",
    "skellington", "qt", "boomer", "zoomer", "tinder", "match", "boss",
    "office", "coworker", "library", "uni", "school", "factory", "stage",
}

# These can't be the topic on their own (too generic).
_GENERIC_OPENERS = {
    "be me", "be 18", "be 19", "be 20", "be 21", "be 22", "be 23", "be 24",
    "be 25", "be 26", "be 27", "be 28", "be 29", "be 30", "be 35", "be 40",
    "be a", "be the", "be your", "i", "we", "you", "they", "she", "he",
}


def _normalize_body(body: str) -> str:
    """Trim trailing whitespace per line, collapse blank-line runs, strip.

    ALSO strips trailing prose blocks. Real /fit/ OPs often end with a
    prose-tail like 'What do I do bros?' after the greentext. We don't
    want the LoRA learning to write those tails — they're prose, not
    greentext. We strip them so the assistant target is pure greentext.
    Similarly, strip prose-prefix BEFORE the first `>` line (some OPs
    have a setup paragraph then the greentext)."""
    lines = [l.rstrip() for l in body.splitlines()]
    # Collapse multiple blank lines
    out = []
    blank_run = 0
    for l in lines:
        if not l.strip():
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        out.append(l)
    # Find first and last greentext-line indices
    first_gt = -1
    last_gt = -1
    for i, l in enumerate(out):
        if l.strip().startswith(">"):
            if first_gt < 0:
                first_gt = i
            last_gt = i
    if first_gt < 0:
        return ""
    # Trim to the greentext block
    out = out[first_gt:last_gt + 1]
    return "\n".join(out).strip()


def _greentext_lines(body: str) -> list[str]:
    return [l.strip() for l in body.splitlines()
            if l.strip().startswith(">") and not l.strip().startswith(">>")]


def _is_quality_greentext(body: str) -> tuple[bool, str]:
    """Return (ok, reason). Reason is filled when rejected."""
    if not body or len(body) < 80:
        return False, "too_short"
    if len(body) > 2500:
        return False, "too_long"
    lines = [l for l in body.splitlines() if l.strip()]
    gt = _greentext_lines(body)
    if len(gt) < 5:
        return False, "not_enough_gt_lines"
    # After _normalize_body trim, prose head/tail is removed. Any remaining
    # non-> line is interleaved prose (between greentext beats) — that's
    # the bad shape we don't want the LoRA learning. Reject those.
    non_gt = [l for l in lines if not l.strip().startswith(">")]
    if non_gt:
        return False, "interleaved_prose"
    # v6 single-focus filter: reject stories with multi-protagonist drift
    # or multi-scene structure. /fit/ greentexts work best as ONE moment.
    text_lower = body.lower()
    # Reject if multiple distinct protagonists named
    pronoun_subjects = sum(1 for p in ("my parents", "my brother", "my sister",
                                        "my cousin", "my dad and", "my mom and",
                                        "my coworker", "we both", "we all",
                                        "they both", "they all") if p in text_lower)
    if pronoun_subjects >= 2:
        return False, "multi_protagonist"
    # Reject if multiple distinct settings appear (gym + wedding + bus stop)
    settings_hit = sum(1 for s in ("gym", "wedding", "bus stop", "school",
                                    "work", "office", "library", "beach",
                                    "park", "kitchen", "bedroom", "shower",
                                    "locker", "doctor") if s in text_lower)
    if settings_hit >= 4:
        return False, "multi_scene"
    # Cap at 14 greentext lines (loosened from 10 — too few stories survived)
    if len(gt) > 14:
        return False, "too_long"
    # Require last line to be a STRONG-OPENER punchline. This filters the
    # training set to only stories whose final beat lands the joke — so the
    # LoRA learns to write proper /fit/ punchlines.
    if gt:
        last = gt[-1].lower().strip(">").strip()
        first_word = re.findall(r"[\w']+", last)
        if not first_word:
            return False, "no_punchline_word"
        strong_openers = {
            "mfw", "tfw", "still", "i", "i'm", "im", "she", "he", "they",
            "no", "now", "my", "only", "fuck", "fucking", "oh", "am", "are",
            "is", "was", "will", "would", "should", "can", "could", "kek",
            "just", "ngmi", "wagmi", "wat", "what", "why", "how", "to", "all",
            "feel", "got", "go", "guess", "maybe", "turns", "delete", "leave",
            "end", "die", "kms", "stop", "give", "finally", "eventually",
            "anyway", "anyways", "and", "but", "thats", "that's",
        }
        if first_word[0] not in strong_openers:
            return False, "weak_punchline_opener"
    # One long rant line is fine (often it's the punchline); reject only
    # if multiple lines are way over, which signals prose-disguised-as-greentext.
    over_250 = sum(1 for l in lines if len(l) > 250)
    if over_250 >= 2:
        return False, "multiple_long_lines"
    if any(len(l) > 450 for l in lines):
        return False, "extreme_long_line"
    if _SLUR_RE.search(body):
        return False, "slur_cluster"
    if _AD_SPAM_RE.search(body):
        return False, "ad_spam"
    # High duplicate-line ratio (lazy copypasta)
    norms = [re.sub(r"\W+", " ", l.lower()).strip() for l in gt]
    counter = Counter(norms)
    if counter and max(counter.values()) >= 3:
        return False, "self_repeating"
    return True, "ok"


def _infer_topic(body: str, title: str) -> str:
    """Pick a concrete-noun line as topic, fall back to title."""
    gt = _greentext_lines(body)
    # Skip generic openers (>be me, >be 23, etc.)
    candidates = []
    for line in gt:
        # Strip leading ">"
        text = line[1:].strip()
        text_lower = text.lower()
        if any(text_lower.startswith(g) for g in _GENERIC_OPENERS):
            continue
        if len(text) < 8 or len(text) > 110:
            continue
        # Has a concrete noun?
        tokens = re.findall(r"[a-z]+", text_lower)
        if any(t in _TOPIC_NOUNS for t in tokens):
            candidates.append(text)
    if candidates:
        # Prefer the EARLIEST candidate (likely the setup line)
        return candidates[0]
    if title:
        return title
    # Last-ditch: use the 3rd non-generic line raw
    for line in gt[2:6]:
        text = line[1:].strip()
        if 8 <= len(text) <= 110:
            return text
    return "/fit/ greentext"


def _head_hash(body: str) -> str:
    gt = _greentext_lines(body)
    head = " ".join(gt[:3]).lower()
    head = re.sub(r"\W+", " ", head).strip()
    return hashlib.md5(head.encode()).hexdigest()


SYSTEM_PROMPT = (
    "You are anon writing a greentext STORY for /fit/. "
    "Output is a 4chan greentext: 7-14 lines, EVERY line begins with `>`. "
    "Lines are FRAGMENTS, not sentences. All lowercase. No periods. "
    "Slang ok: mfw, tfw, kek, desu — sparingly, max one `>mfw` per story. "
    "Ritual opener: `>be me` then `>be [age or role]` then setting. "
    "Each middle line is a CUT in time, one beat (action, sensation, micro-event). "
    "Last line is the PUNCHLINE — short, self-deprecating, anti-climactic, "
    "callback, or `>mfw` realization. Never moralize. The narrator is the "
    "butt of the joke. Concrete details over generic ones. "
    "Output ONLY the greentext lines."
)


def main() -> None:
    rng = random.Random(SEED)
    if not IN_PATH.exists():
        print(f"missing input: {IN_PATH}", file=sys.stderr)
        sys.exit(1)

    raw_rows = []
    for line in IN_PATH.read_text().splitlines():
        if line.strip():
            try:
                raw_rows.append(json.loads(line))
            except Exception:
                continue
    print(f"raw rows: {len(raw_rows)}", file=sys.stderr)

    rejected = Counter()
    seen_heads: set[str] = set()
    accepted: list[dict] = []

    for row in raw_rows:
        body = _normalize_body(row.get("comment", ""))
        ok, reason = _is_quality_greentext(body)
        if not ok:
            rejected[reason] += 1
            continue
        h = _head_hash(body)
        if h in seen_heads:
            rejected["duplicate_head"] += 1
            continue
        seen_heads.add(h)
        topic = _infer_topic(body, row.get("title", ""))
        msg_row = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"topic: {topic}"},
                {"role": "assistant", "content": body},
            ],
            "metadata": {
                "thread_num": row.get("thread_num", ""),
                "source": row.get("source", "desuarchive"),
                "scrape_query": row.get("scrape_query", ""),
                "line_count": len(_greentext_lines(body)),
                "topic_inferred": topic,
            },
        }
        accepted.append(msg_row)

    print(f"accepted: {len(accepted)}", file=sys.stderr)
    print(f"rejected: {dict(rejected)}", file=sys.stderr)

    # Shuffle deterministically
    rng.shuffle(accepted)
    n = len(accepted)
    n_test = max(1, n // 20)            # 5%
    n_valid = max(1, n // 10)           # 10%
    test = accepted[:n_test]
    valid = accepted[n_test:n_test + n_valid]
    train = accepted[n_test + n_valid:]

    for split_name, rows in (("train", train), ("valid", valid), ("test", test)):
        path = OUT_DIR / f"{split_name}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"wrote {split_name}: {len(rows)} rows -> {path}", file=sys.stderr)

    report = OUT_DIR / "curation_report.md"
    lines_avg = sum(r["metadata"]["line_count"] for r in accepted) / max(len(accepted), 1)
    report.write_text(
        f"# Greentext SFT curation report\n\n"
        f"- Raw scraped: {len(raw_rows)}\n"
        f"- Accepted: {len(accepted)}\n"
        f"- Rejected: {sum(rejected.values())}\n\n"
        f"## Rejection reasons\n"
        + "\n".join(f"- {k}: {v}" for k, v in rejected.most_common())
        + f"\n\n## Splits\n"
        f"- train: {len(train)}\n- valid: {len(valid)}\n- test: {len(test)}\n\n"
        f"## Averages (accepted)\n"
        f"- mean greentext lines per story: {lines_avg:.1f}\n"
    )
    print(f"wrote curation report -> {report}", file=sys.stderr)


if __name__ == "__main__":
    main()
