"""
Source directory utilities for the djangoscrap views package.

All public names defined here are re-exported via views/__init__.py.
"""
from __future__ import annotations
import json
import threading
import time
from pathlib import Path
from datetime import datetime
from typing import Any
from urllib.parse import quote
from PIL import Image, UnidentifiedImageError
from django.conf import settings
from django.core.cache import cache
from ._constants import (
    LOCAL_SOURCES_ROOT,
    LOCAL_AUDIO_SOURCES_ROOT,
    LOCAL_SOURCE_ALLOWED_MEDIA_EXTS,
    LOCAL_SOURCE_IMAGE_EXTS,
    LOCAL_SOURCE_VIDEO_EXTS,
)

try:
    from moviepy.editor import VideoFileClip
except Exception:
    VideoFileClip = None  # type: ignore

try:
    from PIL import ImageResampling
    RESAMPLING_METHOD = ImageResampling.LANCZOS
except ImportError:
    from PIL import Image as _pil
    RESAMPLING_METHOD = _pil.LANCZOS  # type: ignore

__all__ = [
    "_local_source_filename_allowed",
    "_local_source_path_allowed",
    "_local_source_dir_media_files",
    "_resolve_source_media_file",
    "_INGESTION_SOURCE_MEDIA_COUNT_CACHE_TTL_S",
    "_INGESTION_SOURCE_MEDIA_COUNT_DISK_CACHE_MAX_AGE_S",
    "_INGESTION_SOURCE_MEDIA_COUNT_DISK_CACHE_PATH",
    "_INGESTION_SOURCE_MEDIA_COUNT_DISK_LOCK",
    "_INGESTION_SOURCE_MEDIA_COUNT_DISK_LOADED",
    "_INGESTION_SOURCE_MEDIA_COUNT_DISK_DIRTY",
    "_load_ingestion_media_count_disk_cache_once",
    "_flush_ingestion_media_count_disk_cache",
    "_cached_local_source_media_count",
    "get_local_sources",
    "get_local_audio_sources",
    "_source_picker_cards",
    "pick_representative_source_image",
    "_is_landscape_media",
    "collect_source_assets",
    "collect_audio_source_files",
    "_extract_video_frame_image",
]

def _local_source_filename_allowed(filename: str) -> bool:
    n = (filename or "").strip().lower()
    # Incomplete browser downloads / temp shards — never treat as catalog media.
    if n.endswith(".crdownload") or n.endswith(".part") or n.endswith(".filepart"):
        return False
    ext = Path(filename).suffix.lower()
    return bool(ext) and ext in LOCAL_SOURCE_ALLOWED_MEDIA_EXTS


def _local_source_path_allowed(path: Path) -> bool:
    return _local_source_filename_allowed(path.name)


def _local_source_dir_media_files(source_dir: Path) -> list[Path]:
    """Allowed media files in a source folder, including nested legacy batches."""
    try:
        files: list[Path] = []
        for entry in source_dir.iterdir():
            if entry.is_file():
                if entry.name.startswith("."):
                    continue
                if not _local_source_path_allowed(entry):
                    continue
                files.append(entry)
                continue
            if not entry.is_dir():
                continue
            if entry.name.startswith("."):
                continue
            for p in entry.rglob("*"):
                if not p.is_file():
                    continue
                if p.name.startswith("."):
                    continue
                if not _local_source_path_allowed(p):
                    continue
                files.append(p)
        return files
    except OSError:
        return []


def _resolve_source_media_file(source_dir: Path, file_name: str) -> Path | None:
    """Resolve a source file by basename, supporting nested legacy folders."""
    direct = (source_dir / file_name).resolve()
    if source_dir in direct.parents and direct.exists() and direct.is_file() and _local_source_path_allowed(direct):
        return direct
    for candidate in _local_source_dir_media_files(source_dir):
        if candidate.name != file_name:
            continue
        resolved = candidate.resolve()
        if source_dir in resolved.parents and resolved.is_file():
            return resolved
    return None


