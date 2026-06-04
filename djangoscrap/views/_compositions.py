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
from ._utils import (
    parse_composition_hashtags,
    normalize_page_link,
    parse_positive_int,
    parse_float_in_range,
    build_preview_image_from_sources,
)
from ._nft import _composition_media_context
from ._ingestion_dedup import *
s3 = boto3.client('s3')


# Whitelist of preset keys the client-side renderer understands. Anything else
# falls back to 'static' silently so a typo never crashes a save.
_TEXT_LAYER_PRESETS = {
    "static", "flash", "flicker", "typewriter", "scroll-band", "scroll-up",
    "bounce", "karaoke", "glitch", "rgb-shift", "strobe", "drop-in",
}
_TEXT_LAYER_POSITIONS = {"top", "center", "bottom", "top-left", "top-right", "bottom-left", "bottom-right"}
_TEXT_LAYER_Z_TARGETS = {"above-bg", "above-fg", "above-overlay", "top"}
_TEXT_LAYER_MAX = 1

def _parse_text_layers_from_post(post) -> list[dict]:
    """Pull text-layer rows out of the admin form into a clean list of dicts
    suitable for the JSONField. Empty rows (no text) are dropped so the user
    doesn't accidentally save garbage entries.

    Form fields per row i (1..N):
      text_layer_text_<i>           — the actual string (required)
      text_layer_preset_<i>         — one of _TEXT_LAYER_PRESETS
      text_layer_position_<i>       — one of _TEXT_LAYER_POSITIONS (or empty for custom)
      text_layer_z_target_<i>       — one of _TEXT_LAYER_Z_TARGETS
      text_layer_font_family_<i>    — CSS font-family
      text_layer_font_size_pct_<i>  — number 1..50 (% of viewport height)
      text_layer_color_<i>          — #RRGGBB
      text_layer_start_sec_<i>      — number ≥ 0
      text_layer_duration_sec_<i>   — number > 0  (0 → omitted, runs indefinitely)
    """
    out = []
    for i in range(1, _TEXT_LAYER_MAX + 1):
        text = (post.get(f"text_layer_text_{i}") or "").strip()
        if not text:
            continue
        preset = (post.get(f"text_layer_preset_{i}") or "static").strip().lower()
        if preset not in _TEXT_LAYER_PRESETS:
            preset = "static"
        position = (post.get(f"text_layer_position_{i}") or "center").strip().lower()
        if position not in _TEXT_LAYER_POSITIONS:
            position = "center"
        z_target = (post.get(f"text_layer_z_target_{i}") or "above-overlay").strip().lower()
        if z_target not in _TEXT_LAYER_Z_TARGETS:
            z_target = "above-overlay"
        try: font_size_pct = max(1.0, min(50.0, float(post.get(f"text_layer_font_size_pct_{i}") or 6)))
        except (TypeError, ValueError): font_size_pct = 6.0
        try: start_sec = max(0.0, float(post.get(f"text_layer_start_sec_{i}") or 0))
        except (TypeError, ValueError): start_sec = 0.0
        try: duration_sec = max(0.0, float(post.get(f"text_layer_duration_sec_{i}") or 0))
        except (TypeError, ValueError): duration_sec = 0.0
        color = (post.get(f"text_layer_color_{i}") or "#ffffff").strip()[:9] or "#ffffff"
        font_family = (post.get(f"text_layer_font_family_{i}") or "").strip()[:80]
        entry = {
            "text": text[:500],
            "preset": preset,
            "position": position,
            "z_target": z_target,
            "font_family": font_family or "Inter, sans-serif",
            "font_size_pct": font_size_pct,
            "color": color,
            "start_sec": start_sec,
        }
        if duration_sec > 0:
            entry["duration_sec"] = duration_sec
        out.append(entry)
    return out


def _r2_client():
    account_id = (os.getenv("R2_ACCOUNT_ID") or "").strip()
    access_key = (os.getenv("R2_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID") or "").strip()
    secret_key = (os.getenv("R2_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()
    if not account_id or not access_key or not secret_key:
        raise ValueError("Missing R2 credentials. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY.")
    endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )



def _composition_source_names(composition: Composition) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for source_name in (composition.background_sources or []) + (composition.foreground_sources or []) + (composition.overlay_sources or []):
        token = (source_name or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out



def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()



def _local_manifest_for_composition(composition: Composition) -> list[dict]:
    items: list[dict] = []
    excluded = {str(p).strip() for p in (composition.publish_excluded_files or []) if str(p).strip()}
    for source_name in _composition_source_names(composition):
        source_dir = (LOCAL_SOURCES_ROOT / source_name).resolve()
        if not source_dir.exists() or not source_dir.is_dir():
            continue
        for media_path in _local_source_dir_media_files(source_dir):
            rel_in_source = media_path.resolve().relative_to(source_dir).as_posix()
            rel_path = f"{source_name}/{rel_in_source}"
            if rel_path in excluded:
                continue
            mime, _ = mimetypes.guess_type(media_path.name)
            size = 0
            try:
                size = int(media_path.stat().st_size)
            except OSError:
                size = 0
            items.append(
                {
                    "relative_path": rel_path,
                    "absolute_path": str(media_path.resolve()),
                    "file_size": size,
                    "sha256": _file_sha256(media_path.resolve()),
                    "mime_type": mime or "application/octet-stream",
                }
            )
    items.sort(key=lambda row: row["relative_path"])
    return items



def _remote_keys_for_prefix(client, bucket: str, prefix: str) -> set[str]:
    keys: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    clean_prefix = prefix.strip().strip("/")
    target_prefix = f"{clean_prefix}/" if clean_prefix else ""
    for page in paginator.paginate(Bucket=bucket, Prefix=target_prefix):
        for obj in page.get("Contents", []):
            key = str(obj.get("Key") or "")
            if not key:
                continue
            keys.add(key)
    return keys



def _sources_root_prefix(raw_prefix: str | None) -> str:
    clean = (raw_prefix or "").strip().strip("/")
    return clean or "sources"



def _manifest_local_and_remote_keys(manifest: list[dict], prefix: str) -> set[str]:
    base_prefix = f"{prefix}/" if prefix else ""
    return {f"{base_prefix}{row['relative_path']}" for row in manifest}



def _remote_keys_for_manifest_sources(client, bucket: str, manifest: list[dict], prefix: str) -> set[str]:
    source_names: set[str] = set()
    for row in manifest:
        rel = str(row.get("relative_path") or "").strip().lstrip("/")
        if "/" not in rel:
            continue
        source_names.add(rel.split("/", 1)[0])
    keys: set[str] = set()
    for source_name in sorted(source_names):
        keys |= _remote_keys_for_prefix(client, bucket, f"{prefix}/{source_name}")
    return keys



def _resolve_composition_relative_media_path(relative_path: str) -> tuple[str, Path, Path] | None:
    token = (relative_path or "").strip().lstrip("/").replace("\\", "/")
    if not token or "/" not in token:
        return None
    source_name, rel_in_source = token.split("/", 1)
    source_name = source_name.strip()
    rel_in_source = rel_in_source.strip().lstrip("/")
    if not source_name or not rel_in_source:
        return None
    source_dir = (LOCAL_SOURCES_ROOT / source_name).resolve()
    if not source_dir.exists() or not source_dir.is_dir():
        return None
    target = (source_dir / rel_in_source).resolve()
    if source_dir not in target.parents or not target.exists() or not target.is_file():
        return None
    if not _local_source_path_allowed(target):
        return None
    return source_name, source_dir, target


def _trigger_poster_generation(composition_id: int) -> None:
    """Generate the composition's poster in a *detached subprocess*.

    Why subprocess and not a daemon thread: poster generation launches Playwright
    Chromium, which can balloon RSS by 200-500 MB. With one gunicorn worker
    × 96 threads, several concurrent posters can OOM the worker — taking the
    whole site down. A subprocess is in its own address space, so OOM there
    only kills the capture, not the web server. The supervisor loop in
    run_gunicorn.sh also auto-restarts if a worker does die for any other reason.
    """
    import subprocess
    import sys
    from pathlib import Path as _Path
    project_root = _Path(__file__).resolve().parent.parent.parent
    script = (
        "import os, sys, time, traceback, django\n"
        f"sys.path.insert(0, {str(project_root)!r})\n"
        f"os.chdir({str(project_root)!r})\n"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'djangoscrap.settings')\n"
        "django.setup()\n"
        "from djangoscrap.nft_media import generate_composition_media_assets\n"
        "from djangoscrap.models import Composition\n"
        f"_cid = {int(composition_id)}\n"
        "_label = f'comp={_cid} kinds=poster (auto-on-save)'\n"
        "_t = time.time()\n"
        "print(f'[{time.strftime(\"%H:%M:%S\")}] START {_label}', flush=True)\n"
        "try:\n"
        "    c = Composition.objects.prefetch_related('media_assets').get(id=_cid)\n"
        "    if c.url:\n"
        "        res = generate_composition_media_assets(c, kinds=['poster'])\n"
        "        dt = time.time() - _t\n"
        "        print(f'[{time.strftime(\"%H:%M:%S\")}] DONE  {_label} in {dt:.1f}s -> {list(res.keys()) if res else []}', flush=True)\n"
        "    else:\n"
        "        print(f'[{time.strftime(\"%H:%M:%S\")}] SKIP  {_label} (no public URL yet)', flush=True)\n"
        "except Exception as e:\n"
        "    dt = time.time() - _t\n"
        "    print(f'[{time.strftime(\"%H:%M:%S\")}] FAIL  {_label} after {dt:.1f}s: {type(e).__name__}: {e}', flush=True)\n"
        "    traceback.print_exc()\n"
    )
    log_path = str(project_root / "media_gen.log")
    try:
        with open(log_path, "ab") as logf:
            subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=logf,
                stderr=logf,
                start_new_session=True,  # detach from gunicorn's process group
                close_fds=True,
            )
    except Exception:
        # If subprocess spawn itself fails (rare), don't propagate — caller
        # already returned a redirect to the user; we just lose this poster.
        pass


