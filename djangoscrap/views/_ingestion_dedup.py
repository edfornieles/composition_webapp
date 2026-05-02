"""
Ingestion deduplication helpers for the djangoscrap views package.

All names defined here are re-exported via views/__init__.py.
"""
from __future__ import annotations
import hashlib
import math
import io
import struct
import base64
import threading
import logging
from pathlib import Path
from collections import defaultdict
from PIL import Image, UnidentifiedImageError
from ._constants import (
    INGESTION_MEDIA_ROOT, LOCAL_SOURCES_ROOT,
    LOCAL_SOURCE_IMAGE_EXTS,
)
from ..models import IngestionItem, IngestionBatch

try:
    from PIL import ImageResampling
    RESAMPLING_METHOD = ImageResampling.LANCZOS
except ImportError:
    from PIL import Image as _pil
    RESAMPLING_METHOD = _pil.LANCZOS  # type: ignore

__all__ = [
    "_sha256_file",
    "_average_hash_hex",
    "_average_hash_hex_letterbox",
    "_dedupe_phash_for_item",
    "_destination_folder_dedupe_fingerprints",
    "_image_metadata",
    "_hamming_distance_hex",
    "_CLIP_EMBED_DIM",
    "_CLIP_MODEL_NAME",
    "_CLIP_MODEL_SINGLETON",
    "_CLIP_MODEL_LOCK",
    "_CLIP_MODEL_DISABLED",
    "_CLIP_LOGGER",
    "_clip_get_model",
    "_clip_embed_path",
    "_clip_embed_encode",
    "_clip_embed_decode",
    "_clip_load_batch_embeddings",
    "_detect_duplicate",
    "_ensure_ingestion_item_phash",
    "_dedupe_batch_exact_sha256",
    "_dedupe_batch_visual_crops",
]

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _average_hash_hex(path: Path) -> str:
    try:
        with Image.open(path) as img:
            gray = img.convert("L").resize((8, 8), RESAMPLING_METHOD)
            vals = list(gray.getdata())
        if not vals:
            return ""
        avg = sum(vals) / len(vals)
        bits = "".join("1" if v >= avg else "0" for v in vals)
        return f"{int(bits, 2):016x}"
    except Exception:
        return ""


def _average_hash_hex_letterbox(path: Path, box: int = 128) -> str:
    """
    Resize-to-fit inside a square, letterbox, then 8×8 average hash.
    Comparable across different resolutions and crops of the same artwork (full image vs crop).
    """
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            w, h = img.size
            if w <= 0 or h <= 0:
                return ""
            m = max(w, h, 1)
            nw = max(1, int(w * box / m))
            nh = max(1, int(h * box / m))
            resized = img.resize((nw, nh), RESAMPLING_METHOD)
            canvas = Image.new("RGB", (box, box), (0, 0, 0))
            ox = (box - nw) // 2
            oy = (box - nh) // 2
            canvas.paste(resized, (ox, oy))
            gray = canvas.convert("L").resize((8, 8), RESAMPLING_METHOD)
            vals = list(gray.getdata())
        if not vals:
            return ""
        avg = sum(vals) / len(vals)
        bits = "".join("1" if v >= avg else "0" for v in vals)
        return f"{int(bits, 2):016x}"
    except Exception:
        return ""


def _dedupe_phash_for_item(item: IngestionItem) -> str:
    """Perceptual hash for batch dedupe only — letterbox-normalized, not the stored item.phash."""
    if (item.media_kind or "") != "image":
        return ""
    rel = (item.file_path or "").strip().replace("\\", "/")
    if not rel or ".." in rel:
        return ""
    full = INGESTION_MEDIA_ROOT / rel
    if not full.is_file():
        return ""
    return _average_hash_hex_letterbox(full)


def _destination_folder_dedupe_fingerprints(source_name: str) -> tuple[set[str], list[str], int]:
    """
    SHA256 and letterbox pHash of each allowed media file in the destination source folder.
    Non-media files in the folder are ignored. Returns (sha256_set, letterbox_phashes, media_file_count).
    """
    # Lazy imports to avoid circular dependencies with _source_utils.
    from ._source_utils import get_local_sources, _local_source_dir_media_files  # noqa: PLC0415

    cleaned = (source_name or "").strip()
    if not cleaned:
        return set(), [], 0
    lookup = {p.name.lower(): p for p in get_local_sources()}
    source_dir = lookup.get(cleaned.lower())
    if not source_dir or not source_dir.is_dir():
        return set(), [], 0
    shas: set[str] = set()
    phashes: list[str] = []
    n_files = 0
    try:
        for p in sorted(_local_source_dir_media_files(source_dir), key=lambda x: x.name.lower()):
            n_files += 1
            try:
                shas.add(_sha256_file(p))
            except Exception:
                pass
            if p.suffix.lower() in LOCAL_SOURCE_IMAGE_EXTS:
                ph = _average_hash_hex_letterbox(p)
                if ph:
                    phashes.append(ph)
    except OSError:
        return set(), [], 0
    return shas, phashes, n_files


