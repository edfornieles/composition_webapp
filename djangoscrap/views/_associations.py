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


def _associations_archive_info(composition: Composition) -> tuple[str, Path | None]:
    assoc = {}
    if isinstance(composition.filter_settings, dict):
        raw = composition.filter_settings.get("associations") or {}
        if isinstance(raw, dict):
            assoc = raw
    source_name = str(assoc.get("chain_name") or "").strip()
    if not source_name:
        slug = _composition_slug_from_url(composition.url or "")
        source_name = _associations_chain_dir_name(slug) if slug else ""
    if not source_name:
        legacy_source = str(assoc.get("archive_source") or "").strip()
        if not legacy_source and composition.background_sources:
            legacy_source = str(composition.background_sources[0] or "").strip()
        if legacy_source:
            legacy_dir = (LOCAL_SOURCES_ROOT / legacy_source).resolve()
            legacy_root = LOCAL_SOURCES_ROOT.resolve()
            if legacy_root == legacy_dir or legacy_root in legacy_dir.parents:
                return legacy_source, legacy_dir
        return "", None
    source_dir = (ASSOCIATIONS_CHAINS_ROOT / source_name).resolve()
    root = ASSOCIATIONS_CHAINS_ROOT.resolve()
    if root != source_dir and root not in source_dir.parents:
        return source_name, None
    return source_name, source_dir



def _composition_slug_from_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    path = (parsed.path or "").strip("/")
    if not path:
        return ""
    return path.split("/")[-1]


@login_required

def aggregation_library(request):
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        try:
            composition_id = int(request.POST.get("composition_id") or 0)
        except (TypeError, ValueError):
            composition_id = 0
        composition = Composition.objects.filter(id=composition_id, type__iexact="associations").first() if composition_id else None
        if not composition:
            messages.error(request, "Associations chain not found.")
            return redirect("aggregation_library")

        source_name, source_dir = _associations_archive_info(composition)
        if action == "delete_chain":
            if source_dir and source_dir.exists() and source_dir.is_dir():
                resolved = source_dir.resolve()
                allowed_roots = [ASSOCIATIONS_CHAINS_ROOT.resolve(), LOCAL_SOURCES_ROOT.resolve()]
                if not any(root in resolved.parents for root in allowed_roots):
                    messages.error(request, "Refusing to delete unexpected chain folder.")
                    return redirect("aggregation_library")
                try:
                    shutil.rmtree(source_dir)
                except Exception:
                    messages.error(request, "Could not delete chain folder from disk.")
                    return redirect("aggregation_library")
            composition.delete()
            messages.success(request, "Association chain deleted.")
            return redirect("aggregation_library")

        if action == "delete_image":
            file_name = (request.POST.get("file_name") or "").strip()
            if not source_dir or not source_dir.exists() or not file_name:
                messages.error(request, "Image could not be resolved.")
                return redirect("aggregation_library")
            target = _resolve_source_media_file(source_dir, file_name)
            if not target or not target.exists():
                messages.error(request, "Image not found.")
                return redirect("aggregation_library")
            try:
                target.unlink()
            except Exception:
                messages.error(request, "Could not delete image file.")
                return redirect("aggregation_library")

            if isinstance(composition.filter_settings, dict):
                fs = dict(composition.filter_settings)
            else:
                fs = {}
            assoc = fs.get("associations")
            if not isinstance(assoc, dict):
                assoc = {}
            saved_map = assoc.get("saved_by_url")
            if not isinstance(saved_map, dict):
                saved_map = {}
            pruned = {k: v for k, v in saved_map.items() if str(v).strip() != file_name}
            assoc["saved_by_url"] = pruned
            seed_url = str(assoc.get("seed_url") or "").strip()
            if source_name and seed_url.endswith(f"/{quote(file_name, safe='')}"):
                assoc["seed_url"] = ""
            fs["associations"] = assoc
            composition.filter_settings = fs
            composition.save(update_fields=["filter_settings"])
            messages.success(request, f"Deleted image {file_name}.")
            return redirect("aggregation_library")

        messages.error(request, "Unknown action.")
        return redirect("aggregation_library")

    chains = Composition.objects.filter(type__iexact="associations").order_by("-created_at")
    rows = []
    for comp in chains:
        source_name, source_dir = _associations_archive_info(comp)
        files: list[Path] = []
        if source_dir and source_dir.exists() and source_dir.is_dir():
            files = [p for p in _local_source_dir_media_files(source_dir) if p.suffix.lower() in LOCAL_SOURCE_IMAGE_EXTS]
            files.sort(key=lambda p: p.name.lower())
        image_rows = [
            {
                "name": p.name,
                "url": (
                    _association_chain_media_url(source_name, p.name)
                    if source_dir and ASSOCIATIONS_CHAINS_ROOT.resolve() in source_dir.resolve().parents
                    else f"/source-media/{quote(source_name, safe='')}/{quote(p.name, safe='')}"
                ),
                "size_kb": max(1, int((p.stat().st_size or 0) / 1024)),
            }
            for p in files
        ]
        rows.append(
            {
                "composition": comp,
                "slug": _composition_slug_from_url(comp.url or ""),
                "source_name": source_name,
                "image_count": len(image_rows),
                "images": image_rows,
            }
        )
    return render(request, "admin/aggregation_library.html", {"chains": rows})