def add_composition(request, composition_id=None):
    # Define paths for downloaded images & videos. Anchor to BASE_DIR so these
    # resolve correctly regardless of the worker's cwd — gunicorn workers don't
    # preserve `runserver`'s implicit project-root cwd, so relative paths break.
    _BASE = settings.BASE_DIR
    TEMP_BG_FOLDER = str(_BASE / "media/temp_s3_back_files")
    TEMP_FG_FOLDER = str(_BASE / "media/temp_s3_fore_files")
    VIDEO_DIR = str(_BASE / "media/videos")
    TEMP_IMAGE_FOLDER = str(_BASE / "media/temp_images")
    THUMBNAIL_DIR = str(_BASE / "static/composition_thumbnails")
    AUDIO_DIR = str(_BASE / "compositions/audios")
    MERGED_IMAGE_DIR = str(_BASE / "media/merged_images")
    
    # Ensure necessary directories exist
    os.makedirs(TEMP_BG_FOLDER, exist_ok=True)
    os.makedirs(TEMP_FG_FOLDER, exist_ok=True)
    os.makedirs(VIDEO_DIR, exist_ok=True)
    os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    os.makedirs(MERGED_IMAGE_DIR, exist_ok=True)  
    os.makedirs(TEMP_IMAGE_FOLDER, exist_ok=True)

    matching_buckets = [source.name for source in get_local_sources()]
    composition = get_object_or_404(Composition, id=composition_id) if composition_id else None
   
    return_page = (request.POST.get("return_page") if request.method == "POST" else request.GET.get("return_page")) or ""
    return_page = str(return_page).strip()
    if not return_page.isdigit():
        return_page = ""
    return_folder = (request.POST.get("return_folder") if request.method == "POST" else request.GET.get("return_folder")) or ""
    return_folder = re.sub(r"[^A-Za-z0-9_\-]", "", str(return_folder).strip())[:32]

    def redirect_to_composition_view_with_page(preserve_page: bool = True):
        if return_folder:
            return redirect("composition_folder_view", grid_id=return_folder)
        if preserve_page and return_page:
            return redirect(f"{reverse('composition-view')}?page={return_page}")
        return redirect("composition-view")

    def redirect_to_form():
        if composition:
            url = reverse("composition-edit", kwargs={"composition_id": composition.id})
            if return_folder:
                url += f"?return_folder={return_folder}"
            elif return_page:
                url += f"?return_page={return_page}"
            return redirect(url)
        return redirect("composition-add")

    if request.method == "POST":
        def normalize_transition_mode(raw_value: str, fallback: str = "none") -> str:
            raw = (raw_value or fallback or "none").strip().lower()
            compact = raw.replace(" ", "").replace("-", "").replace("_", "")
            if compact == "crossfade":
                return "crossfade"
            if compact == "fade":
                return "fade"
            if compact == "melt":
                return "melt"
            return "none"

        composition_type = (request.POST.get("type") or "classic").strip()
        if composition_type == "morph":
            composition_type = "single"
        source_type = (request.POST.get("source_type") or "s3").strip()
        random_link_scope = (request.POST.get("random_link_scope") or "all").strip()
        mood_rating = (request.POST.get("mood_rating") or (composition.mood_rating if composition else "mid") or "mid").strip().lower()
        if mood_rating not in {"chill", "mid", "agro"}:
            mood_rating = "mid"
        composition_hashtags = parse_composition_hashtags(request.POST.get("composition_hashtags", ""))
        composition_emotions = parse_composition_hashtags(request.POST.get("composition_emotions", ""))
        composition_themes = parse_composition_hashtags(request.POST.get("composition_themes", "")) if "composition_themes" in request.POST else None
        composition_characters = parse_composition_hashtags(request.POST.get("composition_characters", ""))
        allowed_persona_ids_raw = request.POST.getlist("allowed_persona_ids")
        allowed_persona_ids: list[int] = []
        for raw in allowed_persona_ids_raw:
            try:
                allowed_persona_ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        selected_series_id = (request.POST.get("series_id") or "").strip()
        new_series_name = (request.POST.get("new_series_name") or "").strip()
        series_submitted = "series_id" in request.POST or "new_series_name" in request.POST

        background_sources = [request.POST.get(f"background_sources{i}") for i in range(1, 5)]
        background_sources = [src for src in background_sources if src]
        if not background_sources:
            legacy_bg = [request.POST.get(f"bg_bucket{i}") for i in range(1, 5)]
            background_sources = [src for src in legacy_bg if src]

        foreground_sources = [request.POST.get(f"foreground_sources{i}") for i in range(1, 5)]
        foreground_sources = [src for src in foreground_sources if src]
        if not foreground_sources:
            legacy_fg = [request.POST.get(f"fg_bucket{i}") for i in range(1, 5)]
            foreground_sources = [src for src in legacy_fg if src]
        overlay_sources = [request.POST.get("overlay_source_folder")]
        overlay_sources = [src for src in overlay_sources if src]

        if composition_type in {"classic", "psychedelic-classic", "circle-foreground", "circle-foreground-rotate", "circle-foreground-rotate-mirror", "cross-foreground", "cross-foreground-rotate", "cross-foreground-tunnel", "bat-foreground", "bat-foreground-rotate", "left-to-right", "top-and-bottom", "strobe-double", "tunnel-double", "mash-fine-double"} and source_type != "upload":
            if not background_sources or not foreground_sources:
                messages.error(request, "Please select at least one background and one foreground source.")
                return redirect_to_form()
        if composition_type in {"cylinder-room", "triple-cylinder-room", "rotating-cube", "rotating-cube-strobe", "road-trip"} and source_type != "upload":
            if not foreground_sources:
                messages.error(request, "Please select at least one foreground source.")
                return redirect_to_form()
        if composition_type in {"single", "vibrate", "morph-fast", "eye-morph", "center-shift", "star-shift", "prism-burst", "prism-burst-neon", "sleep-drift", "calm-wave", "anger-surge", "anxiety-loop", "box-room"} and source_type != "upload":
            if not background_sources:
                messages.error(request, "Please select at least one background source.")
                return redirect_to_form()
        if composition_type in {"psychedelic", "psychedelic-calm", "psychedelic-intense"} and source_type != "upload":
            if not background_sources:
                messages.error(request, "Please select at least one background source.")
                return redirect_to_form()
        if composition_type == "kaleidoscope" and source_type != "upload":
            if not background_sources:
                messages.error(request, "Please select at least one background source.")
                return redirect_to_form()
        if composition_type == "strobe" and source_type != "upload":
            if not background_sources:
                messages.error(request, "Please select at least one background source.")
                return redirect_to_form()
        if composition_type in {"tunnel", "psychedelic-tunnel", "tunnel-burst", "zoom-forward", "zoom-slow", "zoom-backward", "zoom-backward-slow", "swirl", "quad", "mash", "mash-fine", "mash-fine-flux", "mash-superfine", "mash-superfine-flux", "vertical-stripes", "horizontal-stripes", "vertical-stripes-thick", "horizontal-stripes-thick", "social-scroll", "dirty-scroll", "scrollhole", "strobe", "vibrate", "morph-fast", "eye-morph", "center-shift", "star-shift", "prism-burst", "prism-burst-neon", "sleep-drift", "calm-wave", "anger-surge", "anxiety-loop", "box-room", "liquid-mirror", "liquid-mirror-extreme", "wax-melt", "slot-machine", "eye-wake"} and not background_sources:
            messages.error(request, "Please select at least one background source.")
            return redirect_to_form()
        if composition_type == "associations":
            has_seed_url = bool((request.POST.get("associations_seed_url") or "").strip())
            has_seed_source = bool((request.POST.get("associations_seed_source") or "").strip())
            has_seed_upload = bool(request.FILES.get("associations_seed_upload"))
            has_local_pool = bool(background_sources or foreground_sources)
            if not (has_seed_url or has_seed_source or has_seed_upload or has_local_pool):
                messages.error(request, "Associations mode needs a seed image (URL, upload, or source folder) or local sources.")
                return redirect_to_form()

        base_url = request.POST.get("base_url", "").rstrip("/") or f"http://{request.get_host()}"
        url_slug = request.POST.get("url_slug", "").strip().strip("/")
        requested_name = (request.POST.get("name") or "").strip()
        if not url_slug and requested_name:
            url_slug = slugify(requested_name.replace(" ", "_"))
        full_url = f"/{url_slug}" if url_slug else None
        page_link = normalize_page_link(request.POST.get("page_link"), request)
        selected_series = None
        if series_submitted:
            if new_series_name:
                selected_series, _ = Series.objects.get_or_create(name=new_series_name)
            elif selected_series_id:
                try:
                    selected_series = Series.objects.get(id=int(selected_series_id))
                except (ValueError, Series.DoesNotExist):
                    selected_series = None
        raw_filter_settings = request.POST.get("filter_settings_json", "{}")
        try:
            parsed_filter_settings = json.loads(raw_filter_settings) if raw_filter_settings else {}
            if not isinstance(parsed_filter_settings, dict):
                parsed_filter_settings = {}
        except (TypeError, ValueError):
            parsed_filter_settings = {}
        layer_transitions = parsed_filter_settings.get("layer_transitions") or {}
        if not isinstance(layer_transitions, dict):
            layer_transitions = {}
        overlay_effect_raw = (request.POST.get("overlay_effect") or "").strip().lower()
        overlay_effect_alias = overlay_effect_raw.replace(" ", "").replace("-", "").replace("_", "")
        if overlay_effect_alias in {"circle", "circlerotate"}:
            overlay_effect = "circle_rotate"
        elif overlay_effect_alias in {"circlemirrror", "circlemirror", "circlerotatemirror"}:
            overlay_effect = "circle_rotate_mirror"
        elif overlay_effect_alias in {"mash", "glitchmash", "overlaymash"}:
            overlay_effect = "mash"
        elif overlay_effect_alias in {"orb", "orbsphere", "folderorb", "sphere"}:
            overlay_effect = "orb"
        elif overlay_effect_alias in {"rotate3d", "spin3d", "rotate"}:
            overlay_effect = "rotate_3d"
        elif overlay_effect_alias in {"rotate3dreverse", "spin3dreverse", "rotatereverse",
                                      "rotateopposite", "rotatebackwards", "rotateccw"}:
            overlay_effect = "rotate_3d_reverse"
        elif overlay_effect_alias in {"rotate3dvertical", "spin3dvertical", "rotatevertical",
                                      "rotateflip", "rotateforward", "rotatetumble", "verticalrotate"}:
            overlay_effect = "rotate_3d_vertical"
        elif overlay_effect_alias in {"liquidmirror", "liquid", "mirror", "ripple", "liquidwarp"}:
            overlay_effect = "liquid_mirror"
        elif overlay_effect_alias in {"liquidmirrorrotate", "liquidrotate", "ripplerotate",
                                      "liquidspin", "spinliquid"}:
            overlay_effect = "liquid_mirror_rotate"
        elif overlay_effect_alias in {"liquidmirrorrotatevertical", "liquidrotatevertical",
                                      "liquidspinvertical", "spinliquidvertical"}:
            overlay_effect = "liquid_mirror_rotate_vertical"
        elif overlay_effect_alias in {"kaleidoquad", "kaleido4"}:
            overlay_effect = "kaleido_quad"
        elif overlay_effect_alias in {"kaleidoocta", "kaleido8"}:
            overlay_effect = "kaleido_octa"
        elif overlay_effect_alias == "strobe":
            overlay_effect = "strobe"
        elif overlay_effect_alias in {"none", ""}:
            overlay_effect = "none"
        else:
            legacy_rotate = request.POST.get("overlay_rotate") == "on"
            overlay_effect = "rotate_3d" if legacy_rotate else "none"
        parsed_filter_settings["overlay_effect"] = overlay_effect
        default_transition = normalize_transition_mode(
            request.POST.get("background_transition") or (composition.transition if composition else "none"),
            fallback=(composition.transition if composition else "none"),
        )
        background_transition = normalize_transition_mode(
            request.POST.get("background_transition") or layer_transitions.get("background") or default_transition,
            fallback=default_transition,
        )
        foreground_transition = normalize_transition_mode(
            request.POST.get("foreground_transition") or layer_transitions.get("foreground") or default_transition,
            fallback=default_transition,
        )
        overlay_transition = normalize_transition_mode(
            request.POST.get("overlay_transition") or layer_transitions.get("overlay") or "none",
            fallback="none",
        )
        parsed_filter_settings["layer_transitions"] = {
            "background": background_transition,
            "foreground": foreground_transition,
            "overlay": overlay_transition,
        }

        if composition_type == "associations":
            assoc_settings = parsed_filter_settings.get("associations") or {}
            if not isinstance(assoc_settings, dict):
                assoc_settings = {}
            seed_url = (request.POST.get("associations_seed_url") or "").strip()
            seed_source = (request.POST.get("associations_seed_source") or "").strip()
            tempo_sec = max(1.0, min(30.0, float(request.POST.get("associations_tempo_sec", 6) or 6)))
            inventiveness = max(0.0, min(1.0, float(request.POST.get("associations_inventiveness", 55) or 55) / 100.0))
            personal_influence = max(0.0, min(1.0, float(request.POST.get("associations_personal_influence", 30) or 30) / 100.0))
            web_enabled = (request.POST.get("associations_use_web") or "on").lower() in {"on", "1", "true", "yes"}
            seed_upload = request.FILES.get("associations_seed_upload")
            if seed_upload:
                ext = os.path.splitext(seed_upload.name or "")[1].lower() or ".jpg"
                seed_name = f"{get_random_string(12)}{ext}"
                saved_seed = default_storage.save(os.path.join("associations_seed", seed_name), ContentFile(seed_upload.read()))
                seed_url = default_storage.url(saved_seed)
            elif seed_source and not seed_url:
                source_assets = collect_source_assets([seed_source], landscape_only=False)
                if source_assets:
                    seed_url = str((source_assets[0] or {}).get("url") or "").strip()
            assoc_settings.update(
                {
                    "seed_url": seed_url,
                    "seed_source": seed_source,
                    "tempo_sec": tempo_sec,
                    "inventiveness": inventiveness,
                    "personal_influence": personal_influence,
                    "use_web": web_enabled,
                }
            )
            parsed_filter_settings["associations"] = assoc_settings

        audio_file = request.FILES.get("audio_file")
        selected_audio_source = (request.POST.get("audio_source_folder") or "").strip()
        selected_audio_track = (request.POST.get("audio_source_file") or "").strip()
        saved_audio_path = None
        if audio_file:
            ext = os.path.splitext(audio_file.name)[1]
            audio_name = f"{get_random_string(12)}{ext}"
            saved_audio_path = default_storage.save(os.path.join("audio", audio_name), ContentFile(audio_file.read()))
        elif selected_audio_source and selected_audio_track:
            source_dir = (LOCAL_AUDIO_SOURCES_ROOT / selected_audio_source).resolve()
            track_file = (source_dir / selected_audio_track).resolve()
            if source_dir.exists() and source_dir in track_file.parents and track_file.exists() and track_file.is_file():
                ext = track_file.suffix
                audio_name = f"{get_random_string(12)}{ext}"
                with open(track_file, "rb") as src:
                    saved_audio_path = default_storage.save(os.path.join("audio", audio_name), ContentFile(src.read()))

        create_kwargs = {
            "name": requested_name or f"composition_{datetime.now().strftime('%Y%m%d%H%M%S')}_{get_random_string(6)}",
            "type": composition_type,
            "url": full_url,
            "page_url": page_link,
            "status": composition.status if composition else "pending",
            "brightness": int(request.POST.get("brightness", 50) or 50),
            "saturation": int(request.POST.get("saturation", 50) or 50),
            "opacity": int(request.POST.get("opacity", 100) or 100),
            "playback_speed": float(request.POST.get("playback_speed", 1.0) or 1.0),
            "source_playback_mode": (
                "chronological"
                if (request.POST.get("source_playback_mode") or "").strip().lower() == "chronological"
                else "random"
            ),
            "landscape_only": request.POST.get("landscape_only") == "on",
            "auto_link_delay_seconds": parse_positive_int(request.POST.get("auto_link_delay_seconds"), 0),
            "random_link_enabled": request.POST.get("random_link_enabled") == "on",
            "random_link_scope": random_link_scope if random_link_scope in {"all", "same_series"} else "all",
            "mood_rating": mood_rating,
            "series": selected_series,
            "filter_preset": (request.POST.get("filter_preset") or "none").strip(),
            "filter_intensity": max(0, min(100, parse_positive_int(request.POST.get("filter_intensity"), 40))),
            "filter_param_1": parse_float_in_range(request.POST.get("filter_param_1"), 0.5),
            "filter_param_2": parse_float_in_range(request.POST.get("filter_param_2"), 0.5),
            "filter_param_3": parse_float_in_range(request.POST.get("filter_param_3"), 0.5),
            "filter_settings": parsed_filter_settings,
            "transition": background_transition,
            "background_sources": background_sources,
            "foreground_sources": foreground_sources,
            "overlay_sources": overlay_sources,
            "overlay_landscape_only": request.POST.get("overlay_landscape_only") == "on",
            "overlay_speed": max(0.25, min(3.0, float(request.POST.get("overlay_speed", 1.0) or 1.0))),
            "overlay_scale": max(0.2, min(3.0, float(request.POST.get("overlay_scale", 1.0) or 1.0))),
            "overlay_opacity": max(0.0, min(1.0, float(request.POST.get("overlay_opacity", 1.0) or 1.0))),
            "color_layer_enabled": request.POST.get("color_layer_enabled") == "on",
            "color_layer_color": (
                request.POST.get("color_layer_color") or "#000000"
            ).strip()[:9] or "#000000",
            "color_layer_opacity": max(0.0, min(1.0, float(request.POST.get("color_layer_opacity", 0.3) or 0.3))),
            "color_layer_target": (
                (request.POST.get("color_layer_target") or "background").strip().lower()
                if (request.POST.get("color_layer_target") or "background").strip().lower() in {"background", "overlay"}
                else "background"
            ),
            # Text layers — up to 4 cards in the admin form, each named
            # text_layer_<field>_<index>. We accept blank rows silently and
            # only persist entries whose text field is non-empty.
            "text_layers": _parse_text_layers_from_post(request.POST),
            "overlay_rotate": request.POST.get("overlay_rotate") == "on",
            "overlay_fit": (
                (request.POST.get("overlay_fit") or "free").lower().strip()
                if (request.POST.get("overlay_fit") or "free").lower().strip() in {"free", "framed", "square", "portrait"}
                else "free"
            ),
            "overlay_frame_margin": max(
                0.0,
                min(0.45, float(request.POST.get("overlay_frame_margin", 12) or 12) / 100.0),
            ),
            "composition_hashtags": composition_hashtags,
            "composition_emotions": composition_emotions,
            "composition_themes": composition_themes,
            "composition_characters": composition_characters,
            "nft_enabled": request.POST.get("nft_enabled") == "on",
            "nft_mode": (
                (request.POST.get("nft_mode") or "live").strip().lower()
                if (request.POST.get("nft_mode") or "live").strip().lower() in {"live", "epoch", "frozen"}
                else "live"
            ),
            "nft_name": (request.POST.get("nft_name") or "").strip(),
            "nft_description": (request.POST.get("nft_description") or "").strip(),
            "nft_external_url": (request.POST.get("nft_external_url") or "").strip(),
            "grid_id": re.sub(r"[^A-Za-z0-9_\-]", "", (request.POST.get("grid_id") or "").strip())[:32],
            "grid_cell_index": max(0, min(9, parse_positive_int(request.POST.get("grid_cell_index"), 0))),
        }
        if not series_submitted:
            create_kwargs.pop("series", None)
        if composition_themes is None:
            create_kwargs.pop("composition_themes", None)
        if saved_audio_path:
            create_kwargs["audio_file"] = saved_audio_path

        if source_type == "upload":
            bg_upload = request.FILES.get("background_video")
            fg_upload = request.FILES.get("foreground_video")
            if not bg_upload or not fg_upload:
                messages.error(request, "Please upload both background and foreground videos.")
                return redirect_to_form()
            bg_name = default_storage.save(os.path.join("videos/backgrounds", bg_upload.name), ContentFile(bg_upload.read()))
            fg_name = default_storage.save(os.path.join("videos/foregrounds", fg_upload.name), ContentFile(fg_upload.read()))
            create_kwargs["background_video"] = bg_name
            create_kwargs["foreground_video"] = fg_name
        else:
            preview_name = build_preview_image_from_sources(background_sources or foreground_sources)
            if preview_name:
                create_kwargs["img"] = preview_name

        if composition:
            for field, value in create_kwargs.items():
                setattr(composition, field, value)
            composition.save()
            composition.allowed_personas.set(
                MonologuePersona.objects.filter(id__in=allowed_persona_ids)
            )
            _trigger_poster_generation(composition.id)
            messages.success(request, "Composition updated successfully.")
            return redirect_to_composition_view_with_page(preserve_page=True)
        else:
            new_comp = Composition.objects.create(**create_kwargs)
            new_comp.allowed_personas.set(
                MonologuePersona.objects.filter(id__in=allowed_persona_ids)
            )
            _trigger_poster_generation(new_comp.id)
            messages.success(request, "Composition created successfully.")
            return redirect_to_composition_view_with_page(preserve_page=False)

        if request.POST.get("type") == "classic":
            # Retrieve form data
            if request.POST.get("source_type") == "s3":
                selected_type = request.POST.get("type")  # classic, tunnel, right-to-left, left-to-right
                selected_background_bucket = request.POST.get("bg_bucket1")
                selected_foreground_bucket = request.POST.get("fg_bucket1")
                audio_file = request.FILES.get("audio_file")
                background_brightness = request.POST.get("background_brightness")
                background_saturation = request.POST.get("background_saturation")
                background_opacity = request.POST.get("background_opacity")
                background_transition = request.POST.get("background_transition")
                foreground_brightness = request.POST.get("foreground_brightness")
                foreground_saturation = request.POST.get("foreground_saturation")
                foreground_opacity = request.POST.get("foreground_opacity")
                foreground_transition = request.POST.get("foreground_transition")
                bg_bucket2 = request.POST.get("bg_bucket2")
                bg_bucket3 = request.POST.get("bg_bucket3")
                bg_bucket4 = request.POST.get("bg_bucket4")

                fg_bucket2 = request.POST.get("fg_bucket2")
                fg_bucket3 = request.POST.get("fg_bucket3")
                fg_bucket4 = request.POST.get("fg_bucket4")
                
                base_url = request.POST.get("base_url", "").rstrip("/")
                url_slug = request.POST.get("url_slug", "").lstrip("/")
                linkto = request.POST.get("linkto", "").lstrip("/")
                
                # 🔁 Validate slug manually (don't auto-generate)
                if url_slug and Composition.objects.filter(slug=url_slug).exists():
                    messages.error(request, f"Error: The slug '{url_slug}' already exists. Please choose a different one.")
                    return redirect("composition-add")
                
                # ✅ Construct full URL and slug
                full_url = f"/{url_slug}" if url_slug else None
                slug = url_slug or ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
                
                downloaded_background_files = []

                # ✅ Download Background Images
                if selected_background_bucket:
                    downloaded_background_files = download_s3_files(selected_background_bucket, TEMP_BG_FOLDER)
                    print(f"✅ Downloaded Background Files: {downloaded_background_files}")

                # ✅ Save Audio File
                audio_path = None
                if audio_file:
                    audio_path = os.path.join(AUDIO_DIR, audio_file.name)
                    with open(audio_path, "wb") as f:
                        f.write(audio_file.read())
                
                def generate_auto_name():
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
                    return f"composition_{timestamp}_{random_str}"

                auto_name = generate_auto_name()
                output_path = f"{VIDEO_DIR}/{auto_name}.mp4"
                video_filename = f"{auto_name}.mp4"
                audio_filename = f"{audio_file}"
                
                # ✅ Generate Thumbnail from first background image (safe)
                thumbnail_path = os.path.join(THUMBNAIL_DIR, f"thumbnail_{auto_name}.jpg")
                first_bg_image = downloaded_background_files[0] if downloaded_background_files else None

                if first_bg_image:
                    try:
                        os.makedirs(os.path.dirname(thumbnail_path), exist_ok=True)
                        ext = os.path.splitext(first_bg_image)[1].lower()
                        from PIL import Image

                        if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                            img = Image.open(first_bg_image).convert("RGB")
                            img.save(thumbnail_path, "JPEG")
                            print(f"✅ Thumbnail (image) saved at: {thumbnail_path}")

                        elif ext == ".gif":
                            img = Image.open(first_bg_image)
                            img.seek(0)  # Use the first frame of the GIF
                            img.convert("RGB").save(thumbnail_path, "JPEG")
                            print(f"✅ Thumbnail (GIF) saved at: {thumbnail_path}")

                        elif ext == ".mp4":
                            with VideoFileClip(first_bg_image) as clip:
                                frame = clip.get_frame(0)  # Get the first frame
                                img = Image.fromarray(frame).convert("RGB")
                                img.save(thumbnail_path, "JPEG")
                                print(f"✅ Thumbnail (video) saved at: {thumbnail_path}")

                        else:
                            print(f"❌ Unsupported format for thumbnail: {ext}")
                            thumbnail_path = None

                    except Exception as e:
                        print(f"❌ Error saving thumbnail: {e}")
                        thumbnail_path = None
                comps = Composition.objects.create(
                    name=auto_name,
                    type=selected_type,
                    background_video="null",
                    foreground_video="null",
                    audio_file=audio_path,
                    background_brightness=background_brightness,
                    background_saturation=background_saturation,
                    background_opacity=background_opacity,
                    background_transition=background_transition,
                    foreground_brightness=foreground_brightness,
                    foreground_opacity=foreground_opacity,
                    foreground_saturation=foreground_saturation,
                    foreground_transition=foreground_transition,
                    img=thumbnail_path,  # you may want to upload this too
                    url=full_url,
                    page_url=linkto,
                    slug=slug,  # 🔥 Added
                    bg_bucket1=selected_background_bucket,
                    bg_bucket2=bg_bucket2,
                    bg_bucket3=bg_bucket3,
                    bg_bucket4=bg_bucket4,

                    fg_bucket1=selected_foreground_bucket,
                    fg_bucket2=fg_bucket2,
                    fg_bucket3=fg_bucket3,
                    fg_bucket4=fg_bucket4,
                    status="uncompleted"
                )

                comID = comps.id  # ✅ Ensure `comID` is an integer
             
                params_dict = {
                    "selected_type": str(selected_type),
                    "selected_background_bucket": str(selected_background_bucket),
                    "selected_foreground_bucket": str(selected_foreground_bucket),
                    "audio_file_path": str(audio_path),
                    "ids": int(comID)  # Ensure it's an integer
                }
                
                

                    # ✅ Correct way to pass the dictionary
                    #classic_task.delay(**params_dict)
            
                #classic_task.delay(selected_background_bucket, selected_foreground_bucket, saved_path);

                messages.success(request, "🎉 Composition added successfully!")
                return redirect("composition-view")
            
            #Uploads
            else:
               selected_type = request.POST.get("upload")
            # Retrieve files from request
            stype = request.POST.get("type")
            background_video = request.FILES.get("background_video")
            foreground_video = request.FILES.get("foreground_video")
            audio_file = request.FILES.get("audio_file")
            background_brightness = request.POST.get("background_brightness")
            background_saturation = request.POST.get("background_saturation")
            background_opacity = request.POST.get("background_opacity")
            background_transition = request.POST.get("background_transition")
            foreground_brightness = request.POST.get("foreground_brightness")
            foreground_saturation = request.POST.get("foreground_saturation")
            foreground_opacity = request.POST.get("foreground_opacity")
            foreground_transition = request.POST.get("foreground_transition")
            base_url = request.POST.get("base_url", "").rstrip("/")
            url_slug = request.POST.get("url_slug", "").lstrip("/")
            linkto = request.POST.get("linkto", "").lstrip("/")

            # 🔁 Validate slug manually (don't auto-generate)
            if url_slug and Composition.objects.filter(slug=url_slug).exists():
                messages.error(request, f"Error: The slug '{url_slug}' already exists. Please choose a different one.")
                return redirect("composition-add")

            # ✅ Construct full URL and slug
            full_url = f"/{url_slug}" if url_slug else None
            slug = url_slug or ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

            # Debugging print
            print(f"Background Video: {background_video}, Foreground Video: {foreground_video}, Audio : {audio_file}")

            # Validate required files
            if not background_video or not foreground_video:
                messages.error(request, "Error: Missing background or foreground video.")
                return redirect("composition-add")

            if not audio_file:
                messages.error(request, "Error: No audio file uploaded.")
                return redirect("composition-add")

            # Generate unique filename
            def generate_auto_name():
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
                return f"composition_{timestamp}_{random_str}"

            auto_name = generate_auto_name()

            # Save files to disk
            background_path = os.path.join(VIDEO_DIR, background_video.name)
            #print("Video Bc" , background_path)
            foreground_path = os.path.join(VIDEO_DIR, foreground_video.name)
            #print("Video Fc" , foreground_path)
            audio_path = os.path.join(AUDIO_DIR, audio_file.name)

            with open(background_path, "wb") as f:
                f.write(background_video.read())

            with open(foreground_path, "wb") as f:
                f.write(foreground_video.read())

            with open(audio_path, "wb") as f:
                f.write(audio_file.read())

            # Define Output Path
            output_path = f"{VIDEO_DIR}/{auto_name}.mp4"
            video_filename = f"{auto_name}.mp4"
            audio_filename = f"{audio_file}"

            # ✅ Pass file paths (strings) instead of TemporaryUploadedFile objects
            combine_video_with_audio(background_path, foreground_path, audio_path, output_path)

            # Generate Thumbnail
            thumbnail_path = os.path.join(THUMBNAIL_DIR, f"thumbnail_{auto_name}.jpg")
            generate_video_thumbnail(output_path, thumbnail_path)

            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                    messages.error(request, f"Error: Final video not found or empty at {output_path}")
                    return redirect("composition-add")

            # Save Composition in Database
            Composition.objects.create(
                name=auto_name,
                type=stype,
                background_video="null",
                foreground_video="null",
                audio_file=audio_path,
                final_video=video_filename,
                img=thumbnail_path,
                background_brightness=background_brightness,
                background_saturation=background_saturation,
                background_opacity=background_opacity,
                background_transition=background_transition,
                foreground_brightness=foreground_brightness,
                foreground_opacity=foreground_opacity,
                foreground_saturation=foreground_saturation,
                foreground_transition=foreground_transition,
                url=full_url,
                page_url=linkto,
                slug=slug,  # 🔥 Added
                status="Completed"
            )

            messages.success(request, "🎉 Composition added successfully!")
            return redirect("composition-view")

                
            
        #Tunnel  Buckets
        elif request.POST.get("type") == "tunnel":
            
            selected_background_buckets = request.POST.getlist("bg_bucket1")
            background_brightness = request.POST.get("background_brightness")
            background_saturation = request.POST.get("background_saturation")
            background_opacity = request.POST.get("background_opacity")
            background_transition = request.POST.get("background_transition")
            audio_file = request.FILES.get("audio_file")
            bg_bucket1 = request.POST.get("bg_bucket1")
            bg_bucket2 = request.POST.get("bg_bucket2")
            bg_bucket3 = request.POST.get("bg_bucket3")
            bg_bucket4 = request.POST.get("bg_bucket4")

            base_url = request.POST.get("base_url", "").rstrip("/")
            url_slug = request.POST.get("url_slug", "").lstrip("/")
            linkto = request.POST.get("linkto", "").lstrip("/")

            # 🔁 Validate slug manually (don't auto-generate)
            if url_slug and Composition.objects.filter(slug=url_slug).exists():
                messages.error(request, f"Error: The slug '{url_slug}' already exists. Please choose a different one.")
                return redirect("composition-add")

            # ✅ Construct full URL and slug
            full_url = f"/{url_slug}" if url_slug else None
            slug = url_slug or ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
            
            downloaded_background_files = []

            # ✅ Download Background Images
            if selected_background_buckets:
                for bucket_name in selected_background_buckets:
                    if bucket_name:
                        files = download_s3_files(bucket_name, TEMP_BG_FOLDER)
                        downloaded_background_files.extend(files)
                print(f"✅ Downloaded Background Files: {downloaded_background_files}")

           
            if not selected_background_buckets:
                print("❌ No S3 bucket selected.")
                return

            
            # ✅ Generate unique name for video
            def generate_auto_name():
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")  # Get current timestamp
                    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=6))  # Random 6-character string
                    return f"composition_{timestamp}_{random_str}"

            auto_name = generate_auto_name()
            
            
            # ✅ Create Video from Merged Images
            output_path = os.path.join(VIDEO_DIR, f"{auto_name}.mp4")
            video_filename = f"{auto_name}.mp4"
            
            # ✅ Save Audio File
            audio_path = None
            if audio_file:
                audio_path = os.path.join(AUDIO_DIR, audio_file.name)
                with open(audio_path, "wb") as f:
                    f.write(audio_file.read())
                    
            # ✅ Generate Thumbnail from first background image (safe)
            thumbnail_path = os.path.join(THUMBNAIL_DIR, f"thumbnail_{auto_name}.jpg")
            first_bg_image = downloaded_background_files[0] if downloaded_background_files else None

            if first_bg_image:
                try:
                    os.makedirs(os.path.dirname(thumbnail_path), exist_ok=True)
                    ext = os.path.splitext(first_bg_image)[1].lower()
                    from PIL import Image

                    if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                        img = Image.open(first_bg_image).convert("RGB")
                        img.save(thumbnail_path, "JPEG")
                        print(f"✅ Thumbnail (image) saved at: {thumbnail_path}")

                    elif ext == ".gif":
                        img = Image.open(first_bg_image)
                        img.seek(0)  # Use the first frame of the GIF
                        img.convert("RGB").save(thumbnail_path, "JPEG")
                        print(f"✅ Thumbnail (GIF) saved at: {thumbnail_path}")

                    elif ext == ".mp4":
                        with VideoFileClip(first_bg_image) as clip:
                            frame = clip.get_frame(0)  # Get the first frame
                            img = Image.fromarray(frame).convert("RGB")
                            img.save(thumbnail_path, "JPEG")
                            print(f"✅ Thumbnail (video) saved at: {thumbnail_path}")

                    else:
                        print(f"❌ Unsupported format for thumbnail: {ext}")
                        thumbnail_path = None

                except Exception as e:
                    print(f"❌ Error saving thumbnail: {e}")
                    thumbnail_path = None
            
            # ✅ Save Audio File Securely
            saved_path = None
            if audio_file:
                # Generate a secure, unique filename
                filename = get_random_string(12) + os.path.splitext(audio_file.name)[1]
                file_path = os.path.join("uploads", filename)  # Relative path inside MEDIA_ROOT
                
                # Save the file securely
                saved_path = default_storage.save(file_path, ContentFile(audio_file.read()))
           
            # ✅ Save Composition in Database
            comps = Composition.objects.create(
                        name=auto_name,
                        type="Tunnel",
                        background_video="null",
                        foreground_video="null",
                        background_brightness=background_brightness,
                        background_saturation=background_saturation,
                        background_opacity=background_opacity,
                        background_transition=background_transition,
                        img=thumbnail_path,
                        url=full_url,
                        audio_file=audio_path,
                        slug=slug,  # 🔥 Added
                        bg_bucket1=bg_bucket1,
                        bg_bucket2=bg_bucket2,
                        bg_bucket3=bg_bucket3,
                        bg_bucket4=bg_bucket4,
                        status="uncompleted",
                        page_url=linkto,
                    )

            comID = comps.id  # ✅ Ensure `comID` is an integer
            params_tunnel = {
                "selected_background_buckets": selected_background_buckets, 
                "audio_file_path": str(saved_path),
                "ids": int(comID)
            }
            # ✅ Correct way to pass the dictionary
            #tunnel_task.delay(params_tunnel)
            
            print("🎉 Tunnel composition added successfully!")
            return redirect("composition-view")
            
            # LEFT  TO RIGHT
        elif request.POST.get("type") == "left-to-right":
            bg_bucket = request.POST.get("bg_bucket1")
            fg_bucket = request.POST.get("fg_bucket1")
            audio_file = request.FILES.get("audio_file")
            background_brightness = request.POST.get("background_brightness")
            background_saturation = request.POST.get("background_saturation")
            background_opacity = request.POST.get("background_opacity")
            background_transition = request.POST.get("background_transition")
            foreground_brightness = request.POST.get("foreground_brightness")
            foreground_saturation = request.POST.get("foreground_saturation")
            foreground_opacity = request.POST.get("foreground_opacity")
            foreground_transition = request.POST.get("foreground_transition")
            bg_bucket2 = request.POST.get("bg_bucket2")
            bg_bucket3 = request.POST.get("bg_bucket3")
            bg_bucket4 = request.POST.get("bg_bucket4")

            fg_bucket2 = request.POST.get("fg_bucket2")
            fg_bucket3 = request.POST.get("fg_bucket3")
            fg_bucket4 = request.POST.get("fg_bucket4")
            base_url = request.POST.get("base_url", "").rstrip("/")
            url_slug = request.POST.get("url_slug", "").lstrip("/")
            linkto = request.POST.get("linkto", "").lstrip("/")
 

            # 🔁 Validate slug manually (don't auto-generate)
            if url_slug and Composition.objects.filter(slug=url_slug).exists():
                messages.error(request, f"Error: The slug '{url_slug}' already exists. Please choose a different one.")
                return redirect("composition-add")

            # ✅ Construct full URL and slug
            full_url = f"/{url_slug}" if url_slug else None
            slug = url_slug or ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
            
            downloaded_background_files = []
            downloaded_foreground_files = []

            # ✅ Download Background Images
            if bg_bucket:
                downloaded_background_files = download_s3_files(bg_bucket, TEMP_BG_FOLDER)
                print(f"✅ Downloaded Background Files: {downloaded_background_files}")

            
            if not bg_bucket or not fg_bucket:
                messages.error(request, "❌ Please select background and foreground sources.")
                return redirect("composition-add")

            # Generate unique name
            def generate_auto_name():
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
                return f"composition_{timestamp}_{random_str}"

            auto_name = generate_auto_name()
            output_path = os.path.join(VIDEO_DIR, f"{auto_name}.mp4")
            audio_filename = f"{audio_file}"
           
            # ✅ Generate Thumbnail from first background image (safe)
            thumbnail_path = os.path.join(THUMBNAIL_DIR, f"thumbnail_{auto_name}.jpg")
            first_bg_image = downloaded_background_files[0] if downloaded_background_files else None

            if first_bg_image:
                try:
                    os.makedirs(os.path.dirname(thumbnail_path), exist_ok=True)
                    ext = os.path.splitext(first_bg_image)[1].lower()
                    from PIL import Image

                    if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                        img = Image.open(first_bg_image).convert("RGB")
                        img.save(thumbnail_path, "JPEG")
                        print(f"✅ Thumbnail (image) saved at: {thumbnail_path}")

                    elif ext == ".gif":
                        img = Image.open(first_bg_image)
                        img.seek(0)  # Use the first frame of the GIF
                        img.convert("RGB").save(thumbnail_path, "JPEG")
                        print(f"✅ Thumbnail (GIF) saved at: {thumbnail_path}")

                    elif ext == ".mp4":
                        with VideoFileClip(first_bg_image) as clip:
                            frame = clip.get_frame(0)  # Get the first frame
                            img = Image.fromarray(frame).convert("RGB")
                            img.save(thumbnail_path, "JPEG")
                            print(f"✅ Thumbnail (video) saved at: {thumbnail_path}")

                    else:
                        print(f"❌ Unsupported format for thumbnail: {ext}")
                        thumbnail_path = None

                except Exception as e:
                    print(f"❌ Error saving thumbnail: {e}")
                    thumbnail_path = None
                    
            # ✅ Save Audio File
            audio_path = None
            if audio_file:
                audio_path = os.path.join(AUDIO_DIR, audio_file.name)
                with open(audio_path, "wb") as f:
                    f.write(audio_file.read())
                    
            # Create DB entry (video paths will be updated later)
            comps = Composition.objects.create(
                name=auto_name,
                type="left-to-right",
                
                background_video="null",
                foreground_video="null",
                background_brightness=background_brightness,
                background_saturation=background_saturation,
                background_opacity=background_opacity,
                background_transition=background_transition,
                foreground_brightness=foreground_brightness,
                foreground_opacity=foreground_opacity,
                foreground_saturation=foreground_saturation,
                foreground_transition=foreground_transition,
                img=thumbnail_path,
                url=full_url,
                page_url=linkto,
                audio_file=audio_path,
                slug=slug,  # 🔥 Added
                bg_bucket1=bg_bucket,
                bg_bucket2=bg_bucket2,
                bg_bucket3=bg_bucket3,
                bg_bucket4=bg_bucket4, 

                fg_bucket1=fg_bucket,
                fg_bucket2=fg_bucket2,
                fg_bucket3=fg_bucket3,
                fg_bucket4=fg_bucket4,
                status="uncompleted"
            )

            params_left = {
                "background_bucket": bg_bucket,
                "foreground_bucket": fg_bucket,
                "audio_file_path": str(audio_path),
                "ids": comps.id
            }

            # Call Celery Task
            #left_to_right_task.delay(params_left)

            messages.success(request, "🎉 Left to Right Composition added successfully!")
            return redirect("composition-view")
            
        else:
            print("test")
            #return False
          
    return render(request, "admin/composition.html", {
        "buckets": matching_buckets,
        "audio_source_map": collect_audio_source_files(),
        "series_list": Series.objects.all(),
        "persona_list": MonologuePersona.objects.order_by("title"),
        "composition": composition,
        "edit_mode": bool(composition),
        "selected_background_sources": composition.background_sources if composition else [],
        "selected_foreground_sources": composition.foreground_sources if composition else [],
        "selected_overlay_sources": composition.overlay_sources if composition else [],
        "selected_background_transition": (
            ((composition.filter_settings or {}).get("layer_transitions") or {}).get("background")
            if composition
            else "none"
        ) or ((composition.transition if composition else "none") or "none"),
        "selected_foreground_transition": (
            ((composition.filter_settings or {}).get("layer_transitions") or {}).get("foreground")
            if composition
            else "none"
        ) or ((composition.transition if composition else "none") or "none"),
        "selected_overlay_transition": (
            ((composition.filter_settings or {}).get("layer_transitions") or {}).get("overlay")
            if composition
            else "none"
        ) or "none",
        "selected_overlay_effect": (
            ((composition.filter_settings or {}).get("overlay_effect") or "")
            if composition
            else ""
        ) or ("rotate_3d" if (composition and composition.overlay_rotate) else "none"),
        "selected_allowed_persona_ids": [
            int(pid) for pid in (composition.allowed_personas.values_list("id", flat=True) if composition else [])
        ],
        "current_hashtags": ", ".join((composition.composition_hashtags or [])) if composition else "",
        "current_emotions": ", ".join((composition.composition_emotions or [])) if composition else "",
        "current_themes": ", ".join((composition.composition_themes or [])) if composition else "",
        "current_characters": ", ".join((composition.composition_characters or [])) if composition else "",
        "current_url_slug": composition.url.rstrip("/").split("/")[-1] if composition and composition.url else "",
        "current_page_link": composition.page_url if composition and composition.page_url else "",
        "release_history": list(composition.releases.all()[:15]) if composition else [],
        "r2_default_bucket": (os.getenv("R2_PROD_BUCKET") or "edfornieles-composition-prod").strip(),
        "nft_metadata_url": (
            reverse("composition_nft_metadata", kwargs={"composition_id": int(composition.id)})
            if composition
            else ""
        ),
        "nft_media": _composition_media_context(request, composition) if composition else None,
        "nft_versions": list(composition.nft_versions.order_by("-version_number")[:10]) if composition else [],
        "active_voucher": composition.mint_vouchers.filter(redeemed=False).order_by("-created_at").first() if composition else None,
        "return_page": return_page,
        "return_folder": return_folder,
    })