def _image_metadata(path: Path) -> dict:
    meta = {
        "width": 0,
        "height": 0,
        "orientation": "unknown",
        "color_mode": "unknown",
        "dominant_color_hex": "",
    }
    try:
        with Image.open(path) as img:
            w, h = img.size
            meta["width"] = int(w or 0)
            meta["height"] = int(h or 0)
            if w and h:
                if abs(w - h) <= max(4, int(min(w, h) * 0.03)):
                    meta["orientation"] = "square"
                elif w > h:
                    meta["orientation"] = "landscape"
                else:
                    meta["orientation"] = "portrait"
            rgb = img.convert("RGB")
            tiny = rgb.resize((1, 1), RESAMPLING_METHOD)
            r, g, b = tiny.getpixel((0, 0))
            meta["dominant_color_hex"] = f"#{int(r):02x}{int(g):02x}{int(b):02x}"
            gray = rgb.convert("L")
            # Low chroma difference implies grayscale-ish image.
            px = list(rgb.resize((16, 16), RESAMPLING_METHOD).getdata())
            avg_delta = sum(abs(p[0] - p[1]) + abs(p[1] - p[2]) + abs(p[0] - p[2]) for p in px) / max(1, len(px))
            meta["color_mode"] = "grayscale" if avg_delta < 15 else "color"
    except Exception:
        pass
    return meta


def _hamming_distance_hex(a: str, b: str) -> int:
    if not a or not b:
        return 999
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 999


# --------------------------------------------------------------------------- #
#  CLIP semantic dedupe                                                       #
# --------------------------------------------------------------------------- #
#
# pHash is cheap and catches pixel-level near-duplicates (same image, different
# JPEG encode, small crops). It fails on the hardest case we see in practice:
# the "same subject, different photograph" pattern — e.g. twenty different
# Pinterest shots of blue fur, taken with different lighting and framing, that
# all belong in the same visual cluster but have phash distances > 14.
#
# CLIP ViT-B/32 embeddings turn each image into a 512-D vector where
# semantically-similar images cluster in cosine space. Industry standard for
# this job (img2dataset, LAION, imagededup's CNN method, etc.). Cosine ≥ 0.92
# is our default "near-duplicate" threshold — tight enough that we're not
# throwing away legitimate variety, loose enough to catch the "another blue
# fur shot" case.
#
# The implementation is designed to degrade gracefully:
#   - If sentence-transformers or torch aren't importable, every helper returns
#     None / "" and the campaign cycle falls back to the existing phash path.
#     That's fine, it just means no semantic dedupe on that machine.
#   - Embeddings are cached inline on IngestionItem (base64-packed float16,
#     ~1.4 KB per row) so we never re-encode the same image twice.
#   - Model is loaded lazily and cached in a module-level singleton so a
#     campaign cycle with 150 downloads pays the ~2 s load cost once.
#
# See also: backfill_clip_embeddings management command for populating the
# field across existing batches.

_CLIP_EMBED_DIM = 512
_CLIP_MODEL_NAME = "clip-ViT-B-32"
_CLIP_MODEL_SINGLETON = None  # sentence_transformers.SentenceTransformer | None
_CLIP_MODEL_LOCK = threading.Lock()
_CLIP_MODEL_DISABLED = False  # flipped to True after an import / load failure
_CLIP_LOGGER = logging.getLogger("djangoscrap.clip")