@login_required

def associations_studio(request):
    matching_buckets = [source.name for source in get_local_sources()]
    if request.method == "POST":
        requested_slug = (request.POST.get("url_slug") or "").strip().strip("/")
        page_slug = slugify(requested_slug)
        if not page_slug:
            messages.error(request, "Please enter a valid URL name.")
            return redirect("associations_studio")
        if _find_composition_by_slug(page_slug):
            messages.error(request, f"URL name '{page_slug}' already exists. Choose another.")
            return redirect("associations_studio")

        associations_source_name = _associations_chain_dir_name(page_slug)
        associations_source_dir = (ASSOCIATIONS_CHAINS_ROOT / associations_source_name).resolve()
        associations_source_dir.mkdir(parents=True, exist_ok=True)

        random_seed = (request.POST.get("seed_mode") or "").strip().lower() == "random"
        random_seed_source = (request.POST.get("random_seed_source") or "").strip()
        seed_upload = request.FILES.get("seed_upload")
        seed_raw = None
        # If a file is uploaded, always use it as the seed.
        # Random mode only applies when no upload is provided.
        if seed_upload:
            try:
                seed_raw = seed_upload.read()
            except Exception:
                seed_raw = None
        elif random_seed:
            picked = _pick_random_seed_image(random_seed_source or None)
            if picked:
                _, seed_raw = picked

        if not seed_raw:
            messages.error(request, "Please upload a seed image or choose random seed.")
            return redirect("associations_studio")

        seed_filename = "assoc_000001.jpg"
        seed_path = associations_source_dir / seed_filename
        if not _save_square_image_bytes(seed_raw, seed_path):
            messages.error(request, "Could not process seed image. Please try another file.")
            return redirect("associations_studio")
        seed_url = _association_chain_media_url(associations_source_name, seed_filename)

        tempo_sec = max(1.0, min(30.0, float(request.POST.get("tempo_sec", 6) or 6)))
        inventiveness = max(0.0, min(1.0, float(request.POST.get("inventiveness", 55) or 55) / 100.0))
        personal_influence = max(0.0, min(1.0, float(request.POST.get("personal_influence", 30) or 30) / 100.0))
        use_web = (request.POST.get("use_web") or "on").lower() in {"on", "1", "true", "yes"}
        display_name = (request.POST.get("name") or "").strip() or f"associations_{page_slug}"
        base_url = (request.POST.get("base_url") or f"{request.scheme}://{request.get_host()}").rstrip("/")
        full_url = f"{base_url}/{page_slug}"

        filter_settings = {
            "associations": {
                "seed_url": seed_url,
                "tempo_sec": tempo_sec,
                "inventiveness": inventiveness,
                "personal_influence": personal_influence,
                "use_web": use_web,
                "chain_name": associations_source_name,
                "saved_by_url": {},
            }
        }

        composition = Composition.objects.create(
            name=display_name,
            type="associations",
            url=full_url,
            page_url="",
            status="pending",
            brightness=50,
            saturation=50,
            opacity=100,
            transition="none",
            playback_speed=1.0,
            source_playback_mode="chronological",
            mood_rating="mid",
            filter_preset="none",
            filter_intensity=40,
            filter_param_1=0.5,
            filter_param_2=0.5,
            filter_param_3=0.5,
            filter_settings=filter_settings,
            background_sources=[],
            foreground_sources=[],
            overlay_sources=[],
        )
        composition.background_video = ""
        composition.foreground_video = ""
        composition.save(update_fields=["background_video", "foreground_video"])

        messages.success(request, f"Associations composition created at /{page_slug}")
        return redirect(full_url)

    return render(
        request,
        "admin/associations_studio.html",
        {
            "buckets": matching_buckets,
            "default_base_url": f"{request.scheme}://{request.get_host()}",
        },
    )