@login_required

def composition_publish_diff(request, composition_id):
    if request.method != "GET":
        return JsonResponse({"ok": False, "error": "GET required."}, status=405)
    composition = get_object_or_404(Composition, id=composition_id)
    bucket = ((request.GET.get("bucket") or "").strip() or (os.getenv("R2_PROD_BUCKET") or "edfornieles-composition-prod").strip())
    prefix = _sources_root_prefix(request.GET.get("prefix"))
    try:
        manifest = _local_manifest_for_composition(composition)
        client = _r2_client()
        remote_keys = _remote_keys_for_manifest_sources(client, bucket, manifest, prefix)
        local_keys = _manifest_local_and_remote_keys(manifest, prefix)
        uploads = sorted(local_keys - remote_keys)
        keep = sorted(local_keys & remote_keys)
        deletes = sorted(remote_keys - local_keys)
        return JsonResponse(
            {
                "ok": True,
                "bucket": bucket,
                "prefix": prefix,
                "local_count": len(local_keys),
                "remote_count": len(remote_keys),
                "to_upload_count": len(uploads),
                "to_delete_count": len(deletes),
                "unchanged_count": len(keep),
                "to_upload_preview": uploads[:25],
                "to_delete_preview": deletes[:25],
            }
        )
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)


@login_required

