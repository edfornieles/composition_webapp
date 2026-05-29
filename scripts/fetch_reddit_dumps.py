#!/usr/bin/env python3
"""Fetch Reddit Pushshift dumps from academictorrents via aria2c.

Companion to scrape_reddit_archive.py. Reads a manifest of torrent/HTTP sources
and downloads them in parallel using aria2c (which handles both .torrent files
and magnet links).

Setup:
    brew install aria2

Manifest format (default: scripts/reddit_dumps.manifest):
    Lines starting with '#' are comments. Each non-comment line is a URL —
    one of:
        - https://academictorrents.com/download/<INFOHASH>.torrent
        - magnet:?xt=urn:btih:<INFOHASH>&dn=...
        - https://<mirror>/<file>.zst    (direct HTTP, e.g. the-eye.eu)

    Optionally prefix with a label and a tab:
        Menopause<TAB>magnet:?xt=urn:btih:...

Where to find Pushshift Reddit torrents on academictorrents:
    - Search "pushshift reddit" → full 2005-2023 bundle (~3 TB).
    - Search "subreddits24 watchful1" → per-subreddit slices, much smaller.
    - Each result page has both a magnet link and a .torrent download.

Usage:
    # Edit reddit_dumps.manifest with the URLs you want, then:
    python scripts/fetch_reddit_dumps.py \\
        --manifest scripts/reddit_dumps.manifest \\
        --out /Volumes/Oom/reddit_dumps

    # Or pass URLs directly:
    python scripts/fetch_reddit_dumps.py --out /Volumes/Oom/reddit_dumps \\
        "magnet:?xt=urn:btih:..." \\
        "https://academictorrents.com/download/abc123.torrent"
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def ensure_aria2c() -> str:
    path = shutil.which("aria2c")
    if path:
        return path
    sys.exit(
        "aria2c not found.\n"
        "  Install with: brew install aria2\n"
        "  (or: sudo apt install aria2 / sudo port install aria2)"
    )


def parse_manifest(path: Path) -> list[str]:
    urls: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            line = line.split("\t", 1)[1].strip()
        urls.append(line)
    return urls


def run_aria2c(urls: list[str], out_dir: Path, seed_time: int, max_concurrent: int):
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "aria2c",
        "--dir", str(out_dir),
        "--seed-time", str(seed_time),         # 0 = stop seeding immediately after download
        "--max-concurrent-downloads", str(max_concurrent),
        "--continue=true",                     # resume partial downloads
        "--max-connection-per-server=8",
        "--split=8",
        "--min-split-size=4M",
        "--bt-max-peers=120",
        "--bt-request-peer-speed-limit=0",
        "--summary-interval=30",
        "--console-log-level=warn",
        "--auto-file-renaming=false",
        "--allow-overwrite=false",
        # Follow .torrent files automatically (download metadata then content)
        "--follow-torrent=true",
    ]
    cmd.extend(urls)
    print(f"Running: aria2c with {len(urls)} URL(s) → {out_dir}", flush=True)
    print(f"  seed-time={seed_time}s  concurrent={max_concurrent}", flush=True)
    print(flush=True)
    return subprocess.call(cmd)


def list_downloaded(out_dir: Path) -> None:
    zsts = sorted(out_dir.rglob("*.zst"))
    if not zsts:
        print("\nNo .zst files found yet under " + str(out_dir), flush=True)
        return
    total = sum(p.stat().st_size for p in zsts)
    print(f"\n{len(zsts)} .zst file(s), {total / 1e9:.1f} GB total:", flush=True)
    for p in zsts:
        rel = p.relative_to(out_dir)
        print(f"  {p.stat().st_size / 1e9:6.2f} GB  {rel}", flush=True)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", default="/Volumes/Oom/reddit_dumps",
                    help="Download directory.")
    ap.add_argument("--manifest", default="",
                    help="Path to manifest file (one URL per line, or label<TAB>URL).")
    ap.add_argument("--seed-time", type=int, default=0,
                    help="Minutes to seed after download (0 = stop immediately).")
    ap.add_argument("--concurrent", type=int, default=3,
                    help="Max concurrent downloads.")
    ap.add_argument("urls", nargs="*",
                    help="Direct URLs / magnets / .torrent paths.")
    args = ap.parse_args()

    ensure_aria2c()

    urls: list[str] = list(args.urls)
    if args.manifest:
        mpath = Path(args.manifest)
        if not mpath.exists():
            sys.exit(f"Manifest not found: {mpath}")
        urls.extend(parse_manifest(mpath))

    if not urls:
        default_manifest = Path(__file__).parent / "reddit_dumps.manifest"
        if default_manifest.exists():
            urls = parse_manifest(default_manifest)
            print(f"Using default manifest: {default_manifest}", flush=True)

    if not urls:
        sys.exit(
            "No URLs supplied. Either:\n"
            "  - pass URLs as positional args\n"
            "  - pass --manifest scripts/reddit_dumps.manifest\n"
            "  - populate scripts/reddit_dumps.manifest (a starter template was created)"
        )

    out_dir = Path(args.out)
    rc = run_aria2c(urls, out_dir, args.seed_time, args.concurrent)
    list_downloaded(out_dir)
    sys.exit(rc)


if __name__ == "__main__":
    main()