# Ingestion overview scans every source folder; cache media counts to avoid
# repeated O(N files) directory walks on each /ingestion/ refresh.
_INGESTION_SOURCE_MEDIA_COUNT_CACHE_TTL_S = 20
_INGESTION_SOURCE_MEDIA_COUNT_DISK_CACHE_MAX_AGE_S = 24 * 60 * 60
_INGESTION_SOURCE_MEDIA_COUNT_DISK_CACHE_PATH = Path(settings.BASE_DIR) / ".cache" / "ingestion_media_counts.json"
_INGESTION_SOURCE_MEDIA_COUNT_DISK_LOCK = threading.Lock()
_INGESTION_SOURCE_MEDIA_COUNT_DISK_LOADED = False
_INGESTION_SOURCE_MEDIA_COUNT_DISK_DIRTY = False
_INGESTION_SOURCE_MEDIA_COUNT_DISK: dict[str, dict] = {}


def _load_ingestion_media_count_disk_cache_once() -> None:
    global _INGESTION_SOURCE_MEDIA_COUNT_DISK_LOADED, _INGESTION_SOURCE_MEDIA_COUNT_DISK
    if _INGESTION_SOURCE_MEDIA_COUNT_DISK_LOADED:
        return
    with _INGESTION_SOURCE_MEDIA_COUNT_DISK_LOCK:
        if _INGESTION_SOURCE_MEDIA_COUNT_DISK_LOADED:
            return
        data: dict[str, dict] = {}
        try:
            cp = _INGESTION_SOURCE_MEDIA_COUNT_DISK_CACHE_PATH
            if cp.is_file():
                raw = json.loads(cp.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    now = int(time.time())
                    for k, v in raw.items():
                        if not isinstance(k, str) or not isinstance(v, dict):
                            continue
                        try:
                            ts = int(v.get("ts") or 0)
                            mtime_ns = int(v.get("mtime_ns") or 0)
                            count = int(v.get("count") or 0)
                        except (TypeError, ValueError):
                            continue
                        if ts <= 0 or (now - ts) > _INGESTION_SOURCE_MEDIA_COUNT_DISK_CACHE_MAX_AGE_S:
                            continue
                        if mtime_ns <= 0 or count < 0:
                            continue
                        data[k] = {"mtime_ns": mtime_ns, "count": count, "ts": ts}
        except Exception:
            data = {}
        _INGESTION_SOURCE_MEDIA_COUNT_DISK = data
        _INGESTION_SOURCE_MEDIA_COUNT_DISK_LOADED = True


def _flush_ingestion_media_count_disk_cache() -> None:
    global _INGESTION_SOURCE_MEDIA_COUNT_DISK_DIRTY
    if not _INGESTION_SOURCE_MEDIA_COUNT_DISK_DIRTY:
        return
    with _INGESTION_SOURCE_MEDIA_COUNT_DISK_LOCK:
        if not _INGESTION_SOURCE_MEDIA_COUNT_DISK_DIRTY:
            return
        try:
            cp = _INGESTION_SOURCE_MEDIA_COUNT_DISK_CACHE_PATH
            cp.parent.mkdir(parents=True, exist_ok=True)
            tmp = cp.with_suffix(cp.suffix + ".tmp")
            tmp.write_text(json.dumps(_INGESTION_SOURCE_MEDIA_COUNT_DISK, separators=(",", ":")), encoding="utf-8")
            tmp.replace(cp)
            _INGESTION_SOURCE_MEDIA_COUNT_DISK_DIRTY = False
        except Exception:
            # Keep running even if disk cache write fails; list view still works.
            pass


def _cached_local_source_media_count(source_dir: Path) -> tuple[int, datetime | None]:
    """
    Return (allowed_media_count, mtime) for ``source_dir`` with two-layer cache:
      1) process-local LocMem cache for hot refreshes,
      2) on-disk cache keyed by (folder, mtime_ns) for process cold starts.

    Folder mtime changes invalidate both layers immediately for that folder.
    """
    global _INGESTION_SOURCE_MEDIA_COUNT_DISK_DIRTY
    try:
        st = source_dir.stat()
        mtime = datetime.fromtimestamp(st.st_mtime)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
    except OSError:
        return 0, None

    key_name = source_dir.name.lower()
    ck = f"ingestion:media-count:{key_name}:{mtime_ns}"
    cached = cache.get(ck)
    if isinstance(cached, int):
        return cached, mtime

    _load_ingestion_media_count_disk_cache_once()
    row = _INGESTION_SOURCE_MEDIA_COUNT_DISK.get(key_name)
    if isinstance(row, dict):
        try:
            if int(row.get("mtime_ns") or 0) == mtime_ns:
                dc = int(row.get("count") or 0)
                if dc >= 0:
                    cache.set(ck, dc, timeout=_INGESTION_SOURCE_MEDIA_COUNT_CACHE_TTL_S)
                    return dc, mtime
        except (TypeError, ValueError):
            pass

    count = len(_local_source_dir_media_files(source_dir))
    cache.set(ck, count, timeout=_INGESTION_SOURCE_MEDIA_COUNT_CACHE_TTL_S)
    now = int(time.time())
    _INGESTION_SOURCE_MEDIA_COUNT_DISK[key_name] = {"mtime_ns": mtime_ns, "count": count, "ts": now}
    _INGESTION_SOURCE_MEDIA_COUNT_DISK_DIRTY = True
    return count, mtime


def get_local_sources():
    root = LOCAL_SOURCES_ROOT
    try:
        mtime = root.stat().st_mtime
    except OSError:
        mtime = 0
    cache_key = f"local_sources_list_{int(mtime)}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = sorted([p for p in root.iterdir() if p.is_dir()])
    cache.set(cache_key, result, 600)
    return result


def get_local_audio_sources():
    return sorted([p for p in LOCAL_AUDIO_SOURCES_ROOT.iterdir() if p.is_dir()])


def _source_picker_cards(limit: int = 240) -> list[dict]:
    source_dirs = get_local_sources()
    image_exts = LOCAL_SOURCE_IMAGE_EXTS
    video_exts = LOCAL_SOURCE_VIDEO_EXTS
    cards: list[dict] = []
    for source_dir in source_dirs[:limit]:
        media_files = sorted(_local_source_dir_media_files(source_dir), key=lambda p: p.name.lower())
        rep_file = media_files[0] if media_files else None
        thumbnail_url = None
        if rep_file:
            if rep_file.suffix.lower() in image_exts:
                thumbnail_url = f"/source-media/{quote(source_dir.name, safe='')}/{quote(rep_file.name, safe='')}"
            else:
                thumbnail_url = f"/source-thumbnail/{quote(source_dir.name, safe='')}/{quote(rep_file.name, safe='')}"
        cards.append(
            {
                "name": source_dir.name,
                "thumbnail_url": thumbnail_url,
                "file_count": len(media_files),
            }
        )
    return cards


def _extract_video_frame_image(video_path):
    try:
        with VideoFileClip(str(video_path)) as clip:
            if clip.duration is None:
                frame_time = 0
            else:
                # Skip opening/black leader frames; use an interior frame for more faithful previews.
                frame_time = max(0.1, min(float(clip.duration) - 0.1, float(clip.duration) * 0.35))
            frame = clip.get_frame(frame_time)
        return Image.fromarray(frame).convert("RGBA")
    except Exception:
        return None


def pick_representative_source_image(source_names):
    valid_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm"}
    low_value_tokens = {"thumb", "thumbnail", "preview", "small", "icon", "logo", "sprite", "mask"}
    for source_name in (source_names or []):
        source_dir = LOCAL_SOURCES_ROOT / source_name
        if not source_dir.exists():
            continue
        ranked = []
        for file in sorted(source_dir.iterdir()):
            if file.name.startswith(".") or not file.is_file():
                continue
            ext = file.suffix.lower()
            if ext not in valid_exts:
                continue
            stem = file.stem.lower()
            score = 0
            if ext in {".jpg", ".jpeg", ".png", ".webp"}:
                score += 4
            elif ext == ".gif":
                score += 2
            else:
                score += 1
            if any(tok in stem for tok in low_value_tokens):
                score -= 5
            ranked.append((score, file))
        if not ranked:
            continue
        ranked.sort(key=lambda x: (-x[0], x[1].name))
        # Avoid always choosing the same first frame/file while staying deterministic.
        idx = min(len(ranked) - 1, max(0, int(len(ranked) * 0.25)))
        return ranked[idx][1]
    return None


def _is_landscape_media(file: Path) -> bool:
    ext = file.suffix.lower()
    try:
        if ext in {".mp4", ".mov", ".webm"}:
            with VideoFileClip(str(file)) as clip:
                w = int(getattr(clip, "w", 0) or 0)
                h = int(getattr(clip, "h", 0) or 0)
                return w > h
        with Image.open(file) as img:
            if ext == ".gif":
                try:
                    img.seek(0)
                except Exception:
                    pass
            w, h = img.size
            return int(w or 0) > int(h or 0)
    except Exception:
        return False


_SOURCE_ASSETS_CACHE_TTL = 60  # seconds


def _gif_loop_duration_ms(file_path: Path) -> int | None:
    """Sum frame durations for an animated GIF, in milliseconds.

    Returns None for static GIFs (single frame) or on any read error. Result
    is cached implicitly via the surrounding collect_source_assets cache.
    """
    try:
        with Image.open(file_path) as im:
            if getattr(im, "n_frames", 1) <= 1:
                return None
            total = 0
            for frame_idx in range(im.n_frames):
                im.seek(frame_idx)
                # Per-frame duration in ms; GIF spec defaults to 100ms when unset.
                total += int(im.info.get("duration", 100))
            return total if total > 0 else None
    except Exception:
        return None


def collect_source_assets(source_names, landscape_only: bool = False):
    names = sorted(source_names or [])
    cache_key = f"src_assets:{'|'.join(names)}:{int(landscape_only)}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    assets = []
    seen = set()
    for source_name in names:
        source_dir = LOCAL_SOURCES_ROOT / source_name
        if not source_dir.exists():
            continue
        for file in sorted(_local_source_dir_media_files(source_dir), key=lambda p: p.name.lower()):
            ext = file.suffix.lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm"}:
                continue
            if landscape_only and not _is_landscape_media(file):
                continue
            key = (source_name, file.name)
            if key in seen:
                continue
            seen.add(key)
            kind = "video" if ext in {".mp4", ".mov", ".webm"} else "image"
            media_url = f"/source-media/{quote(source_name, safe='')}/{quote(file.name, safe='')}"
            preview_url = (
                f"/source-thumbnail/{quote(source_name, safe='')}/{quote(file.name, safe='')}"
                if kind == "video"
                else media_url
            )
            entry = {
                "kind": kind,
                "url": media_url,
                "preview_url": preview_url,
                "name": file.name,
                "source_name": source_name,
            }
            # Animated GIFs: probe the loop duration so the overlay layer can
            # hold each clip for exactly one (or N) full loops, eliminating
            # mid-frame cuts. Static GIFs / read errors -> no duration field.
            if ext == ".gif":
                d = _gif_loop_duration_ms(file)
                if d:
                    entry["duration_ms"] = d
            assets.append(entry)
    cache.set(cache_key, assets, _SOURCE_ASSETS_CACHE_TTL)
    return assets


def _associations_archive_source_name(page_slug: str) -> str:
    return f"associations-{slugify(page_slug or '') or 'untitled'}"


def _associations_chain_dir_name(page_slug: str) -> str:
    return slugify(page_slug or "") or "untitled"


def _association_chain_media_url(chain_name: str, file_name: str) -> str:
    return f"/association-chain-media/{quote(chain_name, safe='')}/{quote(file_name, safe='')}"


def _resolve_association_chain_file(chain_name: str, file_name: str) -> Path | None:
    chain_dir = (ASSOCIATIONS_CHAINS_ROOT / chain_name).resolve()
    root = ASSOCIATIONS_CHAINS_ROOT.resolve()
    if root != chain_dir and root not in chain_dir.parents:
        return None
    return _resolve_source_media_file(chain_dir, file_name)


def collect_association_chain_assets(composition: Composition, page_slug: str) -> list[dict[str, Any]]:
    assoc = composition.filter_settings.get("associations") if isinstance(composition.filter_settings, dict) else {}
    if not isinstance(assoc, dict):
        assoc = {}
    chain_name = str(assoc.get("chain_name") or _associations_chain_dir_name(page_slug)).strip()
    chain_dir = (ASSOCIATIONS_CHAINS_ROOT / chain_name).resolve()
    root = ASSOCIATIONS_CHAINS_ROOT.resolve()
    if root != chain_dir and root not in chain_dir.parents:
        return []
    assets: list[dict[str, Any]] = []
    if not chain_dir.exists():
        return assets
    for file in sorted(_local_source_dir_media_files(chain_dir), key=lambda p: p.name.lower()):
        if file.suffix.lower() not in LOCAL_SOURCE_IMAGE_EXTS:
            continue
        url = _association_chain_media_url(chain_name, file.name)
        assets.append({"kind": "image", "url": url, "preview_url": url, "name": file.name, "source": "association_chain"})
    return assets


def _save_square_image_bytes(raw: bytes, target_path: Path) -> bool:
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        if w <= 0 or h <= 0:
            return False
        side = min(w, h)
        left = max(0, (w - side) // 2)
        top = max(0, (h - side) // 2)
        img = img.crop((left, top, left + side, top + side))
        img = img.resize((1080, 1080), Image.LANCZOS)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(target_path, format="JPEG", quality=90)
        return True
    except Exception:
        return False


def _pick_random_seed_image(source_name: str | None = None) -> tuple[str, bytes] | None:
    candidate_files: list[Path] = []
    if source_name:
        src_dir = (LOCAL_SOURCES_ROOT / source_name).resolve()
        if src_dir.exists() and src_dir.is_dir():
            candidate_files.extend([p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() in LOCAL_SOURCE_IMAGE_EXTS])
    else:
        for src_dir in get_local_sources():
            try:
                candidate_files.extend([p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() in LOCAL_SOURCE_IMAGE_EXTS])
            except Exception:
                continue
    if not candidate_files:
        return None
    picked = random.choice(candidate_files)
    try:
        return picked.name, picked.read_bytes()
    except Exception:
        return None


def _parse_source_media_url_to_bytes(media_url: str) -> bytes | None:
    raw_url = (media_url or "").strip()
    if not raw_url:
        return None
    parsed = urlparse(raw_url)
    path_only = parsed.path or raw_url
    if path_only.startswith("/association-chain-media/"):
        try:
            remainder = path_only[len("/association-chain-media/"):]
            chain_name_enc, file_name_enc = remainder.split("/", 1)
            target = _resolve_association_chain_file(unquote(chain_name_enc), unquote(file_name_enc))
            if target and target.exists():
                return target.read_bytes()
        except Exception:
            return None
    if path_only.startswith("/source-media/"):
        try:
            remainder = path_only[len("/source-media/"):]
            source_name_enc, file_name_enc = remainder.split("/", 1)
            source_name = unquote(source_name_enc)
            file_name = unquote(file_name_enc)
            source_dir = (LOCAL_SOURCES_ROOT / source_name).resolve()
            target = _resolve_source_media_file(source_dir, file_name)
            if target and target.exists():
                return target.read_bytes()
        except Exception:
            return None
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        try:
            if parsed.hostname and _host_resolves_private(parsed.hostname):
                return None
            req = Request(raw_url, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urlopen(req, timeout=12) as resp:
                return resp.read()
        except Exception:
            return None
    return None


def _next_association_filename(folder: Path) -> str:
    idx = 1
    existing = sorted([p.name for p in folder.glob("*.jpg")])
    if existing:
        last = existing[-1]
        m = re.search(r"(\d+)\.jpg$", last)
        if m:
            idx = int(m.group(1)) + 1
    return f"assoc_{idx:06d}.jpg"


def _ensure_associations_archive_source(composition: Composition, page_slug: str) -> tuple[str, Path]:
    assoc_settings = {}
    if isinstance(composition.filter_settings, dict):
        raw_assoc = composition.filter_settings.get("associations") or {}
        if isinstance(raw_assoc, dict):
            assoc_settings = raw_assoc
    chain_name = str(assoc_settings.get("chain_name") or "").strip() or _associations_chain_dir_name(page_slug)
    source_dir = (ASSOCIATIONS_CHAINS_ROOT / chain_name).resolve()
    root = ASSOCIATIONS_CHAINS_ROOT.resolve()
    if root != source_dir and root not in source_dir.parents:
        raise ValueError("Invalid association chain folder.")
    source_dir.mkdir(parents=True, exist_ok=True)
    assoc_settings["chain_name"] = chain_name
    assoc_settings.pop("archive_source", None)
    fs = composition.filter_settings or {}
    if not isinstance(fs, dict):
        fs = {}
    fs["associations"] = assoc_settings
    composition.filter_settings = fs
    composition.save(update_fields=["filter_settings"])
    return chain_name, source_dir


def collect_audio_source_files():
    allowed_exts = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}
    result = {}
    for source_dir in get_local_audio_sources():
        files = []
        for file in sorted(source_dir.iterdir()):
            if not file.is_file() or file.name.startswith("."):
                continue
            if file.suffix.lower() not in allowed_exts:
                continue
            files.append(file.name)
        if files:
            result[source_dir.name] = files
    return result