def composition_publish_release(request, composition_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)
    composition = get_object_or_404(Composition, id=composition_id)
    mode = ((request.POST.get("mode") or "update").strip().lower())
    if mode not in {"update", "prune"}:
        mode = "update"
    bucket = ((request.POST.get("bucket") or "").strip() or (os.getenv("R2_PROD_BUCKET") or "edfornieles-composition-prod").strip())
    prefix = _sources_root_prefix(request.POST.get("prefix"))
    note = (request.POST.get("release_note") or "").strip()
    if not bucket:
        return JsonResponse({"ok": False, "error": "Missing bucket."}, status=400)
    try:
        manifest = _local_manifest_for_composition(composition)
        client = _r2_client()
        base_prefix = f"{prefix}/" if prefix else ""
        remote_keys = _remote_keys_for_manifest_sources(client, bucket, manifest, prefix)
        local_keys = _manifest_local_and_remote_keys(manifest, prefix)
        upload_count = 0
        for row in manifest:
            key = f"{base_prefix}{row['relative_path']}"
            with open(row["absolute_path"], "rb") as fh:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=fh,
                    ContentType=row.get("mime_type") or "application/octet-stream",
                )
            upload_count += 1

        deleted_count = 0
        if mode == "prune":
            for key in sorted(remote_keys - local_keys):
                client.delete_object(Bucket=bucket, Key=key)
                deleted_count += 1

        latest_release = composition.releases.order_by("-version_number").first()
        next_version = 1 if not latest_release else int(latest_release.version_number) + 1
        manifest_hash = hashlib.sha256(
            json.dumps(
                [{"relative_path": r["relative_path"], "sha256": r["sha256"], "file_size": r["file_size"]} for r in manifest],
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        release = CompositionRelease.objects.create(
            composition=composition,
            version_number=next_version,
            status="published",
            publish_mode=mode,
            release_note=note,
            r2_bucket=bucket,
            r2_prefix=prefix,
            manifest=[{"relative_path": r["relative_path"], "sha256": r["sha256"], "file_size": r["file_size"], "mime_type": r["mime_type"]} for r in manifest],
            manifest_hash=manifest_hash,
            uploaded_count=upload_count,
            deleted_count=deleted_count,
            metadata_snapshot={
                "type": composition.type,
                "hashtags": composition.composition_hashtags or [],
                "scenes": composition.composition_emotions or [],
                "emotions": composition.composition_emotions or [],
                "themes": composition.composition_themes or [],
                "characters": composition.composition_characters or [],
                "nft_name": composition.nft_name or composition.name,
                "nft_description": composition.nft_description or "",
            },
        )
        CompositionReleaseFile.objects.bulk_create(
            [
                CompositionReleaseFile(
                    release=release,
                    relative_path=r["relative_path"],
                    file_size=int(r["file_size"]),
                    sha256=r["sha256"],
                    mime_type=r["mime_type"] or "",
                    included=True,
                )
                for r in manifest
            ],
            ignore_conflicts=True,
        )
        return JsonResponse(
            {
                "ok": True,
                "release_id": int(release.id),
                "version": int(release.version_number),
                "uploaded_count": upload_count,
                "deleted_count": deleted_count,
                "bucket": bucket,
                "prefix": prefix,
            }
        )
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)


@login_required

def composition_release_rollback(request, composition_id, release_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)
    composition = get_object_or_404(Composition, id=composition_id)
    release = get_object_or_404(CompositionRelease, id=release_id, composition=composition)
    snapshot = release.metadata_snapshot or {}
    if snapshot:
        composition.type = snapshot.get("type") or composition.type
        composition.composition_hashtags = snapshot.get("hashtags") or composition.composition_hashtags
        composition.composition_emotions = (
            snapshot.get("scenes")
            or snapshot.get("emotions")
            or composition.composition_emotions
        )
        composition.composition_themes = snapshot.get("themes") or composition.composition_themes
        composition.composition_characters = snapshot.get("characters") or composition.composition_characters
        composition.nft_name = snapshot.get("nft_name") or composition.nft_name
        composition.nft_description = snapshot.get("nft_description") or composition.nft_description
        composition.save()
    return JsonResponse({"ok": True, "message": f"Rolled metadata back to release v{release.version_number}."})


@login_required

def composition_file_inventory(request, composition_id):
    if request.method != "GET":
        return JsonResponse({"ok": False, "error": "GET required."}, status=405)
    composition = get_object_or_404(Composition, id=composition_id)
    excluded = {str(p).strip() for p in (composition.publish_excluded_files or []) if str(p).strip()}
    rows = []
    for source_name in _composition_source_names(composition):
        source_dir = (LOCAL_SOURCES_ROOT / source_name).resolve()
        if not source_dir.exists() or not source_dir.is_dir():
            continue
        for media_path in _local_source_dir_media_files(source_dir):
            rel_in_source = media_path.resolve().relative_to(source_dir).as_posix()
            rel_path = f"{source_name}/{rel_in_source}"
            size = 0
            try:
                size = int(media_path.stat().st_size)
            except OSError:
                size = 0
            transforms = (composition.source_image_transforms or {}).get(rel_path) or {}
            rows.append(
                {
                    "relative_path": rel_path,
                    "source_name": source_name,
                    "name": media_path.name,
                    "size": size,
                    "included": rel_path not in excluded,
                    "flipH": bool(transforms.get("flipH")),
                    "flipV": bool(transforms.get("flipV")),
                    "crop": transforms.get("crop") or None,
                }
            )
    rows.sort(key=lambda row: row["relative_path"])
    return JsonResponse({"ok": True, "files": rows, "count": len(rows)})


@login_required

def composition_file_action(request, composition_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)
    composition = get_object_or_404(Composition, id=composition_id)
    action = (request.POST.get("action") or "").strip().lower()
    relative_path = (request.POST.get("relative_path") or "").strip().lstrip("/")
    if not relative_path:
        return JsonResponse({"ok": False, "error": "relative_path is required."}, status=400)

    excluded = {str(p).strip() for p in (composition.publish_excluded_files or []) if str(p).strip()}

    if action == "toggle_include":
        include = (request.POST.get("include") or "").strip().lower() in {"1", "true", "yes", "on"}
        if include:
            excluded.discard(relative_path)
        else:
            excluded.add(relative_path)
        composition.publish_excluded_files = sorted(excluded)
        composition.save(update_fields=["publish_excluded_files"])
        return JsonResponse({"ok": True, "included": include})

    if action in ("set_flip", "set_crop", "clear_transform"):
        transforms = dict(composition.source_image_transforms or {})
        entry = dict(transforms.get(relative_path) or {})
        if action == "set_flip":
            flip_h = (request.POST.get("flipH") or "").strip().lower() in {"1", "true", "yes", "on"}
            flip_v = (request.POST.get("flipV") or "").strip().lower() in {"1", "true", "yes", "on"}
            entry["flipH"] = flip_h
            entry["flipV"] = flip_v
        elif action == "set_crop":
            try:
                x = int(request.POST.get("x") or 0)
                y = int(request.POST.get("y") or 0)
                w = int(request.POST.get("w") or 0)
                h = int(request.POST.get("h") or 0)
                if w <= 0 or h <= 0:
                    return JsonResponse({"ok": False, "error": "Crop width and height must be positive."}, status=400)
                entry["crop"] = [x, y, x + w, y + h]
            except (ValueError, TypeError):
                return JsonResponse({"ok": False, "error": "Invalid crop values."}, status=400)
        elif action == "clear_transform":
            what = (request.POST.get("what") or "all").strip().lower()
            if what == "flip":
                entry.pop("flipH", None)
                entry.pop("flipV", None)
            elif what == "crop":
                entry.pop("crop", None)
            else:
                entry = {}
        if entry:
            transforms[relative_path] = entry
        else:
            transforms.pop(relative_path, None)
        composition.source_image_transforms = transforms
        composition.save(update_fields=["source_image_transforms"])
        return JsonResponse({"ok": True, "transforms": entry})

    resolved = _resolve_composition_relative_media_path(relative_path)
    if not resolved:
        return JsonResponse({"ok": False, "error": "File not found in local source roots."}, status=404)
    source_name, source_dir, target = resolved

    if action == "rename":
        new_name = (request.POST.get("new_name") or "").strip()
        if not new_name:
            return JsonResponse({"ok": False, "error": "new_name is required for rename."}, status=400)
        if "/" in new_name or "\\" in new_name:
            return JsonResponse({"ok": False, "error": "new_name must be a simple filename."}, status=400)
        if not _local_source_filename_allowed(new_name):
            return JsonResponse({"ok": False, "error": "new_name has unsupported extension."}, status=400)
        new_path = target.with_name(new_name).resolve()
        if source_dir not in new_path.parents:
            return JsonResponse({"ok": False, "error": "Invalid rename target."}, status=400)
        if new_path.exists():
            return JsonResponse({"ok": False, "error": "Target file already exists."}, status=400)
        target.rename(new_path)
        old_token = relative_path
        new_rel = new_path.relative_to(source_dir).as_posix()
        new_token = f"{source_name}/{new_rel}"
        if old_token in excluded:
            excluded.discard(old_token)
            excluded.add(new_token)
            composition.publish_excluded_files = sorted(excluded)
            composition.save(update_fields=["publish_excluded_files"])
        return JsonResponse({"ok": True, "relative_path": new_token})

    if action == "delete":
        stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        trash_dir = (LOCAL_SOURCE_TRASH_ROOT / source_name / stamp).resolve()
        trash_dir.mkdir(parents=True, exist_ok=True)
        dest_path = (trash_dir / target.name).resolve()
        if trash_dir not in dest_path.parents:
            return JsonResponse({"ok": False, "error": "Invalid trash destination."}, status=400)
        target.rename(dest_path)
        excluded.discard(relative_path)
        composition.publish_excluded_files = sorted(excluded)
        composition.save(update_fields=["publish_excluded_files"])
        return JsonResponse({"ok": True, "trashed_to": str(dest_path)})

    return JsonResponse({"ok": False, "error": "Unsupported action."}, status=400)



def _composition_list_thumbnail_src(comp: Composition) -> str:
    """
    URL for the composition list thumbnail. Prefer the generated poster asset
    (CompositionMediaAsset kind='poster') when available; fall back to the saved
    ImageField, then to the dynamic preview endpoint.
    """
    try:
        for asset in comp.media_assets.all():
            if asset.kind == "poster" and asset.status == "ready" and asset.file:
                name = str(asset.file.name or "").strip()
                if name and default_storage.exists(name):
                    return default_storage.url(name)
    except Exception:
        pass
    try:
        if comp.img:
            name = comp.img.name
            if name and default_storage.exists(name):
                return comp.img.url
    except Exception:
        pass
    return f"/composition-preview/{comp.id}/"



def composition_view(request):
    search_query = (request.GET.get("q") or "").strip()
    # Default browse view shows only folder roots — generated grid children
    # (cell_index 2..9) live inside their folder page, not in this list.
    # BUT when the user has typed a search query, drop the cell_index filter
    # so they also see grid children that match (otherwise orphaned grids —
    # cells 2-9 whose folder root was deleted — become invisible from
    # library search, which is what bit us with `bla_bla_bling`).
    compositions_list = Composition.objects.prefetch_related("media_assets")
    if not search_query:
        compositions_list = compositions_list.filter(grid_cell_index__lte=1)
    if (request.GET.get("ready") or "").strip() == "1":
        compositions_list = compositions_list.filter(ready_for_deployment=True)
    if search_query:
        compositions_list = compositions_list.filter(
            Q(name__icontains=search_query)
            | Q(type__icontains=search_query)
            | Q(url__icontains=search_query)
            | Q(page_url__icontains=search_query)
        )
    compositions_list = compositions_list.order_by("-id")
    paginator = Paginator(compositions_list, 50)
    page_number = request.GET.get("page")
    compositions = paginator.get_page(page_number)
    for comp in compositions:
        comp.preview_url = f"/composition-preview/{comp.id}/"
        comp.list_thumbnail_src = _composition_list_thumbnail_src(comp)
        comp.preview_video_url = None
        comp.nft_online_video_ready = False
        comp.media_status = {"poster": "none", "clip": "none"}
        for asset in comp.media_assets.all():
            if asset.kind == "poster":
                comp.media_status["poster"] = asset.status
            if asset.kind == "preview_10s":
                comp.media_status["clip"] = asset.status
                if asset.status == "ready" and asset.file:
                    try:
                        comp.preview_video_url = default_storage.url(asset.file.name)
                    except Exception:
                        pass
                    comp.nft_online_video_ready = True
    return render(
        request,
        "admin/composition-view.html",
        {
            "compositions": compositions,
            "series_list": Series.objects.all().order_by("name"),
            "search_query": search_query,
        },
    )


@require_POST

def composition_duplicate(request, composition_id):
    source = get_object_or_404(Composition, id=composition_id)
    return_page = (request.POST.get("return_page") or request.GET.get("return_page") or "").strip()
    if not return_page.isdigit():
        return_page = ""
    source_name = (source.name or "composition").strip() or "composition"

    match = re.match(r"^(.*?)(?:\s+(\d+))?$", source_name)
    base_name = (match.group(1) if match else source_name).strip() or source_name
    start_suffix = int(match.group(2)) if (match and match.group(2)) else 1

    used_names = set(
        Composition.objects.filter(name__startswith=base_name).values_list("name", flat=True)
    )
    next_suffix = max(1, start_suffix)
    candidate_name = f"{base_name} {next_suffix}"
    while candidate_name in used_names:
        next_suffix += 1
        candidate_name = f"{base_name} {next_suffix}"

    clone = Composition.objects.get(id=source.id)
    clone.pk = None
    clone.id = None
    clone.name = candidate_name
    # Force editor flow to define a new public destination for the variation.
    clone.url = None
    clone.page_url = None
    clone.status = "pending"
    clone.created_at = timezone.now()
    clone.save()
    clone.allowed_personas.set(source.allowed_personas.all())

    messages.success(request, f'Duplicated "{source_name}" as "{clone.name}".')
    if return_page:
        return redirect(f"{reverse('composition-edit', kwargs={'composition_id': clone.id})}?return_page={return_page}")
    return redirect("composition-edit", composition_id=clone.id)


# Pool of comp types for "vary type" mode. Constrained to types that visually
# tile well in a 3x3 gallery and only need a background source pool.
_GRID_VARY_TYPE_POOL = (
    "classic", "psychedelic-classic", "tunnel", "psychedelic-tunnel",
    "tunnel-burst", "swirl", "kaleidoscope", "liquid-mirror", "wax-melt",
)


def composition_generate_grid(request, composition_id):
    """Clone the source composition into 9 cells linked by a shared grid_id.

    POST params:
      basis:    "overlay" → keep overlay fixed, rotate background per cell.
                "composition" → clone exactly, all 9 cells start identical
                                (user edits each later).
      vary_type: "1" → each cell picks a different comp type from the pool.
                 anything else → all cells keep the source's type.
    """
    if request.method != "POST":
        return redirect("composition-edit", composition_id=composition_id)
    source = get_object_or_404(Composition, id=composition_id)
    vary_type = (request.POST.get("vary_type") or "").strip() == "1"
    # Default: clone the source exactly into all 9 cells. The user edits each
    # cell individually inside the folder afterwards.
    basis = (request.POST.get("basis") or "composition").strip().lower()
    if basis not in {"overlay", "composition"}:
        basis = "composition"

    # Build the bg pool: prefer the source's selected bg sources, then top up
    # from local source folders so we have at least 9 distinct candidates.
    bg_pool: list[str] = [s for s in (source.background_sources or []) if s]
    seen = set(bg_pool)
    for path in get_local_sources():
        name = path.name
        if name not in seen:
            bg_pool.append(name)
            seen.add(name)
        if len(bg_pool) >= 24:
            break
    if not bg_pool:
        messages.error(request, "Cannot generate grid: no background sources available.")
        return redirect("composition-edit", composition_id=composition_id)

    # Each composition is its own folder (auto-assigned grid_id at save).
    # Generate Grid populates siblings into that folder, replacing any existing
    # cells 2..9 so re-running gives a clean regenerated grid.
    if not source.grid_id:
        source.grid_id = uuid.uuid4().hex[:10]
    if not source.grid_cell_index:
        source.grid_cell_index = 1
    source.save(update_fields=["grid_id", "grid_cell_index"])
    grid_id = source.grid_id
    base_name = (source.name or "composition").strip() or "composition"
    source_slug = ""
    if source.url:
        parts = [p for p in urlparse(source.url).path.split("/") if p]
        if parts:
            source_slug = parts[-1].lower()
    if not source_slug:
        source_slug = slugify(base_name) or f"comp{source.id}"

    # Fill missing cells only — never overwrite hand-edited siblings.
    occupied = set(
        Composition.objects.filter(grid_id=grid_id)
        .values_list("grid_cell_index", flat=True)
    )

    type_pool = list(_GRID_VARY_TYPE_POOL)
    random.shuffle(type_pool)

    for cell in range(2, 10):
        if cell in occupied:
            continue
        clone = Composition.objects.get(id=source.id)
        clone.pk = None
        clone.id = None
        clone.name = f"{base_name} · grid {grid_id} · cell {cell}"
        clone.url = f"/{source_slug}grid{grid_id}cell{cell}/"
        clone.page_url = None
        clone.status = "pending"
        clone.created_at = timezone.now()
        clone.grid_id = grid_id
        clone.grid_cell_index = cell
        if basis == "overlay":
            # Rotate bg pool so each cell starts with a different background.
            rotated = bg_pool[(cell - 1) :] + bg_pool[: (cell - 1)]
            clone.background_sources = rotated[:4]
        # basis=="composition": clone keeps source.background_sources verbatim.
        if vary_type:
            clone.type = type_pool[(cell - 1) % len(type_pool)]
        clone.save()
        clone.allowed_personas.set(source.allowed_personas.all())

    messages.success(
        request,
        f'Folder "{grid_id}" filled — {9 - len(occupied)} new cells added.',
    )
    return redirect("composition_folder_view", grid_id=grid_id)


def composition_folder_reset(request, composition_id):
    """Wipe cells 2..9 in this folder and re-clone the source into all of them.

    Destructive — discards hand edits on the cell siblings. Cell 1 (the source)
    is never touched.
    """
    if request.method != "POST":
        return redirect("composition-edit", composition_id=composition_id)
    source = get_object_or_404(Composition, id=composition_id)
    if not source.grid_id:
        messages.error(request, "Source has no folder yet.")
        return redirect("composition-edit", composition_id=composition_id)
    grid_id = source.grid_id
    Composition.objects.filter(grid_id=grid_id).exclude(id=source.id).delete()
    base_name = (source.name or "composition").strip() or "composition"
    source_slug = ""
    if source.url:
        parts = [p for p in urlparse(source.url).path.split("/") if p]
        if parts:
            source_slug = parts[-1].lower()
    if not source_slug:
        source_slug = slugify(base_name) or f"comp{source.id}"
    for cell in range(2, 10):
        clone = Composition.objects.get(id=source.id)
        clone.pk = None
        clone.id = None
        clone.name = f"{base_name} · grid {grid_id} · cell {cell}"
        clone.url = f"/{source_slug}grid{grid_id}cell{cell}/"
        clone.page_url = None
        clone.status = "pending"
        clone.created_at = timezone.now()
        clone.grid_id = grid_id
        clone.grid_cell_index = cell
        clone.save()
        clone.allowed_personas.set(source.allowed_personas.all())
    messages.success(request, f'Folder "{grid_id}" reset — 8 fresh clones of "{base_name}".')
    return redirect("composition_folder_view", grid_id=grid_id)


def composition_folder_view(request, grid_id):
    """Folder page: lists every composition in this folder + 'generate more'.

    Replaces the older grid-only view. Shows a 3×3 preview of cells 1..9 plus
    any extras as a flat list, with controls to generate missing cells, render
    the 3×3 in standalone mode, and edit each cell.
    """
    cells = list(
        Composition.objects.filter(grid_id=grid_id)
        .prefetch_related("media_assets")
        .order_by("grid_cell_index", "id")
    )
    if not cells:
        messages.error(request, f'No folder found with id "{grid_id}".')
        return redirect("composition-view")
    source = cells[0]
    source_slug = ""
    if source.url:
        parts = [p for p in urlparse(source.url).path.split("/") if p]
        if parts:
            source_slug = parts[-1].lower()
    if not source_slug:
        source_slug = slugify(source.name or "composition") or f"comp{source.id}"
    for cell in cells:
        if cell.id != source.id and not cell.url:
            cell.url = f"/{source_slug}grid{grid_id}cell{cell.grid_cell_index}/"
            cell.save(update_fields=["url"])
    for cell in cells:
        cell.list_thumbnail_src = _composition_list_thumbnail_src(cell)
        cell.preview_video_url = ""
        for asset in cell.media_assets.all():
            if asset.kind == "preview_10s" and asset.status == "ready" and getattr(asset, "file", None):
                try:
                    cell.preview_video_url = asset.file.url
                except Exception:
                    cell.preview_video_url = ""
                break
        cell.nft_online_video_ready = bool(cell.preview_video_url)
    cells_by_slot: list = [None] * 9
    for cell in cells:
        idx = cell.grid_cell_index
        if 1 <= idx <= 9 and cells_by_slot[idx - 1] is None:
            cells_by_slot[idx - 1] = cell
    return render(
        request,
        "composition_folder.html",
        {
            "grid_id": grid_id,
            "source": source,
            "cells": cells,
            "cells_by_slot": cells_by_slot,
            "missing_count": sum(1 for c in cells_by_slot if c is None),
        },
    )


def composition_grid_view(request, grid_id):
    """Render a bare 3×3 layout (for the gallery wall, no chrome)."""
    cells = list(
        Composition.objects.filter(grid_id=grid_id).order_by("grid_cell_index", "id")
    )
    if not cells:
        messages.error(request, f'No grid found with id "{grid_id}".')
        return redirect("dashboard")
    return render(
        request,
        "grid_view.html",
        {"grid_id": grid_id, "cells": cells[:9]},
    )


# --- Character composition seeding -----------------------------------------
# Curated palettes of source folder names (under composition_sources_unprocessed/)
# and ready-to-use composition recipes. The seeder only uses sources that
# actually exist on disk, so missing folders are skipped gracefully.

_CHARACTER_PALETTES: dict[str, dict] = {
    "goth": {
        "themes": ["goth", "dark", "occult"],
        "hashtags": ["goth", "dark", "witchy", "occult"],
        "emotions": ["melancholy", "longing", "dread", "ecstasy"],
        # Atmospheric / textured backdrops
        "backgrounds": [
            "goth_bg_blood_texture_search",
            "goth_black_leather_bg_search",
            "goth_black_bed_search",
            "goth_coffin_bed_search",
            "goth_dark_coffee_aesthetics_search",
            "goth_hr_giger_search",
            "goth_fire_bonfire_gif_search",
            "goth_tombstones_search",
            "goth_witch_workspace_search",
            "moon_insta",
            "sailormoon-gif_search",
            "fire_wall_search",
            "thunderstorm_insta",
        ],
        # Figures / characters
        "foregrounds": [
            "goth_fem_nightsister_vamp_search",
            "goth_ghost_klan_girl_tiktok_search",
            "goth_ghost_tiktok_search",
            "witch_black_portrait_search",
            "witch_green_search",
            "witches_three",
            "knight_lonely_search",
            "goth_party_search",
            "goth_welloffables_acc",
            "tumblr_ponyleg_goth_acc",
            "goth_tumblr_myrottenviscera_acc",
            "goth_latex<bdsm_portriat_search",
            "goth_real_surgery_search",
            "goth_stomach_alien_search_solo",
            "goth_matrix_wake_solo",
        ],
        # Small accent overlays
        "overlays": [
            "goth_bats_search",
            "skull_gif_solo",
            "skeleton_dance_gif_search",
            "goth_candle_solo",
            "goth_matrix_wake_solo",
        ],
        # Audio tracks (under composition_audio_sources/noise_studio/)
        "audio_tracks": [
            "skeleton_dance_1.wav",
            "crash_wobble.wav",
            "dance_mania_2.wav",
        ],
    },
}

# Recipes describe a composition shape. Each is picked, then the seeder fills
# in random sources/transitions/effects to produce a unique composition.
_COMPOSITION_RECIPES: list[dict] = [
    {"type": "classic", "needs": ("bg", "fg"), "overlay": "maybe"},
    {"type": "circle-foreground-rotate-mirror", "needs": ("bg", "fg"), "overlay": "maybe"},
    {"type": "circle-foreground-rotate", "needs": ("bg", "fg"), "overlay": "maybe"},
    {"type": "circle-foreground", "needs": ("bg", "fg"), "overlay": "maybe"},
    {"type": "cross-foreground-tunnel", "needs": ("bg", "fg"), "overlay": "no"},
    {"type": "cross-foreground-rotate", "needs": ("bg", "fg"), "overlay": "maybe"},
    {"type": "cross-foreground", "needs": ("bg", "fg"), "overlay": "maybe"},
    {"type": "bat-foreground-rotate", "needs": ("bg", "fg"), "overlay": "no"},
    {"type": "tunnel", "needs": ("bg",), "overlay": "no"},
    {"type": "psychedelic-tunnel", "needs": ("bg",), "overlay": "maybe"},
    {"type": "tunnel-burst", "needs": ("bg",), "overlay": "no"},
    {"type": "liquid-mirror", "needs": ("bg",), "overlay": "maybe"},
    {"type": "liquid-mirror-extreme", "needs": ("bg",), "overlay": "maybe"},
    {"type": "wax-melt", "needs": ("bg",), "overlay": "maybe"},
    {"type": "mirror-rotate", "needs": ("bg",), "overlay": "maybe"},
    {"type": "kaleido-quad", "needs": ("bg",), "overlay": "maybe"},
    {"type": "kaleido-octa", "needs": ("bg",), "overlay": "maybe"},
    {"type": "strobe", "needs": ("bg",), "overlay": "no"},
    {"type": "strobe-double", "needs": ("bg", "fg"), "overlay": "no"},
    {"type": "mash-fine-flux", "needs": ("bg",), "overlay": "no"},
    {"type": "mash-superfine-flux", "needs": ("bg",), "overlay": "no"},
    {"type": "mash-fine-double", "needs": ("bg", "fg"), "overlay": "no"},
    {"type": "quad", "needs": ("bg",), "overlay": "no"},
    {"type": "horizontal-stripes-thick", "needs": ("bg", "fg"), "overlay": "no"},
    {"type": "vertical-stripes-thick", "needs": ("bg", "fg"), "overlay": "no"},
    {"type": "left-to-right", "needs": ("bg", "fg"), "overlay": "maybe"},
    {"type": "scrollhole", "needs": ("bg",), "overlay": "no"},
    {"type": "box-room", "needs": ("bg",), "overlay": "no"},
]



def _palette_sources_on_disk(names: list[str]) -> list[str]:
    """Filter source-folder names down to ones that exist with at least one media file."""
    available: list[str] = []
    for name in names:
        d = (LOCAL_SOURCES_ROOT / name).resolve()
        if not d.exists() or not d.is_dir():
            continue
        if _local_source_dir_media_files(d):
            available.append(name)
    return available



def _existing_audio_tracks(track_names: list[str]) -> list[str]:
    """Filter Noise Studio track filenames down to ones that exist on disk."""
    folder = LOCAL_AUDIO_SOURCES_ROOT / "noise_studio"
    if not folder.exists():
        return []
    have = {p.name for p in folder.iterdir() if p.is_file()}
    return [t for t in track_names if t in have]



def _all_existing_slugs() -> set[str]:
    slugs: set[str] = set()
    for url in Composition.objects.exclude(url__isnull=True).exclude(url__exact="").values_list("url", flat=True):
        normalized = (url or "").rstrip("/")
        if "/" in normalized:
            slugs.add(normalized.rsplit("/", 1)[-1])
    return slugs



def _unique_slug(base: str, taken: set[str]) -> str:
    base_slug = slugify(base) or "comp"
    base_slug = base_slug[:48] or "comp"
    candidate = base_slug
    while not candidate or candidate in taken:
        candidate = f"{base_slug}-{get_random_string(5).lower()}"
    taken.add(candidate)
    return candidate



def _build_seed_composition_kwargs(character: str, palette: dict, taken_slugs: set[str], host_origin: str) -> dict | None:
    """Pick a recipe + sources and return Composition.objects.create kwargs (or None if not buildable)."""
    bg_pool = _palette_sources_on_disk(palette.get("backgrounds") or [])
    fg_pool = _palette_sources_on_disk(palette.get("foregrounds") or [])
    overlay_pool = _palette_sources_on_disk(palette.get("overlays") or [])
    audio_pool = _existing_audio_tracks(palette.get("audio_tracks") or [])

    if not bg_pool:
        return None  # nothing to draw from

    eligible_recipes = [r for r in _COMPOSITION_RECIPES if "fg" not in r["needs"] or fg_pool]
    if not eligible_recipes:
        return None
    recipe = random.choice(eligible_recipes)

    bg = random.sample(bg_pool, k=min(len(bg_pool), random.randint(1, 3))) if "bg" in recipe["needs"] else []
    fg = random.sample(fg_pool, k=min(len(fg_pool), random.randint(1, 2))) if "fg" in recipe["needs"] else []
    use_overlay = recipe["overlay"] == "maybe" and overlay_pool and random.random() < 0.55
    ov = random.sample(overlay_pool, k=min(len(overlay_pool), random.randint(1, 2))) if use_overlay else []

    bg_t = random.choice(_TRANSITIONS)
    fg_t = random.choice(_TRANSITIONS)
    ov_t = random.choice(_TRANSITIONS) if ov else "none"

    overlay_effect = "none"
    if ov:
        if recipe["type"].startswith("circle-foreground-rotate-mirror"):
            overlay_effect = random.choice(("none", "circle_rotate", "circle_rotate_mirror", "mash"))
        else:
            overlay_effect = random.choice(_OVERLAY_EFFECTS)

    name_seed_parts = [character, recipe["type"].replace("-", "_")]
    if bg:
        name_seed_parts.append(bg[0].split("_")[0])
    if fg:
        name_seed_parts.append(fg[0].split("_")[0])
    name_base = " ".join(name_seed_parts)
    name = f"{name_base} {get_random_string(4).lower()}"

    slug = _unique_slug(name, taken_slugs)
    full_url = f"/{slug}/"

    audio_path = ""
    if audio_pool and random.random() < 0.7:
        audio_path = f"audio_sources/noise_studio/{random.choice(audio_pool)}"

    filter_settings = {
        "layer_transitions": {"background": bg_t, "foreground": fg_t, "overlay": ov_t},
        "overlay_effect": overlay_effect,
    }

    preview_name = build_preview_image_from_sources(bg or fg) or None

    return {
        "name": name,
        "type": recipe["type"],
        "url": full_url,
        "page_url": full_url,
        "status": "pending",
        "brightness": random.randint(35, 65),
        "saturation": random.randint(40, 75),
        "opacity": random.choice((90, 95, 100, 100, 100)),
        "playback_speed": round(random.uniform(0.85, 1.25), 2),
        "source_playback_mode": "random",
        "landscape_only": False,
        "auto_link_delay_seconds": 0,
        "random_link_enabled": True,
        "random_link_scope": "all",
        "mood_rating": random.choice(("chill", "mid", "agro")),
        "filter_preset": random.choice(("none", "none", "none", "vhs", "noir", "neon", "duotone")),
        "filter_intensity": random.randint(15, 55),
        "filter_param_1": round(random.uniform(0.2, 0.8), 2),
        "filter_param_2": round(random.uniform(0.2, 0.8), 2),
        "filter_param_3": round(random.uniform(0.2, 0.8), 2),
        "filter_settings": filter_settings,
        "transition": bg_t,  # legacy field; per-layer values live in filter_settings
        "background_sources": bg,
        "foreground_sources": fg,
        "overlay_sources": ov,
        "overlay_landscape_only": False,
        "overlay_speed": round(random.uniform(0.6, 1.4), 2),
        # Free-fit overlays scale relative to the full viewport. Scales >=1.0 produce
        # an overlay that covers the entire background, hiding it. Cap at 0.7 and pair
        # with a random fit so most seeded overlays sit comfortably inside their pane.
        "overlay_scale": round(random.uniform(0.45, 0.7), 2),
        "overlay_rotate": overlay_effect == "rotate_3d",
        "overlay_fit": random.choice(("square", "framed", "framed", "free")),
        "overlay_frame_margin": 0.12,
        "composition_hashtags": list(palette.get("hashtags") or []),
        "composition_emotions": list(palette.get("emotions") or []),
        "composition_themes": list(palette.get("themes") or []),
        "composition_characters": [character],
        "img": preview_name,
        "audio_file": audio_path or None,
    }



def _seed_character_compositions(character: str, count: int, host_origin: str = "") -> tuple[int, list[str]]:
    """Create up to `count` compositions for `character`. Returns (created, errors)."""
    palette = _CHARACTER_PALETTES.get((character or "").lower().strip())
    if not palette:
        return 0, [f"No palette defined for character '{character}'."]
    count = max(1, min(int(count or 0), 100))
    taken = _all_existing_slugs()
    created = 0
    errors: list[str] = []
    for _ in range(count):
        kwargs = _build_seed_composition_kwargs(character, palette, taken, host_origin)
        if not kwargs:
            errors.append("Skipped one — no sources available on disk for any recipe.")
            continue
        try:
            Composition.objects.create(**kwargs)
            created += 1
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"Create failed: {exc}")
    return created, errors


