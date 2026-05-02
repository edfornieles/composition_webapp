"""
Immutable module-level constants for the djangoscrap views package.

All names defined here are re-exported via views/__init__.py.
"""
import os
import logging
from pathlib import Path
from django.conf import settings

__all__ = [
    # S3 / upload
    "BUCKET_NAME",
    "UPLOAD_DIR",
    "VIDEO_DIR",
    # Source roots
    "LOCAL_SOURCES_ROOT",
    "LOCAL_SOURCE_TRASH_ROOT",
    "ASSOCIATIONS_TRAINING_SET_DIR",
    "ASSOCIATIONS_CHAINS_ROOT",
    "LOCAL_AUDIO_SOURCES_ROOT",
    "INGESTION_MEDIA_ROOT",
    # Extension sets
    "LOCAL_SOURCE_VIDEO_EXTS",
    "LOCAL_SOURCE_IMAGE_EXTS",
    "LOCAL_SOURCE_ALLOWED_MEDIA_EXTS",
    # Query helpers
    "_QUERY_STOP_WORDS",
    "_CREATURE_QUERY_MARKERS",
    # Dedupe
    "_DEDUPE_THRESHOLDS",
    "_TOMBSTONE_CAP",
    # Stock markers
    "_STOCK_FILENAME_MARKERS",
    # Composition
    "_TRANSITIONS",
    "_OVERLAY_EFFECTS",
    "COMPOSITION_PREVIEW_CACHE_DIR",
    "COMPOSITION_PREVIEW_SIZE",
]

# ---------------------------------------------------------------------------
# S3 / upload directories
# ---------------------------------------------------------------------------
BUCKET_NAME = "composition-final"
UPLOAD_DIR = "media/uploads/"
VIDEO_DIR = "media/videos/"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Local source roots
# ---------------------------------------------------------------------------
LOCAL_SOURCES_ROOT = Path(settings.BASE_DIR).parent / "composition_sources_unprocessed"
LOCAL_SOURCES_ROOT.mkdir(parents=True, exist_ok=True)
LOCAL_SOURCE_TRASH_ROOT = Path(settings.BASE_DIR) / "archive" / "source_trash"
LOCAL_SOURCE_TRASH_ROOT.mkdir(parents=True, exist_ok=True)
ASSOCIATIONS_TRAINING_SET_DIR = (Path(settings.BASE_DIR) / "associations" / "associations training set").resolve()
ASSOCIATIONS_CHAINS_ROOT = settings.ASSOCIATIONS_CHAINS_ROOT
ASSOCIATIONS_CHAINS_ROOT.mkdir(parents=True, exist_ok=True)
LOCAL_AUDIO_SOURCES_ROOT = settings.COMPOSITION_AUDIO_SOURCES_ROOT
LOCAL_AUDIO_SOURCES_ROOT.mkdir(parents=True, exist_ok=True)
INGESTION_MEDIA_ROOT = Path(settings.MEDIA_ROOT) / "ingestion_items"
INGESTION_MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

# Only these extensions may live under LOCAL_SOURCES_ROOT (no .txt, .json, .zip, etc.).
LOCAL_SOURCE_VIDEO_EXTS = frozenset({".mp4", ".mov", ".webm"})
LOCAL_SOURCE_IMAGE_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".tif", ".tiff", ".svg"}
)
LOCAL_SOURCE_ALLOWED_MEDIA_EXTS = LOCAL_SOURCE_IMAGE_EXTS | LOCAL_SOURCE_VIDEO_EXTS

# ---------------------------------------------------------------------------
# Query / search helpers
# ---------------------------------------------------------------------------
_QUERY_STOP_WORDS = frozenset(
    {
        "search",
        "images",
        "image",
        "img",
        "pics",
        "pic",
        "batch",
        "new",
        "src",
        "source",
        "folder",
        "general",
        "web",
        "www",
        "com",
        "style",
        "tumblr",
        "reddit",
        "wallpaper",
        "wallpapers",
        "hd",
        "4k",
        "the",
        "a",
        "an",
        "and",
        "or",
        "for",
        "to",
        "of",
        "in",
        "on",
    }
)

# If any subject token contains these (substring match), bias queries toward creatures/monsters
# and away from generic "character art", which pulls unrelated portraits into dragon batches.
_CREATURE_QUERY_MARKERS = (
    "dragon",
    "drake",
    "wyvern",
    "wyrm",
    "griffin",
    "griffon",
    "gryphon",
    "phoenix",
    "hydra",
    "basilisk",
    "manticore",
    "chimera",
    "demon",
    "balrog",
    "behemoth",
    "kraken",
    "leviathan",
    "serpent",
    "beast",
    "monster",
    "kaiju",
    "cryptid",
)

# ---------------------------------------------------------------------------
# Dedupe thresholds
# ---------------------------------------------------------------------------
_DEDUPE_THRESHOLDS = {
    "loose":  {"clip_cosine_max": 0.98, "hamming_max": 6},
    "normal": {"clip_cosine_max": 0.95, "hamming_max": 10},
    "strict": {"clip_cosine_max": 0.92, "hamming_max": 12},
}

# ---------------------------------------------------------------------------
# Tombstone cap
# ---------------------------------------------------------------------------
_TOMBSTONE_CAP = 5000

# ---------------------------------------------------------------------------
# Stock filename markers
# ---------------------------------------------------------------------------
_STOCK_FILENAME_MARKERS = (
    "shutterstock",
    "gettyimages",
    "istockphoto",
    "adobestock",
    "depositphotos",
    "dreamstime",
    "alamy",
    "123rf",
    "pond5",
    "bigstock",
    "canstockphoto",
    "stocksy",
    "stockphoto",
)

# ---------------------------------------------------------------------------
# Composition transitions / overlay effects
# ---------------------------------------------------------------------------
_TRANSITIONS = ("none", "fade", "crossfade")
_OVERLAY_EFFECTS = ("none", "rotate_3d", "circle_rotate", "circle_rotate_mirror", "mash")

# ---------------------------------------------------------------------------
# Composition preview cache
# ---------------------------------------------------------------------------
COMPOSITION_PREVIEW_CACHE_DIR = Path(settings.MEDIA_ROOT) / "composition_preview_cache"
COMPOSITION_PREVIEW_SIZE = (120, 120)
