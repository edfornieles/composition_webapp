"""Scrape the live /fit/ catalog and write OP seeds for the board simulator.

Usage:
    python3.10 scripts/scrape_fit_op_seeds.py

Reads /fit/ catalog from the public 4chan JSON API (rate-limited 1 req/s),
extracts OP subjects + bodies, runs each candidate through target_policy +
quality filters, and writes the keepers to
`training/fit_lora/board_op_seeds.json` for `_fit_board.py` to load.

This is read-only against the live API — no posting. ~1 round-trip per
catalog page; 15-20 pages total = ~20 seconds.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# Make the djangoscrap package importable so we can use target_policy
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")
try:
    import django
    django.setup()
except Exception:
    pass

from djangoscrap.imageboard_ingestion import target_policy as _tp  # noqa: E402

# Pass-2-containment rule: even rhetorical self-harm framing should not be
# surfaced as a board OP, because the chat-reply path silences responses to
# those — the resulting thread would have an empty reply tail. Use the same
# regex the chat input gate uses.
from djangoscrap.views._characters import _SELF_HARM_INPUT_RE  # noqa: E402

# Also reject violent framing in OPs (rhetorical or not) — the artwork
# doesn't autonomously surface "kill them / hurt them" prompts.
_VIOLENT_OP_RE = re.compile(
    r"\b(?:kill|hurt|harm|stab|shoot|punch|beat\s+up|bash)\s+(?:them|him|her|people)\b"
    r"|\bbody[\s-]?slam\b"
    r"|\bzercher\s+deadlifts?\s+(?:so|to)\b",
    re.I,
)


CATALOG_URL = "https://a.4cdn.org/fit/catalog.json"
OUT_PATH = ROOT / "training" / "fit_lora" / "board_op_seeds.json"
USER_AGENT = "Mozilla/5.0 (research-corpus-comparison)"

# Skip well-known sticky / general / megathread OPs. We want fresh threads.
_GENERAL_MARKERS = re.compile(
    r"\b(?:sticky|general|edition|previous|prev\b|##\s*mod|"
    r"FAQ|recommended reading|click here to view)\b",
    re.I,
)


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _strip_html(text: str) -> str:
    text = text or ""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<a [^>]*>([^<]+)</a>", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return text.strip()


def _guess_image_keyword(body: str, subject: str) -> str:
    """Cheap heuristic — pick a board-flavored noun present in the text."""
    bag = (body + " " + subject).lower()
    pool = [
        "mirror", "scale", "barbell", "bench", "rack", "fridge", "gym",
        "spreadsheet", "protein", "phone", "alarm", "locker", "bus",
        "stairwell", "park", "chalk", "tape", "shorts", "hoodie",
        "scale", "creatine", "tren", "deca", "test", "jaw", "shoulder",
    ]
    for word in pool:
        if word in bag:
            return word
    return "thumb"


def _is_quality_op(subject: str, body: str) -> bool:
    """Keep only OPs that look like real /fit/ openers."""
    text = (subject + " " + body).strip()
    if not text:
        return False
    if _GENERAL_MARKERS.search(text):
        return False
    if "http" in body.lower() or "www." in body.lower():
        # Most link-heavy OPs are general threads / link dumps
        return False
    words = body.split()
    if len(words) < 4 or len(words) > 40:
        return False
    # Skip pure image OPs
    if len(text.replace("\n", " ").strip()) < 10:
        return False
    # Drop OPs that quote heavily (>2 lines starting with >)
    gt_lines = sum(1 for line in body.splitlines() if line.strip().startswith(">"))
    if gt_lines > 3:
        return False
    # Pass-2-containment: never surface self-harm-rhetorical or violent OPs.
    if _SELF_HARM_INPUT_RE.search(body) or _VIOLENT_OP_RE.search(body):
        return False
    return True


def main() -> None:
    print(f"Fetching {CATALOG_URL}…", file=sys.stderr)
    catalog = _fetch(CATALOG_URL)
    time.sleep(1.1)

    raw_ops: list[tuple[str, str]] = []
    for page in catalog:
        for t in page.get("threads", []):
            sub = _strip_html(t.get("sub", ""))
            body = _strip_html(t.get("com", ""))
            raw_ops.append((sub, body))

    print(f"  catalog: {len(raw_ops)} OPs total", file=sys.stderr)

    seeds: list[dict] = []
    accepted = 0
    rejected_quality = 0
    rejected_policy = 0
    rejected_duplicate = 0
    seen_norms: set[str] = set()

    for sub, body in raw_ops:
        if not _is_quality_op(sub, body):
            rejected_quality += 1
            continue
        policy = _tp.classify_target(body)
        if policy.status == "target_reject":
            rejected_policy += 1
            continue
        body_use = policy.redacted_text if policy.status == "target_ok_with_redaction" else policy.raw_text
        # Light cleanup
        body_use = re.sub(r"\s+\n", "\n", body_use)
        body_use = re.sub(r"\n{3,}", "\n\n", body_use).strip()
        # Cap to first 3 lines / 240 chars to keep OP shape
        body_use = "\n".join(body_use.splitlines()[:3])[:240].rstrip()
        # Normalize for dedupe
        norm = re.sub(r"\W+", "", body_use.lower())[:60]
        if not norm or norm in seen_norms:
            rejected_duplicate += 1
            continue
        seen_norms.add(norm)
        seeds.append({
            "subject": sub[:80],
            "body": body_use,
            "image_keyword": _guess_image_keyword(body_use, sub),
            "policy_status": policy.status,
        })
        accepted += 1

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(seeds, indent=2, ensure_ascii=False),
                        encoding="utf-8")

    print(f"\n=== scrape summary ===", file=sys.stderr)
    print(f"  catalog OPs:        {len(raw_ops)}", file=sys.stderr)
    print(f"  accepted:           {accepted}", file=sys.stderr)
    print(f"    raw (no redact):  {sum(1 for s in seeds if s['policy_status'] == 'target_ok_realistic')}", file=sys.stderr)
    print(f"    redacted:         {sum(1 for s in seeds if s['policy_status'] == 'target_ok_with_redaction')}", file=sys.stderr)
    print(f"  rejected quality:   {rejected_quality}", file=sys.stderr)
    print(f"  rejected policy:    {rejected_policy}", file=sys.stderr)
    print(f"  rejected duplicate: {rejected_duplicate}", file=sys.stderr)
    print(f"  written to:         {OUT_PATH}", file=sys.stderr)

    # Print a handful for human eyeballing
    print(f"\n=== first 8 accepted seeds ===")
    for s in seeds[:8]:
        print(f"  subject: {s['subject']!r}")
        print(f"  body:    {s['body']!r}")
        print(f"  image:   {s['image_keyword']}")
        print()


if __name__ == "__main__":
    main()
