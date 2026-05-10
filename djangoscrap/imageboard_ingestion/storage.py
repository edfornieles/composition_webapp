"""Filesystem layout + JSONL manifest writer for imageboard corpora.

Layout (under settings-configured root):
    corpora/imageboards/<source_key>/
        raw_json/<board>/<thread_no>.json
        text/<board>/<thread_no>/<post_no>.txt        # plain_text
        images/<board>/<tim><ext>
        thumbnails/<board>/<tim>s.jpg
        manifests/<source_key>.jsonl                  # one post per line
        profiles/fit_profile.json
        embeddings/fit_tfidf.pkl
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Iterator, Optional

try:
    from django.conf import settings as django_settings  # type: ignore
    _BASE_DIR = Path(getattr(django_settings, "BASE_DIR", Path.cwd())).resolve()
    _CONFIGURED_ROOT = getattr(django_settings, "IMAGEBOARD_CORPUS_ROOT", None)
except Exception:
    _BASE_DIR = Path.cwd()
    _CONFIGURED_ROOT = None

# Priority: env var > Django settings > default in repo
_ENV_ROOT = os.environ.get("IMAGEBOARD_CORPUS_ROOT")
if _ENV_ROOT:
    CORPUS_ROOT = Path(_ENV_ROOT)
elif _CONFIGURED_ROOT:
    CORPUS_ROOT = Path(_CONFIGURED_ROOT)
else:
    CORPUS_ROOT = _BASE_DIR / "corpora" / "imageboards"


def set_corpus_root(path) -> Path:
    """Override the corpus root at runtime (used by --corpus-root flag)."""
    global CORPUS_ROOT
    CORPUS_ROOT = Path(path)
    return CORPUS_ROOT


def source_root(source_key: str) -> Path:
    return CORPUS_ROOT / source_key


def ensure_layout(source_key: str) -> Path:
    root = source_root(source_key)
    for sub in ("raw_json", "text", "images", "thumbnails", "manifests",
                "profiles", "embeddings", "retrieval", "training", "models", "eval"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def retrieval_path(source_key: str, name: str = "fit_retrieval.pkl") -> Path:
    return source_root(source_key) / "retrieval" / name


def training_path(source_key: str, name: str = "fit_be_me_sft_train.jsonl") -> Path:
    return source_root(source_key) / "training" / name


def model_dir(source_key: str, name: str = "fit_be_me_lora") -> Path:
    return source_root(source_key) / "models" / name


def eval_path(source_key: str, name: str = "fit_eval_report.json") -> Path:
    return source_root(source_key) / "eval" / name


def manifest_path(source_key: str) -> Path:
    return source_root(source_key) / "manifests" / f"{source_key}.jsonl"


_write_locks: dict[str, threading.Lock] = {}
_write_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _write_locks_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _write_locks[key] = lock
        return lock


def append_manifest(source_key: str, record: dict) -> None:
    path = manifest_path(source_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with _lock_for(path):
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def iter_manifest(source_key: str) -> Iterator[dict]:
    path = manifest_path(source_key)
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


def load_seen_post_ids(source_key: str) -> set[str]:
    """Return the set of post_uids already in the manifest, for incremental scrapes."""
    seen: set[str] = set()
    for rec in iter_manifest(source_key):
        uid = rec.get("post_uid")
        if uid:
            seen.add(uid)
    return seen


def save_raw_thread(source_key: str, board: str, thread_no: int, payload: dict) -> Path:
    root = source_root(source_key) / "raw_json" / board
    root.mkdir(parents=True, exist_ok=True)
    out = root / f"{thread_no}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return out


def save_post_text(source_key: str, board: str, thread_no: int, post_no: int, text: str) -> Path:
    root = source_root(source_key) / "text" / board / str(thread_no)
    root.mkdir(parents=True, exist_ok=True)
    out = root / f"{post_no}.txt"
    out.write_text(text or "", encoding="utf-8")
    return out


def save_image(source_key: str, board: str, tim: int, ext: str, data: bytes, *, thumbnail: bool = False) -> Path:
    sub = "thumbnails" if thumbnail else "images"
    root = source_root(source_key) / sub / board
    root.mkdir(parents=True, exist_ok=True)
    fname = f"{tim}s.jpg" if thumbnail else f"{tim}{ext}"
    out = root / fname
    out.write_bytes(data)
    return out


def profile_path(source_key: str, name: str = "fit_profile.json") -> Path:
    return source_root(source_key) / "profiles" / name


def embeddings_path(source_key: str, name: str = "fit_tfidf.pkl") -> Path:
    return source_root(source_key) / "embeddings" / name
