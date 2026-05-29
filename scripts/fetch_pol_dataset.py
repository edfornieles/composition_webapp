#!/usr/bin/env python3
"""Fetch the Papasavva /pol/ dataset (Raiders of the Lost Kek) from Zenodo.

Dataset: https://zenodo.org/records/3606810
Paper:   https://arxiv.org/abs/2001.07487
Size:    ~24 GB compressed (.tar.zst), ~70-80 GB uncompressed NDJSON
Content: 134.5M /pol/ posts, June 2016 – Nov 2019, with Perspective API
         toxicity scores and spaCy NER labels per post.
License: CC-BY 4.0

Usage:
    python scripts/fetch_pol_dataset.py --out /Volumes/Oom/chan_corpus/raw/pol
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

FILES = [
    ("pol_0616-1119_labeled.tar.zst",
     "https://zenodo.org/records/3606810/files/pol_0616-1119_labeled.tar.zst?download=1"),
    ("Readme.pdf",
     "https://zenodo.org/records/3606810/files/Readme.pdf?download=1"),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="/Volumes/Oom/chan_corpus/raw/pol",
                    help="Download directory.")
    args = ap.parse_args()

    if not shutil.which("aria2c"):
        sys.exit("aria2c not found. Install: brew install aria2")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for fname, url in FILES:
        dest = out / fname
        if dest.exists() and dest.stat().st_size > 100_000_000 and fname.endswith(".tar.zst"):
            print(f"[skip] {fname} already present ({dest.stat().st_size/1e9:.1f} GB)")
            continue
        if dest.exists() and fname.endswith(".pdf"):
            print(f"[skip] {fname} already present")
            continue
        print(f"[fetch] {fname}")
        rc = subprocess.call([
            "aria2c",
            "--dir", str(out),
            "--out", fname,
            "--continue=true",
            "--max-connection-per-server=8",
            "--split=8",
            "--min-split-size=4M",
            "--summary-interval=20",
            "--console-log-level=warn",
            "--auto-file-renaming=false",
            "--allow-overwrite=false",
            url,
        ])
        if rc != 0:
            sys.exit(f"aria2c exited {rc} on {fname}")

    print("\nDone.")
    for fname, _ in FILES:
        p = out / fname
        if p.exists():
            print(f"  {p.stat().st_size/1e9:6.2f} GB  {p}")


if __name__ == "__main__":
    main()