@require_http_methods(["GET", "POST"])

def seed_character_compositions(request):
    if request.method == "GET":
        messages.info(
            request,
            "Character bulk-create runs from Composition List: use “Bulk-create from character palette”, "
            "pick a palette and count, then click Generate compositions.",
        )
        return redirect("composition-view")
    character = (request.POST.get("character") or "goth").strip().lower()
    try:
        count = int(request.POST.get("count") or 12)
    except ValueError:
        count = 12
    return_page = (request.POST.get("return_page") or "").strip()
    host_origin = f"{request.scheme}://{request.get_host()}"
    created, errors = _seed_character_compositions(character, count, host_origin=host_origin)
    if created:
        messages.success(request, f"Created {created} '{character}' composition(s). Edit or delete as needed.")
    if errors:
        messages.warning(request, f"{len(errors)} skip(s)/issue(s): {errors[0]}")
    target = reverse("composition-view")
    if return_page.isdigit():
        target = f"{target}?page={return_page}"
    return redirect(target)


@require_POST

def set_composition_ready(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    is_ready = request.POST.get("ready_for_deployment") == "on"
    composition.ready_for_deployment = is_ready
    composition.save(update_fields=["ready_for_deployment"])
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "ready": is_ready})
    return_folder = request.POST.get("return_folder")
    if return_folder:
        return redirect("composition_folder_view", grid_id=return_folder)
    return redirect("composition-view")



