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


def audio_source_map_json(request):
    if request.method != "GET":
        return JsonResponse({"ok": False, "error": "GET required."}, status=405)
    return JsonResponse({"ok": True, "audio_source_map": collect_audio_source_files()})


@require_POST

def delete_noise_studio_track(request):
    track = Path((request.POST.get("track") or "").strip()).name
    if not track:
        return JsonResponse({"ok": False, "error": "Track is required."}, status=400)
    source_dir = (LOCAL_AUDIO_SOURCES_ROOT / "noise_studio").resolve()
    root_dir = LOCAL_AUDIO_SOURCES_ROOT.resolve()
    if root_dir not in source_dir.parents and source_dir != root_dir:
        return JsonResponse({"ok": False, "error": "Invalid source path."}, status=400)
    target = (source_dir / track).resolve()
    if source_dir not in target.parents or not target.exists() or not target.is_file():
        return JsonResponse({"ok": False, "error": "Track not found."}, status=404)
    try:
        target.unlink()
    except OSError as exc:
        return JsonResponse({"ok": False, "error": f"Could not delete track: {exc}"}, status=500)
    return JsonResponse(
        {
            "ok": True,
            "deleted_track": track,
            "audio_source_map": collect_audio_source_files(),
        }
    )



def audio_source_library(request):
    source_dirs = get_local_audio_sources()
    sources = []
    for source_dir in source_dirs:
        files = [f for f in sorted(source_dir.iterdir()) if f.is_file() and not f.name.startswith(".")]
        sources.append({
            "name": source_dir.name,
            "file_count": len(files),
            "updated_at": datetime.fromtimestamp(source_dir.stat().st_mtime),
        })
    return render(request, "admin/audio-source-library.html", {"sources": sources})



def create_audio_source(request):
    if request.method != "POST":
        return redirect("new-audio-source")

    source_name_raw = (request.POST.get("name") or "").strip()
    append_existing = request.POST.get("append_existing") == "on"
    uploaded_files = request.FILES.getlist("files")

    if not source_name_raw:
        messages.error(request, "Audio source name is required.")
        return redirect("new-audio-source")

    source_name = source_name_raw.lower().replace(" ", "-")
    source_path = LOCAL_AUDIO_SOURCES_ROOT / source_name
    source_exists = source_path.exists()
    if source_exists and not append_existing:
        messages.error(request, f"Audio source '{source_name}' exists. Enable append mode to add more files.")
        return redirect("new-audio-source")
    source_path.mkdir(parents=True, exist_ok=True)

    if not uploaded_files:
        messages.success(request, f"Audio source '{source_name}' is ready.")
        return redirect("audio_source_contents", source_name=source_name)

    allowed_exts = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}
    uploaded_count = 0
    skipped_count = 0
    for file in uploaded_files:
        original_name = Path(file.name).name
        ext = Path(original_name).suffix.lower()
        if ext not in allowed_exts:
            skipped_count += 1
            continue
        candidate_path = source_path / original_name
        if candidate_path.exists():
            stem = Path(original_name).stem
            counter = 1
            while True:
                renamed = source_path / f"{stem}_{counter}{ext}"
                if not renamed.exists():
                    candidate_path = renamed
                    break
                counter += 1
        try:
            with open(candidate_path, "wb") as destination:
                for chunk in file.chunks():
                    destination.write(chunk)
            uploaded_count += 1
        except Exception:
            skipped_count += 1

    if uploaded_count:
        messages.success(
            request,
            f"Uploaded {uploaded_count} audio file(s) to '{source_name}'."
            + (f" Skipped {skipped_count} file(s)." if skipped_count else ""),
        )
    else:
        messages.error(request, "No valid audio files were uploaded.")
    return redirect("audio_source_contents", source_name=source_name)



def new_audio_source(request):
    return render(request, "admin/new-audio-source.html")



