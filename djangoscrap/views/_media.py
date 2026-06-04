import io
import os
import base64
import csv
import random
import difflib
import html as html_lib
import boto3
import shutil
import mimetypes
import ipaddress
import socket
from urllib.parse import quote, urlparse, unquote, parse_qs, urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
from bs4 import BeautifulSoup
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.core.files.base import ContentFile
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.cache import cache
from ..models import (
    Composition, CompositionRelease, CompositionReleaseFile, CompositionNFT,
    MintCollectionSettings, Profile, S3Bucket, Bucket, VideoComposition,
    BackgroundImage, ForegroundImage, Series, MonologuePersona,
    AdvancedThoughtScenario, WallProfile, WallRun, WallAssignment, WallHeartbeat,
    IngestionBatch, IngestionItem,
)
from ..forms import MintCollectionSettingsForm, BucketForm, CompositionForm, MonologuePersonaForm
from ..nft_media import (
    composition_media_assets, composition_source_signature,
    generate_composition_media_assets, capture_composition_video,
)
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from ..monologue_utils import build_segments
from ..monologue_compat import monologue_persona_queryset, monologue_visual_columns_ready
from ..monologue_streaming import generate_stream_segment, pick_fallback_segment
from ..monologue_visuals import refresh_persona_visual_cache
from ..research_packs import load_pack
from ..openai_monologue import run_monologue_research
from ..advanced_thoughts_engine import generate_advanced_tick
from ..ingestion_url_rewriter import rewrite_candidate_urls, RewriteStats
from ..ingestion_http import HOST_THROTTLE, DEFAULT_USER_AGENT, DownloadStats, head_probe
from ..ingestion_cookies import resolve_netscape_cookie_file, urllib_opener_with_netscape, yandex_playwright_cookie_list
from ..ingestion_download import parallel_download_urls, DEFAULT_MAX_WORKERS as _INGEST_DL_WORKERS
from ..associations_service import (
    pick_association_candidate,
    pick_association_candidates,
    record_association_feedback,
    record_association_winner_choice,
    bootstrap_feedback_from_training,
)
from ..video_processing import combine_video_with_audio, create_video_ffmpegNew
import ffmpeg
from django.core.files import File
from django.core.files.temp import NamedTemporaryFile
from PIL import Image, UnidentifiedImageError
from PIL import ImageOps
from ajax_datatable.views import AjaxDatatableView
from botocore.exceptions import ClientError, BotoCoreError, NoCredentialsError
import uuid
from django.http import JsonResponse, HttpResponse, HttpResponseRedirect, FileResponse, Http404
import re
import io
import json
import hashlib
import math
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods
from django.db import transaction
from django.db.models import Q
from moviepy.editor import ImageSequenceClip, VideoFileClip, ImageClip, CompositeVideoClip, AudioFileClip, concatenate_videoclips
import moviepy.editor as mp
import random, json, tempfile, zipfile, os
import string
from datetime import datetime
import subprocess
import time
import shutil as py_shutil
import sys
import threading
import logging
from typing import Any
from collections import defaultdict
from django.db import close_old_connections
from celery_app import classic_task, left_to_right_task, tunnel_task, right_to_left_task
from django.utils.crypto import get_random_string
from django.utils.text import get_valid_filename, slugify
from django.core.exceptions import SuspiciousFileOperation
from ..wall_runtime import compute_wall_tick, parse_screen_tile_index
try:
    from PIL import ImageResampling
    RESAMPLING_METHOD = ImageResampling.LANCZOS
except ImportError:
    RESAMPLING_METHOD = Image.LANCZOS
from ._constants import *
from ._source_utils import *
from ._ingestion_dedup import *
s3 = boto3.client('s3')


def get_sample_image_url(bucket_name):
    source_dir = LOCAL_SOURCES_ROOT / bucket_name
    if not source_dir.exists():
        return None
    for file in source_dir.iterdir():
        if file.suffix.lower() in LOCAL_SOURCE_IMAGE_EXTS:
            return f"/media/{bucket_name}/{file.name}"
    return None




def training_set_media_file(request, file_name):
    target = (ASSOCIATIONS_TRAINING_SET_DIR / file_name).resolve()
    root = ASSOCIATIONS_TRAINING_SET_DIR.resolve()
    if root != target and root not in target.parents:
        raise Http404("Training image not found")
    if not target.exists() or not target.is_file() or not _local_source_path_allowed(target):
        raise Http404("Training image not found")
    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(open(target, "rb"), content_type=content_type)



