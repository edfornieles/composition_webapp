#!/usr/bin/env python3
"""Generate a single-file HTML preview of a bucket.

Drops `_preview.html` into the bucket folder. Sorts images by
aesthetic_score (manifest field) when present, else by recency.

Usage:
    python scripts/bucket_preview.py --bucket /Volumes/Oom/aggregation/got/dark_wooded_forest
    open /Volumes/Oom/aggregation/got/dark_wooded_forest/_preview.html
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body{{margin:0;background:#111;color:#ddd;font-family:Helvetica,sans-serif;}}
.bar{{padding:10px 20px;background:#000;border-bottom:1px solid #333;
     position:sticky;top:0;z-index:10;}}
.bar h1{{margin:0 0 4px;font-size:16px;}}
.bar .meta{{font-size:12px;color:#888;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:6px;padding:10px;}}
.cell{{position:relative;background:#000;border:1px solid #222;}}
.cell img{{width:100%;display:block;cursor:pointer;}}
.cell .info{{position:absolute;bottom:0;left:0;right:0;padding:4px 6px;
            background:rgba(0,0,0,.7);font-size:10px;color:#aaa;
            display:flex;justify-content:space-between;gap:6px;
            opacity:0;transition:opacity .15s;}}
.cell:hover .info{{opacity:1;}}
.cell .score{{color:#9c9;}}
.cell .src{{color:#88a;}}
.modal{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.95);z-index:100;
       align-items:center;justify-content:center;cursor:pointer;}}
.modal img{{max-width:95vw;max-height:95vh;}}
</style></head>
<body>
<div class="bar">
  <h1>{title}</h1>
  <div class="meta">{n} files · sort: {sort} · ruscha: {ruscha}</div>
</div>
<div class="grid">{cells}</div>
<div class="modal" id="modal" onclick="this.style.display='none'"><img id="mimg"></div>
<script>
document.querySelectorAll('.cell img').forEach(im=>{{
  im.onclick=()=>{{
    document.getElementById('mimg').src=im.src;
    document.getElementById('modal').style.display='flex';
  }};
}});
</script>
</body></html>
"""

CELL = """<div class="cell"><img src="{src}" loading="lazy">
<div class="info"><span class="score">{score}</span><span class="src">{sub}</span></div></div>"""


def load_manifest(bucket_dir: Path) -> list[dict]:
    rows = []
    p = bucket_dir / ".manifest.jsonl"
    if not p.exists():
        return rows
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--sort", default="score", choices=["score", "recent", "random"])
    args = ap.parse_args()

    bdir = Path(args.bucket)
    rows = load_manifest(bdir)

    if args.sort == "score":
        rows.sort(key=lambda r: -float(r.get("aesthetic_score") or 0))
    elif args.sort == "recent":
        rows.sort(key=lambda r: -int(r.get("fetched_at") or 0))
    else:
        import random
        random.shuffle(rows)

    spec = {}
    spec_path = bdir / ".spec.json"
    if spec_path.exists():
        try:
            spec = json.loads(spec_path.read_text())
        except json.JSONDecodeError:
            pass

    cells = []
    for r in rows:
        fname = r.get("filename") or ""
        if not fname or not (bdir / fname).exists():
            continue
        score = r.get("aesthetic_score")
        score_s = f"{float(score):.2f}" if score is not None else ""
        sub = html.escape((r.get("source_subkey") or r.get("source_type") or "")[:30])
        cells.append(CELL.format(src=html.escape(fname), score=score_s, sub=sub))

    page = PAGE.format(
        title=html.escape(bdir.name),
        n=len(cells),
        sort=html.escape(args.sort),
        ruscha=html.escape((spec.get("ruscha_rule") or "")[:240]),
        cells="\n".join(cells),
    )
    out = bdir / "_preview.html"
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out} ({len(cells)} cells)")


if __name__ == "__main__":
    main()