def _clip_get_model():
    """
    Load (or return the cached) CLIP ViT-B/32 model. Returns None if the local
    environment doesn't have sentence-transformers / torch, in which case every
    caller should silently skip CLIP checks.
    """
    global _CLIP_MODEL_SINGLETON, _CLIP_MODEL_DISABLED
    if _CLIP_MODEL_DISABLED:
        return None
    if _CLIP_MODEL_SINGLETON is not None:
        return _CLIP_MODEL_SINGLETON
    with _CLIP_MODEL_LOCK:
        if _CLIP_MODEL_SINGLETON is not None:
            return _CLIP_MODEL_SINGLETON
        if _CLIP_MODEL_DISABLED:
            return None
        try:
            # Imported lazily so servers without torch can still run the rest
            # of the app. Both libs are in requirements.txt but we don't want
            # a missing wheel to break ingestion entirely.
            from sentence_transformers import SentenceTransformer  # type: ignore
            _CLIP_MODEL_SINGLETON = SentenceTransformer(_CLIP_MODEL_NAME)
            _CLIP_LOGGER.info("Loaded CLIP model %s", _CLIP_MODEL_NAME)
        except Exception as exc:  # noqa: BLE001
            _CLIP_LOGGER.warning(
                "CLIP unavailable (%s); semantic dedupe disabled for this process.",
                exc,
            )
            _CLIP_MODEL_DISABLED = True
            _CLIP_MODEL_SINGLETON = None
    return _CLIP_MODEL_SINGLETON