def _apply_image_transforms(image_bytes: bytes, content_type: str, transforms: dict) -> tuple[bytes, str]:
    """Apply flipH, flipV, crop to raw image bytes. Returns (bytes, content_type)."""
    try:
        from PIL import Image
    except ImportError:
        return image_bytes, content_type
    if not transforms:
        return image_bytes, content_type
    flip_h = bool(transforms.get("flipH"))
    flip_v = bool(transforms.get("flipV"))
    crop = transforms.get("crop")  # [x1, y1, x2, y2] in pixels
    if not flip_h and not flip_v and not crop:
        return image_bytes, content_type
    img = Image.open(io.BytesIO(image_bytes))
    if crop:
        try:
            x1, y1, x2, y2 = int(crop[0]), int(crop[1]), int(crop[2]), int(crop[3])
            img = img.crop((x1, y1, x2, y2))
        except (IndexError, ValueError, TypeError):
            pass
    if flip_h:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if flip_v:
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    out = io.BytesIO()
    fmt = img.format or "JPEG"
    if fmt == "JPEG" and img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.save(out, format=fmt)
    out.seek(0)
    out_ct = mimetypes.guess_type(f"x.{fmt.lower()}")[0] or content_type
    return out.read(), out_ct


def source_media_file(request, source_name, file_name):
    source_dir = (LOCAL_SOURCES_ROOT / source_name).resolve()
    target = _resolve_source_media_file(source_dir, file_name)
    if not target:
        raise Http404("Source file not found")
    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"

    # Apply per-image transforms if a composition_id is provided and the file is an image.
    comp_id = request.GET.get("comp")
    if comp_id and content_type.startswith("image/"):
        try:
            from ..models import Composition
            comp = Composition.objects.only("source_image_transforms").get(id=int(comp_id))
            rel_path = f"{source_name}/{file_name}"
            transforms = (comp.source_image_transforms or {}).get(rel_path) or {}
            if transforms:
                raw = target.read_bytes()
                raw, content_type = _apply_image_transforms(raw, content_type, transforms)
                from django.http import HttpResponse
                return HttpResponse(raw, content_type=content_type)
        except Exception:
            pass  # Fall through to raw serve on any error

    return FileResponse(open(target, "rb"), content_type=content_type)



def association_chain_media_file(request, chain_name, file_name):
    target = _resolve_association_chain_file(chain_name, file_name)
    if not target:
        raise Http404("Association chain file not found")
    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(open(target, "rb"), content_type=content_type)



def source_thumbnail_image(request, source_name, file_name):
    source_dir = (LOCAL_SOURCES_ROOT / source_name).resolve()
    target = _resolve_source_media_file(source_dir, file_name)
    if not target:
        raise Http404("Source file not found")

    ext = target.suffix.lower()
    is_image = ext in LOCAL_SOURCE_IMAGE_EXTS
    if not is_image and ext not in LOCAL_SOURCE_VIDEO_EXTS:
        raise Http404("Unsupported thumbnail type")

    # Max edge for the cached thumbnail. Folder-listing tiles render small, so a
    # downscaled JPEG (typically a few KB) replaces multi-MB originals — the
    # whole point of this endpoint for images, which previously passed the
    # full-resolution file straight through.
    THUMB_MAX_EDGE = 480

    # Serve from disk cache if the thumbnail is newer than the source file.
    cache_dir = Path(settings.MEDIA_ROOT) / "thumb_cache" / source_name
    safe_stem = hashlib.md5(file_name.encode()).hexdigest()
    cache_path = cache_dir / f"{safe_stem}.jpg"
    if cache_path.exists():
        try:
            if cache_path.stat().st_mtime >= target.stat().st_mtime:
                with open(cache_path, "rb") as fh:
                    return HttpResponse(fh.read(), content_type="image/jpeg")
        except OSError:
            pass

    if is_image:
        try:
            with Image.open(target) as im:
                im = ImageOps.exif_transpose(im)
                # Flatten alpha onto white so the thumbnail is a small JPEG.
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                else:
                    im = im.convert("RGB")
                im.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), RESAMPLING_METHOD)
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=82, optimize=True)
                jpeg_data = buf.getvalue()
        except Exception:
            # Corrupt/unsupported image: fall back to serving the original so the
            # tile still shows something rather than a broken image.
            return source_media_file(request, source_name, file_name)
    else:
        frame = _extract_video_frame_image(target)
        if frame is None:
            raise Http404("Unable to generate thumbnail")
        jpeg_bytes = io.BytesIO()
        frame.convert("RGB").save(jpeg_bytes, format="JPEG", quality=86)
        jpeg_data = jpeg_bytes.getvalue()

    # Write to cache for subsequent requests.
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_bytes(jpeg_data)
        tmp.replace(cache_path)
    except OSError:
        pass

    return HttpResponse(jpeg_data, content_type="image/jpeg")



