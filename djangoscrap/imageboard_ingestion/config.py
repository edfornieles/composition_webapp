"""Local-first chan-corpus storage configuration.

Coexists with the original `storage.py` (which underpins the existing fit
persona pipeline). This module owns the *new* large-corpus layout used by the
ingest_imageboard_board / analyze_image_quality / embed / cluster /
materialise_imageboard_folders commands.

Root resolution order:
  1. --root flag passed to a command
  2. CHAN_CORPUS_ROOT env var
  3. Django settings.CHAN_CORPUS_ROOT
  4. /Volumes/oom/chan_corpus (default — external drive)

Layout:
    {root}/
      raw/{source}/{board}/threads_json/<thread_id>.json
      raw/{source}/{board}/originals/<thread_id>/<post_id>_<tim><ext>
      raw/{source}/{board}/thumbnails/<thread_id>/<post_id>_<tim>s.jpg
      manifests/
        images.jsonl                   # one row per image
        posts.jsonl                    # one row per post (with or without image)
        images.parquet                 # optional, written if pyarrow available
        posts.parquet
        image_features.parquet         # phase 2
        duplicates.parquet             # phase 2
        captions.jsonl
        captions.parquet
        clusters.parquet               # phase 4
        cluster_membership.parquet
        umap_projection.parquet
        embeddings/
          openclip_<model>_<source>_<board>.npy
          openclip_<model>_<source>_<board>_ids.json
        imageplot_labels.jsonl         # round-tripped from ImagePlot
      derived/
        fastdup/<source>_<board>/
        fiftyone/                       # FiftyOne dataset stash
        imageplot_exports/              # CSVs the user opens in ImagePlot
        folders/<source>_<board>/<by>/<sanitised_label>/
      quarantine/
        no_preview/<source>/<board>/<post_id>_<tim>.bin   # raw bytes only,
                                                          # no thumbnail ever made
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

DEFAULT_CHAN_ROOT = Path("/Volumes/oom/chan_corpus")


def _from_django_settings() -> Optional[Path]:
    try:
        from django.conf import settings  # type: ignore
        v = getattr(settings, "CHAN_CORPUS_ROOT", None)
        return Path(v) if v else None
    except Exception:
        return None


def resolve_root(cli_arg: Optional[str | os.PathLike] = None) -> Path:
    if cli_arg:
        return Path(cli_arg).expanduser()
    env = os.environ.get("CHAN_CORPUS_ROOT")
    if env:
        return Path(env).expanduser()
    settings_root = _from_django_settings()
    if settings_root:
        return settings_root
    return DEFAULT_CHAN_ROOT


# ── Layout helpers ──────────────────────────────────────────────────────────

def raw_threads_dir(root: Path, source: str, board: str) -> Path:
    return root / "raw" / source / board / "threads_json"


def originals_dir(root: Path, source: str, board: str, thread_id: int | str | None = None) -> Path:
    base = root / "raw" / source / board / "originals"
    return base / str(thread_id) if thread_id is not None else base


def thumbnails_dir(root: Path, source: str, board: str, thread_id: int | str | None = None) -> Path:
    base = root / "raw" / source / board / "thumbnails"
    return base / str(thread_id) if thread_id is not None else base


def manifests_dir(root: Path) -> Path:
    return root / "manifests"


def derived_dir(root: Path, *parts: str) -> Path:
    return root.joinpath("derived", *parts)


def quarantine_dir(root: Path, source: str, board: str) -> Path:
    return root / "quarantine" / "no_preview" / source / board


def ensure_layout(root: Path, source: str | None = None, board: str | None = None) -> Path:
    """Create the directory layout. Idempotent."""
    root = Path(root)
    for sub in ("manifests", "manifests/embeddings", "derived",
                "derived/fastdup", "derived/fiftyone",
                "derived/imageplot_exports", "derived/folders",
                "quarantine", "quarantine/no_preview"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    if source and board:
        for sub in (raw_threads_dir(root, source, board),
                    originals_dir(root, source, board),
                    thumbnails_dir(root, source, board),
                    quarantine_dir(root, source, board)):
            sub.mkdir(parents=True, exist_ok=True)
    return root


# ── Manifest paths ──────────────────────────────────────────────────────────

def images_manifest(root: Path) -> Path:
    return manifests_dir(root) / "images.jsonl"


def posts_manifest(root: Path) -> Path:
    return manifests_dir(root) / "posts.jsonl"


def captions_manifest(root: Path) -> Path:
    return manifests_dir(root) / "captions.jsonl"


def image_features_path(root: Path) -> Path:
    return manifests_dir(root) / "image_features.parquet"


def duplicates_path(root: Path) -> Path:
    return manifests_dir(root) / "duplicates.parquet"


def clusters_path(root: Path) -> Path:
    return manifests_dir(root) / "clusters.parquet"


def cluster_membership_path(root: Path) -> Path:
    return manifests_dir(root) / "cluster_membership.parquet"


def umap_projection_path(root: Path) -> Path:
    return manifests_dir(root) / "umap_projection.parquet"


def embeddings_npy(root: Path, model_name: str, source: str, board: str) -> Path:
    return manifests_dir(root) / "embeddings" / f"openclip_{model_name}_{source}_{board}.npy"


def embeddings_ids(root: Path, model_name: str, source: str, board: str) -> Path:
    return manifests_dir(root) / "embeddings" / f"openclip_{model_name}_{source}_{board}_ids.json"