def _clip_embed_path(path):
    """
    Encode an image file as a 512-D L2-normalized float32 numpy array. Returns
    None on any failure (unreadable image, CLIP unavailable, etc.) — callers
    are expected to treat None as "no CLIP signal, fall through".
    """
    try:
        import numpy as np  # local import so the view module still imports without numpy
    except Exception:
        return None
    model = _clip_get_model()
    if model is None:
        return None
    try:
        p = Path(path) if not isinstance(path, Path) else path
        if not p.is_file():
            return None
        with Image.open(p) as img:
            img = img.convert("RGB")
            vec = model.encode(
                [img],
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        if vec is None or len(vec) == 0:
            return None
        arr = np.asarray(vec[0], dtype=np.float32)
        if arr.shape != (_CLIP_EMBED_DIM,):
            return None
        # Defensive re-normalize: SentenceTransformer already does this, but a
        # tiny numerical drift would throw off the cosine threshold.
        n = float(np.linalg.norm(arr))
        if n <= 0.0:
            return None
        return arr / n
    except Exception as exc:  # noqa: BLE001
        _CLIP_LOGGER.debug("CLIP encode failed for %s: %s", path, exc)
        return None


def _clip_embed_encode(vec) -> str:
    """Pack a CLIP vector as base64(float16). Empty string on None."""
    if vec is None:
        return ""
    try:
        import numpy as np
        arr = np.asarray(vec, dtype=np.float16)
        if arr.shape != (_CLIP_EMBED_DIM,):
            return ""
        return base64.b64encode(arr.tobytes()).decode("ascii")
    except Exception:
        return ""


def _clip_embed_decode(s: str):
    """Inverse of _clip_embed_encode. Returns a float32 np.ndarray or None."""
    if not s:
        return None
    try:
        import numpy as np
        raw = base64.b64decode(s.encode("ascii"))
        # 512 × 2 bytes = 1024 bytes when the row is well-formed.
        if len(raw) != _CLIP_EMBED_DIM * 2:
            return None
        arr = np.frombuffer(raw, dtype=np.float16).astype(np.float32)
        if arr.shape != (_CLIP_EMBED_DIM,):
            return None
        return arr
    except Exception:
        return None


def _clip_load_batch_embeddings(batch_id: int, limit: int = 2000):
    """
    Return (ids: list[int], matrix: np.ndarray[n,512]) for the last ``limit``
    items in ``batch_id`` that have a stored CLIP embedding. Newest first, so
    the matrix compares against the most recently-accepted neighbourhood first
    — which is exactly the cluster a new candidate is most likely to collide
    with. Returns (None, None) if CLIP or numpy is unavailable.
    """
    try:
        import numpy as np
    except Exception:
        return None, None
    qs = (
        IngestionItem.objects
        .filter(batch_id=batch_id)
        .exclude(clip_embedding="")
        .exclude(decision="rejected")
        .only("id", "clip_embedding")
        .order_by("-id")[:limit]
    )
    ids: list[int] = []
    rows: list = []
    for item in qs:
        vec = _clip_embed_decode(item.clip_embedding)
        if vec is None:
            continue
        ids.append(item.id)
        rows.append(vec)
    if not rows:
        return ids, None
    matrix = np.vstack(rows).astype(np.float32)
    return ids, matrix


def _detect_duplicate(
    sha256: str,
    phash: str,
    *,
    batch_id: int | None = None,
    hamming_max: int = 12,
    scan_cap: int = 4000,
    clip_embedding=None,
    clip_cosine_max: float = 0.92,
    clip_scan_cap: int = 2000,
) -> tuple[str, str]:
    """
    Decide whether an incoming item is a duplicate of something we've already ingested.

    - Exact match by sha256 is always rejected (cheapest, highest-confidence signal).
    - For phash (perceptual hash) we prefer scanning inside the *current* batch because:
        * the old behaviour — first 600 rows globally with no ordering — was effectively
          random across batches and almost never contained the nearest neighbours in a
          large, active batch (e.g. batch 141 with 1177 items), so near-duplicates slipped
          through and the grid filled with repeated textures.
        * scoping to the batch keeps scans fast while making the distance metric meaningful.
    - Hamming threshold is 12 (vs. the old 6) to match the UI's manual "Reject similar /
      duplicate crops" pass and to better catch low-entropy textures like fur/ice/sky
      where phash drifts a few bits even between visually identical CDN re-encodes.
    - When a CLIP embedding is supplied, a final cosine-similarity pass catches the
      "same subject, different photograph" cluster (e.g. twenty different blue-fur
      shots) that phash cannot — this is the dominant form of repetition we saw on
      batch 141 after the phash path was tightened.
    - IMPORTANT: the phash and CLIP scans deliberately exclude rejected items.
      Before that fix, a rejected item's phash/embedding stayed in the pool and
      caused a *cascade*: an early auto-reject would become the "near-duplicate
      target" for the next candidate, which would then be rejected too and added
      to the pool as yet another poison anchor. On batch 161 (fire_wall_search)
      this collapsed a 76-item cycle into 1 accepted + 75 rejected with no
      surviving pending. Scanning only non-rejected rows means dedupe only
      protects against reimporting something that's still "alive" (pending or
      accepted); already-rejected items are inert. User-deleted items are still
      filtered out permanently via the separate ``deleted_tombstones`` path.
    """
    exact = (
        IngestionItem.objects.filter(sha256=sha256)
        .exclude(sha256="")
        .exclude(decision="rejected")
        .first()
    )
    if exact:
        return "rejected", f"exact duplicate of item #{exact.id}"
    if phash:
        qs = (
            IngestionItem.objects.exclude(phash="")
            .exclude(decision="rejected")
        )
        if batch_id is not None:
            qs = qs.filter(batch_id=batch_id)
        best_id: int | None = None
        best_dist = 999
        for c in qs.only("id", "phash").order_by("-id")[:scan_cap]:
            dist = _hamming_distance_hex(phash, c.phash)
            if dist < best_dist:
                best_dist = dist
                best_id = c.id
                if best_dist == 0:
                    break
        if best_id is not None and best_dist <= hamming_max:
            return "rejected", f"near-duplicate of item #{best_id} (hamming={best_dist})"

    # CLIP semantic near-duplicate check. We only run it when the caller has
    # actually computed an embedding for the incoming item (campaign cycle +
    # manual ingestion paths do this; cheap/legacy callers can omit it and
    # fall through on phash alone). The check is batch-scoped for the same
    # reason phash is: cross-batch collisions would be noisy and we want the
    # distance metric to reflect "is this a repeat of something I've already
    # put in THIS folder".
    if clip_embedding is not None and batch_id is not None:
        try:
            import numpy as np  # local import, see _clip_embed_path
            ids, matrix = _clip_load_batch_embeddings(batch_id, limit=clip_scan_cap)
            if matrix is not None and len(ids) > 0:
                incoming = np.asarray(clip_embedding, dtype=np.float32)
                # Both sides are L2-normalized, so cosine similarity == dot product.
                sims = matrix @ incoming
                idx = int(np.argmax(sims))
                best_sim = float(sims[idx])
                if best_sim >= clip_cosine_max:
                    return (
                        "rejected",
                        f"near-duplicate of item #{ids[idx]} (clip cosine={best_sim:.3f})",
                    )
        except Exception as exc:  # noqa: BLE001
            # Never let a CLIP-path failure block ingestion — phash+sha already
            # caught the cheap cases. Log and move on.
            _CLIP_LOGGER.debug("CLIP dedupe skipped: %s", exc)
    return "pending", ""


def _ensure_ingestion_item_phash(item: IngestionItem) -> str:
    ph = (item.phash or "").strip()
    if ph:
        return ph
    if (item.media_kind or "") != "image":
        return ""
    rel = (item.file_path or "").strip().replace("\\", "/")
    if not rel or ".." in rel:
        return ""
    full = INGESTION_MEDIA_ROOT / rel
    if not full.is_file():
        return ""
    ph = _average_hash_hex(full)
    if ph:
        item.phash = ph
        item.save(update_fields=["phash"])
    return ph or ""


def _dedupe_batch_exact_sha256(
    batch: IngestionBatch,
    *,
    accepted_only: bool = False,
    folder_shas: set[str] | None = None,
) -> dict:
    """
    Reject ingestion rows whose file hash matches the destination folder, then same-hash duplicates within the batch.
    """
    from django.db.models import F

    folder_shas = folder_shas or set()
    base = batch.items.filter(media_kind="image").exclude(sha256="")
    if accepted_only:
        base = base.filter(decision="accepted")
    else:
        base = base.filter(decision__in=["pending", "accepted"])
    items = list(base.annotate(px=F("width") * F("height")).order_by("-px", "id"))
    by_sha: dict[str, list[IngestionItem]] = {}
    for item in items:
        s = (item.sha256 or "").strip()
        if not s:
            continue
        by_sha.setdefault(s, []).append(item)
    rejected = 0
    rejected_ids: list[int] = []
    folder_dup = 0
    batch_dup = 0
    for s, group in by_sha.items():
        if s in folder_shas:
            for dup in group:
                dup.decision = "rejected"
                dup.dedupe_note = "same file already in destination source folder"[:255]
                dup.save(update_fields=["decision", "dedupe_note"])
                rejected += 1
                folder_dup += 1
                rejected_ids.append(dup.id)
            continue
        if len(group) <= 1:
            continue
        group.sort(key=lambda x: (-(x.width or 0) * (x.height or 0), x.id))
        keeper = group[0]
        for dup in group[1:]:
            dup.decision = "rejected"
            dup.dedupe_note = f"exact duplicate of item #{keeper.id} (same file hash)"[:255]
            dup.save(update_fields=["decision", "dedupe_note"])
            rejected += 1
            batch_dup += 1
            rejected_ids.append(dup.id)
    return {
        "rejected": rejected,
        "rejected_ids": rejected_ids,
        "folder_sha_duplicate": folder_dup,
        "batch_duplicate": batch_dup,
    }


def _dedupe_batch_visual_crops(
    batch: IngestionBatch,
    *,
    max_hamming: int = 14,
    accepted_only: bool = False,
    folder_phashes: list[str] | None = None,
) -> dict:
    """
    Cluster by letterbox-normalized perceptual hash. Compare against images already in the
    destination source folder, then against larger items earlier in this batch.
    """
    from django.db.models import F

    folder_phashes = folder_phashes or []
    base = batch.items.filter(media_kind="image")
    if accepted_only:
        base = base.filter(decision="accepted")
    else:
        base = base.filter(decision__in=["pending", "accepted"])
    items = list(base.annotate(px=F("width") * F("height")).order_by("-px", "id"))
    no_phash_skipped = 0
    rejected = 0
    rejected_ids: list[int] = []
    rejected_folder_visual = 0
    rejected_batch_visual = 0
    kept: list[tuple[int, str]] = []
    for item in items:
        ph = _dedupe_phash_for_item(item)
        if not ph:
            no_phash_skipped += 1
            continue
        match_folder = False
        for kph in folder_phashes:
            if _hamming_distance_hex(ph, kph) <= max_hamming:
                match_folder = True
                break
        if match_folder:
            item.decision = "rejected"
            item.dedupe_note = f"matches image already in destination folder (letterbox h≤{max_hamming})"[:255]
            item.save(update_fields=["decision", "dedupe_note"])
            rejected += 1
            rejected_folder_visual += 1
            rejected_ids.append(item.id)
            continue
        dup_of = None
        for kid, kph in kept:
            if _hamming_distance_hex(ph, kph) <= max_hamming:
                dup_of = kid
                break
        if dup_of is not None:
            note = f"near-duplicate of item #{dup_of} (visual crop, h≤{max_hamming})"
            item.decision = "rejected"
            item.dedupe_note = note[:255]
            item.save(update_fields=["decision", "dedupe_note"])
            rejected += 1
            rejected_batch_visual += 1
            rejected_ids.append(item.id)
        else:
            kept.append((item.id, ph))
    return {
        "rejected": rejected,
        "rejected_ids": rejected_ids,
        "kept_canonical": len(kept),
        "skipped_no_phash": no_phash_skipped,
        "rejected_folder_visual": rejected_folder_visual,
        "rejected_batch_visual": rejected_batch_visual,
    }

