#!/usr/bin/env python3
"""Convert Pushshift / Arctic Shift Reddit archive dumps into the women-40s corpus.

Reads per-subreddit `.zst` dumps (submissions and/or comments) and emits JSONL
in the same schema as scrape_reddit_full.py, so output can drop into the same
directory tree.

Where to get the dumps:
    - academictorrents.com — search "reddit pushshift". The full bundle is ~3 TB.
    - Per-subreddit slices (much smaller) are mirrored at:
        * https://the-eye.eu/redarcs/        (older, may be down)
        * https://academictorrents.com/      (per-subreddit torrents exist)
        * https://arctic-shift.photon-reddit.com/download-tool   (HTTP, current)
    - Arctic Shift also offers an HTTP search API; this script does not use it,
      but `--api` mode is a reasonable extension.

Filename conventions this script understands:
    {subreddit}_submissions.zst       (Pushshift / arctic-shift bundle)
    {subreddit}_comments.zst
    RS_YYYY-MM.zst                    (monthly, all subreddits — submissions)
    RC_YYYY-MM.zst                    (monthly, all subreddits — comments)

Usage:
    pip install zstandard

    # Convert per-subreddit slices for the women-40s set:
    python scripts/scrape_reddit_archive.py \\
        --dumps /Volumes/Oom/reddit_dumps \\
        --out   /Volumes/Oom/reddit_women40s \\
        --since 2018-01-01 \\
        --images

    # Convert monthly bundle, filtering to TARGET_SUBREDDITS:
    python scripts/scrape_reddit_archive.py \\
        --dumps /Volumes/Oom/reddit_dumps/2024 \\
        --out   /Volumes/Oom/reddit_women40s
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import zstandard as zstd
except ImportError:
    sys.exit("Missing dependency: pip install zstandard")

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

UA = "women40s-corpus/0.3 (academic character research)"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def parse_date(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def iter_zst(path: Path):
    """Stream JSON lines from a zst-compressed Pushshift dump."""
    dctx = zstd.ZstdDecompressor(max_window_size=2**31)
    with path.open("rb") as fh:
        with dctx.stream_reader(fh) as reader:
            buf = b""
            while True:
                chunk = reader.read(2**24)
                if not chunk:
                    break
                buf += chunk
                lines = buf.split(b"\n")
                buf = lines[-1]
                for line in lines[:-1]:
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
            if buf:
                try:
                    yield json.loads(buf)
                except json.JSONDecodeError:
                    pass


def clean(text) -> str:
    if not text or text in ("[removed]", "[deleted]"):
        return ""
    return str(text).strip()


def extract_image_urls(d: dict) -> list[str]:
    urls: list[str] = []
    url = d.get("url") or d.get("url_overridden_by_dest") or ""
    if url:
        from urllib.parse import urlparse
        path = urlparse(url).path
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


def classify_dump(path: Path) -> tuple[str, str | None]:
    """Return (kind, subreddit_or_None). kind in {'submissions','comments'}."""
    name = path.name.lower()
    if name.startswith("rs_"):
        return ("submissions", None)
    if name.startswith("rc_"):
        return ("comments", None)
    stem = path.stem.lower()
    if stem.endswith("_submissions"):
        return ("submissions", stem[: -len("_submissions")])
    if stem.endswith("_comments"):
        return ("comments", stem[: -len("_comments")])
    if "submission" in stem:
        return ("submissions", None)
    if "comment" in stem:
        return ("comments", None)
    return ("submissions", None)


def open_shard(text_dir: Path, sub: str):
    return (text_dir / f"{sub.lower()}.jsonl").open("a", encoding="utf-8")


def process_dump(
    path: Path,
    text_dir: Path,
    image_dir: Path,
    target_set: set[str],
    since_ts: int,
    until_ts: int,
    download_images: bool,
    image_throttle: float,
) -> dict:
    kind, sub_hint = classify_dump(path)
    print(f"[{path.name}] kind={kind} subreddit={sub_hint or '<mixed>'}", flush=True)

    shards: dict[str, object] = {}
    counts: dict[str, int] = {}
    last_image_t = 0.0

    def get_shard(sub: str):
        sub_l = sub.lower()
        if sub_l not in shards:
            shards[sub_l] = open_shard(text_dir, sub_l)
        return shards[sub_l]

    n_seen = 0
    for d in iter_zst(path):
        n_seen += 1
        if n_seen % 100_000 == 0:
            print(f"  scanned {n_seen:,}  written {sum(counts.values()):,}", flush=True)

        sub = d.get("subreddit")
        if not sub:
            continue
        if target_set and sub.lower() not in target_set:
            continue
        ts = int(d.get("created_utc") or 0)
        if since_ts and ts < since_ts:
            continue
        if until_ts and ts > until_ts:
            continue

        if kind == "submissions":
            thread_id = d.get("id")
            if not thread_id:
                continue
            title = clean(d.get("title"))
            if not title:
                continue
            selftext = clean(d.get("selftext"))
            op_body = f"{title}\n\n{selftext}".strip() if selftext else title

            image_urls = extract_image_urls(d)
            image_keys: list[str] = []
            if download_images and image_urls:
                img_sub_dir = image_dir / sub.lower()
                img_sub_dir.mkdir(parents=True, exist_ok=True)
                for idx, img_url in enumerate(image_urls):
                    if image_throttle:
                        gap = image_throttle - (time.time() - last_image_t)
                        if gap > 0:
                            time.sleep(gap)
                    from urllib.parse import urlparse
                    ext = os.path.splitext(urlparse(img_url).path)[1].lower() or ".jpg"
                    fname = f"{thread_id}_{idx}{ext}"
                    dest = img_sub_dir / fname
                    if download_image(img_url, dest):
                        image_keys.append(f"{sub.lower()}/{fname}")
                    last_image_t = time.time()

            row = {
                "subreddit": sub.lower(),
                "thread_id": thread_id,
                "post_id": d.get("name") or f"t3_{thread_id}",
                "timestamp": ts,
                "is_op": True,
                "subject": title,
                "comment": op_body,
                "score": int(d.get("score") or 0),
                "source": "reddit",
                "has_image": bool(image_urls),
                "image_keys": image_keys,
                "image_urls": image_urls,
            }
        else:  # comments
            body = clean(d.get("body"))
            if not body:
                continue
            link_id = d.get("link_id") or ""
            thread_id = link_id[3:] if link_id.startswith("t3_") else link_id
            row = {
                "subreddit": sub.lower(),
                "thread_id": thread_id,
                "post_id": d.get("name") or d.get("id") or "",
                "timestamp": ts,
                "is_op": False,
                "subject": "",
                "comment": body,
                "score": int(d.get("score") or 0),
                "source": "reddit",
                "has_image": False,
                "image_keys": [],
                "image_urls": [],
            }

        fh = get_shard(sub)
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        counts[sub.lower()] = counts.get(sub.lower(), 0) + 1

    for fh in shards.values():
        fh.close()

    print(f"  done. seen={n_seen:,}  written={sum(counts.values()):,}", flush=True)
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dumps", required=True,
                    help="Directory containing .zst dump files (or a single .zst file).")
    ap.add_argument("--out", default="/Volumes/Oom/reddit_women40s",
                    help="Output root (text/, images/ written under here).")
    ap.add_argument("--subreddits", default=",".join(TARGET_SUBREDDITS),
                    help="Comma-separated whitelist. Set to 'all' to keep every subreddit.")
    ap.add_argument("--since", default="", help="Earliest created_utc, YYYY-MM-DD.")
    ap.add_argument("--until", default="", help="Latest created_utc, YYYY-MM-DD.")
    ap.add_argument("--images", action="store_true",
                    help="Download OP images from extracted URLs.")
    ap.add_argument("--image-rps", type=float, default=2.0,
                    help="Max image downloads per second (only if --images).")
    args = ap.parse_args()

    dumps_path = Path(args.dumps)
    if dumps_path.is_file():
        files = [dumps_path]
    else:
        files = sorted(dumps_path.glob("*.zst"))
    if not files:
        sys.exit(f"No .zst files found at {dumps_path}")

    if args.subreddits.strip().lower() == "all":
        target_set: set[str] = set()
    else:
        target_set = {s.strip().lower() for s in args.subreddits.split(",") if s.strip()}

    since_ts = parse_date(args.since) if args.since else 0
    until_ts = parse_date(args.until) if args.until else 0
    image_throttle = (1.0 / args.image_rps) if (args.images and args.image_rps > 0) else 0.0

    out = Path(args.out)
    text_dir = out / "text"
    image_dir = out / "images"
    text_dir.mkdir(parents=True, exist_ok=True)

    print(f"Dumps:   {len(files)} file(s) from {dumps_path}", flush=True)
    print(f"Output:  {out}", flush=True)
    print(f"Filter:  {len(target_set) or 'ALL'} subreddits"
          f"{' since ' + args.since if args.since else ''}"
          f"{' until ' + args.until if args.until else ''}", flush=True)
    print(f"Images:  {'yes' if args.images else 'no'}", flush=True)
    print(flush=True)

    grand_total = 0
    t0 = time.time()
    for f in files:
        counts = process_dump(
            f, text_dir, image_dir,
            target_set=target_set,
            since_ts=since_ts, until_ts=until_ts,
            download_images=args.images,
            image_throttle=image_throttle,
        )
        grand_total += sum(counts.values())

    elapsed = int(time.time() - t0)
    print(f"\nDone. total_rows={grand_total:,}  elapsed={elapsed}s", flush=True)


if __name__ == "__main__":
    main()
