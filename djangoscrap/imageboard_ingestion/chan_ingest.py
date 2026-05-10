"""Large-corpus chan ingestion — driver for ingest_imageboard_board.

Wraps the existing rate-limited 4chan client + safety modules and writes into
the new local-first layout defined in config.py.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import time
import urllib.error
from pathlib import Path
from typing import Iterator, Optional

from . import config, fourchan_client, safety, safety_decision

# ── Manifest helpers ────────────────────────────────────────────────────────

def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _iter_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load_seen_keys(root: Path) -> tuple[set[str], set[str]]:
    """Return (seen_post_uids, seen_image_keys). image_key = f'{source}:{board}:{tim}'."""
    seen_posts: set[str] = set()
    seen_images: set[str] = set()
    for rec in _iter_jsonl(config.posts_manifest(root)):
        uid = rec.get("post_uid")
        if uid:
            seen_posts.add(uid)
    for rec in _iter_jsonl(config.images_manifest(root)):
        k = rec.get("image_key")
        if k:
            seen_images.add(k)
    return seen_posts, seen_images


# ── Image validation ────────────────────────────────────────────────────────

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_with_pillow(path: Path, expected_w: int = 0, expected_h: int = 0) -> tuple[bool, str, int, int]:
    """Return (ok, reason, w, h). ok=False ⇒ corrupt or unverifiable."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return True, "pillow_missing_skip_validate", expected_w, expected_h
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            w, h = im.size
        if expected_w and expected_h and (w, h) != (expected_w, expected_h):
            return False, f"dim_mismatch:{w}x{h}_vs_{expected_w}x{expected_h}", w, h
        return True, "ok", w, h
    except Exception as e:
        return False, f"pillow_verify_failed:{type(e).__name__}", 0, 0


# ── Post → record helpers ───────────────────────────────────────────────────

def _post_to_post_record(source: str, board: str, post: dict, thread_no: int) -> dict:
    text = (post.get("com") or "").strip()
    plain = _strip_html(text)
    safety_flags = safety.classify(plain)
    post_no = int(post.get("no") or 0)
    return {
        "source": source,
        "board": board,
        "thread_id": int(thread_no),
        "post_id": post_no,
        "post_uid": f"{source}:{board}:{post_no}",
        "resto": int(post.get("resto") or 0),
        "time_unix": int(post.get("time") or 0),
        "datetime_utc": _fmt_dt(post.get("time")),
        "name": post.get("name") or "",
        "subject": post.get("sub") or "",
        "trip": post.get("trip") or "",
        "raw_html": text,
        "plain_text": plain,
        "safety": safety_flags,
        "scraped_at_unix": int(time.time()),
        "source_url": _post_url(board, thread_no, post_no),
        "semantic_url": _semantic_url(board, thread_no, post.get("semantic_url"), post_no),
    }


def _post_to_image_record(source: str, board: str, post: dict, thread_no: int) -> Optional[dict]:
    tim = post.get("tim")
    ext = post.get("ext")
    if not tim or not ext:
        return None
    post_no = int(post.get("no") or 0)
    return {
        "source": source,
        "board": board,
        "thread_id": int(thread_no),
        "post_id": post_no,
        "image_key": f"{source}:{board}:{tim}",
        "tim": int(tim),
        "ext": ext,
        "filename_original": post.get("filename") or "",
        "md5_4chan": post.get("md5") or "",
        "fsize_remote": int(post.get("fsize") or 0),
        "width": int(post.get("w") or 0),
        "height": int(post.get("h") or 0),
        "thumb_width": int(post.get("tn_w") or 0),
        "thumb_height": int(post.get("tn_h") or 0),
        "original_media_url": f"{fourchan_client.IMG_HOST}/{board}/{tim}{ext}",
        "thumbnail_url": f"{fourchan_client.IMG_HOST}/{board}/{tim}s.jpg",
        "post_uid": f"{source}:{board}:{post_no}",
    }


import re as _re
_TAGS = _re.compile(r"<[^>]+>")
_BR = _re.compile(r"<br\s*/?>", _re.IGNORECASE)


def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = _BR.sub("\n", s)
    s = _TAGS.sub("", s)
    s = s.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&").replace("&quot;", '"').replace("&#039;", "'")
    return s.strip()


def _fmt_dt(unix_ts) -> str:
    if not unix_ts:
        return ""
    try:
        return _dt.datetime.fromtimestamp(int(unix_ts), tz=_dt.timezone.utc).isoformat()
    except Exception:
        return ""