# Stale lock timeout: if a warmup marker is older than this, treat it as abandoned.
_WARM_LOCK_TTL_SECONDS = 3600


def _thumb_warm_lock_path(source_name: str) -> Path:
    lock_dir = Path(settings.MEDIA_ROOT) / "thumb_cache" / ".warming"
    return lock_dir / hashlib.md5(source_name.encode()).hexdigest()


def _thumb_warm_acquire(source_name: str) -> bool:
    """Write a marker file. Returns True if this caller should do the warmup."""
    lock_path = _thumb_warm_lock_path(source_name)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.exists():
            age = __import__("time").time() - lock_path.stat().st_mtime
            if age < _WARM_LOCK_TTL_SECONDS:
                return False
        lock_path.write_text(str(__import__("os").getpid()))
        return True
    except OSError:
        return False


def _thumb_warm_release(source_name: str) -> None:
    try:
        _thumb_warm_lock_path(source_name).unlink(missing_ok=True)
    except OSError:
        pass


def _warm_video_thumbnails_bg(source_names: list[str]) -> None:
    """Pre-generate on-disk thumbnail cache for video assets in the given sources."""
    from ._source_utils import LOCAL_SOURCES_ROOT, LOCAL_SOURCE_VIDEO_EXTS, _local_source_dir_media_files
    from ._source_utils import _extract_video_frame_image

    for source_name in source_names:
        source_dir = LOCAL_SOURCES_ROOT / source_name
        if not source_dir.exists():
            _thumb_warm_release(source_name)
            continue
        cache_dir = Path(settings.MEDIA_ROOT) / "thumb_cache" / source_name
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            _thumb_warm_release(source_name)
            continue
        for file in sorted(_local_source_dir_media_files(source_dir), key=lambda p: p.name.lower()):
            if file.suffix.lower() not in LOCAL_SOURCE_VIDEO_EXTS:
                continue
            safe_stem = hashlib.md5(file.name.encode()).hexdigest()
            cache_path = cache_dir / f"{safe_stem}.jpg"
            if cache_path.exists():
                try:
                    if cache_path.stat().st_mtime >= file.stat().st_mtime:
                        continue
                except OSError:
                    pass
            try:
                frame = _extract_video_frame_image(file)
                if frame is None:
                    continue
                out = io.BytesIO()
                frame.convert("RGB").save(out, format="JPEG", quality=86)
                tmp = cache_path.with_suffix(".tmp")
                tmp.write_bytes(out.getvalue())
                tmp.replace(cache_path)
            except Exception:
                pass
        _thumb_warm_release(source_name)


def source_preview_assets(request):
    raw_sources = request.GET.get("sources", "")
    source_names = [s.strip() for s in raw_sources.split(",") if s.strip()]
    playback_mode = (request.GET.get("playback_mode") or "chronological").strip().lower()
    landscape_only = (request.GET.get("landscape_only") or "").strip().lower() in {"1", "true", "yes", "on"}
    max_items = 40
    if playback_mode == "random" and len(source_names) > 1:
        by_source = []
        for source_name in source_names:
            source_assets = collect_source_assets([source_name], landscape_only=landscape_only)
            if source_assets:
                random.shuffle(source_assets)
                by_source.append(source_assets)
        assets = []
        cursor = 0
        while len(assets) < max_items and by_source:
            next_round = []
            for source_assets in by_source:
                if cursor < len(source_assets):
                    assets.append(source_assets[cursor])
                    if len(assets) >= max_items:
                        break
                if cursor + 1 < len(source_assets):
                    next_round.append(source_assets)
            by_source = next_round
            cursor += 1
    else:
        assets = collect_source_assets(source_names, landscape_only=landscape_only)[:max_items]

    # Kick off background thumbnail pre-warming for sources not already being warmed.
    import threading
    to_warm = [name for name in source_names if _thumb_warm_acquire(name)]
    if to_warm:
        threading.Thread(target=_warm_video_thumbnails_bg, args=(to_warm,), daemon=True).start()

    return JsonResponse({"assets": assets})



from ._source_utils import _resolve_association_chain_file
__all__ = [n for n in list(vars().keys()) if not n.startswith('__')]
