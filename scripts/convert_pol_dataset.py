"""Convert the Papasavva /pol/ dataset (.tar.zst NDJSON) to chan corpus JSONL.

Reads the raw Zenodo bundle and emits per-month shards in the schema
build_chan_sft.py expects. Streams everything — no full decompression to disk.

Output layout:
    {out}/
      pol-2016-06.jsonl
      pol-2016-07.jsonl
      ...
      pol-2019-11.jsonl
      _stats.json

Each line:
    {
      "board": "pol",
      "thread_num": int,
      "post_num": int,
      "timestamp": int,
      "is_op": bool,
      "subject": str,
      "comment": str,
      "source": "raiders_of_lost_kek",
      "has_image": bool,
      "image_key": null,
      "perspective": { ... toxicity scores ... },
      "entities":    [ ... NER spans ... ]
    }

Usage:
    pip install zstandard

    python scripts/convert_pol_dataset.py \\
        --bundle /Volumes/Oom/chan_corpus/raw/pol/pol_0616-1119_labeled.tar.zst \\
        --out    /Volumes/Oom/chan_corpus/raw/pol/jsonl

The dataset is one JSON object per line. Each line is *either* a thread
(with a `posts` array) or a single post — this script handles both shapes.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import tarfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

try:
    import zstandard as zstd
except ImportError:
    sys.exit("Missing dependency: pip install zstandard")

PERSPECTIVE_KEYS = {
    "TOXICITY", "SEVERE_TOXICITY", "INSULT", "THREAT",
    "PROFANITY", "IDENTITY_ATTACK", "SEXUALLY_EXPLICIT", "FLIRTATION",
    "ATTACK_ON_AUTHOR", "ATTACK_ON_COMMENTER", "INCOHERENT",
    "INFLAMMATORY", "OBSCENE", "SPAM", "UNSUBSTANTIAL",
}

# Tier thresholds — see chan_anon framing memory.
# T0 clean   : low toxicity, no slurs, no identity attack
# T1 hostile : aggressive/insulting but not protected-class hate
# T2 attack  : identity-targeted abuse / slurs
# T3 severe  : severe hate / threat / sexually explicit (used for adversarial eval only)
def toxicity_tier(persp: dict, comment: str) -> str:
    if not persp:
        return "T0_clean"
    sev = float(persp.get("SEVERE_TOXICITY") or 0.0)
    threat = float(persp.get("THREAT") or 0.0)
    sexual = float(persp.get("SEXUALLY_EXPLICIT") or 0.0)
    ident = float(persp.get("IDENTITY_ATTACK") or 0.0)
    tox = float(persp.get("TOXICITY") or 0.0)
    insult = float(persp.get("INSULT") or 0.0)

    if sev >= 0.75 or threat >= 0.7 or sexual >= 0.85:
        return "T3_severe"
    if ident >= 0.6 or sev >= 0.5:
        return "T2_identity_attack"
    if tox >= 0.5 or insult >= 0.6:
        return "T1_hostile"
    return "T0_clean"


# 4chan greentext / quote markers — fast structural features.
import re as _re
_GREENTEXT_RE = _re.compile(r"^\s*&gt;[^&]", _re.MULTILINE)
_QUOTE_RE = _re.compile(r">>(\d+)")


def structural_tags(comment_html: str, comment_text: str) -> dict:
    quotes = _QUOTE_RE.findall(comment_html or "")
    greentext_lines = len(_GREENTEXT_RE.findall(comment_html or ""))
    return {
        "quotes_count": len(quotes),
        "reply_to": int(quotes[0]) if quotes else None,
        "greentext_lines": greentext_lines,
        "is_greentext": greentext_lines >= 2,
        "char_len": len(comment_text or ""),
    }


def parse_post(d: dict, thread_num: int | None) -> dict | None:
    """Map a Papasavva post object to the chan corpus schema. Returns None on bad records."""
    post_no = d.get("no") or d.get("post_no") or d.get("post_id")
    if post_no is None:
        return None
    tnum = thread_num
    if tnum is None:
        tnum = d.get("resto") or d.get("thread_no") or post_no
    is_op = (post_no == tnum) or bool(d.get("is_op"))

    com_html = d.get("com") or d.get("comment") or ""
    if isinstance(com_html, str) and com_html:
        # /pol/ JSON gives HTML; build_chan_sft.py also strips tags but
        # we drop the heavy escapes here for cleaner storage.
        comment = unescape(com_html)
        # Convert <br>, <wbr> and quote spans to plain text.
        comment = (
            comment.replace("<br>", "\n")
                   .replace("<br/>", "\n")
                   .replace("<br />", "\n")
                   .replace("<wbr>", "")
        )
    else:
        comment = ""

    subject = d.get("sub") or d.get("subject") or ""
    if isinstance(subject, str):
        subject = unescape(subject)

    ts = int(d.get("time") or d.get("timestamp") or 0)
    image_key = None
    has_image = bool(d.get("tim") or d.get("filename") or d.get("md5"))

    perspective = {}
    for k in PERSPECTIVE_KEYS:
        if k in d:
            perspective[k] = d[k]
        elif k.lower() in d:
            perspective[k] = d[k.lower()]
    # Common nested field
    persp_obj = d.get("perspective_scores") or d.get("perspective")
    if isinstance(persp_obj, dict):
        for k, v in persp_obj.items():
            perspective.setdefault(k.upper(), v)

    entities = d.get("entities") or d.get("ner") or []

    com_text = comment.strip()
    tags = structural_tags(com_html, com_text)
    tier = toxicity_tier(perspective, com_text)

    year = None
    if ts > 0:
        from datetime import datetime as _dt, timezone as _tz
        year = _dt.fromtimestamp(ts, tz=_tz.utc).year

    return {
        "board": "pol",
        "thread_num": int(tnum),
        "post_num": int(post_no),
        "timestamp": ts,
        "year": year,
        "is_op": bool(is_op),
        "subject": subject,
        "comment": com_text,
        "source": "raiders_of_lost_kek",
        "has_image": has_image,
        "image_key": image_key,
        "reply_to": tags["reply_to"],
        "quotes_count": tags["quotes_count"],
        "greentext_lines": tags["greentext_lines"],
        "is_greentext": tags["is_greentext"],
        "char_len": tags["char_len"],
        "toxicity": {
            "tier": tier,
            **{k: float(v) for k, v in perspective.items() if isinstance(v, (int, float))},
        },
        "entities": entities,
    }


def iter_records_from_jsonl(stream) -> "iterable[dict]":
    """Yield dicts from a binary stream of newline-delimited JSON."""
    buf = b""
    while True:
        chunk = stream.read(2**20)
        if not chunk:
            break
        buf += chunk
        while True:
            nl = buf.find(b"\n")
            if nl < 0:
                break
            line, buf = buf[:nl], buf[nl+1:]
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    if buf.strip():
        try:
            yield json.loads(buf)
        except json.JSONDecodeError:
            pass


def expand_record(rec: dict):
    """A record may be a thread {'posts': [...]} or a flat post."""
    if isinstance(rec, dict) and isinstance(rec.get("posts"), list):
        thread_num = None
        for p in rec["posts"]:
            tn = p.get("resto") or p.get("no")
            if tn:
                thread_num = tn
                break
        for p in rec["posts"]:
            yield p, thread_num
    elif isinstance(rec, dict):
        yield rec, None


def open_member_stream(tar: tarfile.TarFile, member: tarfile.TarInfo):
    f = tar.extractfile(member)
    if f is None:
        return None
    name = member.name.lower()
    if name.endswith(".zst"):
        return zstd.ZstdDecompressor(max_window_size=2**31).stream_reader(f)
    if name.endswith(".gz"):
        import gzip
        return gzip.GzipFile(fileobj=f)
    return f


def shard_path(out_dir: Path, ts: int) -> Path:
    if ts <= 0:
        return out_dir / "pol-unknown.jsonl"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return out_dir / f"pol-{dt.year:04d}-{dt.month:02d}.jsonl"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", required=True,
                    help="Path to pol_0616-1119_labeled.tar.zst")
    ap.add_argument("--out", required=True,
                    help="Output directory for monthly JSONL shards.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Stop after N posts (debug).")
    args = ap.parse_args()

    bundle = Path(args.bundle)
    if not bundle.exists():
        sys.exit(f"Bundle not found: {bundle}")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    open_shards: dict[Path, object] = {}
    counts: dict[str, int] = defaultdict(int)
    n_posts = 0
    n_threads = 0
    t0 = time.time()

    print(f"Bundle: {bundle} ({bundle.stat().st_size/1e9:.1f} GB)", flush=True)
    print(f"Output: {out_dir}", flush=True)
    print(flush=True)

    dctx = zstd.ZstdDecompressor(max_window_size=2**31)
    with bundle.open("rb") as raw, dctx.stream_reader(raw) as zr:
        with tarfile.open(fileobj=zr, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                print(f"[member] {member.name} ({member.size/1e9:.2f} GB)", flush=True)
                stream = open_member_stream(tar, member)
                if stream is None:
                    continue
                # tarfile gives a non-buffered stream — wrap it for readline-ish behaviour
                stream = io.BufferedReader(stream) if not hasattr(stream, "read1") else stream
                for rec in iter_records_from_jsonl(stream):
                    n_threads += 1 if isinstance(rec, dict) and "posts" in rec else 0
                    for raw_post, tnum in expand_record(rec):
                        row = parse_post(raw_post, tnum)
                        if row is None:
                            continue
                        if not row["comment"] and not row["subject"]:
                            continue
                        sp = shard_path(out_dir, row["timestamp"])
                        fh = open_shards.get(sp)
                        if fh is None:
                            fh = sp.open("a", encoding="utf-8")
                            open_shards[sp] = fh
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                        counts[sp.name] += 1
                        n_posts += 1
                        if n_posts % 200_000 == 0:
                            elapsed = time.time() - t0
                            print(f"  posts={n_posts:,}  threads={n_threads:,}  rate={n_posts/elapsed:.0f}/s", flush=True)
                        if args.limit and n_posts >= args.limit:
                            raise StopIteration
                # close current member's stream so next member can be read
                try:
                    stream.close()
                except Exception:
                    pass

    for fh in open_shards.values():
        fh.close()

    stats = {
        "posts_written": n_posts,
        "threads_seen": n_threads,
        "shards": dict(counts),
        "elapsed_sec": int(time.time() - t0),
    }
    (out_dir / "_stats.json").write_text(json.dumps(stats, indent=2))
    print(f"\nDone. posts={n_posts:,}  threads≈{n_threads:,}  elapsed={stats['elapsed_sec']}s", flush=True)


if __name__ == "__main__":
    try:
        main()
    except StopIteration:
        pass