def _post_url(board: str, thread_no: int, post_no: int) -> str:
    return f"https://boards.4chan.org/{board}/thread/{thread_no}#p{post_no}"


def _semantic_url(board: str, thread_no: int, slug, post_no: int) -> str:
    if slug:
        return f"https://boards.4chan.org/{board}/thread/{thread_no}/{slug}"
    return f"https://boards.4chan.org/{board}/thread/{thread_no}"


# ── Parquet writer (optional; we always also write JSONL) ──────────────────

def _try_write_parquet(jsonl_path: Path) -> Optional[Path]:
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except Exception:
        return None
    rows = list(_iter_jsonl(jsonl_path))
    if not rows:
        return None
    pq_path = jsonl_path.with_suffix(".parquet")
    # Normalise schema: collect all keys
    keys = sorted({k for r in rows for k in r.keys()})
    cols = {k: [r.get(k) for r in rows] for k in keys}
    pq.write_table(pa.Table.from_pydict(cols), pq_path)
    return pq_path


# ── Main ingest loop ────────────────────────────────────────────────────────

def ingest_board(
    *,
    source: str,
    board: str,
    root: Path,
    limit_threads: int = 0,
    download_originals: bool = True,
    download_thumbnails: bool = True,
    force: bool = False,
    dry_run: bool = False,
    min_image_dim: int = 0,
    log=print,
) -> dict:
    """Run one ingestion pass against a single board.

    Returns a stats dict. Idempotent: re-running skips already-seen posts/images.
    """
    if source != "fourchan":
        raise NotImplementedError(f"Only source=fourchan is wired up; got {source!r}")

    config.ensure_layout(root, source, board)
    seen_posts, seen_images = load_seen_keys(root)

    log(f"[ingest] {source}/{board} → {root}  (already-seen posts: {len(seen_posts)}, images: {len(seen_images)})")

    stats = {
        "threads_seen": 0,
        "posts_new": 0,
        "posts_skipped": 0,
        "images_new": 0,
        "images_skipped_seen": 0,
        "images_skipped_dryrun": 0,
        "images_quarantined": 0,
        "images_deleted": 0,
        "images_corrupt": 0,
        "thumbs_new": 0,
        "errors": 0,
    }

    if dry_run:
        log("[ingest] DRY RUN — no files will be written")

    catalog, _ = fourchan_client.fetch_catalog(board)
    thread_nos: list[int] = []
    for page in catalog:
        for t in page.get("threads", []):
            thread_nos.append(int(t["no"]))
    if limit_threads > 0:
        thread_nos = thread_nos[:limit_threads]
    log(f"[ingest] {len(thread_nos)} thread(s) to walk")

    for ti, thread_no in enumerate(thread_nos):
        try:
            thread, _ = fourchan_client.fetch_thread(board, thread_no)
        except urllib.error.URLError as e:
            log(f"  thread {thread_no}: network error {e}")
            stats["errors"] += 1
            continue
        except Exception as e:
            log(f"  thread {thread_no}: {type(e).__name__}: {e}")
            stats["errors"] += 1
            continue

        stats["threads_seen"] += 1
        # Save raw JSON
        if not dry_run:
            tj_path = config.raw_threads_dir(root, source, board) / f"{thread_no}.json"
            tj_path.parent.mkdir(parents=True, exist_ok=True)
            tj_path.write_text(json.dumps(thread, ensure_ascii=False), encoding="utf-8")

        for post in thread.get("posts", []):
            post_rec = _post_to_post_record(source, board, post, thread_no)
            uid = post_rec["post_uid"]
            if uid in seen_posts and not force:
                stats["posts_skipped"] += 1
            else:
                if not dry_run:
                    _append_jsonl(config.posts_manifest(root), post_rec)
                seen_posts.add(uid)
                stats["posts_new"] += 1

            img_meta = _post_to_image_record(source, board, post, thread_no)
            if img_meta is None:
                continue
            ikey = img_meta["image_key"]
            if ikey in seen_images and not force:
                stats["images_skipped_seen"] += 1
                continue
            # Pre-download size filter: 4chan posts publish dimensions
            if min_image_dim and (
                img_meta.get("width", 0) < min_image_dim
                or img_meta.get("height", 0) < min_image_dim
            ):
                stats.setdefault("images_skipped_too_small", 0)
                stats["images_skipped_too_small"] += 1
                continue

            decision = safety_decision.decide_for_post(post_rec)

            if dry_run:
                stats["images_skipped_dryrun"] += 1
                continue

            # Quarantine / delete-immediately bytes
            if decision.delete_immediately:
                stats["images_deleted"] += 1
                # Still record metadata, but mark no_disk:
                img_meta["safety_decision"] = decision.to_dict()
                img_meta["original_path"] = ""
                img_meta["thumbnail_path"] = ""
                _append_jsonl(config.images_manifest(root), img_meta)
                seen_images.add(ikey)
                continue

            if decision.quarantine_no_preview:
                # Save originals into quarantine/no_preview ONLY. Never thumbnail.
                if download_originals:
                    try:
                        data = fourchan_client.fetch_image(board, img_meta["tim"], img_meta["ext"], thumbnail=False)
                        qpath = (config.quarantine_dir(root, source, board)
                                 / f'{img_meta["post_id"]}_{img_meta["tim"]}{img_meta["ext"]}')
                        qpath.parent.mkdir(parents=True, exist_ok=True)
                        qpath.write_bytes(data)
                        img_meta["original_path"] = str(qpath)
                        img_meta["sha256"] = _sha256(data)
                    except Exception as e:
                        log(f"  quarantine fetch failed for {ikey}: {e}")
                        stats["errors"] += 1
                img_meta["safety_decision"] = decision.to_dict()
                img_meta["thumbnail_path"] = ""  # never preview
                _append_jsonl(config.images_manifest(root), img_meta)
                seen_images.add(ikey)
                stats["images_quarantined"] += 1
                continue

            # ALLOW path
            ok = True
            if download_originals:
                orig_dir = config.originals_dir(root, source, board, img_meta["thread_id"])
                orig_path = orig_dir / f'{img_meta["post_id"]}_{img_meta["tim"]}{img_meta["ext"]}'
                if orig_path.exists() and not force:
                    img_meta["original_path"] = str(orig_path)
                    try:
                        img_meta["sha256"] = _sha256(orig_path.read_bytes())
                    except Exception:
                        img_meta["sha256"] = ""
                else:
                    try:
                        data = fourchan_client.fetch_image(board, img_meta["tim"], img_meta["ext"], thumbnail=False)
                        orig_dir.mkdir(parents=True, exist_ok=True)
                        orig_path.write_bytes(data)
                        img_meta["original_path"] = str(orig_path)
                        img_meta["sha256"] = _sha256(data)
                    except Exception as e:
                        log(f"  original fetch failed for {ikey}: {e}")
                        stats["errors"] += 1
                        ok = False

                if ok:
                    valid, reason, w, h = _validate_with_pillow(orig_path, img_meta["width"], img_meta["height"])
                    img_meta["pillow_ok"] = valid
                    img_meta["pillow_reason"] = reason
                    if not valid:
                        # Treat as quarantine_corrupt
                        decision = safety_decision.merge(decision, safety_decision.decide_for_unreadable_image(reason))
                        img_meta["safety_decision"] = decision.to_dict()
                        stats["images_corrupt"] += 1

            if download_thumbnails and decision.allow:
                thumb_dir = config.thumbnails_dir(root, source, board, img_meta["thread_id"])
                thumb_path = thumb_dir / f'{img_meta["post_id"]}_{img_meta["tim"]}s.jpg'
                if thumb_path.exists() and not force:
                    img_meta["thumbnail_path"] = str(thumb_path)
                else:
                    try:
                        tdata = fourchan_client.fetch_image(board, img_meta["tim"], img_meta["ext"], thumbnail=True)
                        thumb_dir.mkdir(parents=True, exist_ok=True)
                        thumb_path.write_bytes(tdata)
                        img_meta["thumbnail_path"] = str(thumb_path)
                        stats["thumbs_new"] += 1
                    except Exception as e:
                        log(f"  thumb fetch failed for {ikey}: {e}")

            img_meta["safety_decision"] = decision.to_dict()
            _append_jsonl(config.images_manifest(root), img_meta)
            seen_images.add(ikey)
            stats["images_new"] += 1

        if (ti + 1) % 10 == 0:
            log(f"  …{ti+1}/{len(thread_nos)} threads, images_new={stats['images_new']}, quarantine={stats['images_quarantined']}")

    # Write parquet siblings if pyarrow installed
    if not dry_run:
        for jl in (config.images_manifest(root), config.posts_manifest(root)):
            try:
                _try_write_parquet(jl)
            except Exception as e:
                log(f"  parquet write skipped for {jl.name}: {e}")

    log(f"[ingest] DONE  {stats}")
    return stats
