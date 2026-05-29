"""Scrape /fit/ greentext OPs from desuarchive.org for LoRA training.

Strategy:
  - Hit the search API for multiple greentext-marker queries (`>be me`,
    `>tfw`, `>mfw`, `>still no gf`, etc.) on the /fit/ board.
  - Paginate up to MAX_PAGES_PER_QUERY each.
  - Filter to OPs only (num == thread_num).
  - Filter to greentext shape: ≥5 lines starting with `>`.
  - De-duplicate by thread_num.
  - Save raw to training/fit_lora/greentext_corpus/raw_desuarchive.jsonl.

Polite scraping:
  - User-Agent identifies as research corpus build.
  - 1.5s sleep between requests.
  - Handles 429/503 with exponential backoff.

Output JSONL row: {"thread_num", "timestamp", "title", "comment", "source"}
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "training" / "fit_lora" / "greentext_corpus"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_PATH = OUT_DIR / "raw_desuarchive.jsonl"
STATE_PATH = OUT_DIR / "scrape_state.json"

USER_AGENT = "Mozilla/5.0 (greentext-research-corpus-build)"
BASE = "https://desuarchive.org/_/api/chan/search/"

# Query terms — each captures a class of greentext openers. Order is
# rough priority (most common first). Each runs MAX_PAGES_PER_QUERY pages.
QUERIES: list[str] = [
    ">be me",
    ">be 23",
    ">be 25",
    ">tfw no gf",
    ">tfw",
    ">mfw",
    ">be 28",
    ">be 30",
    ">dyel",
    ">be manlet",
    ">be skellington",
    ">be skinnyfat",
    ">go to gym",
    ">arrive at gym",
    ">first day at gym",
    ">mirror",
    ">spreadsheet",
    ">wife",
    ">gf",
    ">she said",
    ">bus stop",
    # supplemental — fired after first pass
    ">walk in",
    ">walk into",
    ">just",
    ">cant",
    ">trying to",
    ">decide to",
    ">finally",
    ">accidentally",
    ">someone",
    ">girl at",
    ">guy at",
    ">today",
    ">last week",
    ">last night",
    ">this morning",
    ">at 6am",
    ">at planet fitness",
    ">on tinder",
    ">on bumble",
    ">on tren",
    ">on cycle",
    ">at work",
    ">at school",
    ">at the beach",
    ">at a wedding",
    ">at a party",
    ">at the doctor",
    ">in the locker",
    ">in the shower",
    ">my mom",
    ">my dad",
    ">my coach",
    ">my boss",
    ">my brother",
    ">my sister",
    ">post body",
    ">post results",
    ">post timestamp",
    ">be poor",
    ">be fat",
    ">be skinny",
    ">be tall",
    ">be short",
    ">no gf",
    ">still no",
    ">friend",
    ">brother",
    ">cousin",
    ">coworker",
    ">stranger",
    ">trainer",
    ">spotted",
]

MAX_PAGES_PER_QUERY = 12   # ~25 results per page; 12 pages = ~300 per query
SLEEP_S = 1.5
MAX_BACKOFF_S = 60
TARGET_OP_COUNT = 2500     # stop once we have this many unique greentext OPs


def _strip_html(s: str) -> str:
    """desuarchive comments have <br>, <a>, html entities. Convert to text."""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<a [^>]*>([^<]+)</a>", r"\1", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return s.strip()


def _fetch(url: str) -> dict:
    """Fetch JSON with retry on 429/503/network errors."""
    backoff = 2
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 502, 504):
                wait = min(backoff, MAX_BACKOFF_S)
                print(f"  http {e.code}, backoff {wait}s", file=sys.stderr)
                time.sleep(wait)
                backoff *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            wait = min(backoff, MAX_BACKOFF_S)
            print(f"  net err {e}, backoff {wait}s", file=sys.stderr)
            time.sleep(wait)
            backoff *= 2
            continue
    raise RuntimeError(f"Gave up on {url}")


def _is_greentext_shaped(comment: str) -> bool:
    if not comment:
        return False
    lines = [l.strip() for l in comment.splitlines() if l.strip()]
    if len(lines) < 5:
        return False
    gt_lines = sum(1 for l in lines if l.startswith(">") and not l.startswith(">>"))
    if gt_lines < 5:
        return False
    # Most lines should be greentext (avoid replies with a few quotes)
    if gt_lines / len(lines) < 0.55:
        return False
    return True


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"seen_threads": [], "queries_done": []}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _load_existing_ops() -> set[str]:
    seen = set()
    if OUT_PATH.exists():
        for line in OUT_PATH.read_text().splitlines():
            try:
                seen.add(json.loads(line)["thread_num"])
            except Exception:
                continue
    return seen


def main() -> None:
    state = _load_state()
    existing = _load_existing_ops()
    seen_threads: set[str] = set(state.get("seen_threads", [])) | existing
    queries_done: set[str] = set(state.get("queries_done", []))

    print(f"Start: {len(seen_threads)} threads already saved.", file=sys.stderr)
    out_fh = OUT_PATH.open("a", encoding="utf-8")

    try:
        for q in QUERIES:
            if q in queries_done:
                continue
            if len(seen_threads) >= TARGET_OP_COUNT:
                print(f"Hit target {TARGET_OP_COUNT}, stopping.", file=sys.stderr)
                break
            print(f"\n=== query: {q!r} ===", file=sys.stderr)
            for page in range(1, MAX_PAGES_PER_QUERY + 1):
                params = urllib.parse.urlencode({
                    "boards": "fit", "text": q, "type": "op", "page": str(page),
                })
                url = f"{BASE}?{params}"
                try:
                    payload = _fetch(url)
                except Exception as e:
                    print(f"  page {page}: fetch failed {e}", file=sys.stderr)
                    break
                # Response shape: {"0": {"posts": [...]}}
                posts = []
                for k, v in payload.items():
                    if isinstance(v, dict) and "posts" in v:
                        posts.extend(v["posts"])
                if not posts:
                    print(f"  page {page}: no posts, stopping", file=sys.stderr)
                    break
                new_this_page = 0
                for post in posts:
                    num = str(post.get("num", ""))
                    thread_num = str(post.get("thread_num", ""))
                    # OP only
                    if num != thread_num or not thread_num:
                        continue
                    if thread_num in seen_threads:
                        continue
                    raw_comment = post.get("comment", "") or ""
                    comment = _strip_html(raw_comment)
                    if not _is_greentext_shaped(comment):
                        continue
                    row = {
                        "thread_num": thread_num,
                        "timestamp": post.get("timestamp"),
                        "title": _strip_html(post.get("title") or ""),
                        "comment": comment,
                        "source": "desuarchive",
                        "scrape_query": q,
                    }
                    out_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    out_fh.flush()
                    seen_threads.add(thread_num)
                    new_this_page += 1
                print(f"  page {page}: +{new_this_page} (total {len(seen_threads)})",
                      file=sys.stderr)
                # Save state after each page so a kill doesn't lose progress
                state["seen_threads"] = sorted(seen_threads)
                _save_state(state)
                time.sleep(SLEEP_S)
                if len(seen_threads) >= TARGET_OP_COUNT:
                    break
            queries_done.add(q)
            state["queries_done"] = sorted(queries_done)
            _save_state(state)
    finally:
        out_fh.close()
        state["seen_threads"] = sorted(seen_threads)
        state["queries_done"] = sorted(queries_done)
        _save_state(state)

    print(f"\nDone. {len(seen_threads)} unique greentext OPs in {OUT_PATH}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