def assign_compositions_series(request):
    if request.method == "POST":
        try:
            selected_ids = json.loads(request.POST.get("compositions_to_assign", "[]"))
            selected_ids = [int(cid) for cid in selected_ids]
        except (TypeError, ValueError):
            selected_ids = []

        if not selected_ids:
            messages.error(request, "No compositions selected.")
            return redirect("composition-view")

        clear_series = request.POST.get("clear_series") == "on"
        new_series_name = (request.POST.get("new_series_name") or "").strip()
        selected_series_id = (request.POST.get("series_id") or "").strip()
        selected_series = None

        if clear_series:
            selected_series = None
        elif new_series_name:
            selected_series, _ = Series.objects.get_or_create(name=new_series_name)
        elif selected_series_id:
            try:
                selected_series = Series.objects.get(id=int(selected_series_id))
            except (ValueError, Series.DoesNotExist):
                messages.error(request, "Selected series is invalid.")
                return redirect("composition-view")
        else:
            messages.error(request, "Select an existing series, create a new one, or choose Remove series.")
            return redirect("composition-view")

        queryset = Composition.objects.filter(id__in=selected_ids)
        if not queryset.exists():
            messages.error(request, "No matching compositions found.")
            return redirect("composition-view")

        with transaction.atomic():
            updated = queryset.update(series=selected_series)
        if clear_series:
            messages.success(request, f"Removed series from {updated} composition(s).")
        else:
            messages.success(request, f"Assigned {updated} composition(s) to series '{selected_series.name}'.")
    return redirect("composition-view")



def series_library(request):
    series_items = Series.objects.all().order_by("name")
    series_rows = []
    for series in series_items:
        comps_qs = Composition.objects.filter(series=series).order_by("-id")
        count = comps_qs.count()
        latest = comps_qs.first()
        preview_url = f"/composition-preview/{latest.id}/" if latest else None
        series_rows.append(
            {
                "id": series.id,
                "name": series.name,
                "count": count,
                "latest_date": latest.date if latest else None,
                "preview_url": preview_url,
            }
        )
    return render(request, "admin/series_library.html", {"series_rows": series_rows})



def series_detail(request, series_id):
    series = get_object_or_404(Series, id=series_id)
    compositions_list = Composition.objects.filter(series=series).order_by("-id")
    paginator = Paginator(compositions_list, 20)
    page_number = request.GET.get("page")
    compositions = paginator.get_page(page_number)
    for comp in compositions:
        comp.preview_url = f"/composition-preview/{comp.id}/"
    return render(
        request,
        "admin/series_detail.html",
        {"series": series, "compositions": compositions},
    )