def _sorted_training_images(training_dir: Path) -> list[Path]:
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
    files = []
    if not training_dir.exists() or not training_dir.is_dir():
        return files
    for p in training_dir.iterdir():
        if not p.is_file():
            continue
        if p.name.startswith(".") or p.name.startswith("._"):
            continue
        if p.suffix.lower() not in image_exts:
            continue
        files.append(p)

    def sort_key(path: Path):
        m = re.search(r"(\d+)", path.stem)
        return (0, int(m.group(1))) if m else (1, path.name.lower())

    files.sort(key=sort_key)
    return files



def _resolve_training_seed_file(files: list[Path], requested_seed: str) -> Path | None:
    if not files:
        return None
    raw = str(requested_seed or "").strip()
    if not raw:
        return files[0]

    by_name = {p.name: p for p in files}
    if raw in by_name:
        return by_name[raw]

    # Allow users to type just the numeric index, e.g. "3".
    if raw.isdigit():
        num = str(int(raw))
        exact_stem = [p for p in files if p.stem == num]
        if exact_stem:
            return exact_stem[0]
        prefix_stem = [p for p in files if p.stem.startswith(f"{num}.")]
        if prefix_stem:
            return prefix_stem[0]
    return files[0]



def _cos01(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return 0.5
    if len(a) != len(b):
        return 0.5
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    an = math.sqrt(sum(float(x) * float(x) for x in a))
    bn = math.sqrt(sum(float(y) * float(y) for y in b))
    if an <= 1e-8 or bn <= 1e-8:
        return 0.5
    cos = max(-1.0, min(1.0, dot / (an * bn)))
    return (cos + 1.0) / 2.0



def _img_thumb_data_url(path: Path, max_side: int = 300) -> str:
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side), RESAMPLING_METHOD)
            output = io.BytesIO()
            im.save(output, format="JPEG", quality=88)
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        return ""



def _random_web_seed_image_url() -> str:
    params = {
        "action": "query",
        "format": "json",
        "origin": "*",
        "generator": "random",
        "grnnamespace": 6,
        "grnlimit": 20,
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "iiurlwidth": 1800,
    }
    url = f"https://commons.wikimedia.org/w/api.php?{urlencode(params)}"
    try:
        req = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with urlopen(req, timeout=14) as resp:
            body = resp.read()
        payload = json.loads(body.decode("utf-8", errors="ignore"))
        pages = ((payload or {}).get("query") or {}).get("pages") or {}
        candidates: list[str] = []
        for page in pages.values():
            info = (page or {}).get("imageinfo") or []
            if not info:
                continue
            entry = info[0] or {}
            mime = str(entry.get("mime") or "").lower()
            if not mime.startswith("image/") or mime.endswith("svg+xml"):
                continue
            raw_url = str(entry.get("url") or entry.get("thumburl") or "").strip()
            if not raw_url:
                continue
            lower = raw_url.lower()
            if not any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"]):
                continue
            candidates.append(raw_url)
        if not candidates:
            return ""
        return random.choice(candidates)
    except Exception:
        return ""


@login_required