def audio_source_contents(request, source_name):
    source_dir = LOCAL_AUDIO_SOURCES_ROOT / source_name
    if not source_dir.exists():
        messages.error(request, f"Audio source '{source_name}' not found.")
        return redirect("audio-source-library")

    objects = []
    for file in sorted(source_dir.iterdir()):
        if file.is_file() and not file.name.startswith("."):
            objects.append({
                "Key": file.name,
                "Size": file.stat().st_size,
                "LastModified": datetime.fromtimestamp(file.stat().st_mtime),
                "preview_url": f"/audio-source-media/{quote(source_name, safe='')}/{quote(file.name, safe='')}",
            })
    return render(request, "admin/audio_source_contents.html", {"objects": objects, "source_name": source_name})



def audio_source_media_file(request, source_name, file_name):
    source_dir = (LOCAL_AUDIO_SOURCES_ROOT / source_name).resolve()
    target = (source_dir / file_name).resolve()
    if source_dir not in target.parents or not target.exists() or not target.is_file():
        raise Http404("Audio source file not found")
    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(open(target, "rb"), content_type=content_type)



def noise_studio(request):
    """Digital noise / distortion studio: process library audio or uploads, export to noise_studio source."""
    (LOCAL_AUDIO_SOURCES_ROOT / "noise_studio").mkdir(parents=True, exist_ok=True)
    return render(
        request,
        "admin/noise_studio.html",
        {
            "audio_source_map": collect_audio_source_files(),
        },
    )



def noise_studio_save(request):
    if request.method != "POST":
        return redirect("noise-studio")
    upload = request.FILES.get("file")
    if not upload:
        messages.error(request, "No audio file received.")
        return redirect("noise-studio")
    dest_dir = LOCAL_AUDIO_SOURCES_ROOT / "noise_studio"
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Prefer explicit save_name: FormData blob uploads often arrive as "blob"/"noise.wav" in some browsers.
    preferred = (request.POST.get("save_name") or "").strip()
    if preferred:
        base_name = get_valid_filename(preferred) or "noise-export.wav"
    else:
        base_name = get_valid_filename(upload.name) or "noise-export.wav"
    stem, ext = os.path.splitext(base_name)
    ext_l = ext.lower()
    if ext_l not in {".wav", ".webm", ".ogg"}:
        name = f"{stem}.wav" if stem else "noise-export.wav"
    else:
        name = base_name
    overwrite_requested = (request.POST.get("overwrite") or "").strip().lower() in {"1", "true", "yes", "on"}
    target = dest_dir / name
    if target.exists() and not overwrite_requested:
        base_stem = Path(name).stem
        ext2 = Path(name).suffix
        n = 1
        while (dest_dir / f"{base_stem}_{n}{ext2}").exists():
            n += 1
        target = dest_dir / f"{base_stem}_{n}{ext2}"
    try:
        with open(target, "wb") as f:
            for chunk in upload.chunks():
                f.write(chunk)
    except OSError:
        messages.error(request, "Could not save file.")
        return redirect("noise-studio")
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(
            {
                "ok": True,
                "saved_file": target.name,
                "audio_source_map": collect_audio_source_files(),
            }
        )
    messages.success(
        request,
        f"Saved “{target.name}” to Audio Library → noise_studio. Pick it when editing a composition under Audio.",
    )
    return redirect("noise-studio")



def rename_audio_source(request, source_name):
    if request.method != "POST":
        return redirect("audio_source_contents", source_name=source_name)
    new_name = (request.POST.get("new_name") or "").strip()
    if not new_name:
        messages.error(request, "Please provide a new audio source name.")
        return redirect("audio_source_contents", source_name=source_name)
    normalized_new_name = new_name.lower().replace(" ", "-")
    source_dir = (LOCAL_AUDIO_SOURCES_ROOT / source_name).resolve()
    new_source_dir = (LOCAL_AUDIO_SOURCES_ROOT / normalized_new_name).resolve()
    root_dir = LOCAL_AUDIO_SOURCES_ROOT.resolve()
    if root_dir not in source_dir.parents or source_dir == root_dir:
        messages.error(request, "Invalid source path.")
        return redirect("audio-source-library")
    if new_source_dir.exists():
        messages.error(request, f"Audio source '{normalized_new_name}' already exists.")
        return redirect("audio_source_contents", source_name=source_name)
    source_dir.rename(new_source_dir)
    messages.success(request, f"Audio source renamed to '{normalized_new_name}'.")
    return redirect("audio_source_contents", source_name=normalized_new_name)