@xframe_options_sameorigin
def composition_detail(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "save_hashtags":
            composition.composition_hashtags = parse_composition_hashtags(request.POST.get("composition_hashtags", ""))
            composition.save(update_fields=["composition_hashtags"])
            messages.success(request, "Hashtags updated.")
            return redirect("composition_detail", composition_id=composition.id)
        if action == "refresh_preview":
            preview_name = build_preview_image_from_sources(composition.background_sources or composition.foreground_sources)
            if preview_name:
                composition.img = preview_name
                composition.save(update_fields=["img"])
                messages.success(request, "Preview image refreshed from current sources.")
            else:
                messages.error(request, "Could not generate a preview from the selected sources.")
            return redirect("composition_detail", composition_id=composition.id)
    return render(request, 'admin/composition_detail.html', {'composition': composition})



def composition_delete(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    deleted_name = composition.name
    deleted_id = composition.id
    composition.delete()
    _purge_composition_preview_cache([deleted_id])
    messages.success(request, f'Composition "{deleted_name}" deleted.')
    return redirect("composition-view")



def composition_latest_render(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    if not composition.final_video:
        raise Http404("No rendered video for this composition.")
    name = composition.final_video.name
    if not default_storage.exists(name):
        raise Http404("Latest rendered video file is missing.")
    try:
        fh = default_storage.open(name, "rb")
    except OSError:
        raise Http404("Unable to read rendered video.")
    resp = FileResponse(fh, content_type="video/mp4")
    resp["Content-Disposition"] = 'inline; filename="composition-render.mp4"'
    return resp



def legacy_render_redirect(request, composition_id):
    return redirect("composition_latest_render", composition_id=composition_id)



def _collect_source_media_files(source_names):
    items = []
    for source_name in source_names or []:
        source_dir = LOCAL_SOURCES_ROOT / source_name
        if not source_dir.exists():
            continue
        for file in sorted(_local_source_dir_media_files(source_dir), key=lambda p: p.name.lower()):
            ext = file.suffix.lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm"}:
                continue
            items.append({"path": str(file), "kind": "video" if ext in {".mp4", ".mov", ".webm"} else "image"})
    return items



def _build_layer_clip(media_items, total_duration, speed, size, scale=1.0, opacity=1.0, center=True):
    if not media_items:
        return None
    seg = max(0.2, 1.2 / max(0.25, speed))
    clips = []
    count = max(1, int(math.ceil(total_duration / seg)))
    target_w = max(2, int(size[0] * scale))
    target_h = max(2, int(size[1] * scale))
    for i in range(count):
        item = media_items[i % len(media_items)]
        try:
            if item["kind"] == "video":
                c = VideoFileClip(item["path"]).without_audio()
                if c.duration and c.duration > seg:
                    c = c.subclip(0, seg)
                else:
                    c = c.set_duration(seg)
            else:
                c = ImageClip(item["path"]).set_duration(seg)
            c = c.resize(newsize=(target_w, target_h)).set_duration(seg)
            if opacity < 1.0:
                c = c.set_opacity(max(0.0, min(1.0, opacity)))
            if center:
                c = c.set_position(("center", "center"))
            clips.append(c)
        except Exception:
            continue
    if not clips:
        return None
    return concatenate_videoclips(clips, method="compose").set_duration(total_duration)



def _build_scrolling_layer_clip(media_items, total_duration, speed, size, scale=1.0, opacity=1.0, variant="scrollhole"):
    if not media_items:
        return None
    base_speed = max(0.25, speed)
    if variant == "dirty-scroll":
        travel_sec = max(0.65, 4.8 / math.pow(base_speed, 1.85))
        spawn_sec = max(0.07, travel_sec * 0.14)
    else:
        travel_sec = max(0.9, 4.2 / math.pow(base_speed, 1.35))
        spawn_sec = max(0.11, travel_sec * 0.24)

    target_w = max(2, int(size[0] * scale))
    target_h = max(2, int(size[1] * scale))
    start_y = size[1] * 1.10
    end_y = -size[1]
    total_travel = max(1.0, start_y - end_y)

    clips = []
    t = 0.0
    idx = 0
    while t < total_duration + travel_sec:
        item = media_items[idx % len(media_items)]
        idx += 1
        try:
            if item["kind"] == "video":
                src = VideoFileClip(item["path"]).without_audio()
                seg_start = 0
                if src.duration and src.duration > travel_sec:
                    seg_start = random.uniform(0, max(0, src.duration - travel_sec))
                    layer = src.subclip(seg_start, min(src.duration, seg_start + travel_sec))
                else:
                    layer = src.set_duration(travel_sec)
            else:
                layer = ImageClip(item["path"]).set_duration(travel_sec)
            layer = layer.resize(newsize=(target_w, target_h)).set_duration(travel_sec)

            jitter_x = 0
            jitter_r = 0
            if variant == "dirty-scroll":
                jitter_x = random.uniform(-0.05, 0.05) * size[0]
                jitter_r = random.uniform(-2.4, 2.4)
                layer = layer.rotate(jitter_r, unit="deg")

            x = (size[0] - target_w) / 2 + jitter_x
            v = total_travel / travel_sec
            y_func = lambda tt, _t=t, _v=v: start_y - (_v * max(0, tt - _t))
            layer = layer.set_start(t).set_position(lambda tt, yf=y_func: (x, yf(tt)))
            layer = layer.set_opacity(max(0.0, min(1.0, opacity)))
            clips.append(layer)
        except Exception:
            continue
        t += spawn_sec

    if not clips:
        return None
    return CompositeVideoClip(clips, size=size).set_duration(total_duration)



def render_composition_export(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    if request.method != "POST":
        return redirect("composition_detail", composition_id=composition.id)

    try:
        duration_seconds = int(request.POST.get("duration_seconds", 20) or 20)
    except (TypeError, ValueError):
        duration_seconds = 20
    duration_seconds = max(3, min(600, duration_seconds))
    aspect_preset = (request.POST.get("aspect_preset") or "laptop").strip().lower()
    preset_sizes = {
        "landscape": (1920, 1080),
        "laptop": (1440, 900),
        "portrait": (1080, 1920),
        "square": (1080, 1080),
    }
    size = preset_sizes.get(aspect_preset, preset_sizes["laptop"])

    if not composition.url:
        messages.error(request, "This composition has no public URL to capture.")
        return redirect("composition_detail", composition_id=composition.id)
    previous_final_name = composition.final_video.name if composition.final_video else ""
    try:
        output_name = f"render_{composition.id}_{aspect_preset}_{duration_seconds}s_{uuid.uuid4().hex[:8]}.mp4"
        composition.final_video.name = capture_composition_video(
            composition,
            duration_seconds=duration_seconds,
            storage_path=f"renders/{output_name}",
            aspect_preset=aspect_preset if aspect_preset in preset_sizes else "laptop",
            include_audio=True,
        )
        composition.status = "completed"
        composition.save(update_fields=["final_video", "status"])
        if previous_final_name and previous_final_name != composition.final_video.name:
            try:
                if default_storage.exists(previous_final_name):
                    default_storage.delete(previous_final_name)
            except Exception:
                pass
        extra_hint = ""
        try:
            output_path = Path(settings.MEDIA_ROOT) / composition.final_video.name
            if output_path.exists() and output_path.stat().st_size < 80000:
                extra_hint = (
                    " If the file is black: the exporter now prefers Google Chrome automatically; "
                    "set PLAYWRIGHT_HEADLESS=false to capture with a visible window, "
                    "or PLAYWRIGHT_RENDER_DEBUG=1 and check media/renders/debug_render_*.png."
                )
        except OSError:
            pass
        messages.success(
            request,
            f"Rendered video ({aspect_preset}, {duration_seconds}s) is ready.{extra_hint}",
        )
    except Exception as exc:
        messages.error(request, f"Render failed: {exc}")

    return redirect("composition_detail", composition_id=composition.id)


@login_required
def export_composition_png(request, composition_id):
    """Capture a composition still as a transparent-background PNG and stream it for download.

    Photoshop opens the result with an unlocked, transparent layer instead of
    a flat white background, because Playwright's omit_background=True leaves
    the alpha channel intact.
    """
    from django.http import HttpResponse
    from ..nft_media import capture_composition_still_png

    composition = get_object_or_404(Composition, id=composition_id)
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    try:
        size = int(request.POST.get("size") or 1080)
        size = max(256, min(4096, size))
    except (TypeError, ValueError):
        size = 1080

    transparent = (request.POST.get("transparent") or "1").strip() not in {"0", "false", "no"}

    try:
        png_bytes = capture_composition_still_png(
            composition,
            size=size,
            transparent=transparent,
        )
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)

    slug = (composition.url or f"comp-{composition.id}").strip("/").replace("/", "-")
    filename = f"{slug}_{size}px.png"
    response = HttpResponse(png_bytes, content_type="image/png")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _composition_preview_cache_key(comp) -> str:
    """
    Deterministic cache filename for a composition's 120x120 thumbnail.

    We include every field that can change the rendered preview so that any
    edit produces a new filename and the old thumbnail becomes a dead file
    (cleaned up in composition_delete / delete_compositions). This avoids
    having to do explicit invalidation on save.
    """
    payload = {
        "id": comp.id,
        "bg": list(comp.background_sources or []),
        "fg": list(comp.foreground_sources or []),
        "opacity": comp.opacity,
        "img": getattr(comp, "img", "") or "",
    }
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"comp_{comp.id}_{digest}.jpg"



def _composition_preview_response(body: bytes, etag: str) -> HttpResponse:
    response = HttpResponse(body, content_type="image/jpeg")
    response["ETag"] = etag
    # 1h browser cache is safe because the cache key itself changes whenever
    # any field that affects the preview changes (sources, opacity, img).
    response["Cache-Control"] = "public, max-age=3600"
    return response



def composition_preview_image(request, composition_id):
    # Fetch only the fields the cache key + render need. The /composition-view/
    # table fires 50 of these per page load, so trimming the ORM read shaves
    # meaningful time off when there's no cache hit.
    comp = get_object_or_404(
        Composition.objects.only(
            "id", "background_sources", "foreground_sources", "opacity", "img"
        ),
        id=composition_id,
    )
    cache_name = _composition_preview_cache_key(comp)
    etag = f'W/"{cache_name}"'

    # Short-circuit on If-None-Match so a refresh with a warm browser cache
    # costs microseconds on the server instead of a filesystem walk.
    if request.META.get("HTTP_IF_NONE_MATCH", "") == etag:
        response = HttpResponse(status=304)
        response["ETag"] = etag
        response["Cache-Control"] = "public, max-age=3600"
        return response

    try:
        COMPOSITION_PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    cache_path = COMPOSITION_PREVIEW_CACHE_DIR / cache_name

    if cache_path.is_file():
        try:
            body = cache_path.read_bytes()
            if body:
                return _composition_preview_response(body, etag)
        except Exception:
            # Cache read failures fall through to regenerate.
            pass

    # Cold path — this is the expensive branch that used to run on EVERY
    # thumbnail request. moviepy can take 300-800ms per video source, and with
    # 50 rows on /composition-view/ that's the origin of the page hang the user
    # was seeing on save / delete / refresh.
    bg = _load_first_source_image(comp.background_sources)
    fg = _load_first_source_image(comp.foreground_sources)
    if bg is None and fg is None:
        return HttpResponse(status=404)

    size = COMPOSITION_PREVIEW_SIZE
    canvas = Image.new("RGBA", size, (0, 0, 0, 255))
    if bg is not None:
        bg_fit = ImageOps.fit(bg, size, Image.Resampling.LANCZOS)
        canvas.alpha_composite(bg_fit, (0, 0))

    if fg is not None:
        fg_size = (int(size[0] * 0.62), int(size[1] * 0.62))
        fg_fit = ImageOps.fit(fg, fg_size, Image.Resampling.LANCZOS)
        if comp.opacity is not None:
            alpha = max(0, min(255, int((comp.opacity / 100.0) * 255)))
            fg_fit.putalpha(alpha)
        pos = ((size[0] - fg_size[0]) // 2, (size[1] - fg_size[1]) // 2)
        canvas.alpha_composite(fg_fit, pos)

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="JPEG", quality=88)
    body = output.getvalue()

    # Write via a temp file + rename so concurrent requests don't read a
    # partial JPEG. If the write fails, we still serve the bytes we generated.
    try:
        tmp = cache_path.with_suffix(".jpg.tmp")
        tmp.write_bytes(body)
        os.replace(tmp, cache_path)
    except Exception:
        pass

    return _composition_preview_response(body, etag)



def _purge_composition_preview_cache(composition_ids):
    """Best-effort cleanup of cached preview JPEGs for deleted compositions."""
    if not composition_ids:
        return
    if not COMPOSITION_PREVIEW_CACHE_DIR.is_dir():
        return
    ids = {str(int(cid)) for cid in composition_ids if str(cid).isdigit()}
    if not ids:
        return
    try:
        for p in COMPOSITION_PREVIEW_CACHE_DIR.glob("comp_*.jpg"):
            # Filenames are ``comp_<id>_<hash>.jpg``; the id segment is [1].
            parts = p.stem.split("_")
            if len(parts) >= 3 and parts[1] in ids:
                try:
                    p.unlink()
                except Exception:
                    pass
    except Exception:
        pass



def _find_composition_by_slug(page_slug: str) -> Composition | None:
    """
    Resolve the composition for /<page_slug>/.

    Prefer URLs whose path is exactly ``/<slug>``, then fall back to any URL
    whose last path segment equals the slug (newest id wins if ambiguous).
    Uses DB queries instead of a Python loop for O(1) performance.
    """
    from django.db.models import Q
    want = (page_slug or "").strip().strip("/").lower()
    if not want:
        return None
    qs = Composition.objects.exclude(url__isnull=True).exclude(url__exact="")
    # Exact: single-segment URL like /dragonsass
    exact = qs.filter(url=f"/{want}").order_by("-id").first()
    if exact:
        return exact
    # Nested: URL ending with /want or /want/ (grid cells, etc.)
    return qs.filter(Q(url__endswith=f"/{want}") | Q(url__endswith=f"/{want}/")).order_by("-id").first()



def _fallback_redirect_for_public_slug(page_slug: str):
    slug = (page_slug or "").strip().strip("/")
    if not slug:
        return None
    source_names = [p.name for p in get_local_sources()]
    if slug in source_names:
        return redirect("bucket_contents", bucket_name=slug)
    close_sources = difflib.get_close_matches(slug, source_names, n=1, cutoff=0.35)
    if close_sources:
        return redirect("bucket_contents", bucket_name=close_sources[0])
    return None


@xframe_options_sameorigin

def composition_public_page(request, page_slug):
    try:
        from ._masks import resolve_mask_slug, mask_public_view
        mask = resolve_mask_slug(page_slug)
        if mask is not None:
            return mask_public_view(request, mask)
    except Exception:
        pass
    matched = _find_composition_by_slug(page_slug)
    if not matched:
        fallback_redirect = _fallback_redirect_for_public_slug(page_slug)
        if fallback_redirect:
            return fallback_redirect
        raise Http404("Composition page not found")
    return _render_composition_public(request, matched)


@xframe_options_sameorigin
def composition_live_by_id(request, composition_id):
    """Render the live public composition page by numeric id.

    Lets us iframe-embed any composition (including grid children that have
    no published `url` slug yet).
    """
    matched = get_object_or_404(Composition, id=composition_id)
    return _render_composition_public(request, matched)


_RANDOM_LINKS_CACHE_KEY = "composition_random_links_all"
_RANDOM_LINKS_CACHE_TTL = 90  # seconds


def _get_random_links(exclude_id: int) -> list:
    links = cache.get(_RANDOM_LINKS_CACHE_KEY)
    if links is None:
        links = list(
            Composition.objects.exclude(url__isnull=True)
            .exclude(url__exact="")
            .values_list("url", flat=True)
        )
        links = [u for u in links if u]
        cache.set(_RANDOM_LINKS_CACHE_KEY, links, _RANDOM_LINKS_CACHE_TTL)
    matched_url = Composition.objects.filter(id=exclude_id).values_list("url", flat=True).first() or ""
    return [u for u in links if u != matched_url]


def _attach_image_transforms(assets, composition):
    """Append ?comp=<id> to image URLs that have a per-image flip/crop transform.

    source_media_file only applies a stored flip/crop when the request carries
    the ?comp=<id> param. Without this rewrite the transforms saved on the
    composition would never affect the rendered page or the Playwright capture.
    Returns shallow copies of the affected entries — never mutates the dicts
    returned by collect_source_assets, which are shared across compositions
    through the source-assets cache.
    """
    transforms = (composition.source_image_transforms or {})
    if not transforms:
        return assets
    comp_id = int(composition.id)
    out = []
    for a in assets:
        if a.get("kind") == "image":
            rel = f"{a.get('source_name', '')}/{a.get('name', '')}"
            if rel in transforms:
                a = dict(a)
                sep = "&" if "?" in a["url"] else "?"
                a["url"] = f"{a['url']}{sep}comp={comp_id}"
                a["preview_url"] = a["url"]
        out.append(a)
    return out


def _render_composition_public(request, matched):
    random_links = _get_random_links(matched.id)
    layer_transitions = ((matched.filter_settings or {}).get("layer_transitions") or {})
    if not isinstance(layer_transitions, dict):
        layer_transitions = {}
    overlay_effect = ((matched.filter_settings or {}).get("overlay_effect") or "").strip().lower()
    if not overlay_effect:
        overlay_effect = "rotate_3d" if bool(getattr(matched, "overlay_rotate", False)) else "none"
    fallback_transition = (matched.transition or "none").lower()
    context = {
        "composition": matched,
        "composition_mood_rating": (matched.mood_rating or "mid").lower(),
        "render_mode": request.GET.get("render") == "1",
        # Distinguishes a *still* poster capture (single frame, motion-modes
        # should freeze on a good frame) from a *video* render (motion modes
        # must actually run). Set by capture_composition_still via the URL
        # param `&capture=still`; absent for the 10s/45s video captures.
        "still_capture": request.GET.get("capture") == "still",
        "muted": request.GET.get("muted") == "1",
        "composition_type": (matched.type or "").lower(),
        "composition_transition": (matched.transition or "none").lower(),
        "composition_background_transition": (layer_transitions.get("background") or fallback_transition).lower(),
        "composition_foreground_transition": (layer_transitions.get("foreground") or fallback_transition).lower(),
        "composition_overlay_transition": (layer_transitions.get("overlay") or "none").lower(),
        "composition_speed": matched.playback_speed or 1.0,
        "composition_source_playback_mode": (matched.source_playback_mode or "random").lower(),
        "composition_brightness": matched.brightness if matched.brightness is not None else 50,
        "composition_saturation": matched.saturation if matched.saturation is not None else 50,
        "composition_opacity": matched.opacity if matched.opacity is not None else 100,
        "composition_filter_preset": (matched.filter_preset or "none").lower(),
        "composition_filter_intensity": matched.filter_intensity if matched.filter_intensity is not None else 40,
        "composition_filter_param_1": matched.filter_param_1 if matched.filter_param_1 is not None else 0.5,
        "composition_filter_param_2": matched.filter_param_2 if matched.filter_param_2 is not None else 0.5,
        "composition_filter_param_3": matched.filter_param_3 if matched.filter_param_3 is not None else 0.5,
        "composition_filter_settings": matched.filter_settings or {},
        "background_source_count": len(matched.background_sources or []),
        "overlay_assets": collect_source_assets(
            matched.overlay_sources,
            landscape_only=bool(getattr(matched, "overlay_landscape_only", False)),
        ),
        "overlay_landscape_only": bool(getattr(matched, "overlay_landscape_only", False)),
        "overlay_speed": matched.overlay_speed if matched.overlay_speed is not None else 1.0,
        "overlay_scale": matched.overlay_scale if matched.overlay_scale is not None else 1.0,
        "overlay_opacity": matched.overlay_opacity if matched.overlay_opacity is not None else 1.0,
        "color_layer_enabled": bool(getattr(matched, "color_layer_enabled", False)),
        "color_layer_color": getattr(matched, "color_layer_color", None) or "#000000",
        "color_layer_opacity": matched.color_layer_opacity if matched.color_layer_opacity is not None else 0.3,
        "color_layer_target": (getattr(matched, "color_layer_target", None) or "background"),
        "overlay_rotate": bool(getattr(matched, "overlay_rotate", False)),
        "overlay_effect": overlay_effect,
        "overlay_fit": (matched.overlay_fit or "free"),
        "overlay_frame_margin": matched.overlay_frame_margin if matched.overlay_frame_margin is not None else 0.12,
        "auto_link_delay_seconds": matched.auto_link_delay_seconds or 0,
        # Product decision: all compositions should random-hop to any composition
        # in the full collection.
        "random_link_enabled": True,
        "random_link_scope": "all",
        "composition_hashtags": matched.composition_hashtags or [],
        "random_links": random_links,
        "background_assets": collect_source_assets(
            matched.background_sources,
            landscape_only=bool(getattr(matched, "landscape_only", False)),
        ),
        "foreground_assets": collect_source_assets(
            matched.foreground_sources,
            landscape_only=bool(getattr(matched, "landscape_only", False)),
        ),
        "text_layers": list(getattr(matched, "text_layers", None) or []),
    }

    # Road-trip slot semantics for the four foreground source slots:
    #   slot 1 → left ground (perspective-warped, on the same plane as road)
    #   slot 2 → right ground (perspective-warped)
    #   slot 3 → sky (first source)
    #   slot 4 → sky (second source)
    # The road itself reads from the background source slot.
    # Other composition types ignore these — they're populated
    # unconditionally so the template can json_script them without a
    # per-type check, but the arrays are empty when irrelevant.
    _fg_sources = list(matched.foreground_sources or [])
    if (matched.type or "").lower() == "road-trip":
        _ground_left_sources = _fg_sources[0:1]
        _ground_right_sources = _fg_sources[1:2]
        _sky_sources = _fg_sources[2:4]
    else:
        _ground_left_sources = []
        _ground_right_sources = []
        _sky_sources = []
    _landscape_only = bool(getattr(matched, "landscape_only", False))
    context["road_ground_left_assets"] = collect_source_assets(
        _ground_left_sources, landscape_only=_landscape_only,
    ) if _ground_left_sources else []
    context["road_ground_right_assets"] = collect_source_assets(
        _ground_right_sources, landscape_only=_landscape_only,
    ) if _ground_right_sources else []
    context["road_sky_assets"] = collect_source_assets(
        _sky_sources, landscape_only=_landscape_only,
    ) if _sky_sources else []
    context["poster_url"] = _composition_list_thumbnail_src(matched)
    audio_url = ""
    audio_file = getattr(matched, "audio_file", None)
    if audio_file:
        audio_name = getattr(audio_file, "name", "") or ""
        if audio_name and default_storage.exists(audio_name):
            audio_url = audio_file.url
    context["composition_audio_url"] = audio_url

    for _asset_key in (
        "overlay_assets", "background_assets", "foreground_assets",
        "road_ground_left_assets", "road_ground_right_assets", "road_sky_assets",
    ):
        if context.get(_asset_key):
            context[_asset_key] = _attach_image_transforms(context[_asset_key], matched)

    return render(request, "composition_public.html", context)



def compositions_by_hashtag(request, hashtag):
    tag = parse_composition_hashtags(hashtag)
    if not tag:
        return JsonResponse({"hashtag": "", "count": 0, "compositions": []})
    needle = tag[0]
    comps = Composition.objects.exclude(url__isnull=True).exclude(url__exact="").order_by("-date")
    results = []
    for comp in comps:
        tags = [str(t).strip().lower() for t in (comp.composition_hashtags or []) if str(t).strip()]
        if needle not in tags:
            continue
        preview = reverse("composition-preview", kwargs={"composition_id": comp.id})
        results.append(
            {
                "id": comp.id,
                "name": comp.name,
                "url": comp.url,
                "page_url": comp.page_url or "",
                "type": comp.type,
                "mood_rating": (comp.mood_rating or "mid").lower(),
                "hashtags": tags,
                "preview_image_url": request.build_absolute_uri(preview),
            }
        )
    return JsonResponse({"hashtag": needle, "count": len(results), "compositions": results})



from asgiref.sync import async_to_sync
import asyncio
from django.http import HttpResponseNotAllowed
from django.utils.log import log_response
from ._utils import _load_first_source_image


# ---------------------------------------------------------------------------
# Lazy minting — voucher signing endpoint
# ---------------------------------------------------------------------------

def composition_voucher_sign(request, composition_id):
    """
    POST  — store a signed EIP-712 NFTVoucher for a composition.

    Expected JSON body:
        {
            "min_price_wei": 70000000000000000,
            "uri": "ipfs://...",
            "nonce": 1234567890,
            "signature": "0x...",
            "signed_by": "0x..."
        }

    GET   — return the active (unredeemed) voucher for this composition, if any.
    """
    from ..models import NFTMintVoucher
    import json
    from django.utils.timezone import now as tz_now

    comp = get_object_or_404(Composition, pk=composition_id)

    if request.method == "GET":
        voucher = comp.mint_vouchers.filter(redeemed=False).order_by("-created_at").first()
        if not voucher:
            return JsonResponse({"voucher": None})
        return JsonResponse({
            "voucher": {
                "id": voucher.pk,
                "min_price_wei": voucher.min_price_wei,
                "min_price_eth": float(voucher.min_price_eth),
                "uri": voucher.uri,
                "nonce": voucher.nonce,
                "signature": voucher.signature,
                "signed_by": voucher.signed_by,
                "signed_at": voucher.signed_at.isoformat() if voucher.signed_at else None,
            }
        })

    if request.method == "POST":
        if not request.user.is_authenticated:
            return JsonResponse({"error": "authentication required"}, status=403)
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "invalid JSON"}, status=400)

        required = ["min_price_wei", "uri", "nonce", "signature", "signed_by"]
        missing = [f for f in required if not data.get(f)]
        if missing:
            return JsonResponse({"error": f"missing fields: {', '.join(missing)}"}, status=400)

        nonce = int(data["nonce"])
        if NFTMintVoucher.objects.filter(nonce=nonce).exists():
            return JsonResponse({"error": "nonce already used"}, status=400)

        voucher = NFTMintVoucher.objects.create(
            composition=comp,
            min_price_wei=int(data["min_price_wei"]),
            uri=data["uri"],
            nonce=nonce,
            signature=data["signature"],
            signed_by=data["signed_by"].lower(),
            signed_at=tz_now(),
        )
        return JsonResponse({"ok": True, "voucher_id": voucher.pk})

    return JsonResponse({"error": "method not allowed"}, status=405)


def composition_voucher_record_redeem(request, composition_id):
    """
    POST — mark a voucher as redeemed after the on-chain tx succeeds.

    Expected JSON body:
        { "nonce": 1234567890, "tx_hash": "0x...", "token_id": "42", "wallet": "0x..." }
    """
    from ..models import NFTMintVoucher
    import json

    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)

    comp = get_object_or_404(Composition, pk=composition_id)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "invalid JSON"}, status=400)

    nonce = int(data.get("nonce", 0))
    voucher = comp.mint_vouchers.filter(nonce=nonce, redeemed=False).first()
    if not voucher:
        return JsonResponse({"error": "voucher not found or already redeemed"}, status=404)

    from django.utils.timezone import now as tz_now
    voucher.redeemed = True
    voucher.redeemed_tx = data.get("tx_hash", "")
    voucher.redeemed_token_id = str(data.get("token_id", ""))
    voucher.redeemed_by = (data.get("wallet", "") or "").lower()
    voucher.redeemed_at = tz_now()
    voucher.save()
    return JsonResponse({"ok": True})