def associations_compare(request):
    params = request.POST if request.method == "POST" else request.GET
    requested_seed = (params.get("seed") or "").strip()
    slug_seed = (params.get("slug") or "").strip().strip("/")
    retrieval_mode = (params.get("retrieval_mode") or "balanced").strip().lower()
    if retrieval_mode not in {"strict", "balanced", "generated", "hybrid"}:
        retrieval_mode = "balanced"
    generate_requested = (params.get("generate") or "").strip().lower() in {"1", "true", "yes", "on"}
    chain_max = 60
    chain_length = parse_positive_int(params.get("chain_length"), 8)
    chain_length = max(1, min(chain_max, chain_length))
    inventiveness = parse_slider_0_1(params.get("inventiveness"), 0.5)
    personal_influence = parse_slider_0_1(params.get("personal_influence"), 0.5)

    source_slug = slug_seed
    composition = _find_composition_by_slug(slug_seed) if slug_seed else None
    seed_from_composition = ""
    if composition and isinstance(composition.filter_settings, dict):
        assoc = composition.filter_settings.get("associations") or {}
        if isinstance(assoc, dict):
            seed_from_composition = str(assoc.get("seed_url") or "").strip()

    def _abs_url(raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        if text.startswith("http://") or text.startswith("https://"):
            return text
        if text.startswith("/"):
            return request.build_absolute_uri(text)
        return text

    if request.method == "POST":
        uploaded = request.FILES.get("seed_upload")
        if uploaded:
            ctype = str(getattr(uploaded, "content_type", "") or "").lower()
            ext = Path(str(getattr(uploaded, "name", "") or "upload")).suffix.lower()
            if ctype.startswith("image/") or ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}:
                rel_path = default_storage.save(
                    f"associations_seed_uploads/{timezone.now().strftime('%Y%m%d_%H%M%S')}_{Path(uploaded.name).name}",
                    uploaded,
                )
                media_rel = f"{settings.MEDIA_URL.rstrip('/')}/{str(rel_path).lstrip('/')}"
                requested_seed = request.build_absolute_uri(media_rel)
                generate_requested = True
            else:
                messages.error(request, "Uploaded seed must be an image file.")

    seed_start_url = _abs_url(requested_seed) or _abs_url(seed_from_composition)
    if not seed_start_url:
        seed_start_url = _random_web_seed_image_url()
    if not seed_start_url:
        messages.error(request, "Could not fetch a random seed image right now. Please paste a seed URL.")
        return redirect("associations_studio")

    seed_path_name = Path(unquote(urlparse(seed_start_url).path)).name
    seed_label = seed_path_name or requested_seed or "seed image"
    seed_choices: list[str] = []
    generated_start_idx = 0
    combined_rows = [{"idx": i + 1, "generated": None} for i in range(chain_length)]

    context = {
        "seed_name": seed_label,
        "source_slug": source_slug,
        "generate_requested": generate_requested,
        "chain_length": chain_length,
        "chain_max": chain_max,
        "inventiveness": int(round(inventiveness * 10)),
        "personal_influence": int(round(personal_influence * 10)),
        "training_rows": [],
        "combined_rows": combined_rows,
        "overlap_count": 0,
        "training_adj_avg": 0.0,
        "generated_adj_avg": 0.0,
        "feel_note": "Choose winners row by row to build a human-supervised chain from a single seed.",
        "seed_choices": seed_choices,
        "generated_start_idx": generated_start_idx,
        "seed_start_url": seed_start_url,
        "retrieval_mode": retrieval_mode,
    }
    return render(request, "admin/associations_compare.html", context)



def _host_resolves_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False