def delete_audio_sources(request):
    if request.method == "POST":
        selected = request.POST.getlist("sources")
        for source_name in selected:
            source_dir = LOCAL_AUDIO_SOURCES_ROOT / source_name
            if source_dir.exists():
                shutil.rmtree(source_dir)
        messages.success(request, "Selected audio sources deleted successfully.")
    return redirect("audio-source-library")



def delete_audio_source_files(request, source_name):
    if request.method != "POST":
        return redirect("audio_source_contents", source_name=source_name)
    selected_files = request.POST.getlist("selected_files")
    if not selected_files:
        messages.error(request, "No files selected.")
        return redirect("audio_source_contents", source_name=source_name)
    source_dir = (LOCAL_AUDIO_SOURCES_ROOT / source_name).resolve()
    deleted_count = 0
    for file_name in selected_files:
        target_file = (source_dir / file_name).resolve()
        if source_dir in target_file.parents and target_file.exists() and target_file.is_file():
            target_file.unlink()
            deleted_count += 1
    messages.success(request, f"Deleted {deleted_count} audio file(s).")
    return redirect("audio_source_contents", source_name=source_name)



def upload_audio_to_source(request, source_name):
    if request.method != "POST":
        return redirect("audio_source_contents", source_name=source_name)
    source_dir = (LOCAL_AUDIO_SOURCES_ROOT / source_name).resolve()
    if not source_dir.exists():
        messages.error(request, "Audio source not found.")
        return redirect("audio-source-library")
    uploaded_files = request.FILES.getlist("files")
    if not uploaded_files:
        messages.error(request, "No audio files selected.")
        return redirect("audio_source_contents", source_name=source_name)
    allowed_exts = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}
    uploaded_count = 0
    for file in uploaded_files:
        original_name = Path(file.name).name
        ext = Path(original_name).suffix.lower()
        if ext not in allowed_exts:
            continue
        candidate_path = source_dir / original_name
        if candidate_path.exists():
            stem = Path(original_name).stem
            counter = 1
            while True:
                renamed = source_dir / f"{stem}_{counter}{ext}"
                if not renamed.exists():
                    candidate_path = renamed
                    break
                counter += 1
        with open(candidate_path, "wb") as destination:
            for chunk in file.chunks():
                destination.write(chunk)
        uploaded_count += 1
    messages.success(request, f"Uploaded {uploaded_count} audio file(s).")
    return redirect("audio_source_contents", source_name=source_name)



def download_audio_source_files(request, source_name):
    if request.method != "POST":
        return redirect("audio_source_contents", source_name=source_name)
    selected_files = request.POST.getlist("selected_files")
    if not selected_files:
        messages.error(request, "No files selected.")
        return redirect("audio_source_contents", source_name=source_name)
    source_dir = (LOCAL_AUDIO_SOURCES_ROOT / source_name).resolve()
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zip_file:
        for file_name in selected_files:
            target_file = (source_dir / file_name).resolve()
            if source_dir in target_file.parents and target_file.exists() and target_file.is_file():
                zip_file.write(target_file, arcname=target_file.name)
    zip_buffer.seek(0)
    return FileResponse(zip_buffer, as_attachment=True, filename=f"{source_name}_audio_files.zip")

### S3 Bucket Management


from ._utils import create_video_ffmpeg
from django.http import HttpResponseNotAllowed
from django.utils.log import log_response
__all__ = [n for n in list(vars().keys()) if not n.startswith('__')]