@require_POST
@csrf_exempt
def composition_generate_media_single(request, composition_id):
    """Library-page cog menu posts here to (re)generate poster / 10s clip / all
    media for one composition.

    Critical: runs the work in a *detached subprocess*, not a daemon thread.
    Playwright + Chromium can spike RSS by 200-500 MB per launch. With the
    single gunicorn worker doing the in-process threaded approach, a few
    consecutive clicks would OOM the worker — and the bare `except Exception:
    pass` swallowed every failure, so the UI silently never updated.
    Subprocess isolates the heavy work and lets the watchdog catch real
    hangs.
    """
    import subprocess
    import sys
    from pathlib import Path as _Path

    comp = get_object_or_404(Composition, id=composition_id)
    kinds_param = (request.POST.get("kinds") or "poster,preview_10s").strip()
    kinds = [k.strip() for k in kinds_param.split(",") if k.strip() in ("poster", "preview_10s", "collector_45s")]
    if not kinds:
        return JsonResponse({"error": "No valid kinds specified."}, status=400)
    force = request.POST.get("force") == "1"

    project_root = _Path(__file__).resolve().parent.parent.parent
    # Self-describing subprocess: prints a START line the instant it boots and
    # a DONE / FAIL line on exit (with elapsed seconds + which asset kinds
    # were written). That makes `tail -f media_gen.log` a live progress feed
    # so you can see at a glance what's being generated and what failed.
    script = (
        "import os, sys, time, traceback, django\n"
        f"sys.path.insert(0, {str(project_root)!r})\n"
        f"os.chdir({str(project_root)!r})\n"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'djangoscrap.settings')\n"
        "django.setup()\n"
        "from djangoscrap.nft_media import generate_composition_media_assets\n"
        "from djangoscrap.models import Composition\n"
        f"_cid = {int(composition_id)}\n"
        f"_kinds = {kinds!r}\n"
        f"_force = {bool(force)!r}\n"
        "_label = f'comp={_cid} kinds={\",\".join(_kinds)} force={_force}'\n"
        "_t = time.time()\n"
        "print(f'[{time.strftime(\"%H:%M:%S\")}] START {_label}', flush=True)\n"
        "try:\n"
        "    c = Composition.objects.prefetch_related('media_assets').get(id=_cid)\n"
        "    res = generate_composition_media_assets(c, force=_force, kinds=_kinds)\n"
        "    dt = time.time() - _t\n"
        "    written = list(res.keys()) if res else []\n"
        "    print(f'[{time.strftime(\"%H:%M:%S\")}] DONE  {_label} in {dt:.1f}s -> {written}', flush=True)\n"
        "except Exception as e:\n"
        "    dt = time.time() - _t\n"
        "    print(f'[{time.strftime(\"%H:%M:%S\")}] FAIL  {_label} after {dt:.1f}s: {type(e).__name__}: {e}', flush=True)\n"
        "    traceback.print_exc()\n"
    )

    log_path = str(project_root / "media_gen.log")
    try:
        with open(log_path, "ab") as logf:
            subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=logf,
                stderr=logf,
                start_new_session=True,
                close_fds=True,
            )
    except Exception as e:
        return JsonResponse({"error": f"spawn_failed: {type(e).__name__}: {e}"}, status=500)

    return JsonResponse({"queued": True, "composition_id": composition_id, "kinds": kinds, "force": force})


def composition_media_status(request, composition_id):
    comp = get_object_or_404(Composition, id=composition_id)
    assets = {a.kind: a for a in comp.media_assets.all()}
    result = {}
    for kind, label in [("poster", "poster"), ("preview_10s", "clip")]:
        asset = assets.get(kind)
        if asset:
            result[label] = {"status": asset.status, "error": asset.error_message or ""}
        else:
            result[label] = {"status": "none"}
    return JsonResponse({"composition_id": composition_id, "assets": result})


__all__ = [n for n in list(vars().keys()) if not n.startswith('__')]