def associations_image_proxy(request):
    raw = str(request.GET.get("url") or "").strip()
    if not raw:
        return JsonResponse({"ok": False, "error": "missing url"}, status=400)
    attempts: list[str] = [raw]
    # Some upstream candidate URLs arrive percent-encoded more than once.
    decoded = unquote(raw)
    if decoded and decoded != raw:
        attempts.append(decoded)
    if "%25" in raw:
        decoded_twice = unquote(decoded)
        if decoded_twice and decoded_twice not in attempts:
            attempts.append(decoded_twice)

    for candidate_url in attempts:
        parsed = urlparse(candidate_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        host = parsed.hostname
        if not host or _host_resolves_private(host):
            continue
        try:
            req = Request(candidate_url, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urlopen(req, timeout=14) as resp:
                ctype = str(resp.headers.get("Content-Type") or "").lower()
                body = resp.read(5 * 1024 * 1024 + 1)
            if len(body) > 5 * 1024 * 1024:
                return JsonResponse({"ok": False, "error": "image too large"}, status=413)
            if ctype and not ctype.startswith("image/"):
                continue
            if not ctype:
                ctype = "image/jpeg"
            return HttpResponse(body, content_type=ctype)
        except Exception:
            continue
    return JsonResponse({"ok": False, "error": "proxy fetch failed"}, status=502)



def associations_next_asset(request):
    page_slug = (request.GET.get("slug") or "").strip().strip("/")
    current_url = (request.GET.get("current_url") or "").strip()
    seed_url = (request.GET.get("seed_url") or "").strip()
    session_id = (request.GET.get("session_id") or "").strip()
    inventiveness = parse_slider_0_1(request.GET.get("inventiveness"), 0.5)
    history_raw = request.GET.get("history", "[]")
    history_list = []
    try:
        loaded = json.loads(history_raw) if history_raw else []
        if isinstance(loaded, list):
            history_list = [str(x).strip() for x in loaded if str(x).strip()]
    except (TypeError, ValueError):
        history_list = []

    composition = _find_composition_by_slug(page_slug) if page_slug else None
    local_candidates = []
    use_web = True
    personal_influence = 0.3
    if composition:
        chain_assets = collect_association_chain_assets(composition, page_slug)
        source_assets = collect_source_assets(
            (composition.background_sources or []) + (composition.foreground_sources or []),
            landscape_only=bool(getattr(composition, "landscape_only", False)),
        )
        local_candidates = (chain_assets + source_assets)[:120]

    seed_fallback = ""
    if composition and isinstance(composition.filter_settings, dict):
        assoc = composition.filter_settings.get("associations") or {}
        if isinstance(assoc, dict):
            seed_fallback = str(assoc.get("seed_url") or "").strip()
            if not seed_url:
                seed_url = seed_fallback
            inventiveness = parse_slider_0_1(assoc.get("inventiveness"), inventiveness)
            personal_influence = parse_slider_0_1(assoc.get("personal_influence"), 0.5)
            use_web = assoc.get("use_web") is not False

    effective_current = current_url or seed_url or seed_fallback
    if not effective_current and local_candidates:
        effective_current = str((local_candidates[0] or {}).get("url") or "")
    if not effective_current:
        return JsonResponse({"ok": False, "error": "missing seed/current image"}, status=400)

    api_key = (getattr(settings, "OPENAI_API_KEY", None) or "").strip()
    picked = pick_association_candidate(
        seed_url=seed_url or effective_current,
        current_url=effective_current,
        history_urls=history_list,
        local_candidates=local_candidates,
        inventiveness=inventiveness,
        api_key=api_key if use_web else "",
        personal_influence=personal_influence,
    )
    if not picked:
        return JsonResponse({"ok": False, "error": "no candidate"}, status=404)
    return JsonResponse({"ok": True, "asset": picked})



def associations_candidates(request):
    page_slug = (request.GET.get("slug") or "").strip().strip("/")
    current_url = (request.GET.get("current_url") or "").strip()
    seed_url = (request.GET.get("seed_url") or "").strip()
    session_id = (request.GET.get("session_id") or "").strip()
    inventiveness = parse_slider_0_1(request.GET.get("inventiveness"), 0.5)
    personal_influence = parse_slider_0_1(request.GET.get("personal_influence"), 0.5)
    count = max(1, min(8, parse_positive_int(request.GET.get("count"), 3)))
    retrieval_mode = (request.GET.get("retrieval_mode") or "").strip().lower()
    generation_mode = "retrieve"
    if retrieval_mode == "strict":
        enforce_leap = True
        leap_strength = 0.8
        allow_fallback = False
    elif retrieval_mode == "balanced":
        enforce_leap = True
        leap_strength = 0.58
        allow_fallback = True
    elif retrieval_mode == "generated":
        enforce_leap = True
        leap_strength = 0.65
        allow_fallback = True
        generation_mode = "generated"
    elif retrieval_mode == "hybrid":
        enforce_leap = True
        leap_strength = 0.62
        allow_fallback = True
        generation_mode = "hybrid"
    else:
        enforce_leap = (request.GET.get("enforce_leap") or "1").strip().lower() in {"1", "true", "yes", "on"}
        leap_strength = max(0.0, min(1.0, parse_float_in_range(request.GET.get("leap_strength"), 0.8)))
        allow_fallback = (request.GET.get("allow_fallback") or "0").strip().lower() in {"1", "true", "yes", "on"}

    def _abs_url(raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        if text.startswith("http://") or text.startswith("https://"):
            return text
        return request.build_absolute_uri(text)
    history_raw = request.GET.get("history", "[]")
    history_list: list[str] = []
    try:
        loaded = json.loads(history_raw) if history_raw else []
        if isinstance(loaded, list):
            history_list = [str(x).strip() for x in loaded if str(x).strip()]
    except (TypeError, ValueError):
        history_list = []

    composition = _find_composition_by_slug(page_slug) if page_slug else None
    local_candidates = []
    use_web = True
    if composition:
        chain_assets = collect_association_chain_assets(composition, page_slug)
        source_assets = collect_source_assets(
            (composition.background_sources or []) + (composition.foreground_sources or []),
            landscape_only=bool(getattr(composition, "landscape_only", False)),
        )
        local_candidates = (chain_assets + source_assets)[:120]

    seed_fallback = ""
    if composition and isinstance(composition.filter_settings, dict):
        assoc = composition.filter_settings.get("associations") or {}
        if isinstance(assoc, dict):
            seed_fallback = str(assoc.get("seed_url") or "").strip()
            if not seed_url:
                seed_url = seed_fallback
            inventiveness = parse_slider_0_1(assoc.get("inventiveness"), inventiveness)
            personal_influence = parse_slider_0_1(assoc.get("personal_influence"), personal_influence)
            use_web = assoc.get("use_web") is not False

    current_url = _abs_url(current_url)
    seed_url = _abs_url(seed_url)
    seed_fallback = _abs_url(seed_fallback)
    effective_current = current_url or seed_url or seed_fallback
    if not effective_current and local_candidates:
        effective_current = str((local_candidates[0] or {}).get("url") or "")
    if not effective_current:
        return JsonResponse({"ok": False, "error": "missing seed/current image"}, status=400)

    api_key = (getattr(settings, "OPENAI_API_KEY", None) or "").strip()
    result = pick_association_candidates(
        seed_url=seed_url or effective_current,
        current_url=effective_current,
        history_urls=history_list,
        local_candidates=local_candidates,
        inventiveness=inventiveness,
        api_key=api_key if use_web else "",
        personal_influence=personal_influence,
        count=count,
        enforce_leap=enforce_leap,
        leap_strength=leap_strength,
        generation_mode=generation_mode,
    )
    # Strict mode can over-prune on leap gating; retry with controlled relaxation.
    if retrieval_mode == "strict":
        _diag = (result or {}).get("diagnostics") if isinstance(result, dict) else {}
        _cands = (result or {}).get("candidates") if isinstance(result, dict) else []
        if (not _cands) and isinstance(_diag, dict) and int(_diag.get("rejected_by_leap_band") or 0) > 0:
            for relaxed_strength in (0.68, 0.56):
                retry = pick_association_candidates(
                    seed_url=seed_url or effective_current,
                    current_url=effective_current,
                    history_urls=history_list,
                    local_candidates=local_candidates,
                    inventiveness=inventiveness,
                    api_key=api_key if use_web else "",
                    personal_influence=personal_influence,
                    count=count,
                    enforce_leap=True,
                    leap_strength=relaxed_strength,
                    generation_mode=generation_mode,
                )
                retry_candidates = retry.get("candidates") if isinstance(retry, dict) else []
                if retry_candidates:
                    retry_diag = (retry or {}).get("diagnostics") if isinstance(retry, dict) else {}
                    if isinstance(retry_diag, dict):
                        retry_diag["strict_relaxed_from"] = leap_strength
                        retry_diag["strict_relaxed_to"] = relaxed_strength
                    result = retry
                    break
    candidates = result.get("candidates") if isinstance(result, dict) else []
    if not candidates:
        diagnostics = (result or {}).get("diagnostics") if isinstance(result, dict) else {}
        reason = "No candidates passed current constraints."
        if isinstance(diagnostics, dict):
            if int(diagnostics.get("rejected_by_seed_subject") or 0) > 0:
                reason = "No candidates passed the seed-subject gate. Try hybrid/generated mode."
            elif int(diagnostics.get("rejected_by_anchor") or 0) > 0:
                reason = "No candidates passed the seed-anchor gate. Try balanced or generated mode."
            elif int(diagnostics.get("rejected_by_leap_band") or 0) > 0:
                reason = "No candidates passed the leap-distance gate. Try balanced mode."
        return JsonResponse({"ok": False, "error": "no candidates", "reason": reason, "diagnostics": diagnostics}, status=404)
    return JsonResponse(
        {
            "ok": True,
            "query": str((result or {}).get("query") or ""),
            "concepts": (result or {}).get("concepts") or [],
            "anchors": (result or {}).get("anchors") or [],
            "patterns": (result or {}).get("patterns") or [],
            "pattern_colors": (result or {}).get("pattern_colors") or [],
            "seed_subject_terms": (result or {}).get("seed_subject_terms") or [],
            "directions": (result or {}).get("directions") or [],
            "diagnostics": (result or {}).get("diagnostics") or {},
            "candidates": candidates,
        }
    )


@csrf_exempt

def associations_select_winner(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    slug = str(payload.get("slug") or "").strip().strip("/")
    current_url = str(payload.get("current_url") or "").strip()
    chosen_url = str(payload.get("chosen_url") or "").strip()
    rejected_urls = payload.get("rejected_urls") or []
    if not isinstance(rejected_urls, list):
        rejected_urls = []
    context = {
        "slug": slug,
        "source": str(payload.get("source") or "compare_ui").strip(),
        "reason": str(payload.get("reason") or "").strip(),
    }
    try:
        result = record_association_winner_choice(
            current_url=current_url,
            chosen_url=chosen_url,
            rejected_urls=[str(x).strip() for x in rejected_urls if str(x).strip()],
            context=context,
        )
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    except Exception:
        return JsonResponse({"ok": False, "error": "Could not record winner selection"}, status=500)
    return JsonResponse(result)


@csrf_exempt
@login_required

def associations_bootstrap_training(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    negatives_per_step = max(1, min(8, parse_positive_int(payload.get("negatives_per_step"), 3)))
    max_pairs = max(100, min(12000, parse_positive_int(payload.get("max_pairs"), 2400)))
    try:
        result = bootstrap_feedback_from_training(
            training_folder=str(ASSOCIATIONS_TRAINING_SET_DIR),
            negatives_per_step=negatives_per_step,
            max_pairs=max_pairs,
        )
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    except Exception:
        return JsonResponse({"ok": False, "error": "bootstrap failed"}, status=500)
    return JsonResponse(result)


@login_required

def associations_feedback_stats(request):
    from .associations_service import _FEEDBACK_LOG_PATH, _FEEDBACK_MODEL_PATH

    log_path = _FEEDBACK_LOG_PATH
    model_path = _FEEDBACK_MODEL_PATH
    rows: list[dict[str, Any]] = []
    if log_path.exists():
        try:
            for line in log_path.read_text(encoding="utf-8").splitlines():
                raw = line.strip()
                if not raw:
                    continue
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    rows.append(payload)
        except Exception:
            rows = []
    total = len(rows)
    good = sum(1 for r in rows if str(r.get("label") or "").strip().lower() == "good")
    bad = sum(1 for r in rows if str(r.get("label") or "").strip().lower() in {"bad", "too_close", "too_random"})
    recent = rows[-40:]
    recent_good = sum(1 for r in recent if str(r.get("label") or "").strip().lower() == "good")
    recent_total = len(recent)
    recent_acceptance = (recent_good / recent_total) if recent_total else 0.0

    model_payload: dict[str, Any] = {}
    if model_path.exists():
        try:
            loaded = json.loads(model_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                model_payload = loaded
        except Exception:
            model_payload = {}

    return JsonResponse(
        {
            "ok": True,
            "total_feedback": total,
            "positive_feedback": good,
            "negative_feedback": bad,
            "recent_total": recent_total,
            "recent_positive": recent_good,
            "recent_acceptance": round(float(recent_acceptance), 4),
            "model_active": bool(model_payload),
            "model_embedding_type": str(model_payload.get("embedding_type") or ""),
            "model_feedback_count": int(model_payload.get("feedback_count") or 0),
        }
    )


@csrf_exempt

def associations_feedback(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    label = str(payload.get("label") or "").strip().lower()
    current_url = str(payload.get("current_url") or "").strip()
    candidate_url = str(payload.get("candidate_url") or "").strip()
    slug = str(payload.get("slug") or "").strip().strip("/")
    context = {
        "slug": slug,
        "source": str(payload.get("source") or "").strip(),
    }
    try:
        result = record_association_feedback(
            current_url=current_url,
            candidate_url=candidate_url,
            label=label,
            context=context,
        )
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    except Exception:
        return JsonResponse({"ok": False, "error": "Could not record feedback"}, status=500)
    return JsonResponse(result)


@csrf_exempt

def associations_store_asset(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    page_slug = str(payload.get("slug") or "").strip().strip("/")
    image_url = str(payload.get("image_url") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    if not page_slug or not image_url:
        return JsonResponse({"ok": False, "error": "slug and image_url required"}, status=400)
    composition = _find_composition_by_slug(page_slug)
    if not composition:
        return JsonResponse({"ok": False, "error": "composition not found"}, status=404)

    source_name, source_dir = _ensure_associations_archive_source(composition, page_slug)
    archive_prefix = f"/association-chain-media/{quote(source_name, safe='')}/"
    if image_url.startswith(archive_prefix):
        existing_name = unquote(image_url.split("/", 4)[-1]) if "/" in image_url else ""
        existing_path = (source_dir / existing_name).resolve()
        if existing_path.exists():
            local_url = _association_chain_media_url(source_name, existing_path.name)
            return JsonResponse({"ok": True, "asset": {"kind": "image", "url": local_url, "preview_url": local_url, "name": existing_path.name, "source": "archive"}})

    assoc = {}
    fs = composition.filter_settings if isinstance(composition.filter_settings, dict) else {}
    if isinstance(fs.get("associations"), dict):
        assoc = fs.get("associations") or {}
    saved_map = assoc.get("saved_by_url")
    if not isinstance(saved_map, dict):
        saved_map = {}
    map_key = f"{session_id}::{image_url}" if session_id else image_url
    already = str(saved_map.get(map_key) or "").strip()
    if already:
        existing = (source_dir / already).resolve()
        if existing.exists():
            local_url = _association_chain_media_url(source_name, existing.name)
            return JsonResponse({"ok": True, "asset": {"kind": "image", "url": local_url, "preview_url": local_url, "name": existing.name, "source": "archive"}})

    raw = _parse_source_media_url_to_bytes(image_url)
    if not raw:
        return JsonResponse({"ok": False, "error": "unable to fetch image"}, status=400)

    filename = _next_association_filename(source_dir)
    target = (source_dir / filename).resolve()
    if not _save_square_image_bytes(raw, target):
        return JsonResponse({"ok": False, "error": "unable to process image"}, status=400)

    saved_map[map_key] = filename
    if len(saved_map) > 2000:
        for k in list(saved_map.keys())[: len(saved_map) - 2000]:
            saved_map.pop(k, None)
    assoc["saved_by_url"] = saved_map
    assoc["archive_source"] = source_name
    fs["associations"] = assoc
    composition.filter_settings = fs
    composition.save(update_fields=["filter_settings"])

    local_url = _association_chain_media_url(source_name, filename)
    return JsonResponse({"ok": True, "asset": {"kind": "image", "url": local_url, "preview_url": local_url, "name": filename, "source": "archive"}})

from ._utils import create_video_ffmpeg, parse_float_in_range, parse_positive_int, parse_slider_0_1
from asgiref.sync import async_to_sync
import asyncio
from ._source_utils import collect_association_chain_assets, _associations_chain_dir_name
from ._compositions import _find_composition_by_slug, _purge_composition_preview_cache
__all__ = [n for n in list(vars().keys()) if not n.startswith('__')]
