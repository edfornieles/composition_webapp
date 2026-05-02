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
from urllib.parse import quote
from urllib.parse import urlparse
from urllib.parse import unquote
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
from bs4 import BeautifulSoup
from django.shortcuts import render, redirect,get_object_or_404
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth import login, authenticate,logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.core.files.base import ContentFile
from django.conf import settings
from django.core.paginator import Paginator
from django.core.files.storage import default_storage
from django.core.cache import cache
from ..models import (
    Composition,
    CompositionRelease,
    CompositionReleaseFile,
    CompositionNFT,
    MintCollectionSettings,
    Profile,
    S3Bucket,
    Bucket,
    VideoComposition,
    BackgroundImage,
    ForegroundImage,
    Series,
    MonologuePersona,
    AdvancedThoughtScenario,
    WallProfile,
    WallRun,
    WallAssignment,
    WallHeartbeat,
    IngestionBatch,
    IngestionItem,
)
from ..forms import MintCollectionSettingsForm
from ..nft_media import (
    composition_media_assets,
    composition_source_signature,
    generate_composition_media_assets,
    capture_composition_video,
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
from ..ingestion_http import (
    HOST_THROTTLE,
    DEFAULT_USER_AGENT,
    DownloadStats,
    head_probe,
)
from ..ingestion_cookies import (
    resolve_netscape_cookie_file,
    urllib_opener_with_netscape,
    yandex_playwright_cookie_list,
)
from ..ingestion_download import parallel_download_urls, DEFAULT_MAX_WORKERS as _INGEST_DL_WORKERS
from ..associations_service import (
    pick_association_candidate,
    pick_association_candidates,
    record_association_feedback,
    record_association_winner_choice,
    bootstrap_feedback_from_training,
)


_ADAPTERS_BOOTSTRAPPED = False


def _ensure_default_adapters_registered() -> set[str]:
    """Idempotently register adapters whose Python deps are importable.

    Returns the set of adapter names that are live right now. Safe to call
    on every request: the registry deduplicates by name, and each adapter
    is wrapped in its own try/except so a missing optional dep (instagrapi,
    yt-dlp binary, etc.) doesn't break the batch. The adapters themselves
    are still feature-flagged per-call via ``enabled_adapters`` — this only
    guarantees that if a caller asks for ``"instagrapi"`` there is actually
    an InstagrapiAdapter registered to dispatch to.
    """
    global _ADAPTERS_BOOTSTRAPPED
    live: set[str] = set()
    try:
        from djangoscrap.ingestion_adapters import registry as _reg
    except Exception:
        return live

    try:
        existing = {a.name for a in _reg.list_registered()}
    except Exception:
        existing = set()

    if "instagrapi" not in existing:
        try:
            from djangoscrap.ingestion_adapters.instagrapi_adapter import InstagrapiAdapter
            _reg.register(InstagrapiAdapter())
            live.add("instagrapi")
        except Exception:
            pass
    else:
        live.add("instagrapi")

    if "yt-dlp" not in existing:
        try:
            from djangoscrap.ingestion_adapters.ytdlp import get_singleton as _ytdlp_singleton
            _reg.register(_ytdlp_singleton())
            live.add("yt-dlp")
        except Exception:
            pass
    else:
        live.add("yt-dlp")

    _ADAPTERS_BOOTSTRAPPED = True
    return live
from ..video_processing import combine_video_with_audio,create_video_ffmpegNew
import ffmpeg
from django.core.files import File
from django.core.files.temp import NamedTemporaryFile
from PIL import Image, UnidentifiedImageError
from PIL import ImageOps
from ajax_datatable.views import AjaxDatatableView
from ..forms import BucketForm, CompositionForm, MonologuePersonaForm
from botocore.exceptions import ClientError,BotoCoreError,NoCredentialsError
import uuid
from django.http import JsonResponse, HttpResponse, HttpResponseRedirect, FileResponse, Http404
import re
import io
import json
import hashlib
import math
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db import transaction
from django.db.models import Q
from moviepy.editor import ImageSequenceClip ,VideoFileClip, ImageClip, CompositeVideoClip,AudioFileClip, concatenate_videoclips
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
from django.utils.crypto import get_random_string  # ✅ FIXED: Import added
from django.utils.text import get_valid_filename, slugify
from django.core.exceptions import SuspiciousFileOperation
from ..wall_runtime import compute_wall_tick, parse_screen_tile_index

try:
    from PIL import ImageResampling  # For newer Pillow versi ons
    RESAMPLING_METHOD = ImageResampling.LANCZOS
except ImportError:
    RESAMPLING_METHOD = Image.LANCZOS  # Fallback for older versions

from ._constants import *
from ._source_utils import *
from ._ingestion_dedup import *
# Initialize S3 Client
s3 = boto3.client('s3')

def create_video(request):
    if request.method == "POST" and request.FILES:
        background_images = request.FILES.getlist("background")
        foreground_images = request.FILES.getlist("foreground")
        audio_file = request.FILES.get("audio")

        # Create a VideoComposition object
        video_comp = VideoComposition(audio=audio_file)
        video_comp.save()

        # Save background images
        bg_paths = []
        for bg in background_images:
            bg_obj = BackgroundImage(video=video_comp, image=bg)
            bg_obj.save()
            bg_paths.append(bg_obj.image.path)

        # Save foreground images
        fg_paths = []
        for fg in foreground_images:
            fg_obj = ForegroundImage(video=video_comp, image=fg)
            fg_obj.save()
            fg_paths.append(fg_obj.image.path)

        # Define video duration per image
        duration = 0.5  # Set duration per image
        bg_clips = [ImageClip(bg).set_duration(duration).resize((1280, 720)) for bg in bg_paths]
        fg_clips = [ImageClip(fg).set_duration(duration).resize((400, 300)).set_position(("center", "center")) for fg in fg_paths]

        # Concatenate clips
        final_bg_clip = concatenate_videoclips(bg_clips, method="compose")
        final_fg_clip = concatenate_videoclips(fg_clips, method="compose")

        # Merge background and foreground
        final_clip = CompositeVideoClip([final_bg_clip, final_fg_clip])

        # Add audio if available
        if audio_file:
            audio_path = video_comp.audio.path
            audio = AudioFileClip(audio_path).set_duration(final_clip.duration)
            final_clip = final_clip.set_audio(audio)

        # Export final video
        output_video_path = os.path.join(VIDEO_DIR, f"output_{video_comp.id}.mp4")
        final_clip.write_videofile(output_video_path, fps=24)

        # Save the output video path
        video_comp.output_video.name = f"videos/output_{video_comp.id}.mp4"
        video_comp.save()

        return render(request, "admin/upload_file.html", {"video_path": video_comp.output_video.url})

    return render(request, "admin/upload_file.html")




def _unlink_ingestion_staged_file(file_path: str) -> None:
    if not file_path:
        return
    rel = str(file_path).strip().replace("\\", "/")
    if ".." in rel or rel.startswith("/"):
        return
    full = (INGESTION_MEDIA_ROOT / rel).resolve()
    try:
        full.relative_to(INGESTION_MEDIA_ROOT.resolve())
    except ValueError:
        return
    if full.is_file():
        try:
            full.unlink()
        except OSError:
            pass


def _short_reject_reason(note: str) -> str:
    """
    Map the (relatively verbose) dedupe_note the campaign cycle writes to a
    1-2 word label for the grid badge. Keeps the card chrome compact while
    still answering the immediate question "why is this already rejected?".
    The full note is preserved in the row's ``dedupe_note`` and exposed as
    the badge's ``title`` attribute, so hover gives the precise reason.
    """
    n = (note or "").lower()
    if not n:
        return ""
    if n.startswith("vision_qc"):
        return "AI QC"
    if n.startswith("tombstone"):
        return "deleted before"
    if n.startswith("exact duplicate"):
        return "same bytes"
    if "clip cosine" in n or "near-duplicate" in n:
        return "near-duplicate"
    if "campaign filters" in n or "size/orientation" in n:
        return "too small / wrong shape"
    if "hamming" in n:
        return "visual duplicate"
    return "auto-reject"


def _delete_ingestion_items_queryset(qs) -> int:
    """Delete IngestionItem rows and remove staged files under ingestion_items/. Returns row count removed.

    Publish only copies rows that still exist with decision=accepted; deleted rows cannot be copied to a source folder.
    """
    rows = list(qs.values("id", "file_path"))
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    paths = [r["file_path"] for r in rows]
    deleted_count, _ = IngestionItem.objects.filter(id__in=ids).delete()
    for p in paths:
        _unlink_ingestion_staged_file(p)
    return deleted_count


def _ingestion_kind_from_name(name: str) -> str:
    ext = Path(name or "").suffix.lower()
    if ext in {".mp4", ".mov", ".webm"}:
        return "video"
    return "image"


def _save_ingestion_upload(batch: IngestionBatch, upload_obj) -> tuple[str, str, str, dict]:
    ext = Path(upload_obj.name or "").suffix.lower() or ".bin"
    safe_name = f"{uuid.uuid4().hex}{ext}"
    rel_path = Path(str(batch.id)) / safe_name
    abs_path = INGESTION_MEDIA_ROOT / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    with abs_path.open("wb") as out:
        for chunk in upload_obj.chunks():
            out.write(chunk)
    return str(rel_path), _sha256_file(abs_path), _average_hash_hex(abs_path), _image_metadata(abs_path)


def _download_ingestion_url(
    batch: IngestionBatch,
    source_url: str,
    *,
    stats: DownloadStats | None = None,
) -> tuple[str, str, str, dict] | tuple[None, None, None, None]:
    """
    Download a single candidate URL into the batch's ingestion folder.

    Pipeline stages, in order:

    1. **HEAD probe** — ask the server for Content-Type and Content-Length
       before pulling bytes. Cheap hosts reject 404s, text/html error pages,
       and sub-15-KB thumbnail icons here, preventing them from ever landing
       on disk and later showing up as tombstones.
    2. **Host-level throttle** — both the HEAD and the GET happen under the
       :data:`HOST_THROTTLE` gate so we stay polite on busy CDNs (Pinterest,
       Reddit) and stop drawing 403/429 rate limits on big cycles.
    3. **GET + stream-to-disk** — unchanged from the previous implementation.

    If ``stats`` is provided, per-stage counters are accumulated so the
    campaign cycle can emit a single-line summary in ``campaign_last_report``.
    """
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        if stats is not None:
            stats.get_failed += 1
            stats.bump_reason("bad_scheme")
        return None, None, None, None

    host = (parsed.netloc or "").lower()

    # Stage 1: HEAD probe. On positive or on "server doesn't support HEAD",
    # fall through to the GET. On a clear rejection, bail immediately.
    probe = head_probe(source_url)
    if stats is not None:
        stats.probed += 1
    if not probe.ok and not probe.retry_with_get:
        if stats is not None:
            stats.head_blocked += 1
            stats.bump_reason(probe.reason or "head_blocked")
        return None, None, None, None
    if probe.retry_with_get and stats is not None:
        stats.head_unsupported += 1
        stats.bump_reason(probe.reason or "head_unsupported")

    # If HEAD told us the Content-Type authoritatively, reuse it to pick a
    # file extension before we even open the GET response.
    probe_type = (probe.content_type or "").lower()

    # Instagram / Facebook CDNs (cdninstagram.com, fbcdn.net) return 403 when
    # asked for a media URL without a Referer header that looks like it came
    # from instagram.com. yt-dlp sets this automatically; our urllib downloader
    # does not. Without it, every video URL we resolve from a profile would
    # 403 on fetch and land as a tombstone instead of a downloaded file.
    req_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if any(h in host for h in ("cdninstagram.com", "fbcdn.net")):
        req_headers["Referer"] = "https://www.instagram.com/"
    req = Request(source_url, headers=req_headers)
    try:
        with HOST_THROTTLE.hold(host):
            with urlopen(req, timeout=12) as resp:
                content_type = (resp.headers.get("Content-Type") or "").lower() or probe_type
                if "image" not in content_type and "video" not in content_type:
                    if stats is not None:
                        stats.get_failed += 1
                        stats.bump_reason("wrong_type_on_get")
                    return None, None, None, None
                ext = Path(parsed.path).suffix.lower()
                if not ext:
                    ext = ".jpg" if "image" in content_type else ".mp4"
                safe_name = f"{uuid.uuid4().hex}{ext}"
                rel_path = Path(str(batch.id)) / safe_name
                abs_path = INGESTION_MEDIA_ROOT / rel_path
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                with abs_path.open("wb") as out:
                    shutil.copyfileobj(resp, out)
        if stats is not None:
            stats.get_ok += 1
        return str(rel_path), _sha256_file(abs_path), _average_hash_hex(abs_path), _image_metadata(abs_path)
    except HTTPError as exc:
        if stats is not None:
            stats.get_failed += 1
            stats.bump_reason(f"get_http_{exc.code}")
        return None, None, None, None
    except (URLError, TimeoutError):
        if stats is not None:
            stats.get_failed += 1
            stats.bump_reason("get_network")
        return None, None, None, None
    except Exception:
        if stats is not None:
            stats.get_failed += 1
            stats.bump_reason("get_exception")
        return None, None, None, None


def _extract_image_urls_from_page(page_url: str, max_urls: int = 200) -> list[str]:
    parsed = urlparse(page_url)
    if parsed.scheme not in {"http", "https"}:
        return []
    host = (parsed.netloc or "").lower()
    ck_path = (
        resolve_netscape_cookie_file(getattr(settings, "YANDEX_COOKIES_FILE", "") or "")
        if "yandex." in host
        else None
    )
    opener = urllib_opener_with_netscape(ck_path)
    req = Request(page_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        if opener:
            resp = opener.open(req, timeout=12)
            try:
                html = resp.read().decode("utf-8", errors="ignore")
            finally:
                resp.close()
        else:
            with urlopen(req, timeout=12) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return []

    out: list[str] = []
    seen: set[str] = set()
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        soup = None

    def _add(u: str):
        if not u:
            return
        u = u.strip()
        for stop in ["&quot;", "\"", "'", "\\", "{", "}", ","]:
            if stop in u:
                u = u.split(stop, 1)[0]
        u = u.strip()
        if not (u.startswith("http://") or u.startswith("https://")):
            return
        if len(u) > 1800:
            return
        low = u.lower()
        if not any(ext in low for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".heic"]):
            # Still allow some CDN URLs without extension.
            if "image" not in low and "img" not in low and "photo" not in low:
                return
        if u in seen:
            return
        seen.add(u)
        out.append(u)

    if soup is not None:
        for tag in soup.find_all("img"):
            _add(tag.get("src") or "")
            _add(tag.get("data-src") or "")
            _add(tag.get("data-original") or "")

    # Heuristic extraction from embedded JSON/script blocks (SERP pages often store full image URLs there).
    for m in re.finditer(r'https?://[^"\'\\ ]+\.(?:jpg|jpeg|png|webp|gif|bmp|avif)', html, flags=re.IGNORECASE):
        _add(m.group(0))
        if len(out) >= max_urls:
            break

    # Yandex images pages often embed image targets in query params like `img_url=...`.
    for m in re.finditer(r"(?:img_url|img_href)=([^&\"'\\ ]+)", html, flags=re.IGNORECASE):
        raw = m.group(1) or ""
        decoded = unquote(html_lib.unescape(raw))
        _add(decoded)
        if len(out) >= max_urls:
            break

    return out[:max_urls]


def _extract_with_yandex_html(page_url: str, max_urls: int = 200) -> list[str]:
    """
    Yandex Images pages often keep origin URLs inside escaped JSON blobs
    (`serp-item`, `img_href`, `origin.url`, `dups[*].url`) rather than
    plain <img src=...>. This parser targets those structures.
    """
    parsed = urlparse(page_url)
    if parsed.scheme not in {"http", "https"}:
        return []
    ck_path = resolve_netscape_cookie_file(getattr(settings, "YANDEX_COOKIES_FILE", "") or "")
    opener = urllib_opener_with_netscape(ck_path)
    req = Request(
        page_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        if opener:
            resp = opener.open(req, timeout=14)
            try:
                html = resp.read().decode("utf-8", errors="ignore")
            finally:
                resp.close()
        else:
            with urlopen(req, timeout=14) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return []

    out: list[str] = []
    seen: set[str] = set()
    allowed_exts = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".heic")
    blocked_hosts = ("yandex.", "yastatic.net", "avatars.mds.yandex.net")

    def _clean_url(raw: str) -> str:
        s = (raw or "").strip()
        if not s:
            return ""
        s = html_lib.unescape(s)
        s = unquote(s)
        s = s.replace("\\/", "/")
        s = s.replace("\\u002F", "/")
        s = s.replace("\\u0026", "&")
        s = s.strip(" \"'")
        return s

    def _is_candidate(u: str) -> bool:
        if not (u.startswith("http://") or u.startswith("https://")):
            return False
        low = u.lower()
        if any(h in low for h in blocked_hosts):
            return False
        if any(ext in low for ext in allowed_exts):
            return True
        # allow extensionless CDN URLs if they look image-like
        return any(tok in low for tok in ["image", "img", "photo", "jpeg", "format="])

    def _add(raw: str):
        u = _clean_url(raw)
        if not u or not _is_candidate(u) or len(u) > 1800 or u in seen:
            return
        seen.add(u)
        out.append(u)

    # 1) query-param embeddings
    for m in re.finditer(r"(?:img_url|img_href)=([^&\"'\\ ]+)", html, flags=re.IGNORECASE):
        _add(m.group(1) or "")
        if len(out) >= max_urls:
            return out[:max_urls]

    # 2) escaped JSON fields commonly found in Yandex result blobs
    patterns = [
        r'"img_href"\s*:\s*"([^"]+)"',
        r'"origin"\s*:\s*\{\s*"url"\s*:\s*"([^"]+)"',
        r'"dups"\s*:\s*\[[^\]]*?"url"\s*:\s*"([^"]+)"',
        r'"preview"\s*:\s*\[[^\]]*?"url"\s*:\s*"([^"]+)"',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, flags=re.IGNORECASE):
            _add(m.group(1) or "")
            if len(out) >= max_urls:
                return out[:max_urls]

    # 3) fallback broad scan for direct image links inside scripts
    for m in re.finditer(r"https?://[^\"'\\ ]+\.(?:jpg|jpeg|png|webp|gif|bmp|avif)", html, flags=re.IGNORECASE):
        _add(m.group(0))
        if len(out) >= max_urls:
            return out[:max_urls]

    return out[:max_urls]


def _urls_from_text_blob(blob: str, max_urls: int = 200) -> list[str]:
    if not blob:
        return []
    seen = set()
    out = []
    for m in re.finditer(r"https?://[^\s\"'<>]+", blob, flags=re.IGNORECASE):
        u = m.group(0).strip()
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= max_urls:
            break
    return out


def _extract_with_gallery_dl(page_url: str, max_urls: int = 200) -> list[str]:
    if not py_shutil.which("gallery-dl"):
        return []
    try:
        proc = subprocess.run(
            ["gallery-dl", "--get-urls", page_url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return []
        return _urls_from_text_blob(proc.stdout, max_urls=max_urls)
    except Exception:
        return []


def _extract_with_yt_dlp(page_url: str, max_urls: int = 200) -> list[str]:
    if not py_shutil.which("yt-dlp"):
        return []
    host = (urlparse(page_url).netloc or "").lower()
    cmd = ["yt-dlp", "-g", "--flat-playlist"]
    # Instagram in 2026 refuses anonymous profile scrapes — 0 posts return
    # without a logged-in session. If the operator has exported cookies from
    # a browser and set INSTAGRAM_COOKIES_FILE, thread them in for IG hosts.
    if "instagram.com" in host:
        cookies_path = (getattr(settings, "INSTAGRAM_COOKIES_FILE", "") or "").strip()
        if cookies_path:
            cp = Path(cookies_path).expanduser()
            if cp.is_file():
                cmd += ["--cookies", str(cp)]
        # --playlist-end keeps profile crawls bounded instead of chewing through
        # hundreds of posts in a single subprocess call.
        cmd += ["--playlist-end", str(max(20, min(200, max_urls)))]
    elif "yandex." in host:
        cookies_path = (getattr(settings, "YANDEX_COOKIES_FILE", "") or "").strip()
        if cookies_path:
            cp = Path(cookies_path).expanduser()
            if cp.is_file():
                cmd += ["--cookies", str(cp)]
    cmd.append(page_url)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60 if "instagram.com" in host else 30,
        )
        if proc.returncode != 0:
            return []
        return _urls_from_text_blob(proc.stdout, max_urls=max_urls)
    except Exception:
        return []


def _extract_with_instaloader(page_url: str, max_urls: int = 200) -> list[str]:
    # Instaloader is optimized for Instagram media retrieval. If present,
    # we invoke metadata extraction and parse URL-like fields from stdout/stderr.
    if not py_shutil.which("instaloader"):
        return []
    try:
        proc = subprocess.run(
            ["instaloader", "--no-pictures", "--no-videos", "--no-metadata-json", "--", page_url],
            capture_output=True,
            text=True,
            timeout=35,
        )
        # Many installations print useful media references in stderr on failures;
        # we parse both streams conservatively.
        merged = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return _urls_from_text_blob(merged, max_urls=max_urls)
    except Exception:
        return []


def _extract_with_playwright_dynamic(page_url: str, max_urls: int = 200) -> list[str]:
    """
    Dynamic extractor for JS-heavy pages (notably Yandex Images):
    - loads the page in headless Chromium
    - repeatedly clicks "load more" style buttons
    - scrolls to trigger lazy-loaded results
    - extracts direct media URLs from DOM and href query params
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []

    seen: set[str] = set()
    out: list[str] = []
    allowed_exts = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".heic")

    def _add(u: str):
        if not u:
            return
        u = html_lib.unescape(unquote((u or "").strip()))
        if not (u.startswith("http://") or u.startswith("https://")):
            return
        low = u.lower()
        if any(ext in low for ext in allowed_exts) or any(tok in low for tok in ["image", "img", "photo", "jpeg", "format="]):
            if u not in seen and len(u) < 1800:
                seen.add(u)
                out.append(u)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="en-US",
                viewport={"width": 1600, "height": 2200},
            )
            nav_host = (urlparse(page_url).netloc or "").lower()
            if "yandex." in nav_host:
                ck_path = resolve_netscape_cookie_file(getattr(settings, "YANDEX_COOKIES_FILE", "") or "")
                if ck_path:
                    batch = yandex_playwright_cookie_list(ck_path, page_url)
                    if batch:
                        try:
                            context.add_cookies(batch)
                        except Exception:
                            pass
            page = context.new_page()
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)

            def _collect_once():
                try:
                    srcs = page.eval_on_selector_all(
                        "img",
                        "els => els.map(e => e.currentSrc || e.src || e.getAttribute('data-src') || '').filter(Boolean)",
                    )
                    for s in srcs or []:
                        _add(s)
                except Exception:
                    pass
                try:
                    hrefs = page.eval_on_selector_all(
                        "a[href]",
                        "els => els.map(e => e.getAttribute('href') || '').filter(Boolean)",
                    )
                    for h in hrefs or []:
                        if "img_url=" in h or "img_href=" in h:
                            try:
                                qs = parse_qs(urlparse(h).query or "")
                                for key in ("img_url", "img_href"):
                                    for v in qs.get(key, []):
                                        _add(v)
                            except Exception:
                                continue
                except Exception:
                    pass

            _collect_once()

            load_more_texts = [
                "Load more",
                "Show more",
                "More",
                "Показать ещё",
                "Показать еще",
                "Ещё",
            ]
            rounds = 0
            while len(out) < max_urls and rounds < 18:
                rounds += 1
                clicked = False
                for txt in load_more_texts:
                    try:
                        btn = page.get_by_text(txt, exact=False).first
                        if btn and btn.is_visible(timeout=250):
                            btn.click(timeout=1200)
                            page.wait_for_timeout(900)
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    try:
                        page.mouse.wheel(0, 3200)
                    except Exception:
                        pass
                    page.wait_for_timeout(800)
                _collect_once()

            browser.close()
    except Exception:
        return out[:max_urls]

    return out[:max_urls]


def _extract_candidates_from_page(
    page_url: str,
    max_urls: int = 200,
    engine: str = "auto",
    *,
    rewrite_stats: RewriteStats | None = None,
    enabled_adapters: set[str] | None = None,
    adapter_state: dict | None = None,
) -> tuple[list[str], str]:
    """
    Extract candidate image URLs from ``page_url`` using the best-matching
    engine for the host. Every returned URL is passed through the site-aware
    rewriter (:mod:`djangoscrap.ingestion_url_rewriter`) so thumbnails get
    upgraded to their full-resolution variant and known watermark-only hosts
    are dropped before the caller even sees them.

    If ``rewrite_stats`` is supplied, rewrite counters are accumulated into it
    so the campaign cycle can report e.g. "upgraded 37 pinterest thumbnails,
    dropped 4 dreamstime previews".

    Extraction order:
      1) **Adapter framework** (:mod:`djangoscrap.ingestion_adapters`) — any
         registered adapter whose ``can_handle(page_url)`` returns True gets
         first shot. If an adapter produces URLs, we stop there.
      2) Legacy engine chain below (Yandex / instaloader / yt-dlp / gallery-dl
         / playwright) — the pre-adapter default, still covers most search
         surfaces. Adapters fall through to this chain on empty results.
    """

    host = (urlparse(page_url).netloc or "").lower()
    mode = (engine or "auto").strip().lower()

    def _done(urls: list[str], engine_label: str) -> tuple[list[str], str]:
        # Single chokepoint for the rewriter so every engine benefits without
        # each extractor having to know about the site rules.
        return rewrite_candidate_urls(urls, stats=rewrite_stats), engine_label

    # Adapter-framework first-pass (only in ``auto`` mode so explicit engine
    # selection still works unchanged). Adapters are feature-flagged via
    # ``enabled_adapters``; if the set is empty, no adapter runs.
    if mode == "auto" and enabled_adapters:
        try:
            from djangoscrap.ingestion_adapters import registry as _adapters_registry
        except Exception:
            _adapters_registry = None
        if _adapters_registry is not None:
            adapter, result = _adapters_registry.dispatch(
                page_url,
                max_urls=max_urls,
                state=dict(adapter_state or {}),
                enabled_adapters=enabled_adapters,
            )
            if adapter is not None and result is not None and result.ok:
                engine_label = f"adapter:{adapter.name}"
                return _done(result.urls, engine_label)

    if mode == "yandex_dynamic":
        return _done(_extract_with_playwright_dynamic(page_url, max_urls=max_urls), "yandex-dynamic")
    if mode == "yandex_html":
        return _done(_extract_with_yandex_html(page_url, max_urls=max_urls), "yandex-html")
    if mode == "gallery_dl":
        return _done(_extract_with_gallery_dl(page_url, max_urls=max_urls), "gallery-dl")
    if mode == "yt_dlp":
        return _done(_extract_with_yt_dlp(page_url, max_urls=max_urls), "yt-dlp")
    if mode == "instaloader":
        return _done(_extract_with_instaloader(page_url, max_urls=max_urls), "instaloader")
    if mode == "playwright":
        return _done(_extract_with_playwright_dynamic(page_url, max_urls=max_urls), "playwright-dynamic")

    # auto mode orchestration
    if "yandex." in host:
        urls = _extract_with_yandex_html(page_url, max_urls=max_urls)
        if urls:
            return _done(urls, "yandex-html")
        urls = _extract_with_playwright_dynamic(page_url, max_urls=max_urls)
        if urls:
            return _done(urls, "playwright-dynamic")
        urls = _extract_with_gallery_dl(page_url, max_urls=max_urls)
        if urls:
            return _done(urls, "gallery-dl")
    if "instagram.com" in host:
        urls = _extract_with_instaloader(page_url, max_urls=max_urls)
        if urls:
            return _done(urls, "instaloader")
        urls = _extract_with_gallery_dl(page_url, max_urls=max_urls)
        if urls:
            return _done(urls, "gallery-dl")
        # yt-dlp also indexes Instagram profile/post URLs and is the only IG
        # path installed by default on most machines. Without cookies it will
        # return 0 URLs on private / auth-gated profiles; the caller still gets
        # a graceful empty result.
        urls = _extract_with_yt_dlp(page_url, max_urls=max_urls)
        if urls:
            return _done(urls, "yt-dlp")
        # Hard stop on Instagram URLs: if none of the IG-specific extractors
        # produced results, do NOT fall through to the generic HTML scrapers
        # below. IG's logged-out pages are a skeleton of JS + a handful of
        # tiny UI sprite icons; scraping that just produces noise that HEAD-
        # probes reject as too_small and bloats the candidate report with
        # "imported=0, failed=7" lines that look like a bug. Returning
        # ``instagram-blocked`` makes the status crystal-clear in the run
        # report and tells the operator they need auth (instagrapi creds,
        # cookies file, or a single-post URL rather than a profile).
        return [], "instagram-blocked"
    if any(x in host for x in ["youtube.com", "youtu.be", "tiktok.com", "x.com", "twitter.com"]):
        urls = _extract_with_yt_dlp(page_url, max_urls=max_urls)
        if urls:
            return _done(urls, "yt-dlp")
    urls = _extract_with_gallery_dl(page_url, max_urls=max_urls)
    if urls:
        return _done(urls, "gallery-dl")
    urls = _extract_image_urls_from_page(page_url, max_urls=max_urls)
    if urls:
        return _done(urls, "playwright-html")
    return [], "none"


def _suggest_source_name_from_url(page_url: str) -> str:
    try:
        parsed = urlparse(page_url)
    except Exception:
        return "imported__general__web"
    host = (parsed.netloc or "web").lower()
    host = host.replace("www.", "")
    source = "search"
    if "yandex" in host:
        source = "yandex"
    elif "google" in host:
        source = "google"
    elif "bing" in host:
        source = "bing"
    elif "instagram" in host:
        source = "instagram"
    elif "tumblr" in host:
        source = "tumblr"
    elif "reddit" in host:
        source = "reddit"
    else:
        source = host.split(".")[0] if "." in host else "web"

    q = ""
    try:
        qs = parse_qs(parsed.query or "")
        q = (qs.get("text") or qs.get("q") or qs.get("query") or [""])[0]
    except Exception:
        q = ""
    q_slug = slugify(unquote(q or ""))[:32] if q else ""
    subject = q_slug or "imported"
    return f"{subject}__general__{slugify(source) or 'web'}"



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


def _load_first_source_image(source_names):
    valid_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm"}
    for source_name in source_names or []:
        source_dir = LOCAL_SOURCES_ROOT / source_name
        if not source_dir.exists():
            continue
        for file in sorted(_local_source_dir_media_files(source_dir), key=lambda p: p.name.lower()):
            if file.suffix.lower() not in valid_exts:
                continue
            try:
                ext = file.suffix.lower()
                if ext in {".mp4", ".mov", ".webm"}:
                    frame = _extract_video_frame_image(file)
                    if frame is not None:
                        return frame
                    continue
                with Image.open(file) as img:
                    if ext == ".gif":
                        img.seek(0)
                    return img.convert("RGBA")
            except Exception:
                continue
    return None


def build_preview_image_from_sources(source_names):
    candidate = pick_representative_source_image(source_names)
    if not candidate:
        return None
    ext = candidate.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        with open(candidate, "rb") as src:
            name = f"composition_thumbnails/{get_random_string(12)}{ext}"
            return default_storage.save(name, ContentFile(src.read()))
    if ext == ".gif":
        try:
            with Image.open(candidate) as gif:
                gif.seek(0)
                frame = gif.convert("RGB")
                buffer = io.BytesIO()
                frame.save(buffer, format="JPEG")
                buffer.seek(0)
                name = f"composition_thumbnails/{get_random_string(12)}.jpg"
                return default_storage.save(name, ContentFile(buffer.read()))
        except Exception:
            return None
    if ext in {".mp4", ".mov", ".webm"}:
        frame = _extract_video_frame_image(candidate)
        if frame is None:
            return None
        buffer = io.BytesIO()
        frame.convert("RGB").save(buffer, format="JPEG")
        buffer.seek(0)
        name = f"composition_thumbnails/{get_random_string(12)}.jpg"
        return default_storage.save(name, ContentFile(buffer.read()))
    return None


def normalize_page_link(raw_link, request):
    if not raw_link:
        return None
    raw_link = raw_link.strip()
    if not raw_link:
        return None
    if raw_link.startswith("http://") or raw_link.startswith("https://"):
        return raw_link
    if not raw_link.startswith("/"):
        raw_link = f"/{raw_link}"
    return f"http://{request.get_host()}{raw_link}"


def parse_positive_int(raw_value, default=0):
    try:
        value = int(raw_value)
        return value if value >= 0 else default
    except (TypeError, ValueError):
        return default


def parse_float_in_range(raw_value, default=0.5, min_value=0.0, max_value=1.0):
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def parse_slider_0_1(raw_value, default=0.5):
    """Accept 0..1, 0..10, or 0..100 and normalize to 0..1."""
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = float(default)
    if value > 1.0:
        if value <= 10.0:
            value = value / 10.0
        elif value <= 100.0:
            value = value / 100.0
    return max(0.0, min(1.0, value))


def parse_composition_hashtags(raw_value: str) -> list[str]:
    raw = (raw_value or "").strip()
    if not raw:
        return []
    seen: set[str] = set()
    tags: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        token = chunk.strip().lower().lstrip("#")
        token = re.sub(r"[^a-z0-9_-]+", "", token)
        if not token or token in seen:
            continue
        seen.add(token)
        tags.append(token[:40])
    return tags[:24]


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

def is_valid_bucket_name(bucket_name):
    """Check if the S3 bucket name follows AWS naming rules"""
    return bool(re.match(r'^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$', bucket_name))

def create_bucket(request):
    if request.method == "POST":
        source_name_raw = (request.POST.get("name") or "").strip()
        source_type = (request.POST.get("type") or "manual-upload").strip()
        source_id = (request.POST.get("source_id") or "").strip()
        append_existing = request.POST.get("append_existing") == "on"
        uploaded_files = request.FILES.getlist("files")

        if not source_name_raw:
            messages.error(request, "Source name is required.")
            return redirect("new-source")

        source_name = source_name_raw.lower().replace(" ", "-")
        source_path = LOCAL_SOURCES_ROOT / source_name
        source_exists = source_path.exists()
        bucket_exists = Bucket.objects.filter(name=source_name).exists()

        if (source_exists or bucket_exists) and not append_existing:
            messages.error(
                request,
                f"Source '{source_name}' already exists. Enable 'Append to existing source' to add more images.",
            )
            return redirect("new-source")

        if not source_exists:
            source_path.mkdir(parents=True, exist_ok=True)

        if not bucket_exists:
            Bucket.objects.create(
                name=source_name,
                type=source_type or "manual-upload",
                source_id=source_id or source_name,
                last_scraped=datetime.utcnow().date(),
                query_no=0,
                initial=0,
                max_num=0,
            )
        elif source_type or source_id:
            Bucket.objects.filter(name=source_name).update(
                type=source_type or "manual-upload",
                source_id=source_id or source_name,
            )

        if not uploaded_files:
            if append_existing:
                messages.success(request, f"Source '{source_name}' is ready for additional uploads.")
            else:
                messages.success(request, f"Source '{source_name}' created. You can upload images now.")
            return redirect("bucket_contents", bucket_name=source_name)

        uploaded_count = 0
        skipped_count = 0

        for file in uploaded_files:
            original_name = Path(file.name).name
            ext = Path(original_name).suffix.lower()
            if ext not in LOCAL_SOURCE_ALLOWED_MEDIA_EXTS:
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
                f"Uploaded {uploaded_count} media file(s) to '{source_name}'."
                + (f" Skipped {skipped_count} file(s)." if skipped_count else ""),
            )
        else:
            messages.error(
                request,
                "No valid media files were uploaded. Allowed: common image types and mp4, mov, webm.",
            )

        return redirect("bucket_contents", bucket_name=source_name)

    return render(request, "admin/new-source.html")


def _destination_file_count(source_name: str) -> int:
    """Count of allowed media files in the destination folder (case-insensitive folder name)."""
    cleaned = (source_name or "").strip()
    if not cleaned:
        return 0
    lookup = {p.name.lower(): p for p in get_local_sources()}
    p = lookup.get(cleaned.lower())
    if not p or not p.is_dir():
        return 0
    return len(_local_source_dir_media_files(p))


def _ingestion_folder_last_updated(destination_name: str) -> datetime | None:
    """Last mtime of the destination source folder on disk, or None if missing/empty."""
    cleaned = (destination_name or "").strip()
    if not cleaned:
        return None
    source_dir = LOCAL_SOURCES_ROOT / cleaned
    if not source_dir.exists() or not source_dir.is_dir():
        return None
    try:
        return datetime.fromtimestamp(source_dir.stat().st_mtime)
    except OSError:
        return None


def _sync_ingestion_batch_with_disk_folder(batch: IngestionBatch) -> bool:
    """
    If destination_source_name matches a directory under LOCAL_SOURCES_ROOT (any casing),
    set destination_mode to 'existing' and normalize the name to the on-disk folder name.
    Returns True if the batch row was saved.
    """
    raw = (batch.destination_source_name or "").strip()
    if not raw:
        return False
    lookup = {p.name.lower(): p.name for p in get_local_sources()}
    canonical = lookup.get(raw.lower())
    if not canonical:
        return False
    changed = False
    if batch.destination_source_name != canonical:
        batch.destination_source_name = canonical
        changed = True
    if batch.destination_mode != "existing":
        batch.destination_mode = "existing"
        changed = True
    if changed:
        batch.save(update_fields=["destination_source_name", "destination_mode"])
    return changed


def _resolve_ingestion_destination_folder(
    batch: IngestionBatch,
    destination_mode_raw: str | None,
    source_name_raw: str | None,
    selected_existing_source_raw: str | None,
) -> tuple[str | None, str, str | None]:
    """
    Resolve which local source folder a publish/save should target.
    Returns (canonical_folder_name, destination_mode, error_message). error_message is set if invalid.
    """
    destination_mode = (destination_mode_raw or batch.destination_mode or "new").strip()
    if destination_mode not in {"new", "existing"}:
        destination_mode = "new"
    raw_sn = (source_name_raw or "").strip().lower().replace(" ", "-")
    batch_fallback = (batch.destination_source_name or "").strip().lower().replace(" ", "-")
    source_name = raw_sn or batch_fallback
    selected_existing_source = (selected_existing_source_raw or "").strip()

    existing_names = [p.name for p in get_local_sources()]
    normalized_lookup = {n.strip().lower(): n for n in existing_names}

    if destination_mode == "existing":
        resolved = ""
        if selected_existing_source:
            key = selected_existing_source.strip().lower()
            resolved = normalized_lookup.get(key, "")
            if not resolved:
                return (
                    None,
                    destination_mode,
                    f"No folder named “{selected_existing_source}” under {LOCAL_SOURCES_ROOT}. "
                    "Pick a folder from the list, or use “Create new source” to add one.",
                )
        elif source_name:
            resolved = normalized_lookup.get(source_name.strip().lower(), "")
            if not resolved:
                return (
                    None,
                    destination_mode,
                    f"No folder matching “{source_name}” on disk. Choose the exact folder from the list "
                    f"(folders must already exist under {LOCAL_SOURCES_ROOT}).",
                )
        else:
            return None, destination_mode, "Select which existing source folder to use, or switch to “Create new source”."
        source_name = resolved
    elif not source_name:
        return None, destination_mode, "Enter a destination folder name (e.g. volcano)."

    return source_name, destination_mode, None


def _publish_batch_items_shared(
    batch: IngestionBatch,
    destination_mode_raw: str,
    source_name_raw: str,
    selected_existing_source_raw: str,
    item_ids: list[int] | None = None,
) -> tuple[bool, str]:
    """
    Copy ingestion items to the destination source folder.

    If ``item_ids`` is provided, publishes exactly those rows regardless of
    their current ``decision`` value (and flips them to ``accepted`` first
    so downstream bookkeeping stays consistent). If ``item_ids`` is ``None``
    the original behavior is preserved: publish every row with
    ``decision='accepted'``.

    The ``item_ids`` path is what the flat "Publish selected" UI uses; the
    ``decision='accepted'`` path is still used by legacy callers that rely
    on the old accept/reject workflow.
    """
    source_name, destination_mode, err = _resolve_ingestion_destination_folder(
        batch, destination_mode_raw, source_name_raw, selected_existing_source_raw
    )
    if err or not source_name:
        return False, err or "Destination source name is required."
    source_dir = LOCAL_SOURCES_ROOT / source_name
    if destination_mode == "existing":
        if not source_dir.exists():
            return False, "Selected existing source does not exist."
        Bucket.objects.get_or_create(
            name=source_name,
            defaults={"type": "ingestion", "source_id": str(batch.id), "last_scraped": timezone.now().date(), "query_no": 0, "initial": 0, "max_num": 0},
        )
    else:
        source_dir.mkdir(parents=True, exist_ok=True)
        Bucket.objects.get_or_create(
            name=source_name,
            defaults={"type": "ingestion", "source_id": str(batch.id), "last_scraped": timezone.now().date(), "query_no": 0, "initial": 0, "max_num": 0},
        )
    moved = 0
    skipped_duplicates = 0
    near_duplicate_threshold = 5
    staging_clear_ids: list[int] = []
    existing_hashes: set[str] = set()
    existing_phashes: list[str] = []
    for existing_file in source_dir.iterdir():
        if not existing_file.is_file() or existing_file.name.startswith(".") or not _local_source_path_allowed(existing_file):
            continue
        try:
            existing_hashes.add(_sha256_file(existing_file))
        except Exception:
            pass
        if existing_file.suffix.lower() in LOCAL_SOURCE_IMAGE_EXTS:
            try:
                ph = _average_hash_hex(existing_file)
                if ph:
                    existing_phashes.append(ph)
            except Exception:
                pass
    if item_ids is not None:
        # Explicit publish-selected: accept then copy just these ids.
        target_ids = [int(x) for x in item_ids if str(x).isdigit()]
        if target_ids:
            batch.items.filter(id__in=target_ids).exclude(decision="accepted").update(decision="accepted")
        accepted_ids = list(
            batch.items.filter(id__in=target_ids, decision="accepted").values_list("id", flat=True)
        )
    else:
        accepted_ids = list(batch.items.filter(decision="accepted").values_list("id", flat=True))
    for item_id in accepted_ids:
        item = IngestionItem.objects.filter(pk=item_id, batch_id=batch.id, decision="accepted").first()
        if not item:
            continue
        # Skip "ghost" rows — accepted items whose file was already unlinked
        # (e.g. by vision QC or a prior publish). Without this guard
        # Path("").name is "" and get_valid_filename("") raises
        # SuspiciousFileOperation, aborting the entire publish.
        rel = (item.file_path or "").strip().replace("\\", "/")
        if not rel:
            staging_clear_ids.append(item.id)
            continue
        src = INGESTION_MEDIA_ROOT / rel
        if not src.is_file():
            continue
        src_sha = (item.sha256 or "").strip()
        if not src_sha:
            try:
                src_sha = _sha256_file(src)
            except Exception:
                src_sha = ""
        if src_sha and src_sha in existing_hashes:
            skipped_duplicates += 1
            staging_clear_ids.append(item.id)
            continue
        src_phash = (item.phash or "").strip()
        if src_phash:
            is_near_duplicate = any(_hamming_distance_hex(src_phash, existing_phash) <= near_duplicate_threshold for existing_phash in existing_phashes)
            if is_near_duplicate:
                skipped_duplicates += 1
                staging_clear_ids.append(item.id)
                continue
        try:
            base = get_valid_filename(Path(rel).name)
        except SuspiciousFileOperation:
            continue
        if not _local_source_filename_allowed(base):
            continue
        target = source_dir / base
        n = 2
        while target.exists():
            target = source_dir / f"{Path(base).stem}-{n}{Path(base).suffix}"
            n += 1
        shutil.copy2(src, target)
        if src_sha:
            existing_hashes.add(src_sha)
        if src_phash:
            existing_phashes.append(src_phash)
        moved += 1
        staging_clear_ids.append(item.id)
    cleared = 0
    if staging_clear_ids:
        unique_ids = list(dict.fromkeys(staging_clear_ids))
        cleared = _delete_ingestion_items_queryset(batch.items.filter(id__in=unique_ids))
    batch.status = "published"
    batch.destination_source_name = source_name
    batch.destination_mode = destination_mode
    batch.published_at = timezone.now()
    batch.save(update_fields=["status", "destination_source_name", "destination_mode", "published_at"])
    return (
        True,
        f"Published {moved} accepted item(s) into source '{source_name}'. Skipped {skipped_duplicates} already in folder (exact or near-duplicate). "
        f"Removed {cleared} from ingestion staging so they no longer appear in this batch.",
    )


def _resolve_destination_source_dir(batch: IngestionBatch) -> tuple[str, Path] | None:
    """
    Match batch.destination_source_name to a folder under LOCAL_SOURCES_ROOT (case-insensitive).
    Returns (canonical_name_on_disk, path) so /source-media/ URLs match the real directory.
    """
    raw = (batch.destination_source_name or "").strip()
    if not raw:
        return None
    lookup = {p.name.lower(): p for p in get_local_sources()}
    p = lookup.get(raw.lower())
    if not p or not p.is_dir():
        return None
    return (p.name, p)


def _campaign_seed_snapshot(
    batch: IngestionBatch, sample_limit: int = 24, *, variety_seed: int | None = None
) -> dict:
    """
    Pick reference images / URLs for this campaign run. Uses variety_seed so each run
    shuffles which folder files and accepted URLs are used (not always the same alphabetically / newest).
    """
    rng = random.Random(variety_seed if variety_seed is not None else (batch.id * 100_003 + 1))
    resolved = _resolve_destination_source_dir(batch)
    if resolved:
        source_name, source_dir = resolved
    else:
        source_name = (batch.destination_source_name or "").strip()
        source_dir = LOCAL_SOURCES_ROOT / source_name if source_name else None
    orientation_counts = {"landscape": 0, "portrait": 0, "square": 0}
    sampled_hashes: list[str] = []
    folder_seed_display_urls: list[str] = []
    if source_dir and source_dir.exists():
        image_files = [
            p
            for p in source_dir.iterdir()
            if p.is_file()
            and not p.name.startswith(".")
            and p.suffix.lower() in LOCAL_SOURCE_IMAGE_EXTS
        ]
        rng.shuffle(image_files)
        for img_path in image_files[:sample_limit]:
            folder_seed_display_urls.append(
                f"/source-media/{quote(source_name, safe='')}/{quote(img_path.name, safe='')}"
            )
            meta = _image_metadata(img_path)
            orient = (meta.get("orientation") or "unknown").strip().lower()
            if orient in orientation_counts:
                orientation_counts[orient] += 1
            ph = _average_hash_hex(img_path)
            if ph:
                sampled_hashes.append(ph)
    host_counts: dict[str, int] = {}
    for item in batch.items.filter(decision="accepted").order_by("-id")[:400]:
        host = ""
        try:
            host = (urlparse(item.original_url or "").netloc or "").lower()
            if host.startswith("www."):
                host = host[4:]
        except Exception:
            host = ""
        if host:
            host_counts[host] = host_counts.get(host, 0) + 1
    top_hosts = [k for k, _ in sorted(host_counts.items(), key=lambda x: x[1], reverse=True)[:6]]
    # Pull seeds from the *entire* accepted-history of the batch (not just the most
    # recent 200), so we don't keep re-seeding the campaign with whatever got accepted
    # yesterday. "Last N accepted" creates a feedback loop where CBIR neighbours of the
    # latest accepted items dominate forever — we saw this on batch 141 where every
    # cycle re-imported the same 30 pinimg + etsystatic blue-fur shots. Sampling
    # across the batch's history reintroduces older visual directions as seeds.
    raw_accepted_urls: list[tuple[int, str]] = list(
        batch.items.filter(decision="accepted")
        .exclude(original_url="")
        .values_list("id", "original_url")
    )
    seed_image_urls: list[str] = []
    for _id, u in raw_accepted_urls:
        u = (u or "").strip()
        if u.startswith("http://") or u.startswith("https://"):
            seed_image_urls.append(u)
    # Fallback: before a batch has accepted items, use non-rejected (pending) items as CBIR seeds.
    # Reverse-image search needs SOME public URL to anchor on; without this, early cycles are
    # text-only and drift. Pending items are still under user review, but they're fine to feed
    # to Yandex similar-image search.
    if not seed_image_urls:
        for item in batch.items.exclude(decision="rejected").exclude(original_url="").order_by("-id")[:200]:
            u = (item.original_url or "").strip()
            if u.startswith("http://") or u.startswith("https://"):
                seed_image_urls.append(u)
    unique_seed_urls = list(dict.fromkeys(seed_image_urls))
    # Enforce host diversity on the seed list itself — otherwise 24 seeds can turn into
    # "20 pinimg URLs + 4 others", and CBIR on near-identical pinimg thumbnails returns
    # the same neighbour set every time. Cap at 3 per host, then round-robin the rest.
    seeds_by_host: dict[str, list[str]] = {}
    for u in unique_seed_urls:
        try:
            h = (urlparse(u).netloc or "").lower()
            if h.startswith("www."):
                h = h[4:]
        except Exception:
            h = ""
        seeds_by_host.setdefault(h, []).append(u)
    diversified: list[str] = []
    host_keys = list(seeds_by_host.keys())
    rng.shuffle(host_keys)
    cap_per_host = 3
    # Round 1: cap per host
    for h in host_keys:
        for u in seeds_by_host[h][:cap_per_host]:
            diversified.append(u)
    # Round 2: if still under 24, top up from overflow (so low-volume batches still use
    # all available seeds)
    if len(diversified) < 24:
        for h in host_keys:
            for u in seeds_by_host[h][cap_per_host:]:
                diversified.append(u)
                if len(diversified) >= 24:
                    break
            if len(diversified) >= 24:
                break
    rng.shuffle(diversified)
    unique_seed_urls = diversified[:24]
    # Full folder file count (not just the sampled subset) so callers can gauge
    # reference strength for drift-prevention decisions (e.g. how hard to lean on
    # CBIR vs. text search this cycle).
    folder_file_count = 0
    if source_dir and source_dir.exists():
        try:
            folder_file_count = sum(
                1
                for p in source_dir.iterdir()
                if p.is_file()
                and not p.name.startswith(".")
                and p.suffix.lower() in LOCAL_SOURCE_IMAGE_EXTS
            )
        except OSError:
            folder_file_count = 0
    return {
        "orientation_counts": orientation_counts,
        "sampled_hashes": sampled_hashes[:sample_limit],
        "top_hosts": top_hosts,
        "seed_image_urls": unique_seed_urls,
        "folder_seed_display_urls": folder_seed_display_urls[:sample_limit],
        "folder_file_count": folder_file_count,
    }


def _default_campaign_search_prefs() -> dict:
    return {
        "orientation": "any",
        "min_width": 0,
        "min_height": 0,
        "media_type": "any",
    }


def _get_campaign_search_prefs(batch: IngestionBatch) -> dict:
    raw = ((batch.campaign_state or {}).get("search_prefs") or {}) if batch.campaign_state else {}
    base = _default_campaign_search_prefs()
    ow = (raw.get("orientation") or "any").strip().lower()
    if ow in {"any", "landscape", "portrait", "square"}:
        base["orientation"] = ow
    try:
        base["min_width"] = max(0, min(16000, int(raw.get("min_width") or 0)))
    except (TypeError, ValueError):
        base["min_width"] = 0
    try:
        base["min_height"] = max(0, min(16000, int(raw.get("min_height") or 0)))
    except (TypeError, ValueError):
        base["min_height"] = 0
    mt = (raw.get("media_type") or "any").strip().lower()
    if mt in {"any", "photo", "illustration", "artwork", "render"}:
        base["media_type"] = mt
    return base


def _campaign_item_matches_search_prefs(meta: dict, prefs: dict) -> bool:
    """Drop imports that do not match saved campaign filters (orientation / minimum dimensions)."""
    ow = prefs.get("orientation") or "any"
    if ow in {"landscape", "portrait", "square"}:
        mo = (meta.get("orientation") or "unknown").strip().lower()
        if mo != ow:
            return False
    mw = int(prefs.get("min_width") or 0)
    mh = int(prefs.get("min_height") or 0)
    w = int(meta.get("width") or 0)
    h = int(meta.get("height") or 0)
    if mw > 0:
        if w <= 0 or w < mw:
            return False
    if mh > 0:
        if h <= 0 or h < mh:
            return False
    return True


def _campaign_media_type_query_fragment(media_type: str) -> str:
    return {
        "photo": "realistic photograph",
        "illustration": "digital illustration",
        "artwork": "digital painting artwork",
        "render": "3d render cgi",
        "any": "",
    }.get((media_type or "any").lower().strip(), "")


def _preview_campaign_next_seed(batch: IngestionBatch, variety_seed: int | None = None) -> dict:
    """
    Preview image for the next campaign run.

    What the preview shows must match what the cycle will actually send to
    Yandex as a CBIR seed, otherwise operators see a folder image here and
    wonder why the scraped results look completely different (the classic
    "perfect-smile preview looks like teeth, but scraper returns full-face
    portraits" drift).

    Resolution order:
      1. **Destination folder** — images already under the batch publish
         target (same folder imports will land in). This matches CBIR when
         ``INGESTION_CAMPAIGN_PUBLIC_BASE_URL`` is set.
      2. If the folder has **no** images yet, accepted-item staging URLs
         (`seed_image_urls`) so the preview matches bootstrap CBIR.
      3. Folder-only local preview when the folder has files but no public
         base URL is configured (Yandex cannot fetch those bytes).

    Pass ``variety_seed`` to force a different pick (e.g. from a UI
    refresh click). When ``variety_seed`` is omitted, a value stored in
    ``campaign_state[\"preview_variety_seed\"]`` (after Save settings) is
    used so the dashboard matches the operator's last refresh + save.
    """
    if variety_seed is None:
        cs_pv = (batch.campaign_state or {}).get("preview_variety_seed")
        try:
            if cs_pv is not None and str(cs_pv).strip() != "":
                variety_seed = int(cs_pv)
        except (TypeError, ValueError):
            variety_seed = None
    if variety_seed is None:
        preview_vs = (batch.id * 1_000_003) ^ ((int(batch.campaign_runs_count or 0) + 1) * 265_443_5761) ^ 0x9E3779B9
    else:
        preview_vs = int(variety_seed)
    rng = random.Random(preview_vs)
    snapshot = _campaign_seed_snapshot(batch, sample_limit=24, variety_seed=preview_vs)
    public_base = (getattr(settings, "INGESTION_CAMPAIGN_PUBLIC_BASE_URL", None) or "").strip().rstrip("/")
    seed_image_urls = [u for u in (snapshot.get("seed_image_urls") or []) if (u or "").startswith(("http://", "https://"))]

    resolved = _resolve_destination_source_dir(batch)
    if resolved:
        canonical_name, source_dir = resolved
        image_files = [
            p
            for p in source_dir.iterdir()
            if p.is_file()
            and not p.name.startswith(".")
            and p.suffix.lower() in LOCAL_SOURCE_IMAGE_EXTS
        ]
        if image_files:
            rng.shuffle(image_files)
            pick = image_files[0]
            rel = f"/source-media/{quote(canonical_name, safe='')}/{quote(pick.name, safe='')}"
            if public_base:
                return {
                    "url": rel,
                    "caption": (
                        f"Yandex similar-image seed — “{pick.name}” from your destination folder "
                        f"“{canonical_name}” (served via {public_base})."
                    ),
                    "kind": "folder",
                }
            return {
                "url": rel,
                "caption": (
                    f"Reference from your destination folder “{canonical_name}” — NOT sent to Yandex. "
                    "Set INGESTION_CAMPAIGN_PUBLIC_BASE_URL so these folder images can anchor similar-image search."
                ),
                "kind": "folder_local_only",
            }

    if seed_image_urls:
        # Folder is empty — bootstrap CBIR from staged URLs until publishes land in the folder.
        shuffled = seed_image_urls[:]
        rng.shuffle(shuffled)
        return {
            "url": shuffled[0],
            "caption": "Yandex similar-image seed — staging URL (folder is empty; after you publish, seeds come from the folder).",
            "kind": "url",
        }

    return {
        "url": "",
        "caption": (
            "No CBIR seed yet — next run will be text-only (“"
            f"{(batch.concept_brief or batch.title or '').strip()[:80]}”). "
            "Add images to the destination folder, or accept scraped items so staging URLs can seed until the folder has files."
        ),
        "kind": "none",
    }


def _preview_to_pinned_cbir_url(preview: dict) -> str | None:
    """
    Map a seed-preview dict to an http(s) URL Yandex can fetch, if any.

    Remote staging URLs use http(s) directly. Folder previews carry a
    ``/source-media/…`` path that must be joined with
    ``INGESTION_CAMPAIGN_PUBLIC_BASE_URL``. Local-only folder previews cannot
    be pinned for CBIR.
    """
    kind = (preview.get("kind") or "").strip()
    url = (preview.get("url") or "").strip()
    if kind == "url" and url.startswith(("http://", "https://")):
        return url
    if kind == "folder" and url.startswith("/"):
        base = (getattr(settings, "INGESTION_CAMPAIGN_PUBLIC_BASE_URL", None) or "").strip().rstrip("/")
        if base:
            return f"{base}{url}"
    return None



def _subject_hints_creature_focus(subject_tokens: list[str]) -> bool:
    blob = " ".join(subject_tokens).lower()
    return any(m in blob for m in _CREATURE_QUERY_MARKERS)


def _batch_query_subject_tokens(batch: IngestionBatch) -> list[str]:
    """Meaningful words from destination + concept (strips generic slug noise like 'search')."""
    out: list[str] = []
    for part in re.split(r"[_\-\s,;/]+", (batch.concept_brief or "").strip()):
        p = part.strip().lower()
        if len(p) > 1 and p not in _QUERY_STOP_WORDS:
            out.append(p)
    for part in re.split(r"[_\-\s]+", (batch.destination_source_name or "").strip()):
        p = part.strip().lower()
        if len(p) > 1 and p not in _QUERY_STOP_WORDS:
            out.append(p)
    # de-dupe preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _campaign_query_pack(
    batch: IngestionBatch, snapshot: dict, needed: int, *, variety_seed: int | None = None
) -> list[str]:
    rng = random.Random((variety_seed or 0) + 91)
    prefs = _get_campaign_search_prefs(batch)
    media_frag = _campaign_media_type_query_fragment(prefs.get("media_type") or "any")
    subject_tokens = _batch_query_subject_tokens(batch)
    if subject_tokens:
        primary = " ".join(subject_tokens[:4])
    else:
        primary = (batch.destination_source_name or "visual").replace("_", " ").replace("-", " ").strip() or "visual editorial"
    orient_pref = (prefs.get("orientation") or "any").strip().lower()
    if orient_pref in {"landscape", "portrait", "square"}:
        orientation_token = {"landscape": "wide", "portrait": "vertical", "square": "square"}[orient_pref]
    else:
        orient_counts = snapshot.get("orientation_counts") or {}
        dominant_orientation = max(orient_counts.items(), key=lambda kv: kv[1])[0] if orient_counts else "landscape"
        orientation_token = {"landscape": "wide", "portrait": "vertical", "square": "square"}.get(dominant_orientation, "wide")
    top_hosts = snapshot.get("top_hosts") or []
    sources = [
        "pinterest",
        "tumblr",
        "reddit",
        "artstation",
        "deviantart",
        "instagram",
        "flickr",
        "behance",
        "cgsociety",
        "wallpaperscraft",
        "unsplash",
        "pexels",
    ]
    if top_hosts:
        # Put the batch's existing favourite hosts first, but don't remove the default
        # source pool — we want the query pack to *explore* beyond the current winners.
        sources = top_hosts[:4] + [s for s in sources if s not in top_hosts]
    rng.shuffle(sources)
    # Query cap controls breadth: each query becomes up to 3 Yandex pages (p=0,1,2) later,
    # so a larger cap directly multiplies out into unique candidate URLs per cycle.
    cap = max(10, min(36, int(needed / 15) + 10))
    creature_focus = _subject_hints_creature_focus(subject_tokens)
    if creature_focus:
        # Avoid “character art” (pulls humans); emphasize creature / monster artwork.
        style_bits = [
            "fantasy creature illustration",
            "creature concept art",
            "mythic beast fantasy painting",
            "dragon fantasy artwork",
            "fantasy monster digital painting",
            "creature design illustration",
            "epic fantasy creature scene",
            "high detail fantasy creature",
            "creature oil painting",
            "creature matte painting",
            "fantasy creature sketch",
            "creature turnaround reference",
        ]
    else:
        # Broader style vocabulary — the campaign should explore many aesthetics, not
        # just "fantasy art". Batches like ``bg_blur_fur`` have nothing to do with
        # fantasy, and the old narrow pack was returning mostly Pinterest fan-art.
        style_bits = [
            "photography",
            "editorial photography",
            "product photography",
            "macro photography",
            "texture photography",
            "fine art photography",
            "abstract background",
            "minimalist aesthetic",
            "moodboard aesthetic",
            "tumblr aesthetic",
            "cinematic still",
            "concept art",
            "digital painting",
            "illustration",
            "graphic design",
            "studio shot",
        ]
    rng.shuffle(style_bits)
    pack = []
    for i in range(cap):
        if subject_tokens:
            n = len(subject_tokens)
            a = subject_tokens[i % n]
            b = subject_tokens[(i + 1) % n] if n > 1 else a
            subj = f"{a} {b}" if a != b else a
        else:
            subj = primary
        bit = style_bits[i % len(style_bits)]
        core = f"{subj} {media_frag} {orientation_token} {bit} {sources[i % len(sources)]}".strip()
        while "  " in core:
            core = core.replace("  ", " ")
        pack.append(core)
    rng.shuffle(pack)
    return pack


def _campaign_cbir_seed_url_list(batch: IngestionBatch, snapshot: dict) -> list[str]:
    """
    Public http(s) URLs Yandex can fetch for reverse image search.

    When the **destination folder** (where publishes land) already contains
    images, CBIR uses **only** those files — ``/source-media/…`` URLs joined
    with ``INGESTION_CAMPAIGN_PUBLIC_BASE_URL``. Staging "accepted" URLs are
    not mixed in, so similarity search stays anchored on the folder you are
    building.

    If the folder is still empty, accepted / pending staging URLs are used so
    early cycles can bootstrap. Without a public base URL, local folder files
    cannot be sent to Yandex; we do not fall back to unrelated staging URLs
    when the folder already has images (that would defeat folder anchoring).
    """
    seen: set[str] = set()
    out: list[str] = []
    base = (getattr(settings, "INGESTION_CAMPAIGN_PUBLIC_BASE_URL", None) or "").strip().rstrip("/")
    folder_count = int(snapshot.get("folder_file_count") or 0)

    if folder_count > 0:
        if base:
            for rel in snapshot.get("folder_seed_display_urls") or []:
                rel = (rel or "").strip()
                if rel.startswith("/") and "/source-media/" in rel:
                    full = f"{base}{rel}"
                    if full not in seen:
                        seen.add(full)
                        out.append(full)
        return out

    for u in snapshot.get("seed_image_urls") or []:
        u = (u or "").strip()
        if u.startswith("http://") or u.startswith("https://"):
            if u not in seen:
                seen.add(u)
                out.append(u)
    if base:
        for rel in snapshot.get("folder_seed_display_urls") or []:
            rel = (rel or "").strip()
            if rel.startswith("/") and "/source-media/" in rel:
                full = f"{base}{rel}"
                if full not in seen:
                    seen.add(full)
                    out.append(full)
    return out


def _default_campaign_dedupe_prefs() -> dict:
    """
    Per-batch dedupe thresholds. Read by ``_run_campaign_cycle`` and passed to
    ``_detect_duplicate`` so the same pipeline can be tuned for very different
    subjects without code changes.

    - ``loose`` (CLIP 0.98, Hamming 6): only catches near-identical reposts.
      Use for texture / uniform-subject batches (fire, fur, sand, fog…) where
      every sample is semantically "the same thing" and CLIP clusters tight.
    - ``normal`` (CLIP 0.95, Hamming 10): balanced; the new default.
    - ``strict`` (CLIP 0.92, Hamming 12): the old default. Aggressive; best for
      subjects with high visual variety (characters, scenes, wide concepts).

    Stored under ``campaign_state["dedupe"]``. Existing batches with no value
    inherit the new ``normal`` default automatically.
    """
    return {"strictness": "normal"}



def _get_campaign_dedupe_prefs(batch: IngestionBatch) -> dict:
    raw = ((batch.campaign_state or {}).get("dedupe") or {}) if batch.campaign_state else {}
    base = _default_campaign_dedupe_prefs()
    st = str(raw.get("strictness") or base["strictness"]).strip().lower()
    if st in _DEDUPE_THRESHOLDS:
        base["strictness"] = st
    return base


def _dedupe_thresholds_for_batch(batch: IngestionBatch) -> dict:
    prefs = _get_campaign_dedupe_prefs(batch)
    return dict(_DEDUPE_THRESHOLDS[prefs["strictness"]])


def _default_campaign_archive_prefs() -> dict:
    """
    Per-batch "archive / watcher" mode.

    When ``urls`` is non-empty the batch stops acting as a search-driven
    aggregator and becomes a point-and-download watcher for the listed
    source URLs only:

    - No Yandex / text queries, no CBIR reverse-image seeds.
    - AI QC is bypassed (we trust the source).
    - Dedupe drops to sha-only (identical bytes still get collapsed, but
      "looks similar" matches no longer reject — this is what the user
      asks for when locking a batch to a single Instagram feed etc.).
    - ``media_kind_filter`` optionally rejects downloads that aren't the
      requested kind (``image`` / ``video``). ``any`` = no filter.

    Stored under ``campaign_state["archive"]``.
    """
    return {"urls": [], "media_kind_filter": "any"}


def _get_campaign_archive_prefs(batch: IngestionBatch) -> dict:
    raw = ((batch.campaign_state or {}).get("archive") or {}) if batch.campaign_state else {}
    base = _default_campaign_archive_prefs()
    raw_urls = raw.get("urls")
    if isinstance(raw_urls, list):
        urls = [str(u).strip() for u in raw_urls if isinstance(u, str) and str(u).strip()]
    elif isinstance(raw_urls, str):
        urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]
    else:
        urls = []
    base["urls"] = urls[:50]
    mk = str(raw.get("media_kind_filter") or base["media_kind_filter"]).strip().lower()
    if mk in {"any", "image", "video"}:
        base["media_kind_filter"] = mk
    return base


def _default_campaign_seed_pin_prefs() -> dict:
    """How preview-selected seeds should be applied to future runs."""
    return {"mode": "persistent"}


def _get_campaign_seed_pin_prefs(batch: IngestionBatch) -> dict:
    raw = ((batch.campaign_state or {}).get("seed_pin") or {}) if batch.campaign_state else {}
    base = _default_campaign_seed_pin_prefs()
    mode = str(raw.get("mode") or base["mode"]).strip().lower()
    if mode in {"persistent", "next_run"}:
        base["mode"] = mode
    return base


def _default_campaign_vision_qc_prefs() -> dict:
    # max_per_run must be >= campaign_max_imports_per_run (default 90) or a big chunk of
    # imports land in `pending` without ever being judged, which is the exact failure mode
    # that was polluting the dragons folder.
    #
    # Strictness defaults to "strict" — when the user hasn't explicitly opted into a looser
    # setting, over-rejecting on-topic borderline images is far less painful than letting
    # drift seep in (the frequent complaint is "images are straying from the type I want",
    # not "I wish QC kept more marginal matches"). Existing batches with a stored strictness
    # override this via _get_campaign_vision_qc_prefs.
    return {"enabled": True, "max_per_run": 120, "strictness": "strict"}


def _get_campaign_vision_qc_prefs(batch: IngestionBatch) -> dict:
    raw = ((batch.campaign_state or {}).get("vision_qc") or {}) if batch.campaign_state else {}
    base = _default_campaign_vision_qc_prefs()
    if "enabled" in raw:
        base["enabled"] = bool(raw.get("enabled"))
    try:
        base["max_per_run"] = max(0, min(300, int(raw.get("max_per_run") or base["max_per_run"])))
    except (TypeError, ValueError):
        pass
    # Fall back to the default strictness (now "strict") when the batch hasn't
    # stored an explicit choice. Previously the literal "normal" fallback meant
    # the default change above had no effect on unconfigured batches.
    st = (str(raw.get("strictness") or base["strictness"]).strip().lower())
    if st in {"loose", "normal", "strict"}:
        base["strictness"] = st
    return base


def _vision_qc_encode_jpeg_base64(src_path: Path, max_side: int = 384, quality: int = 70) -> str | None:
    try:
        with Image.open(src_path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            w, h = im.size
            if max(w, h) > max_side:
                if w >= h:
                    nw = max_side
                    nh = max(1, int(h * max_side / w))
                else:
                    nh = max_side
                    nw = max(1, int(w * max_side / h))
                im = im.resize((nw, nh), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _vision_qc_collect_reference_b64(batch: IngestionBatch, limit: int = 2) -> list[str]:
    """Up to `limit` reference thumbnails (published folder first, then accepted batch items)."""
    candidates: list[Path] = []
    resolved = _resolve_destination_source_dir(batch)
    source_dir = resolved[1] if resolved else None
    if source_dir and source_dir.exists():
        folder_images = [
            p for p in source_dir.iterdir()
            if p.is_file()
            and not p.name.startswith(".")
            and p.suffix.lower() in LOCAL_SOURCE_IMAGE_EXTS
        ]
        random.shuffle(folder_images)
        candidates.extend(folder_images[: limit * 3])
    for item in batch.items.filter(decision="accepted").order_by("-id")[: limit * 3]:
        rel = (item.file_path or "").strip().replace("\\", "/")
        if not rel:
            continue
        abs_path = INGESTION_MEDIA_ROOT / rel
        if abs_path.exists():
            candidates.append(abs_path)
    out: list[str] = []
    for p in candidates:
        enc = _vision_qc_encode_jpeg_base64(p)
        if enc:
            out.append(enc)
            if len(out) >= limit:
                break
    return out


def _vision_qc_judge(
    concept_brief: str,
    target_path: Path,
    reference_b64_list: list[str],
    strictness: str = "normal",
) -> tuple[bool, str]:
    """Ask gpt-4o-mini whether `target_path` belongs to this batch. Returns (keep, reason).

    Fails open (returns keep=True) if the API key is missing, the package isn't
    importable, or the call errors — we do not want QC failures to nuke runs.
    """
    api_key = (getattr(settings, "OPENAI_API_KEY", None) or "").strip()
    if not api_key:
        return True, "qc skipped (no api key)"
    target_b64 = _vision_qc_encode_jpeg_base64(target_path)
    if not target_b64:
        return True, "qc skipped (encode failed)"
    try:
        from openai import OpenAI
    except Exception as exc:  # noqa: BLE001
        return True, f"qc skipped (openai import: {exc.__class__.__name__})"
    brief = (concept_brief or "").strip() or "(no concept brief provided)"
    strictness_line = {
        "loose": "Keep unless the candidate is obviously unrelated to what the reference images show. When uncertain, KEEP.",
        "normal": "Keep if the candidate belongs in the same collection as the reference images. When uncertain, KEEP.",
        "strict": "Keep only if the candidate is clearly the same kind of content as the reference images.",
    }.get(
        strictness,
        "Keep if the candidate belongs in the same collection as the reference images. When uncertain, KEEP.",
    )
    usable_refs = [b for b in (reference_b64_list or []) if b][:6]
    if usable_refs:
        intro = (
            "You are a visual quality-control reviewer for an image collection. "
            f"The curator described the collection as: \"{brief}\". "
            "IMPORTANT: treat the curator's words as a loose hint only — the REFERENCE images below are "
            "the ground truth for what belongs in the collection. Do not interpret any word in the brief "
            "as a hard constraint (in particular, words like 'landscape', 'portrait', 'wide', 'vertical' "
            "should NOT be read as aspect-ratio requirements — any orientation is fine as long as the "
            "SUBJECT matches the references). "
            f"{strictness_line} "
            f"The message contains {len(usable_refs)} REFERENCE image(s) showing what the collection "
            "looks like, followed by ONE CANDIDATE image. "
            "Question: does the CANDIDATE depict the same subject / belong in the same collection as "
            "the references? Different art styles, crops, poses, framings, colors, compositions, and "
            "orientations are all acceptable; only the core subject needs to match."
        )
    else:
        intro = (
            "You are a visual quality-control reviewer for an image collection. "
            f"The curator described the collection as: \"{brief}\". "
            "There are no reference images this round. Any word in the brief like 'landscape' or "
            "'portrait' is a loose hint only, not an aspect-ratio constraint. "
            f"{strictness_line} "
            "Question: does the CANDIDATE plausibly depict the subject described in the brief? "
            "Different art styles, crops, poses, framings, and orientations are all fine."
        )
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                intro
                + ' Respond with strict JSON: {"keep": true|false, "reason": "<=6 words"}. '
                + "No prose, no code fences."
            ),
        },
    ]
    for b64 in usable_refs:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
            }
        )
    content.append(
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{target_b64}", "detail": "low"},
        }
    )
    try:
        from openai import OpenAI  # noqa: E402

        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": content}],
            max_tokens=40,
            temperature=0,
            response_format={"type": "json_object"},
        )
        text = (resp.choices[0].message.content or "").strip()
        try:
            payload = json.loads(text)
        except Exception:
            payload = {}
        keep = bool(payload.get("keep", True))
        reason = (payload.get("reason") or "").strip()[:80]
        if not reason:
            reason = "match" if keep else "off-topic"
        return keep, reason
    except Exception as exc:  # noqa: BLE001
        return True, f"qc error ({exc.__class__.__name__})"


def _run_campaign_cycle(
    batch: IngestionBatch,
    trigger: str = "scheduled",
    pinned_seed_url: str | None = None,
) -> dict:
    """
    Run one acquisition cycle for ``batch``.

    ``pinned_seed_url`` is set by the "▶ Run cycle now" button when the
    operator has confirmed a specific seed in the Next-run-seed-preview
    widget. When provided (and it's an http(s) URL), it becomes the
    ONLY CBIR seed for this cycle — overriding the normal shuffle from
    ``_campaign_cbir_seed_url_list``. This is what makes "what you see
    in the preview" match "what Yandex actually receives".

    Scheduled runs pass ``pinned_seed_url=None`` but still honour
    ``campaign_state[\"pinned_cbir_seed_url\"]`` when Save settings persisted
    a usable seed URL after Refresh (same semantics as immediate runs).
    """
    now_ts = timezone.now()
    if not batch.destination_source_name:
        return {"ok": False, "status": "blocked", "message": "Set destination source name first."}
    current_count = _destination_file_count(batch.destination_source_name)
    target_count = max(50, int(batch.campaign_target_count or batch.target_asset_count or 800))
    # Rejected rows are duplicate/noise; they should not block new imports.
    queue_count = batch.items.exclude(decision="rejected").count()
    if current_count >= target_count:
        batch.campaign_last_status = "target_reached"
        batch.campaign_last_run_at = now_ts
        batch.campaign_next_run_at = None
        batch.campaign_last_report = f"Target reached ({current_count}/{target_count} in folder)."
        batch.save(update_fields=["campaign_last_status", "campaign_last_run_at", "campaign_next_run_at", "campaign_last_report"])
        return {"ok": True, "status": "target_reached", "message": batch.campaign_last_report}
    if queue_count >= target_count:
        cadence = max(30, min(24 * 60, int(batch.campaign_cadence_minutes or 120)))
        next_run = now_ts + timezone.timedelta(minutes=cadence)
        batch.campaign_last_status = "review_backlog"
        batch.campaign_last_run_at = now_ts
        # Same rule as the end-of-cycle path: never keep a next_run_at
        # on a disabled batch. The list page would otherwise show a
        # phantom "Next: …" stamp for a batch the scheduler ignores.
        batch.campaign_next_run_at = next_run if batch.campaign_enabled else None
        batch.campaign_last_report = (
            f"Review backlog: {queue_count} non-rejected item(s) in batch (cap {target_count}). "
            "Reject junk, accept keepers, or publish to the folder; next check scheduled."
        )[:4000]
        batch.save(update_fields=["campaign_last_status", "campaign_last_run_at", "campaign_next_run_at", "campaign_last_report"])
        return {"ok": True, "status": "review_backlog", "message": batch.campaign_last_report}
    needed = target_count - current_count
    variety_seed = (batch.id * 1_000_003) ^ int(timezone.now().timestamp() * 1000) ^ int(batch.campaign_runs_count or 0)
    # Archive mode short-circuits the whole search / CBIR pipeline when the
    # user has locked this batch to one or more source URLs (e.g. a single
    # Instagram profile feed). In that mode we skip seed generation, text
    # queries and CBIR ratio entirely and just re-scrape the user's sources.
    archive_prefs = _get_campaign_archive_prefs(batch)
    archive_urls_list = archive_prefs["urls"]
    archive_media_kind = archive_prefs["media_kind_filter"]
    archive_mode = bool(archive_urls_list)
    pin_prefs = _get_campaign_seed_pin_prefs(batch)
    snapshot = _campaign_seed_snapshot(batch, sample_limit=24, variety_seed=variety_seed)
    pinned_seed = (pinned_seed_url or "").strip()
    pinned_from_state = False
    if not pinned_seed:
        pinned_seed = ((batch.campaign_state or {}).get("pinned_cbir_seed_url") or "").strip()
        pinned_from_state = bool(pinned_seed)
    if pinned_seed and not (pinned_seed.startswith("http://") or pinned_seed.startswith("https://")):
        # Relative / local-only URLs (e.g. /source-media/...) cannot be
        # reached by Yandex, so we discard them rather than send a
        # guaranteed-to-fail seed. The preview already labels these
        # "NOT sent to Yandex" to set the right expectation.
        pinned_seed = ""
        pinned_from_state = False
    if archive_mode:
        # Placeholder empties: keeps downstream telemetry happy without
        # spending time computing CBIR seeds we'll never use.
        cbir_seed_urls: list[str] = []
        queries: list[str] = []
    elif pinned_seed:
        # Operator picked a specific seed in the preview widget. Lock
        # this cycle to that one URL and skip text queries — the whole
        # point is "find more like THIS image", so mixing in lexical
        # results here would dilute the anchor the operator chose.
        cbir_seed_urls = [pinned_seed]
        queries = []
    else:
        cbir_seed_urls = _campaign_cbir_seed_url_list(batch, snapshot)
        queries = _campaign_query_pack(batch, snapshot, needed, variety_seed=variety_seed)
    max_pages = max(1, min(20, int(batch.campaign_max_pages_per_run or 4)))
    per_page_limit = max(10, min(300, int((batch.campaign_max_candidates_per_run or 180) / max_pages)))
    seed_image_urls = snapshot.get("seed_image_urls") or []
    folder_seed_display_urls = snapshot.get("folder_seed_display_urls") or []
    folder_file_count_run = int(snapshot.get("folder_file_count") or 0)
    public_base_run = (getattr(settings, "INGESTION_CAMPAIGN_PUBLIC_BASE_URL", None) or "").strip()
    if cbir_seed_urls:
        last_seed_display_url = cbir_seed_urls[0]
        if folder_file_count_run > 0:
            last_seed_caption = "Yandex similar-image seed (image from your destination folder)."
        else:
            last_seed_caption = "Yandex similar-image seed (staging URL until the folder has published images)."
    elif folder_file_count_run > 0 and not public_base_run:
        last_seed_display_url = folder_seed_display_urls[0] if folder_seed_display_urls else ""
        last_seed_caption = (
            "Folder has images but no INGESTION_CAMPAIGN_PUBLIC_BASE_URL — similar-image skipped; text search only."
        )
    elif seed_image_urls:
        last_seed_display_url = seed_image_urls[0]
        last_seed_caption = "Yandex similar-image seed (staging URL — folder empty)."
    elif folder_seed_display_urls:
        last_seed_display_url = folder_seed_display_urls[0]
        last_seed_caption = "Reference from your folder (local preview only; Yandex needs a public base URL for similar-image)."
    else:
        last_seed_display_url = ""
        last_seed_caption = "No thumbnail seed this run (text-only search; add images to the destination folder or accept items)."
    page_rng = random.Random(variety_seed + 513)
    # Build a wider pool of Yandex URLs by adding page offsets (p=0,1,2). Page 0 is the
    # first result page (and is the default); p=1 and p=2 return the next two pages. This
    # ~3x'es the unique candidates we can surface per seed/query without needing more
    # seeds, which is the single biggest cheap lever against the "every cycle imports the
    # same 30 blue-fur shots" problem we saw on batch 141.
    yandex_page_offsets = (0, 1, 2)
    image_pages: list[str] = []
    for seed_url in cbir_seed_urls[: max_pages * 3]:
        quoted = quote(seed_url, safe="")
        for p in yandex_page_offsets:
            base = f"https://yandex.ru/images/search?rpt=imageview&url={quoted}&cbir_page=similar"
            image_pages.append(base if p == 0 else f"{base}&p={p}")
    text_pages: list[str] = []
    for q in queries[: max_pages * 3]:
        quoted_q = quote(q)
        for p in yandex_page_offsets:
            base = f"https://yandex.ru/images/search?text={quoted_q}"
            text_pages.append(base if p == 0 else f"{base}&p={p}")
    # CBIR (reverse-image search) pulls results that *look like* a folder image and is by far
    # the strongest signal for on-theme aggregation. Text queries are broad and drift fast,
    # so only use them to fill pages we can't cover with CBIR seeds.
    if archive_mode:
        # Lock the cycle to the user-supplied URLs. We allow a small amount
        # of duplication across runs (a profile feed may only have one page)
        # — adapters/engines are expected to dedupe internally and the
        # already_seen_urls guard below prevents re-downloading.
        pages = archive_urls_list[:]
        page_rng.shuffle(pages)
        pages = pages[:max(1, max_pages)]
    elif image_pages:
        # How much should this cycle lean on reverse-image search vs. text queries?
        # CBIR is the single strongest anchor against aesthetic drift — text queries
        # reliably pull off-topic clusters once the folder has established a look.
        # Once the batch has a solid reference set (accepted items in the batch, or
        # files in the destination folder), lock CBIR share to ~95% and keep at most
        # one text page for lexical diversity. Early batches (few refs) still get
        # the original 75/25 mix so we can bootstrap coverage.
        _accepted_refs = int(batch.items.filter(decision="accepted").count())
        _dest_refs = int(snapshot.get("folder_file_count") or 0)
        reference_strength = _accepted_refs + _dest_refs
        if reference_strength >= 10:
            cbir_ratio = 0.95
            max_text_pages = 1
        elif reference_strength >= 5:
            cbir_ratio = 0.85
            max_text_pages = max(1, max_pages // 3)
        else:
            cbir_ratio = 0.75
            max_text_pages = max_pages
        target_similar = max(1, min(len(image_pages), max_pages - 1)) if max_pages > 1 else 1
        target_similar = min(target_similar, max(1, int(round(max_pages * cbir_ratio))))
        # Shuffle seed list so we don't always pick the same 1-2 seeds.
        shuffled_seeds = image_pages[:]
        page_rng.shuffle(shuffled_seeds)
        similar_slice = shuffled_seeds[:target_similar]
        text_needed = max(0, max_pages - len(similar_slice))
        text_needed = min(text_needed, max_text_pages)
        text_shuffled = text_pages[:]
        page_rng.shuffle(text_shuffled)
        text_slice = text_shuffled[:text_needed]
        pages = similar_slice + text_slice
        page_rng.shuffle(pages)
    else:
        # No seeds → fall back to text-only.
        pages_pool = text_pages[:]
        page_rng.shuffle(pages_pool)
        pages = pages_pool[:max_pages]
    candidate_urls: list[str] = []
    seen_urls: set[str] = set()
    engines_used: dict[str, int] = {}
    # Accumulate thumbnail→full-res upgrades and watermark-host drops across
    # every page we scrape this cycle so they can be surfaced in the report.
    rewrite_stats = RewriteStats()
    # Accumulate per-URL download telemetry (HEAD blocks, bytes saved, etc.)
    # for the same run-end report line. See ingestion_http.DownloadStats.
    download_stats = DownloadStats()
    # Cap candidates per CDN host in a single cycle. Before this, a strong Pinterest
    # cluster could eat 30+ of the 176 candidate slots on batch 141 and crowd out
    # genuinely new sources. Cap is a soft ratio of the total candidate budget, so
    # small cycles still get enough headroom and big cycles naturally diversify.
    cand_budget = int(batch.campaign_max_candidates_per_run or 180)
    per_host_cap = max(3, min(20, cand_budget // 12 or 3))
    host_counts: dict[str, int] = {}
    host_overflow_skipped = 0
    # Archive mode needs the adapter framework (Instagrapi / yt-dlp etc.) so
    # locked-source URLs like an Instagram profile actually get dispatched to
    # the right extractor. In search mode we leave this at None (the legacy
    # engine chain already covers Yandex pages fine).
    _archive_live_adapters = (
        _ensure_default_adapters_registered() if archive_mode else None
    )
    for page in pages:
        extracted, engine_used = _extract_candidates_from_page(
            page,
            max_urls=per_page_limit,
            engine="auto",
            rewrite_stats=rewrite_stats,
            enabled_adapters=_archive_live_adapters or None,
        )
        engines_used[engine_used] = engines_used.get(engine_used, 0) + 1
        for u in extracted:
            if u in seen_urls:
                continue
            try:
                host = (urlparse(u).netloc or "").lower()
                if host.startswith("www."):
                    host = host[4:]
            except Exception:
                host = ""
            if not archive_mode and host and host_counts.get(host, 0) >= per_host_cap:
                host_overflow_skipped += 1
                continue
            seen_urls.add(u)
            candidate_urls.append(u)
            if host:
                host_counts[host] = host_counts.get(host, 0) + 1
            if len(candidate_urls) >= cand_budget:
                break
        if len(candidate_urls) >= cand_budget:
            break
    search_prefs = _get_campaign_search_prefs(batch)
    qc_prefs = _get_campaign_vision_qc_prefs(batch)
    # Dedupe thresholds are per-batch now so texture / uniform-subject batches
    # (fire, fur, sand, sky…) can loosen CLIP without also loosening content-
    # rich batches. See _default_campaign_dedupe_prefs for the presets.
    dedupe_prefs = _get_campaign_dedupe_prefs(batch)
    dedupe_thresholds = _dedupe_thresholds_for_batch(batch)
    if archive_mode:
        # User asked for point-and-download: "I'm not concerned about finding
        # images that look like other images." Drop visual dedupe to the
        # loosest preset; exact-SHA dedupe still fires inside _detect_duplicate
        # via the URL/SHA paths, so true bit-identical reposts still collapse.
        dedupe_thresholds = dict(_DEDUPE_THRESHOLDS["loose"])
    qc_enabled = bool(qc_prefs.get("enabled")) and bool(
        (getattr(settings, "OPENAI_API_KEY", None) or "").strip()
    )
    # Archive mode trusts the source — no AI QC pass.
    if archive_mode:
        qc_enabled = False
    qc_budget = int(qc_prefs.get("max_per_run") or 0) if qc_enabled else 0
    qc_strictness = qc_prefs.get("strictness") or "normal"
    # 6 references is still cheap at detail=low (~85 tok/image) and gives gpt-4o-mini a far
    # richer sense of what the folder actually looks like — 2 references occasionally picked
    # off-theme outliers and biased approvals in the wrong direction.
    qc_reference_b64 = _vision_qc_collect_reference_b64(batch, limit=6) if qc_enabled else []
    qc_skipped_bootstrap = False
    if qc_enabled and not qc_reference_b64 and not batch.items.filter(decision="accepted").exists():
        # Bootstrap guard: no references anywhere. Let imports land as pending for manual review
        # this cycle; once a few are accepted, QC can run with real references next run.
        qc_budget = 0
        qc_skipped_bootstrap = True
    added = 0
    deduped = 0
    failed = 0
    filtered = 0
    qc_rejected = 0
    qc_checked = 0
    url_skipped = 0
    # Seed the URL set from what this batch has already ingested so we don't re-download
    # the same CDN URL across cycles. Without this, the same image comes back every night
    # via a slightly different CDN response (different bytes → different sha256) and
    # fills the grid with near-identical copies, which is exactly what batch 141 looked
    # like before this guard was added.
    already_seen_urls = set(
        batch.items.exclude(original_url="")
        .values_list("original_url", flat=True)
    )
    # Add deleted-URL tombstones so the same CDN URLs the user threw out last
    # session can't come back this cycle. Deleting an item used to be an
    # ephemeral action — the row disappeared from the dedupe scan and the
    # exact same URL would get re-downloaded on the next run. The tombstone
    # list in ``campaign_state["deleted_tombstones"]`` persists the user's
    # judgement so "delete" actually means "gone forever" for this batch.
    _deleted_tomb = _tombstones_for_batch(batch)
    tomb_url_set = set(_deleted_tomb["urls"])
    already_seen_urls |= tomb_url_set
    tomb_sha_set = set(s.lower() for s in _deleted_tomb["shas"])
    tomb_phash_list = _deleted_tomb["phashes"]
    tomb_url_skipped = 0

    # Parallel download phase: fan out _download_ingestion_url across a small
    # thread pool so HTTP latencies overlap instead of summing. HOST_THROTTLE
    # (inside _download_ingestion_url) guarantees per-host politeness is
    # preserved even under concurrent dispatch. The DB-writing consumer below
    # stays sequential so the SQLite writer lock + dedupe state never race.
    #
    # Candidate URLs are filtered against already_seen_urls up-front so we
    # never spend a worker slot on a URL we've already ingested in a prior
    # cycle.
    pending_urls: list[str] = []
    for u in candidate_urls:
        if u in already_seen_urls:
            url_skipped += 1
            continue
        already_seen_urls.add(u)
        pending_urls.append(u)

    # Budget-aware stop hook: once we've hit the max-imports-per-run ceiling,
    # no point launching more downloads. parallel_download_urls checks this
    # both before submit and before yield, so in-flight work drains cleanly.
    _max_imports = int(batch.campaign_max_imports_per_run or 90)

    def _imports_budget_exhausted() -> bool:
        return added >= _max_imports

    def _do_download(u: str):
        return _download_ingestion_url(batch, u, stats=download_stats)

    import time as _time  # local alias: avoid clashing with the module-level time import elsewhere
    _download_started = _time.monotonic()
    download_stats.parallel_workers = _INGEST_DL_WORKERS

    parallel_iter = parallel_download_urls(
        pending_urls,
        _do_download,
        max_workers=_INGEST_DL_WORKERS,
        stop_when=_imports_budget_exhausted,
    )

    for u, result in parallel_iter:
        if result is None:
            failed += 1
            continue
        rel_path, sha, phash, meta = result
        if not rel_path or not sha:
            failed += 1
            continue
        # Archive media-kind filter: when the user picked "videos only" (or
        # "images only") we still persist the row but mark it rejected so the
        # grid shows exactly what came down and why it wasn't kept.
        if archive_mode and archive_media_kind != "any":
            _got_kind = _ingestion_kind_from_name(rel_path)
            if _got_kind != archive_media_kind:
                IngestionItem.objects.create(
                    batch=batch,
                    original_url=u,
                    file_path=rel_path,
                    media_kind=_got_kind,
                    decision="rejected",
                    sha256=sha,
                    phash=phash,
                    dedupe_note=(
                        f"archive: got {_got_kind}, wanted {archive_media_kind}"
                    )[:255],
                    width=meta.get("width", 0),
                    height=meta.get("height", 0),
                    orientation=meta.get("orientation", "unknown"),
                    color_mode=meta.get("color_mode", "unknown"),
                    dominant_color_hex=meta.get("dominant_color_hex", ""),
                )
                filtered += 1
                continue
        if not _campaign_item_matches_search_prefs(meta, search_prefs):
            IngestionItem.objects.create(
                batch=batch,
                original_url=u,
                file_path=rel_path,
                media_kind=_ingestion_kind_from_name(rel_path),
                decision="rejected",
                sha256=sha,
                phash=phash,
                dedupe_note="campaign filters (size/orientation)"[:255],
                width=meta.get("width", 0),
                height=meta.get("height", 0),
                orientation=meta.get("orientation", "unknown"),
                color_mode=meta.get("color_mode", "unknown"),
                dominant_color_hex=meta.get("dominant_color_hex", ""),
            )
            filtered += 1
            continue
        # Compute CLIP embedding once per candidate so semantic dedupe, the
        # stored row, and any downstream QC all share the same vector. Videos
        # return None (CLIP only meaningful on still images in this pipeline).
        abs_candidate_path = INGESTION_MEDIA_ROOT / rel_path
        incoming_clip_vec = (
            _clip_embed_path(abs_candidate_path)
            if _ingestion_kind_from_name(rel_path) == "image"
            else None
        )
        decision, dedupe_note = _detect_duplicate(
            sha,
            phash,
            batch_id=batch.id,
            clip_embedding=incoming_clip_vec,
            **dedupe_thresholds,
        )
        # Tombstone check: if this batch has previously deleted an item with
        # the same bytes (sha256) or a near-identical thumbnail (phash), treat
        # it as a permanent reject. Prevents the "I deleted that and it came
        # back" loop even when the CDN served different bytes the second time.
        if decision != "rejected":
            if sha and sha.lower() in tomb_sha_set:
                decision = "rejected"
                dedupe_note = "tombstone: user previously deleted same bytes"
            elif phash and tomb_phash_list:
                best = 999
                for cand in tomb_phash_list:
                    d = _hamming_distance_hex(phash, cand)
                    if d < best:
                        best = d
                        if best == 0:
                            break
                if best <= 10:
                    decision = "rejected"
                    dedupe_note = f"tombstone: user previously deleted near-duplicate (hamming={best})"
        if decision != "rejected" and qc_budget > 0 and _ingestion_kind_from_name(rel_path) == "image":
            keep, qc_reason = _vision_qc_judge(
                concept_brief=batch.concept_brief or "",
                target_path=abs_candidate_path,
                reference_b64_list=qc_reference_b64,
                strictness=qc_strictness,
            )
            qc_checked += 1
            qc_budget -= 1
            if not keep:
                try:
                    if abs_candidate_path.exists():
                        abs_candidate_path.unlink()
                except Exception:
                    pass
                decision = "rejected"
                dedupe_note = (f"vision_qc: {qc_reason}")[:255]
                qc_rejected += 1
            else:
                # QC approved → promote to accepted so this URL becomes a CBIR seed next cycle.
                decision = "accepted"
                dedupe_note = (f"vision_qc: {qc_reason}" if qc_reason else "vision_qc: match")[:255]
        # Only persist the CLIP vector for rows whose file still exists on disk.
        # Rejected items whose file was deleted above would otherwise keep an
        # embedding with no corresponding image, which is noise in future
        # similarity scans.
        clip_blob = ""
        if decision != "rejected" or (decision == "rejected" and abs_candidate_path.exists()):
            clip_blob = _clip_embed_encode(incoming_clip_vec)
        # If QC (or any earlier step) deleted the file off disk, don't persist
        # a dangling file_path — that row would later render as a broken <img>
        # in the ingestion grid. We still keep the DB row so future cycles can
        # dedupe on sha256 / phash / URL, but there's no file to point at.
        persisted_file_path = rel_path if abs_candidate_path.exists() else ""
        IngestionItem.objects.create(
            batch=batch,
            original_url=u,
            file_path=persisted_file_path,
            media_kind=_ingestion_kind_from_name(rel_path),
            decision=decision,
            sha256=sha,
            phash=phash,
            clip_embedding=clip_blob,
            dedupe_note=dedupe_note,
            width=meta.get("width", 0),
            height=meta.get("height", 0),
            orientation=meta.get("orientation", "unknown"),
            color_mode=meta.get("color_mode", "unknown"),
            dominant_color_hex=meta.get("dominant_color_hex", ""),
        )
        if decision == "rejected":
            deduped += 1
        else:
            added += 1
        if added >= int(batch.campaign_max_imports_per_run or 90):
            break
    # Stamp parallel-fanout wall time for campaign_last_report.
    download_stats.wall_ms = int((_time.monotonic() - _download_started) * 1000)
    ok = True
    cadence = max(30, min(24 * 60, int(batch.campaign_cadence_minutes or 120)))
    if added < 5:
        cadence = min(12 * 60, max(cadence, 6 * 60))
    elif needed > max(200, int(target_count * 0.35)):
        cadence = max(30, min(cadence, 60))
    next_run = now_ts + timezone.timedelta(minutes=cadence)
    post_count = _destination_file_count(batch.destination_source_name)
    run_state = dict(batch.campaign_state or {})
    run_state.update(
        {
            "last_trigger": trigger,
            "last_queries": queries[: max_pages * 2],
            "last_seed_image_urls": seed_image_urls[: max_pages * 2],
            "last_folder_seed_display_urls": folder_seed_display_urls[: max_pages * 2],
            "last_campaign_seed_display_url": last_seed_display_url,
            "last_campaign_seed_caption": last_seed_caption,
            "last_page_urls": pages,
            "last_variety_seed": variety_seed,
            "last_top_hosts": snapshot.get("top_hosts", []),
            "last_orientation_counts": snapshot.get("orientation_counts", {}),
            "last_candidates": len(candidate_urls),
            "last_imported": added,
            "last_deduped": deduped,
            "last_failed": failed,
            "last_filtered": filtered,
            "last_search_prefs": search_prefs,
            "last_cbir_seed_count": len(cbir_seed_urls),
            "last_destination_count": post_count,
            "last_queue_count": batch.items.count(),
            "last_vision_qc_checked": qc_checked,
            "last_vision_qc_rejected": qc_rejected,
            "last_url_skipped": url_skipped,
            "last_archive_mode": archive_mode,
            "last_archive_urls": archive_urls_list if archive_mode else [],
            "last_archive_media_kind": archive_media_kind,
            "last_engines_used": {k: int(v) for k, v in sorted(engines_used.items())},
        }
    )
    if pinned_from_state and pin_prefs["mode"] == "next_run":
        run_state.pop("pinned_cbir_seed_url", None)
        run_state.pop("preview_variety_seed", None)
    batch.campaign_runs_count = int(batch.campaign_runs_count or 0) + 1
    batch.campaign_last_run_at = now_ts
    # Only schedule a next run when automatic mode is on. Previously an
    # "immediate" one-shot cycle on a disabled batch would still set
    # ``campaign_next_run_at``, which made the list page show a phantom
    # schedule for a batch the scheduler will never actually tick (rows
    # with ``campaign_enabled=False`` are filtered out in
    # ``_ingestion_campaign_tick_thread_worker``).
    if batch.campaign_enabled and post_count < target_count:
        batch.campaign_next_run_at = next_run
    else:
        batch.campaign_next_run_at = None
    batch.campaign_last_status = "ok"
    batch.campaign_state = run_state
    similar_page_n = sum(1 for p in pages if "rpt=imageview" in p)
    text_page_n = len(pages) - similar_page_n
    if qc_skipped_bootstrap:
        qc_report = "qc_skipped=bootstrap(no_refs,no_accepted); "
    elif qc_enabled:
        qc_report = f"qc_checked={qc_checked}; qc_rejected={qc_rejected}; "
    else:
        qc_report = ""
    # When an archive-mode cycle imports nothing, produce a human-readable
    # hint so the operator immediately knows *why* (auth-gated source is
    # the common case). Without this the report was a wall of
    # "imported=0; failed=6; reasons=too_small:6" that looked like a code
    # bug but was actually "we scraped IG's logged-out skeleton page".
    archive_hint = ""
    if archive_mode and added == 0:
        _engines_hit = set(engines_used.keys())
        _ig_source = any(
            "instagram.com" in (urlparse(u).netloc or "").lower()
            for u in archive_urls_list
        )
        if _ig_source:
            if "instagram-blocked" in _engines_hit or not _engines_hit:
                archive_hint = (
                    "; archive_hint=instagram auth required. "
                    "Paste an /p/<code>/ or /reel/<code>/ URL instead of the profile, "
                    "OR install instagrapi (pip install instagrapi) + set "
                    "INSTAGRAM_USER/INSTAGRAM_PASS, OR export cookies and set INSTAGRAM_COOKIES_FILE."
                )
            else:
                archive_hint = (
                    "; archive_hint=source returned 0 usable URLs — "
                    "if this is Instagram, try a single-post URL or add auth credentials."
                )
        else:
            archive_hint = (
                "; archive_hint=source returned 0 candidates. "
                "Check the URL is publicly reachable and isn't gated by auth."
            )
    batch.campaign_last_report = (
        f"trigger={trigger}; pages={len(pages)} (similar={similar_page_n}, text={text_page_n}); "
        f"cbir_seeds={len(cbir_seed_urls)}; candidates={len(candidate_urls)}; imported={added}; "
        f"duplicates={deduped}; filtered={filtered}; failed={failed}; "
        f"url_skipped={url_skipped}; {qc_report}"
        f"folder={post_count}/{target_count}; "
        f"queue={run_state.get('last_queue_count', 0)}; "
        f"engines={','.join(f'{k}:{v}' for k, v in sorted(engines_used.items()))}; "
        f"{rewrite_stats.as_summary()}; {download_stats.as_summary()}"
        f"{archive_hint}; review_then_publish"
    )[:4000]
    batch.save(
        update_fields=[
            "campaign_runs_count",
            "campaign_last_run_at",
            "campaign_next_run_at",
            "campaign_last_status",
            "campaign_state",
            "campaign_last_report",
        ]
    )
    return {"ok": ok, "status": batch.campaign_last_status, "message": batch.campaign_last_report}


def _campaign_cycle_thread_worker(
    batch_id: int,
    trigger: str,
    pinned_seed_url: str | None = None,
) -> None:
    close_old_connections()
    try:
        b = IngestionBatch.objects.get(pk=batch_id)
        _run_campaign_cycle(b, trigger=trigger, pinned_seed_url=pinned_seed_url)
    except Exception as exc:
        logging.exception("Campaign cycle failed for batch %s", batch_id)
        b = IngestionBatch.objects.filter(pk=batch_id).first()
        if b:
            b.campaign_last_status = "error"
            b.campaign_last_report = f"Campaign crashed: {exc}"[:4000]
            b.save(update_fields=["campaign_last_status", "campaign_last_report"])
    finally:
        close_old_connections()


def _ingestion_campaign_tick_thread_worker() -> None:
    close_old_connections()
    try:
        now_ts = timezone.now()
        due = (
            IngestionBatch.objects.filter(campaign_enabled=True)
            .filter(campaign_next_run_at__lte=now_ts)
            .order_by("campaign_next_run_at")[:10]
        )
        for batch in due:
            try:
                _run_campaign_cycle(batch, trigger="scheduled")
            except Exception:
                logging.exception("Campaign tick failed for batch %s", batch.id)
    finally:
        close_old_connections()


@require_POST
def ingestion_campaign_tick(request):
    now_ts = timezone.now()
    total_due = IngestionBatch.objects.filter(campaign_enabled=True).filter(campaign_next_run_at__lte=now_ts).count()
    due_count = min(10, total_due)
    if due_count:
        threading.Thread(target=_ingestion_campaign_tick_thread_worker, daemon=True).start()
        messages.success(
            request,
            f"Running {due_count} due campaign batch(es) in the background. Refresh the list in a minute to see status.",
        )
    else:
        messages.info(request, "Campaign tick complete. No campaign batches due.")
    return redirect("ingestion_batches")


def ingestion_batch_seed_preview(request, batch_id: int):
    """
    JSON endpoint used by the 'Refresh' button on the next-run seed preview.
    Returns a fresh random pick from the destination folder (or fallback sources).
    """
    batch = get_object_or_404(IngestionBatch, pk=batch_id)
    try:
        variety_seed = int(request.GET.get("vs") or 0)
    except (TypeError, ValueError):
        variety_seed = 0
    if not variety_seed:
        variety_seed = random.randint(1, 2**31 - 1) ^ (batch.id * 2654435761)
    preview = _preview_campaign_next_seed(batch, variety_seed=variety_seed)
    return JsonResponse(
        {
            "url": preview.get("url") or "",
            "caption": preview.get("caption") or "",
            "kind": preview.get("kind") or "none",
            "pin_url": _preview_to_pinned_cbir_url(preview) or "",
        }
    )


def ingestion_batch_status(request, batch_id: int):
    """
    Lightweight JSON snapshot for the live campaign watcher on the batch page.

    The UI polls this every few seconds while a campaign is running so the
    counters, status pill, and last-report line stay fresh without a full
    page reload. Intentionally cheap: one batch fetch + a small .values
    aggregate, no filesystem walk, no vision QC, no scrape.

    Keeping the payload shape stable matters — the JS on
    ``ingestion_batch_detail.html`` reads these keys by name.
    """
    from django.db.models import Count  # local import: used only here

    batch = get_object_or_404(IngestionBatch, pk=batch_id)
    # Defensive: exclude "", NULL, and whitespace-only file_path values. The
    # ingest pipeline writes "" for vision-QC rejections whose on-disk file
    # was already unlinked, but legacy rows and any future code path that
    # stores whitespace would otherwise render as broken <img> tiles pointing
    # at /media/ingestion_items/ (spraying 404s at the dev server).
    items_qs = (
        batch.items
        .exclude(file_path="")
        .exclude(file_path__isnull=True)
        .exclude(file_path__regex=r"^\s+$")
    )
    decision_counts = {"pending": 0, "accepted": 0, "rejected": 0}
    for row in items_qs.values("decision").annotate(n=Count("id")):
        key = str(row.get("decision") or "pending")
        if key in decision_counts:
            decision_counts[key] = int(row["n"])
    total = sum(decision_counts.values())
    destination_file_count = _destination_file_count(batch.destination_source_name)
    campaign_target_count = max(
        100, int(batch.campaign_target_count or batch.target_asset_count or 800)
    )
    campaign_remaining = max(0, campaign_target_count - destination_file_count)
    queue_count = total - decision_counts["rejected"]
    # Progress toward the folder target. 0..100 clamped; useful for a bar
    # in the UI without the client having to do the arithmetic.
    progress_pct = (
        int(round(min(100, (destination_file_count / campaign_target_count) * 100)))
        if campaign_target_count > 0
        else 0
    )
    return JsonResponse(
        {
            "batch_id": batch.id,
            "running": bool(batch.campaign_enabled),
            "last_status": batch.campaign_last_status or "",
            "last_report": (batch.campaign_last_report or "")[:2000],
            "runs_count": int(batch.campaign_runs_count or 0),
            "last_run_at": batch.campaign_last_run_at.isoformat() if batch.campaign_last_run_at else "",
            "next_run_at": batch.campaign_next_run_at.isoformat() if batch.campaign_next_run_at else "",
            "counts": {
                "total": total,
                "pending": decision_counts["pending"],
                "accepted": decision_counts["accepted"],
                "rejected": decision_counts["rejected"],
                "queue": queue_count,
            },
            "destination_file_count": destination_file_count,
            "campaign_target_count": campaign_target_count,
            "campaign_remaining": campaign_remaining,
            "progress_pct": progress_pct,
        }
    )


# ---------------------------------------------------------------------------
# Deletion tombstones
# ---------------------------------------------------------------------------
# When a user deletes an IngestionItem the row (with its sha256/phash/URL) goes
# away, which means the next campaign cycle no longer has those signals in its
# dedupe scan and will happily re-download the exact thing the user just threw
# out. That's the "why does the same crap keep coming back?" bug.
#
# The fix is a per-batch tombstone list stored inside ``IngestionBatch.campaign_state``
# so we don't need a schema migration. Each delete appends the URL, sha256 and
# phash so both pre-download URL filtering and post-download sha/phash dedupe
# can treat deleted items as permanently unwanted.
#
# Stored shape:
#     campaign_state["deleted_tombstones"] = {
#         "urls":    [url, ...],      # exact original_url matches
#         "shas":    [sha256, ...],   # exact bytes matches
#         "phashes": [phash, ...],    # visual near-duplicate matches
#     }
#
# The lists are capped to ``_TOMBSTONE_CAP`` entries each (FIFO) so a batch that
# rejects tens of thousands of items over its lifetime doesn't balloon the JSON
# blob. That cap is plenty for meaningful dedupe — nothing older than the last
# few thousand deletes is going to be re-suggested in practice.


def _tombstones_for_batch(batch: IngestionBatch) -> dict:
    cs = batch.campaign_state or {}
    t = cs.get("deleted_tombstones") or {}
    return {
        "urls": list(t.get("urls") or []),
        "shas": list(t.get("shas") or []),
        "phashes": list(t.get("phashes") or []),
    }


def _record_deleted_tombstones(
    batch: IngestionBatch,
    rows: list[dict],
) -> None:
    """
    Merge (url, sha256, phash) triples from freshly deleted rows into the
    batch's persistent tombstone lists. ``rows`` is the ``.values()`` output
    from the queryset that was just deleted.
    """
    if not rows:
        return
    tomb = _tombstones_for_batch(batch)
    url_set = set(tomb["urls"])
    sha_set = set(tomb["shas"])
    phash_set = set(tomb["phashes"])
    for r in rows:
        u = (r.get("original_url") or "").strip()
        if u and u not in url_set:
            tomb["urls"].append(u)
            url_set.add(u)
        s = (r.get("sha256") or "").strip().lower()
        if s and s not in sha_set:
            tomb["shas"].append(s)
            sha_set.add(s)
        p = (r.get("phash") or "").strip().lower()
        if p and p not in phash_set:
            tomb["phashes"].append(p)
            phash_set.add(p)
    # FIFO cap so very long-lived batches don't grow the JSON blob unboundedly.
    for key in ("urls", "shas", "phashes"):
        if len(tomb[key]) > _TOMBSTONE_CAP:
            tomb[key] = tomb[key][-_TOMBSTONE_CAP:]
    cs = batch.campaign_state or {}
    cs["deleted_tombstones"] = tomb
    batch.campaign_state = cs
    batch.save(update_fields=["campaign_state"])


def _is_url_tombstoned(batch: IngestionBatch, url: str) -> bool:
    if not url:
        return False
    tomb = _tombstones_for_batch(batch)
    return url in set(tomb["urls"])


def _is_sha_tombstoned(batch: IngestionBatch, sha256: str) -> bool:
    if not sha256:
        return False
    tomb = _tombstones_for_batch(batch)
    return sha256.lower() in set(s.lower() for s in tomb["shas"])


def _is_phash_near_tombstone(
    batch: IngestionBatch, phash: str, hamming_max: int = 10
) -> tuple[bool, int]:
    """
    Visual near-match against the batch's deleted-phash tombstones. Returns
    (matched, best_distance). A Hamming distance of 10 on an 8x8 average hash
    (64-bit) is the same ballpark the dedupe path uses for "near-duplicate";
    we keep the bar a bit tighter here because a false positive just means
    one candidate gets skipped, not that a real item is nuked.
    """
    if not phash:
        return False, 999
    tomb = _tombstones_for_batch(batch)
    phash_list = tomb["phashes"]
    if not phash_list:
        return False, 999
    best = 999
    for cand in phash_list:
        dist = _hamming_distance_hex(phash, cand)
        if dist < best:
            best = dist
            if best == 0:
                break
    return (best <= hamming_max), best


@require_POST
def ingestion_batch_bulk_ajax(request, batch_id: int):
    """
    JSON bulk action endpoint used by the grid for accept / reject / delete.

    The detail page used to POST those actions to the classic form handler,
    which then 302'd back to ``ingestion_batch_detail`` — re-rendering the
    entire batch (1000+ cards on a big batch) after every single click. For a
    user rejecting 50 images that was 50 full page rebuilds. This endpoint
    just mutates the rows and returns the updated counts so the client can
    drop cards out of the DOM in place.

    On delete we also append (url, sha, phash) tombstones via
    ``_record_deleted_tombstones`` so the campaign cycle doesn't re-download
    the same garbage next night.
    """
    batch = get_object_or_404(IngestionBatch, pk=batch_id)
    action = (request.POST.get("action") or "").strip().lower()
    ids_raw = request.POST.get("ids") or ""
    ids: list[int] = []
    for tok in ids_raw.split(","):
        tok = tok.strip()
        if tok.isdigit():
            ids.append(int(tok))
    if not ids:
        return JsonResponse({"ok": False, "error": "no ids"}, status=400)
    qs = batch.items.filter(id__in=ids)
    if action in {"accept", "reject"}:
        decision = "accepted" if action == "accept" else "rejected"
        updated = qs.update(decision=decision)
        return JsonResponse({"ok": True, "action": action, "updated": int(updated)})
    if action == "delete":
        rows = list(qs.values("id", "original_url", "sha256", "phash", "file_path"))
        _record_deleted_tombstones(batch, rows)
        deleted = _delete_ingestion_items_queryset(qs)
        return JsonResponse({"ok": True, "action": "delete", "updated": int(deleted)})
    return JsonResponse({"ok": False, "error": f"unknown action {action!r}"}, status=400)


def _get_or_create_ingestion_batch_for_source_folder(canonical_folder_name: str) -> IngestionBatch:
    """
    Every on-disk source folder has a dedicated ingestion batch for the
    Aggregate action. Reuses the newest existing batch with the same
    destination if present; otherwise creates one.

    The /ingestion/ listing page calls this once per folder, and
    historically the listing render could be invoked concurrently (tab
    reload + AJAX poll firing in the same request second) producing
    two INSERTs with the same ``destination_source_name`` and
    ``created_at`` to the millisecond. Once the duplicate exists every
    caller reuses the newest row, so the "ghost" batch lingers
    forever. The fix is a module-level lock around the SELECT+INSERT
    (per-folder, so two different folders can still race in parallel),
    plus a transaction so the read and write see a consistent view.
    """
    dest = canonical_folder_name.strip()
    lock = _ingestion_batch_folder_locks.setdefault(dest.lower(), threading.Lock())
    with lock:
        with transaction.atomic():
            batch = (
                IngestionBatch.objects.filter(destination_source_name__iexact=dest)
                .order_by("-created_at")
                .first()
            )
            if batch is None:
                batch = IngestionBatch.objects.create(
                    title=dest,
                    source_kind="ai-autopilot",
                    destination_mode="existing",
                    destination_source_name=dest,
                    mood_rating="mid",
                    concept_brief="",
                    target_asset_count=800,
                    campaign_target_count=800,
                    campaign_mode="scheduled",
                    status="draft",
                )
        _sync_ingestion_batch_with_disk_folder(batch)
        return batch


def _latest_ingestion_batches_by_destination_lower() -> dict[str, IngestionBatch]:
    """
    Latest batch per destination folder (case-insensitive key), preloaded in one query.

    This avoids one DB round-trip per folder on /ingestion/ list renders.
    """
    out: dict[str, IngestionBatch] = {}
    qs = (
        IngestionBatch.objects.exclude(destination_source_name="")
        .order_by("-created_at")
    )
    for b in qs:
        key = (b.destination_source_name or "").strip().lower()
        if not key or key in out:
            continue
        out[key] = b
    return out


# Per-folder mutexes used by _get_or_create_ingestion_batch_for_source_folder.
# WeakValueDictionary would be nicer but we only have a bounded number of
# folders (one per row in the listing), so leaking a Lock per seen folder
# for the process lifetime is fine.
_ingestion_batch_folder_locks: dict[str, "threading.Lock"] = {}


def _ingestion_source_folder_rows():
    """One row per folder under LOCAL_SOURCES_ROOT for the /ingestion/ overview."""
    rows: list[dict] = []
    source_dirs = get_local_sources()
    existing_by_lower = _latest_ingestion_batches_by_destination_lower()
    for source_dir in source_dirs:
        media_count, mtime = _cached_local_source_media_count(source_dir)
        name = source_dir.name
        key = name.lower()
        batch = existing_by_lower.get(key)
        if batch is None:
            batch = _get_or_create_ingestion_batch_for_source_folder(name)
            existing_by_lower[key] = batch
        rows.append(
            {
                "name": name,
                "media_count": media_count,
                "mtime": mtime,
                "batch": batch,
                "disk_path": str(LOCAL_SOURCES_ROOT / name),
            }
        )
    _flush_ingestion_media_count_disk_cache()
    return rows


def ingestion_batches(request):
    if request.method == "POST":
        destination_mode = (request.POST.get("destination_mode") or "new").strip()
        raw_dest = (request.POST.get("destination_source_name") or "").strip()
        if not raw_dest:
            messages.error(request, "Source name is required (it becomes the batch title and folder name).")
            return redirect("ingestion_batches")
        destination_source_name = raw_dest.lower().replace(" ", "-")
        title = raw_dest
        mood_rating = (request.POST.get("mood_rating") or "mid").strip().lower()
        if mood_rating not in {"chill", "mid", "agro"}:
            mood_rating = "mid"
        concept_brief = (request.POST.get("concept_brief") or "").strip()
        try:
            target_asset_count = int(request.POST.get("target_asset_count") or 800)
        except (TypeError, ValueError):
            target_asset_count = 800
        target_asset_count = max(100, min(20000, target_asset_count))
        seed_url = (request.POST.get("seed_url") or "").strip()
        batch = IngestionBatch.objects.create(
            title=title,
            source_kind="ai-autopilot",
            destination_mode=destination_mode if destination_mode in {"new", "existing"} else "new",
            destination_source_name=destination_source_name,
            mood_rating=mood_rating,
            concept_brief=concept_brief,
            target_asset_count=target_asset_count,
            campaign_target_count=target_asset_count,
            campaign_mode="scheduled",
            status="draft",
        )
        _sync_ingestion_batch_with_disk_folder(batch)
        if seed_url:
            try:
                extracted, engine_used = _extract_candidates_from_page(
                    seed_url, max_urls=60, engine="auto"
                )
            except Exception as exc:
                extracted, engine_used = [], f"error:{exc.__class__.__name__}"
            candidate_urls: list[str] = []
            seen_urls: set[str] = set()
            for u in extracted:
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    candidate_urls.append(u)
            added = 0
            accepted = 0
            deduped = 0
            failed = 0
            for u in candidate_urls:
                rel_path, sha, phash, meta = _download_ingestion_url(batch, u)
                if not rel_path or not sha:
                    failed += 1
                    continue
                incoming_clip_vec = (
                    _clip_embed_path(INGESTION_MEDIA_ROOT / rel_path)
                    if _ingestion_kind_from_name(rel_path) == "image"
                    else None
                )
                decision, dedupe_note = _detect_duplicate(
                    sha,
                    phash,
                    batch_id=batch.id,
                    clip_embedding=incoming_clip_vec,
                )
                if decision == "pending":
                    decision = "accepted"
                    accepted += 1
                IngestionItem.objects.create(
                    batch=batch,
                    original_url=u,
                    file_path=rel_path,
                    media_kind=_ingestion_kind_from_name(rel_path),
                    decision=decision,
                    sha256=sha,
                    phash=phash,
                    clip_embedding=_clip_embed_encode(incoming_clip_vec),
                    dedupe_note=dedupe_note,
                    width=meta.get("width", 0),
                    height=meta.get("height", 0),
                    orientation=meta.get("orientation", "unknown"),
                    color_mode=meta.get("color_mode", "unknown"),
                    dominant_color_hex=meta.get("dominant_color_hex", ""),
                )
                if decision == "rejected":
                    deduped += 1
                else:
                    added += 1
            batch.campaign_enabled = True
            batch.campaign_next_run_at = timezone.now()
            batch.last_scrape_engine_summary = (engine_used or "")[:255]
            batch.last_scrape_report = (
                f"new-batch seed: engine={engine_used}; candidates={len(candidate_urls)}; "
                f"imported={added}; auto-accepted={accepted}; duplicates={deduped}; failed={failed}"
            )[:4000]
            batch.save(
                update_fields=[
                    "campaign_enabled",
                    "campaign_next_run_at",
                    "last_scrape_engine_summary",
                    "last_scrape_report",
                ]
            )
            threading.Thread(
                target=_campaign_cycle_thread_worker,
                args=(batch.id, "immediate"),
                daemon=True,
            ).start()
            messages.success(
                request,
                (
                    f'Created "{batch.title}", seeded {accepted} image(s) from {engine_used or "scraper"} '
                    "and started the campaign. First aggregation cycle is running in the background."
                ),
            )
        else:
            messages.success(request, f'Created ingestion batch "{batch.title}".')
        return redirect("ingestion_batch_detail", batch_id=batch.id)
    batches = IngestionBatch.objects.all().order_by("-created_at")[:100]
    ingestion_batch_rows = [
        {"batch": b, "folder_updated_at": _ingestion_folder_last_updated(b.destination_source_name)}
        for b in batches
    ]
    source_rows = _ingestion_source_folder_rows()
    active_campaign_rows = [
        row for row in source_rows if getattr(row.get("batch"), "campaign_enabled", False)
    ]
    prefill_destination = (request.GET.get("prefill_destination") or "").strip().lower().replace(" ", "-")
    prefill_mode = (request.GET.get("prefill_mode") or "existing").strip().lower()
    if prefill_mode not in {"new", "existing"}:
        prefill_mode = "existing"
    return render(
        request,
        "admin/ingestion_batches.html",
        {
            "ingestion_batch_rows": ingestion_batch_rows,
            "now_ts": timezone.now(),
            "source_rows": source_rows,
            "active_campaign_rows": active_campaign_rows,
            "has_active_campaigns": bool(active_campaign_rows),
            "source_root_path": str(LOCAL_SOURCES_ROOT),
            "prefill_destination": prefill_destination,
            "prefill_mode": prefill_mode,
        },
    )


def ingestion_batch_detail(request, batch_id):
    batch = get_object_or_404(IngestionBatch, id=batch_id)
    # Opening from Source folders → Aggregate (?from_folder=…) forces this batch to target that disk folder.
    # Prevents stale destination (e.g. dragons_search) when working the volcano row.
    if request.method == "GET":
        raw_folder = (request.GET.get("from_folder") or "").strip()
        if raw_folder:
            disk_lookup = {p.name.lower(): p.name for p in get_local_sources()}
            canonical = disk_lookup.get(raw_folder.lower())
            if canonical:
                if (batch.destination_source_name or "").strip().lower() != canonical.lower():
                    batch.destination_source_name = canonical
                    batch.destination_mode = "existing"
                    batch.save(update_fields=["destination_source_name", "destination_mode"])
                    messages.info(
                        request,
                        f'Campaign and publish target folder set to “{canonical}” (from Source folders).',
                    )
            else:
                messages.warning(
                    request,
                    f'No source folder named “{raw_folder}” on disk under {LOCAL_SOURCES_ROOT}. Destination left unchanged.',
                )
            return redirect("ingestion_batch_detail", batch_id=batch.id)
    _sync_ingestion_batch_with_disk_folder(batch)

    def _build_ai_query_pack(mood_rating: str, concept_brief: str, target_asset_count: int) -> list[dict]:
        mood = (mood_rating or "mid").strip().lower()
        if mood not in {"chill", "mid", "agro"}:
            mood = "mid"
        concept = (concept_brief or "").strip()
        if not concept:
            concept = "abstract motion, texture, and lifestyle"
        core = [p.strip() for p in re.split(r"[,\n;/]+", concept) if p.strip()]
        if not core:
            core = [concept]
        if len(core) > 6:
            core = core[:6]
        vibe_tokens = {
            "chill": ["soft light", "calm", "slow motion", "ambient", "pastel"],
            "mid": ["editorial", "street", "contrast", "kinetic", "social"],
            "agro": ["high contrast", "intense", "chaotic", "glitch", "hard light"],
        }
        format_tokens = ["4k", "vertical", "landscape", "macro", "close-up", "loop", "cinematic"]
        source_tokens = ["instagram", "tiktok", "tumblr", "pinterest", "vimeo", "youtube shorts"]
        limit = max(12, min(80, int(target_asset_count / 30)))
        pack = []
        for i in range(limit):
            a = core[i % len(core)]
            b = vibe_tokens[mood][i % len(vibe_tokens[mood])]
            c = format_tokens[i % len(format_tokens)]
            d = source_tokens[i % len(source_tokens)]
            query = f"{a} {b} {c} {d}".strip()
            pack.append(
                {
                    "query": query,
                    "mood_rating": mood,
                    "priority": "high" if i < max(4, int(limit * 0.25)) else "normal",
                }
            )
        return pack

    def _filtered_qs_from_params(params):
        # Centralised "safe grid" filter: no caller of this helper should
        # ever see items whose file_path is empty / NULL / whitespace,
        # because those render as broken <img> tiles pointing at the bare
        # /media/ingestion_items/ directory (404 spam) and blow up the
        # publish loop on get_valid_filename(""). Applying the filter here
        # guarantees both the main render branch and the auto-fallback
        # branch (line ~4503) inherit it.
        qs = (
            batch.items.all()
            .exclude(file_path="")
            .exclude(file_path__isnull=True)
            .exclude(file_path__regex=r"^\s+$")
            .order_by("id")
        )
        decision_filter = (params.get("decision_filter") or "").strip()
        orientation_filter = (params.get("orientation_filter") or "").strip()
        color_filter = (params.get("color_filter") or "").strip()
        try:
            min_w = int(params.get("min_width") or 0)
        except (TypeError, ValueError):
            min_w = 0
        try:
            min_h = int(params.get("min_height") or 0)
        except (TypeError, ValueError):
            min_h = 0
        if decision_filter in {"pending", "accepted", "rejected"}:
            qs = qs.filter(decision=decision_filter)
        if orientation_filter in {"landscape", "portrait", "square", "unknown"}:
            qs = qs.filter(orientation=orientation_filter)
        if color_filter in {"color", "grayscale", "unknown"}:
            qs = qs.filter(color_mode=color_filter)
        if min_w > 0:
            qs = qs.filter(width__gte=min_w)
        if min_h > 0:
            qs = qs.filter(height__gte=min_h)
        return qs

    def _publish_batch_items(
        destination_mode_raw: str,
        source_name_raw: str,
        selected_existing_source_raw: str,
        item_ids: list[int] | None = None,
    ) -> tuple[bool, str]:
        return _publish_batch_items_shared(
            batch,
            destination_mode_raw,
            source_name_raw,
            selected_existing_source_raw,
            item_ids=item_ids,
        )

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "accept_all_and_publish":
            # Legacy action (kept for backwards compatibility with older
            # bookmarks). Promote pending → accepted then publish everything
            # accepted. The new UI uses ``publish_selected`` instead.
            batch.items.filter(decision="pending").update(decision="accepted")
            ok, msg = _publish_batch_items(
                request.POST.get("destination_mode"),
                request.POST.get("destination_source_name"),
                request.POST.get("selected_existing_source"),
            )
            if not ok:
                messages.error(request, msg)
                return redirect("ingestion_batch_detail", batch_id=batch.id)
            messages.success(request, msg)
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "publish_selected":
            # Flat-grid UX: publish exactly the rows the user ticked. No
            # pending/accepted/rejected triage; the user is the filter.
            raw_ids = request.POST.get("selected_ids") or ""
            ids: list[int] = []
            for tok in raw_ids.split(","):
                tok = tok.strip()
                if tok.isdigit():
                    ids.append(int(tok))
            if not ids:
                messages.error(request, "Select at least one image before publishing.")
                return redirect("ingestion_batch_detail", batch_id=batch.id)
            ok, msg = _publish_batch_items(
                request.POST.get("destination_mode"),
                request.POST.get("destination_source_name"),
                request.POST.get("selected_existing_source"),
                item_ids=ids,
            )
            if not ok:
                messages.error(request, msg)
                return redirect("ingestion_batch_detail", batch_id=batch.id)
            messages.success(request, msg)
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "save_destination":
            sn, dm, err = _resolve_ingestion_destination_folder(
                batch,
                request.POST.get("destination_mode"),
                request.POST.get("destination_source_name"),
                request.POST.get("selected_existing_source"),
            )
            if err or not sn:
                messages.error(request, err or "Invalid destination.")
                return redirect("ingestion_batch_detail", batch_id=batch.id)
            batch.destination_mode = dm
            batch.destination_source_name = sn
            batch.save(update_fields=["destination_mode", "destination_source_name"])
            messages.success(
                request,
                f'Publish target saved: “{sn}” ({LOCAL_SOURCES_ROOT / sn}). '
                "Confirm this before scraping or publishing.",
            )
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "ai_generate_pack":
            mood_rating = (request.POST.get("mood_rating") or batch.mood_rating or "mid").strip().lower()
            concept_brief = (request.POST.get("concept_brief") or batch.concept_brief or "").strip()
            try:
                target_asset_count = int(request.POST.get("target_asset_count") or batch.target_asset_count or 800)
            except (TypeError, ValueError):
                target_asset_count = batch.target_asset_count or 800
            target_asset_count = max(100, min(20000, target_asset_count))
            batch.mood_rating = mood_rating if mood_rating in {"chill", "mid", "agro"} else "mid"
            batch.concept_brief = concept_brief
            batch.target_asset_count = target_asset_count
            batch.ai_query_pack = _build_ai_query_pack(batch.mood_rating, batch.concept_brief, batch.target_asset_count)
            batch.last_scrape_report = (
                f"AI query pack generated: {len(batch.ai_query_pack)} prompts for mood={batch.mood_rating}, "
                f"target={batch.target_asset_count}."
            )[:4000]
            batch.save(
                update_fields=[
                    "mood_rating",
                    "concept_brief",
                    "target_asset_count",
                    "ai_query_pack",
                    "last_scrape_report",
                ]
            )
            messages.success(request, f"Generated {len(batch.ai_query_pack)} AI acquisition queries.")
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "campaign_save":
            mode = "scheduled"
            try:
                target_count = int(request.POST.get("campaign_target_count") or batch.target_asset_count or 800)
            except (TypeError, ValueError):
                target_count = batch.target_asset_count or 800
            try:
                cadence = int(request.POST.get("campaign_cadence_minutes") or 120)
            except (TypeError, ValueError):
                cadence = 120
            try:
                max_pages = int(request.POST.get("campaign_max_pages_per_run") or 4)
            except (TypeError, ValueError):
                max_pages = 4
            try:
                max_candidates = int(request.POST.get("campaign_max_candidates_per_run") or 180)
            except (TypeError, ValueError):
                max_candidates = 180
            try:
                max_imports = int(request.POST.get("campaign_max_imports_per_run") or 90)
            except (TypeError, ValueError):
                max_imports = 90
            target_count = max(100, min(20000, target_count))
            cadence = max(30, min(24 * 60, cadence))
            max_pages = max(1, min(20, max_pages))
            max_candidates = max(20, min(600, max_candidates))
            max_imports = max(10, min(300, max_imports))
            concept_brief = (request.POST.get("concept_brief") or "").strip()
            batch.concept_brief = concept_brief
            ow = (request.POST.get("search_orientation") or "any").strip().lower()
            if ow not in {"any", "landscape", "portrait", "square"}:
                ow = "any"
            try:
                smw = max(0, min(16000, int(request.POST.get("search_min_width") or 0)))
            except (TypeError, ValueError):
                smw = 0
            try:
                smh = max(0, min(16000, int(request.POST.get("search_min_height") or 0)))
            except (TypeError, ValueError):
                smh = 0
            mt = (request.POST.get("search_media_type") or "any").strip().lower()
            if mt not in {"any", "photo", "illustration", "artwork", "render"}:
                mt = "any"
            cs = dict(batch.campaign_state or {})
            cs["search_prefs"] = {
                "orientation": ow,
                "min_width": smw,
                "min_height": smh,
                "media_type": mt,
            }
            qc_enabled_raw = request.POST.get("vision_qc_enabled")
            qc_enabled_flag = qc_enabled_raw is not None and str(qc_enabled_raw).lower() not in {"0", "false", "off", ""}
            try:
                qc_max_raw = int(request.POST.get("vision_qc_max_per_run") or 120)
            except (TypeError, ValueError):
                qc_max_raw = 120
            qc_max_raw = max(0, min(300, qc_max_raw))
            qc_strictness_raw = (request.POST.get("vision_qc_strictness") or "normal").strip().lower()
            if qc_strictness_raw not in {"loose", "normal", "strict"}:
                qc_strictness_raw = "normal"
            cs["vision_qc"] = {
                "enabled": qc_enabled_flag,
                "max_per_run": qc_max_raw,
                "strictness": qc_strictness_raw,
            }
            dedupe_strictness_raw = (request.POST.get("dedupe_strictness") or "normal").strip().lower()
            if dedupe_strictness_raw not in {"loose", "normal", "strict"}:
                dedupe_strictness_raw = "normal"
            cs["dedupe"] = {"strictness": dedupe_strictness_raw}
            archive_urls_raw = (request.POST.get("archive_urls_text") or "").strip()
            archive_urls_list: list[str] = []
            for _line in archive_urls_raw.splitlines():
                _u = _line.strip()
                if not _u:
                    continue
                _p = urlparse(_u)
                if _p.scheme not in {"http", "https"} or not _p.netloc:
                    continue
                archive_urls_list.append(_u)
                if len(archive_urls_list) >= 50:
                    break
            archive_media_kind_raw = (request.POST.get("archive_media_kind") or "any").strip().lower()
            if archive_media_kind_raw not in {"any", "image", "video"}:
                archive_media_kind_raw = "any"
            cs["archive"] = {
                "urls": archive_urls_list,
                "media_kind_filter": archive_media_kind_raw,
            }
            seed_pin_mode_raw = (request.POST.get("seed_pin_mode") or "persistent").strip().lower()
            if seed_pin_mode_raw not in {"persistent", "next_run"}:
                seed_pin_mode_raw = "persistent"
            cs["seed_pin"] = {"mode": seed_pin_mode_raw}
            vs_raw = (request.POST.get("pinned_preview_variety_seed") or "").strip()
            try:
                vs_val = int(vs_raw) if vs_raw else None
            except (TypeError, ValueError):
                vs_val = None
            if vs_val:
                cs["preview_variety_seed"] = vs_val
                pv = _preview_campaign_next_seed(batch, variety_seed=vs_val)
                pin_u = _preview_to_pinned_cbir_url(pv)
                if pin_u:
                    cs["pinned_cbir_seed_url"] = pin_u
                else:
                    cs.pop("pinned_cbir_seed_url", None)
            else:
                cs.pop("preview_variety_seed", None)
                cs.pop("pinned_cbir_seed_url", None)
            batch.campaign_state = cs
            batch.campaign_mode = mode
            batch.campaign_target_count = target_count
            batch.campaign_cadence_minutes = cadence
            batch.campaign_max_pages_per_run = max_pages
            batch.campaign_max_candidates_per_run = max_candidates
            batch.campaign_max_imports_per_run = max_imports
            if batch.campaign_enabled:
                batch.campaign_next_run_at = timezone.now() + timezone.timedelta(minutes=cadence)
            batch.save(
                update_fields=[
                    "concept_brief",
                    "campaign_state",
                    "campaign_mode",
                    "campaign_target_count",
                    "campaign_cadence_minutes",
                    "campaign_max_pages_per_run",
                    "campaign_max_candidates_per_run",
                    "campaign_max_imports_per_run",
                    "campaign_next_run_at",
                ]
            )
            messages.success(request, "Campaign settings saved.")
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "campaign_start":
            batch.campaign_enabled = True
            cadence = max(30, min(24 * 60, int(batch.campaign_cadence_minutes or 120)))
            batch.campaign_next_run_at = timezone.now()
            batch.save(update_fields=["campaign_enabled", "campaign_next_run_at"])
            messages.success(
                request,
                "Campaign started. Scheduled runs will pick up from the next tick; use “Run cycle now” for an immediate fetch.",
            )
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "campaign_stop":
            batch.campaign_enabled = False
            batch.campaign_next_run_at = None
            batch.save(update_fields=["campaign_enabled", "campaign_next_run_at"])
            messages.info(request, "Campaign stopped. Automatic runs are paused.")
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "campaign_run_now":
            # "What you see is what Yandex gets": if the operator had a
            # specific seed visible in the preview widget when they
            # clicked Run, honour it. The form field is empty for
            # anyone clicking Run without interacting with the
            # preview (scheduled-behaviour parity).
            pinned = (request.POST.get("pinned_seed_url") or "").strip()
            # Clicking "▶ Run cycle now" also enables the campaign —
            # users expect a single button to both kick off the cycle
            # AND leave the batch on a recurring schedule. Previously
            # this action only fired a one-shot thread, which left the
            # list page showing "Off" while the user assumed the
            # campaign was live. The cycle worker itself will set
            # ``campaign_next_run_at`` at the end of the run (now gated
            # on ``campaign_enabled``), so enabling here is what makes
            # scheduled ticks actually fire.
            was_enabled = bool(batch.campaign_enabled)
            if not was_enabled:
                batch.campaign_enabled = True
                batch.save(update_fields=["campaign_enabled"])
            threading.Thread(
                target=_campaign_cycle_thread_worker,
                args=(batch.id, "immediate"),
                kwargs={"pinned_seed_url": pinned or None},
                daemon=True,
            ).start()
            enabled_note = "" if was_enabled else " Campaign is now running on schedule."
            if pinned:
                messages.info(
                    request,
                    f"Campaign run started using the seed you previewed.{enabled_note}",
                )
            else:
                messages.info(
                    request,
                    f"Campaign run started in the background.{enabled_note} Refresh this page to see new items.",
                )
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "apply_preset":
            preset = (request.POST.get("preset") or "").strip()
            if preset == "large_landscape":
                params = {
                    "orientation_filter": "landscape",
                    "min_width": "1600",
                    "min_height": "900",
                    "color_filter": "",
                    "decision_filter": "",
                }
                count = _filtered_qs_from_params(params).update(decision="accepted")
                messages.success(request, f"Preset applied: accepted {count} large landscape items (>=1600x900).")
            elif preset == "square_only":
                params = {
                    "orientation_filter": "square",
                    "min_width": "0",
                    "min_height": "0",
                    "color_filter": "",
                    "decision_filter": "",
                }
                count = _filtered_qs_from_params(params).update(decision="accepted")
                messages.success(request, f"Preset applied: accepted {count} square items.")
            elif preset == "reject_grayscale":
                params = {
                    "orientation_filter": "",
                    "min_width": "0",
                    "min_height": "0",
                    "color_filter": "grayscale",
                    "decision_filter": "",
                }
                count = _filtered_qs_from_params(params).update(decision="rejected")
                messages.success(request, f"Preset applied: rejected {count} grayscale items.")
            else:
                messages.error(request, "Unknown preset.")
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "upload_files":
            files = request.FILES.getlist("files")
            added = 0
            deduped = 0
            for f in files:
                rel_path, sha, phash, meta = _save_ingestion_upload(batch, f)
                if not rel_path or not sha:
                    continue
                incoming_clip_vec = (
                    _clip_embed_path(INGESTION_MEDIA_ROOT / rel_path)
                    if _ingestion_kind_from_name(f.name) == "image"
                    else None
                )
                decision, dedupe_note = _detect_duplicate(
                    sha,
                    phash,
                    batch_id=batch.id,
                    clip_embedding=incoming_clip_vec,
                )
                IngestionItem.objects.create(
                    batch=batch,
                    original_url="",
                    file_path=rel_path,
                    media_kind=_ingestion_kind_from_name(f.name),
                    decision=decision,
                    sha256=sha,
                    phash=phash,
                    clip_embedding=_clip_embed_encode(incoming_clip_vec),
                    dedupe_note=dedupe_note,
                    width=meta.get("width", 0),
                    height=meta.get("height", 0),
                    orientation=meta.get("orientation", "unknown"),
                    color_mode=meta.get("color_mode", "unknown"),
                    dominant_color_hex=meta.get("dominant_color_hex", ""),
                )
                if decision == "rejected":
                    deduped += 1
                else:
                    added += 1
            messages.success(request, f"Uploaded {added} new item(s). {deduped} duplicate(s) auto-rejected.")
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "add_urls":
            raw = (request.POST.get("urls_text") or "").strip()
            urls = [u.strip() for u in raw.splitlines() if u.strip()]
            added = 0
            failed = 0
            deduped = 0
            for u in urls:
                rel_path, sha, phash, meta = _download_ingestion_url(batch, u)
                if not rel_path or not sha:
                    failed += 1
                    continue
                incoming_clip_vec = (
                    _clip_embed_path(INGESTION_MEDIA_ROOT / rel_path)
                    if _ingestion_kind_from_name(rel_path) == "image"
                    else None
                )
                decision, dedupe_note = _detect_duplicate(
                    sha,
                    phash,
                    batch_id=batch.id,
                    clip_embedding=incoming_clip_vec,
                )
                IngestionItem.objects.create(
                    batch=batch,
                    original_url=u,
                    file_path=rel_path,
                    media_kind=_ingestion_kind_from_name(rel_path),
                    decision=decision,
                    sha256=sha,
                    phash=phash,
                    clip_embedding=_clip_embed_encode(incoming_clip_vec),
                    dedupe_note=dedupe_note,
                    width=meta.get("width", 0),
                    height=meta.get("height", 0),
                    orientation=meta.get("orientation", "unknown"),
                    color_mode=meta.get("color_mode", "unknown"),
                    dominant_color_hex=meta.get("dominant_color_hex", ""),
                )
                if decision == "rejected":
                    deduped += 1
                else:
                    added += 1
            messages.success(request, f"Imported {added} URL item(s). {deduped} duplicate(s), {failed} failed.")
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action in {"scrape_pages", "scrape_url"}:
            single_url = (request.POST.get("page_url") or "").strip()
            raw_pages = single_url if single_url else (request.POST.get("page_urls_text") or "").strip()
            scrape_engine = (request.POST.get("scrape_engine") or "auto").strip()
            try:
                per_page_limit = int(request.POST.get("per_page_limit") or 80)
            except (TypeError, ValueError):
                per_page_limit = 80
            per_page_limit = max(10, min(300, per_page_limit))
            try:
                scrape_min_w = int(request.POST.get("scrape_min_width") or 0)
            except (TypeError, ValueError):
                scrape_min_w = 0
            try:
                scrape_min_h = int(request.POST.get("scrape_min_height") or 0)
            except (TypeError, ValueError):
                scrape_min_h = 0
            scrape_min_w = max(0, min(10000, scrape_min_w))
            scrape_min_h = max(0, min(10000, scrape_min_h))
            pages = [u.strip() for u in raw_pages.splitlines() if u.strip()]
            if single_url and not batch.destination_source_name:
                batch.destination_source_name = _suggest_source_name_from_url(single_url)
                batch.save(update_fields=["destination_source_name"])
            candidate_urls: list[str] = []
            seen_urls: set[str] = set()
            engines_used: dict[str, int] = {}
            # Route manual scrapes through the adapter framework in addition
            # to the legacy engine chain. Adapters self-gate on host + deps
            # (InstagrapiAdapter skips itself when creds are missing, etc.)
            # so enabling the full set is safe. Without this, pasting an
            # Instagram profile URL bypasses the purpose-built instagrapi
            # path entirely and falls through to playwright-html, which
            # only sees UI chrome on a logged-out IG page.
            _live_adapters = _ensure_default_adapters_registered()
            for page in pages[:20]:
                extracted, engine_used = _extract_candidates_from_page(
                    page,
                    max_urls=per_page_limit,
                    engine=scrape_engine,
                    enabled_adapters=_live_adapters or None,
                )
                engines_used[engine_used] = engines_used.get(engine_used, 0) + 1
                for u in extracted:
                    if u in seen_urls:
                        continue
                    seen_urls.add(u)
                    candidate_urls.append(u)

            added = 0
            deduped = 0
            failed = 0
            too_small = 0
            wrong_kind = 0
            # Archive prefs apply to manual scrapes too: if the user has
            # locked this batch to "videos only" (or "images only"), manual
            # runs respect that filter. Archive-URL list itself doesn't
            # override the pasted URL — the operator is being deliberate.
            _archive_prefs = _get_campaign_archive_prefs(batch)
            _scrape_media_kind = _archive_prefs["media_kind_filter"]
            for u in candidate_urls:
                rel_path, sha, phash, meta = _download_ingestion_url(batch, u)
                if not rel_path or not sha:
                    failed += 1
                    continue
                if _scrape_media_kind != "any":
                    got_kind = _ingestion_kind_from_name(rel_path)
                    if got_kind != _scrape_media_kind:
                        wrong_kind += 1
                        _unlink_ingestion_staged_file(rel_path)
                        continue
                if (
                    (scrape_min_w and int(meta.get("width", 0) or 0) < scrape_min_w)
                    or (scrape_min_h and int(meta.get("height", 0) or 0) < scrape_min_h)
                ):
                    too_small += 1
                    continue
                incoming_clip_vec = (
                    _clip_embed_path(INGESTION_MEDIA_ROOT / rel_path)
                    if _ingestion_kind_from_name(rel_path) == "image"
                    else None
                )
                decision, dedupe_note = _detect_duplicate(
                    sha,
                    phash,
                    batch_id=batch.id,
                    clip_embedding=incoming_clip_vec,
                )
                IngestionItem.objects.create(
                    batch=batch,
                    original_url=u,
                    file_path=rel_path,
                    media_kind=_ingestion_kind_from_name(rel_path),
                    decision=decision,
                    sha256=sha,
                    phash=phash,
                    clip_embedding=_clip_embed_encode(incoming_clip_vec),
                    dedupe_note=dedupe_note,
                    width=meta.get("width", 0),
                    height=meta.get("height", 0),
                    orientation=meta.get("orientation", "unknown"),
                    color_mode=meta.get("color_mode", "unknown"),
                    dominant_color_hex=meta.get("dominant_color_hex", ""),
                )
                if decision == "rejected":
                    deduped += 1
                else:
                    added += 1
            _kind_note = (
                f", wrong kind {wrong_kind} (filter={_scrape_media_kind})"
                if _scrape_media_kind != "any"
                else ""
            )
            messages.success(
                request,
                (
                    f"Scraped {len(pages[:20])} page(s), found {len(candidate_urls)} candidate URL(s), "
                    f"imported {added}, duplicates {deduped}, too small {too_small}, failed {failed}{_kind_note}. "
                    f"Engines: {', '.join(f'{k}:{v}' for k, v in sorted(engines_used.items()))}."
                ),
            )
            engine_summary = ", ".join(f"{k}:{v}" for k, v in sorted(engines_used.items())) or "none"
            batch.last_scrape_engine_summary = engine_summary[:255]
            batch.last_scrape_report = (
                f"pages={len(pages[:20])}; candidates={len(candidate_urls)}; imported={added}; "
                f"duplicates={deduped}; too_small={too_small}; wrong_kind={wrong_kind}; "
                f"media_kind={_scrape_media_kind}; failed={failed}; engines={engine_summary}"
            )[:4000]
            batch.save(update_fields=["last_scrape_engine_summary", "last_scrape_report"])
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "bulk_decision":
            decision = (request.POST.get("decision") or "").strip()
            scope = (request.POST.get("scope") or "selected").strip()
            if decision not in {"pending", "accepted", "rejected"}:
                messages.error(request, "Invalid decision for bulk action.")
                return redirect("ingestion_batch_detail", batch_id=batch.id)
            if scope == "filtered":
                target_qs = _filtered_qs_from_params(request.POST)
                count = target_qs.update(decision=decision)
                messages.success(request, f"Updated {count} filtered item(s) to {decision}.")
                return redirect("ingestion_batch_detail", batch_id=batch.id)
            raw_ids = (request.POST.get("selected_ids") or "").strip()
            ids = []
            for token in raw_ids.split(","):
                token = token.strip()
                if token.isdigit():
                    ids.append(int(token))
            if not ids:
                messages.error(request, "No items selected.")
                return redirect("ingestion_batch_detail", batch_id=batch.id)
            count = batch.items.filter(id__in=ids).update(decision=decision)
            messages.success(request, f"Updated {count} selected item(s) to {decision}.")
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "bulk_delete":
            scope = (request.POST.get("scope") or "selected").strip()
            if scope == "filtered":
                target_qs = _filtered_qs_from_params(request.POST)
                n = target_qs.count()
                if not n:
                    messages.info(request, "No items match the current filters to delete.")
                    return redirect("ingestion_batch_detail", batch_id=batch.id)
                tomb_rows = list(target_qs.values("id", "original_url", "sha256", "phash"))
                _record_deleted_tombstones(batch, tomb_rows)
                deleted = _delete_ingestion_items_queryset(target_qs)
                messages.success(request, f"Deleted {deleted} item(s) matching filters.")
                return redirect("ingestion_batch_detail", batch_id=batch.id)
            raw_ids = (request.POST.get("selected_ids") or "").strip()
            ids: list[int] = []
            for token in raw_ids.split(","):
                token = token.strip()
                if token.isdigit():
                    ids.append(int(token))
            if not ids:
                messages.error(request, "No items selected.")
                return redirect("ingestion_batch_detail", batch_id=batch.id)
            target_qs = batch.items.filter(id__in=ids)
            tomb_rows = list(target_qs.values("id", "original_url", "sha256", "phash"))
            _record_deleted_tombstones(batch, tomb_rows)
            deleted = _delete_ingestion_items_queryset(target_qs)
            messages.success(request, f"Deleted {deleted} selected item(s).")
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "dedupe_visual_similar":
            try:
                max_h = int(request.POST.get("max_hamming") or 14)
            except (TypeError, ValueError):
                max_h = 14
            max_h = max(4, min(28, max_h))
            scope = (request.POST.get("dedupe_scope") or "pending_and_accepted").strip()
            accepted_only = scope == "accepted_only"
            dest_name = (batch.destination_source_name or "").strip()
            folder_shas, folder_phashes, folder_file_count = _destination_folder_dedupe_fingerprints(dest_name)
            stats_sha = _dedupe_batch_exact_sha256(
                batch, accepted_only=accepted_only, folder_shas=folder_shas
            )
            stats = _dedupe_batch_visual_crops(
                batch,
                max_hamming=max_h,
                accepted_only=accepted_only,
                folder_phashes=folder_phashes,
            )
            combined_ids = list(
                dict.fromkeys((stats_sha.get("rejected_ids") or []) + (stats.get("rejected_ids") or []))
            )
            scope_note = " (accepted only, 2nd pass)" if accepted_only else ""
            folder_line = ""
            if dest_name and folder_file_count:
                folder_line = (
                    f"Compared {folder_file_count} file(s) on disk in “{dest_name}” "
                    f"({len(folder_shas)} hashes, {len(folder_phashes)} image fingerprints). "
                )
            elif dest_name and not folder_file_count:
                folder_line = f"Destination folder “{dest_name}” is empty or missing — batch-only dedupe. "
            deleted_n = 0
            if request.POST.get("delete_rejected_after") == "on" and combined_ids:
                deleted_n = _delete_ingestion_items_queryset(batch.items.filter(id__in=combined_ids))
                parts = [
                    folder_line,
                    f"Dedupe{scope_note}: removed {deleted_n} duplicate row(s) and staged file(s).",
                    f"Same bytes: {stats_sha.get('folder_sha_duplicate', 0)} already on disk, {stats_sha.get('batch_duplicate', 0)} within batch.",
                    f"Visual: {stats.get('rejected_folder_visual', 0)} matched folder, {stats.get('rejected_batch_visual', 0)} matched another ingestion item (Hamming ≤ {max_h}).",
                    f"{stats['kept_canonical']} image(s) kept as largest in each similarity group.",
                    f"Skipped {stats['skipped_no_phash']} without a readable image file.",
                ]
            else:
                parts = [
                    folder_line,
                    f"Dedupe{scope_note}: same bytes — {stats_sha.get('folder_sha_duplicate', 0)} on disk, {stats_sha.get('batch_duplicate', 0)} in-batch; "
                    f"visual — {stats.get('rejected_folder_visual', 0)} folder, {stats.get('rejected_batch_visual', 0)} in-batch (Hamming ≤ {max_h}).",
                    f"{stats['kept_canonical']} image(s) kept (largest area first).",
                    f"Skipped {stats['skipped_no_phash']} without a readable image file.",
                ]
            messages.success(request, " ".join(parts))
            return redirect("ingestion_batch_detail", batch_id=batch.id)
        if action == "publish":
            ok, msg = _publish_batch_items(
                request.POST.get("destination_mode"),
                request.POST.get("destination_source_name"),
                request.POST.get("selected_existing_source"),
            )
            if not ok:
                messages.error(request, msg)
                return redirect("ingestion_batch_detail", batch_id=batch.id)
            messages.success(request, msg)
            return redirect("ingestion_batch_detail", batch_id=batch.id)

    filter_get = request.GET.copy()
    # Flat-grid UX: no implicit decision filter. Every row with a file on
    # disk shows up as a candidate; the user curates by selecting/deleting
    # or selecting/publishing. The decision_filter query param is still
    # honoured when explicitly set (e.g. from saved links) but defaults
    # to empty = show everything the grid-safe filter permits.
    items_qs = _filtered_qs_from_params(filter_get)
    from django.db.models import Count, Q  # local imports: used only in this view

    # One aggregate for total + per-decision counts beats five separate COUNT(*) round trips.
    # Previously the detail view issued total / pending / accepted / rejected / queue queries
    # in sequence; for a 1000+ item batch on SQLite that's a noticeable slice of page load.
    _agg = batch.items.aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(decision="pending")),
        accepted=Count("id", filter=Q(decision="accepted")),
        rejected=Count("id", filter=Q(decision="rejected")),
    )
    total_unfiltered_items = int(_agg.get("total") or 0)
    counts = {
        "pending": int(_agg.get("pending") or 0),
        "accepted": int(_agg.get("accepted") or 0),
        "rejected": int(_agg.get("rejected") or 0),
    }
    total_items = items_qs.count()
    # Legacy variable retained so the template (and any in-flight
    # browser tabs) don't crash on {{ decision_filter_auto_fallback }};
    # the flat-grid UX no longer uses the implicit-pending fallback.
    decision_filter_auto_fallback = False
    # Hard-cap the grid to a single page of 180 cards. Previously the paginator was
    # configured with page_size == total_items, so a batch with 1177 items rendered all
    # 1177 cards inline — the browser then decoded 1177 preview images on page load and
    # every accept/reject/delete round-trip re-rendered the same payload. 180 is large
    # enough to feel "all of it" for typical review sessions while keeping DOM + image
    # decode cost bounded. Client-side filter chips work on the visible page; a "Load
    # more" control pulls further pages via the same route with ?page=N.
    try:
        current_page = max(1, int(request.GET.get("page") or 1))
    except (TypeError, ValueError):
        current_page = 1
    paginator = Paginator(items_qs, 180)
    page_obj = paginator.get_page(current_page)
    items = []
    for it in page_obj.object_list:
        host = ""
        try:
            host = (urlparse(it.original_url or "").netloc or "").lower()
            if host.startswith("www."):
                host = host[4:]
        except Exception:
            host = ""
        items.append(
            {
                "obj": it,
                "preview_url": f"{settings.MEDIA_URL}ingestion_items/{it.file_path}",
                "is_image": it.media_kind == "image",
                "host": host,
                "pixels": int((it.width or 0) * (it.height or 0)),
                "ext": Path(it.file_path or "").suffix.lower().lstrip("."),
                # Short label summarising dedupe_note for the grid badge.
                # Full note is exposed via the badge's title attribute so users
                # can hover to see the exact reason (e.g. CLIP cosine value).
                "reject_reason_short": _short_reject_reason(it.dedupe_note or ""),
            }
        )
    existing_sources = [p.name for p in get_local_sources()]
    engine_diagnostics = {
        "gallery_dl": bool(py_shutil.which("gallery-dl")),
        "instaloader": bool(py_shutil.which("instaloader")),
        "yt_dlp": bool(py_shutil.which("yt-dlp")),
        "playwright": True,
    }
    run_summary = {
        "pages": 0,
        "candidates": 0,
        "imported": 0,
        "duplicates": 0,
        "too_small": 0,
        "failed": 0,
    }
    for key in run_summary.keys():
        m = re.search(rf"{key}=(\d+)", batch.last_scrape_report or "")
        if m:
            try:
                run_summary[key] = int(m.group(1))
            except Exception:
                pass
    destination_file_count = _destination_file_count(batch.destination_source_name)
    _dest_path = LOCAL_SOURCES_ROOT / (batch.destination_source_name or "").strip()
    destination_folder_linked = _dest_path.is_dir()
    campaign_target_count = max(100, int(batch.campaign_target_count or batch.target_asset_count or 800))
    campaign_remaining = max(0, campaign_target_count - destination_file_count)
    campaign_active_queue = counts["pending"] + counts["accepted"]
    campaign_queue_count = campaign_active_queue
    campaign_queue_headroom = max(0, campaign_target_count - campaign_active_queue)
    _campaign_cs = batch.campaign_state or {}
    campaign_last_seed_image_url = (_campaign_cs.get("last_campaign_seed_display_url") or "").strip()
    campaign_last_seed_caption = (_campaign_cs.get("last_campaign_seed_caption") or "").strip()
    campaign_search_prefs = _get_campaign_search_prefs(batch)
    campaign_vision_qc = _get_campaign_vision_qc_prefs(batch)
    campaign_dedupe = _get_campaign_dedupe_prefs(batch)
    campaign_archive = _get_campaign_archive_prefs(batch)
    campaign_seed_pin = _get_campaign_seed_pin_prefs(batch)
    campaign_archive_urls_text = "\n".join(campaign_archive.get("urls") or [])
    campaign_has_openai = bool((getattr(settings, "OPENAI_API_KEY", None) or "").strip())
    _raw_pv = _campaign_cs.get("preview_variety_seed")
    try:
        campaign_preview_variety_seed = (
            str(int(_raw_pv)) if _raw_pv is not None and str(_raw_pv).strip() != "" else ""
        )
    except (TypeError, ValueError):
        campaign_preview_variety_seed = ""
    next_seed_preview = _preview_campaign_next_seed(batch)
    next_seed_pinned_url = _preview_to_pinned_cbir_url(next_seed_preview) or ""
    last_engines_used = _campaign_cs.get("last_engines_used") or {}
    if not isinstance(last_engines_used, dict):
        last_engines_used = {}
    pinned_seed_url = str(_campaign_cs.get("pinned_cbir_seed_url") or "").strip()
    campaign_diag = {
        "last_trigger": str(_campaign_cs.get("last_trigger") or ""),
        "last_candidates": int(_campaign_cs.get("last_candidates") or 0),
        "last_imported": int(_campaign_cs.get("last_imported") or 0),
        "last_deduped": int(_campaign_cs.get("last_deduped") or 0),
        "last_filtered": int(_campaign_cs.get("last_filtered") or 0),
        "last_failed": int(_campaign_cs.get("last_failed") or 0),
        "last_cbir_seed_count": int(_campaign_cs.get("last_cbir_seed_count") or 0),
        "last_archive_mode": bool(_campaign_cs.get("last_archive_mode")),
        "last_seed_url": str(_campaign_cs.get("last_campaign_seed_display_url") or ""),
        "last_seed_caption": str(_campaign_cs.get("last_campaign_seed_caption") or ""),
        "last_engines_used": last_engines_used,
        "has_pinned_seed": bool(pinned_seed_url),
        "pinned_seed_url": pinned_seed_url,
    }
    return render(
        request,
        "admin/ingestion_batch_detail.html",
        {
            "batch": batch,
            "items": items,
            "counts": counts,
            "page_obj": page_obj,
            "existing_sources": existing_sources,
            "engine_diagnostics": engine_diagnostics,
            "run_summary": run_summary,
            "source_root_path": str(LOCAL_SOURCES_ROOT),
            "total_items": total_items,
            "total_unfiltered_items": total_unfiltered_items,
            "decision_filter_auto_fallback": decision_filter_auto_fallback,
            "destination_file_count": destination_file_count,
            "destination_folder_linked": destination_folder_linked,
            "campaign_target_count": campaign_target_count,
            "campaign_remaining": campaign_remaining,
            "campaign_queue_count": campaign_queue_count,
            "campaign_queue_headroom": campaign_queue_headroom,
            "campaign_last_seed_image_url": campaign_last_seed_image_url,
            "campaign_last_seed_caption": campaign_last_seed_caption,
            "campaign_search_prefs": campaign_search_prefs,
            "campaign_vision_qc": campaign_vision_qc,
            "campaign_dedupe": campaign_dedupe,
            "campaign_archive": campaign_archive,
            "campaign_seed_pin": campaign_seed_pin,
            "campaign_archive_urls_text": campaign_archive_urls_text,
            "campaign_has_openai": campaign_has_openai,
            "campaign_diag": campaign_diag,
            "campaign_preview_variety_seed": campaign_preview_variety_seed,
            "next_seed_preview": next_seed_preview,
            "next_seed_pinned_url": next_seed_pinned_url,
            "ai_query_pack_preview": (batch.ai_query_pack or [])[:30],
            "filters": {
                # No implicit decision filter in the flat-grid UX. The
                # hidden bulk-action forms still carry the current filter
                # string forwards (if one is set via URL) so they don't
                # clobber an explicitly-scoped bulk action.
                "decision_filter": (request.GET.get("decision_filter") or "").strip(),
                "orientation_filter": (request.GET.get("orientation_filter") or "").strip(),
                "color_filter": (request.GET.get("color_filter") or "").strip(),
                "min_width": (request.GET.get("min_width") or "").strip(),
                "min_height": (request.GET.get("min_height") or "").strip(),
            },
        },
    )


@require_POST
def ingestion_item_decision(request, item_id):
    item = get_object_or_404(IngestionItem, id=item_id)
    decision = (request.POST.get("decision") or "").strip()
    if decision not in {"pending", "accepted", "rejected"}:
        messages.error(request, "Invalid decision.")
        return redirect("ingestion_batch_detail", batch_id=item.batch_id)
    item.decision = decision
    item.save(update_fields=["decision"])
    return redirect("ingestion_batch_detail", batch_id=item.batch_id)
   
def new_source(request):
    return render(request, "admin/new-source.html")


def source_library(request):
    source_dirs = get_local_sources()
    sources = []
    image_exts = LOCAL_SOURCE_IMAGE_EXTS
    count_cap = 2000
    for source_dir in source_dirs:
        media_count = 0
        rep_file = None
        count_capped = False
        try:
            with os.scandir(source_dir) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    if not entry.is_file() or not _local_source_filename_allowed(entry.name):
                        continue
                    media_count += 1
                    if rep_file is None:
                        rep_file = source_dir / entry.name
                    if media_count >= count_cap:
                        count_capped = True
                        break
        except OSError:
            media_count = 0
            rep_file = None
            count_capped = False
        thumbnail_url = None
        if rep_file:
            if rep_file.suffix.lower() in image_exts:
                thumbnail_url = f"/source-media/{quote(source_dir.name, safe='')}/{quote(rep_file.name, safe='')}"
            else:
                thumbnail_url = f"/source-thumbnail/{quote(source_dir.name, safe='')}/{quote(rep_file.name, safe='')}"
        sources.append({
            "name": source_dir.name,
            "thumbnail_url": thumbnail_url,
            "file_count": media_count,
            "file_count_label": f"{media_count}+" if count_capped else str(media_count),
            "updated_at": datetime.fromtimestamp(source_dir.stat().st_mtime),
        })
    return render(request, "admin/source-library.html", {"sources": sources})


@require_POST
def refresh_source_library(request):
    snapshot_raw = request.POST.get("snapshot", "")
    snapshot = {}
    if snapshot_raw:
        try:
            parsed = json.loads(snapshot_raw)
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    try:
                        snapshot[str(key)] = int(value)
                    except (TypeError, ValueError):
                        continue
        except json.JSONDecodeError:
            snapshot = {}

    source_dirs = get_local_sources()
    current_counts = {}
    for source_dir in source_dirs:
        media_n = len(_local_source_dir_media_files(source_dir))
        current_counts[source_dir.name] = media_n
        Bucket.objects.get_or_create(
            name=source_dir.name,
            defaults={
                "type": "local",
                "source_id": "manual-refresh",
                "last_scraped": timezone.now().date(),
                "query_no": 0,
                "initial": 0,
                "max_num": media_n,
            },
        )

    new_sources = [name for name in current_counts.keys() if name not in snapshot]
    new_file_total = 0
    for name, count in current_counts.items():
        prev = snapshot.get(name, 0)
        if count > prev:
            new_file_total += (count - prev)

    if new_sources or new_file_total:
        messages.success(
            request,
            f"Refresh complete: found {len(new_sources)} new source folder(s) and {new_file_total} new file(s).",
        )
    else:
        messages.info(request, "Refresh complete: no new source folders or files detected.")
    return redirect("source-library")


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

def get_sample_image_url(bucket_name):
    source_dir = LOCAL_SOURCES_ROOT / bucket_name
    if not source_dir.exists():
        return None
    for file in source_dir.iterdir():
        if file.suffix.lower() in LOCAL_SOURCE_IMAGE_EXTS:
            return f"/media/{bucket_name}/{file.name}"
    return None



def _is_stock_image_candidate(filename: str) -> bool:
    """Best-effort stock image detection based on common filename markers."""
    lowered = (filename or "").lower()
    if not lowered:
        return False
    # Ignore non-image files; this detector is intended for still images.
    if Path(lowered).suffix not in LOCAL_SOURCE_IMAGE_EXTS:
        return False
    return any(marker in lowered for marker in _STOCK_FILENAME_MARKERS)

def list_buckets(request):
    buckets_data = []
    for source_dir in get_local_sources():
        image_url = get_sample_image_url(source_dir.name)
        buckets_data.append({
            "Name": source_dir.name,
            "CreationDate": datetime.fromtimestamp(source_dir.stat().st_mtime),
            "image_url": image_url
        })

    return render(request, 'admin/s3_buckets.html', {'buckets': buckets_data})
 
def bucket_contents(request, bucket_name):
    source_dir = LOCAL_SOURCES_ROOT / bucket_name
    if not source_dir.exists():
        messages.error(request, f"Source '{bucket_name}' not found.")
        return redirect("list_buckets")

    download_file = request.GET.get("download")
    if download_file:
        requested_path = (source_dir / download_file).resolve()
        if source_dir.resolve() not in requested_path.parents and requested_path != source_dir.resolve():
            messages.error(request, "Invalid file path.")
            return redirect("bucket_contents", bucket_name=bucket_name)
        if requested_path.exists() and requested_path.is_file():
            return FileResponse(open(requested_path, "rb"), as_attachment=True, filename=requested_path.name)
        messages.error(request, "File not found.")
        return redirect("bucket_contents", bucket_name=bucket_name)

    objects = []
    stray_count = 0
    for file in sorted(source_dir.iterdir()):
        if file.is_file() and not file.name.startswith("."):
            ext = file.suffix.lower()
            allowed = _local_source_path_allowed(file)
            if not allowed:
                stray_count += 1
            is_image = allowed and ext in LOCAL_SOURCE_IMAGE_EXTS
            is_video = allowed and ext in LOCAL_SOURCE_VIDEO_EXTS
            media_url = (
                f"/source-media/{quote(bucket_name, safe='')}/{quote(file.name, safe='')}"
                if (is_image or is_video)
                else None
            )
            # Images serve directly; videos use the thumbnail route, which extracts a frame.
            thumb_url = (
                media_url
                if is_image
                else (
                    f"/source-thumbnail/{quote(bucket_name, safe='')}/{quote(file.name, safe='')}"
                    if is_video
                    else None
                )
            )
            objects.append({
                "Key": file.name,
                "Size": file.stat().st_size,
                "LastModified": datetime.fromtimestamp(file.stat().st_mtime),
                "preview_url": media_url,
                "thumbnail_url": thumb_url,
                "is_image": is_image,
                "is_video": is_video,
                "is_stray": not allowed,
                "is_stock_candidate": _is_stock_image_candidate(file.name),
            })
    try:
        folder_updated_at = datetime.fromtimestamp(source_dir.stat().st_mtime)
    except OSError:
        folder_updated_at = None
    media_file_count = sum(1 for o in objects if not o.get("is_stray"))
    return render(
        request,
        "admin/bucket_contents.html",
        {
            "objects": objects,
            "bucket_name": bucket_name,
            "stray_count": stray_count,
            "media_file_count": media_file_count,
            "folder_updated_at": folder_updated_at,
        },
    )


def _detect_uniform_border_crop_box_from_array(
    arr,
    max_crop_fraction: float = 0.22,
    min_strip_std: float = 12.0,
    dark_thresh: float = 26.0,
    light_thresh: float = 229.0,
):
    """
    Detect black/white borders around an RGB image-like numpy array.
    Returns (left, top, right, bottom) crop box or None.
    """
    try:
        import numpy as np
    except Exception:
        return None

    if arr is None or arr.ndim < 2:
        return None
    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    if arr.shape[2] > 3:
        arr = arr[:, :, :3]
    h, w = arr.shape[:2]
    if h < 40 or w < 40:
        return None

    gray = (0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]).astype("float32")
    max_top = max(2, int(h * max_crop_fraction))
    max_left = max(2, int(w * max_crop_fraction))

    def is_border_strip(values):
        mean = float(values.mean())
        std = float(values.std())
        return std <= min_strip_std and (mean <= dark_thresh or mean >= light_thresh)

    top = 0
    while top < max_top and is_border_strip(gray[top:top + 1, :]):
        top += 1
    bottom = h
    while (h - bottom) < max_top and bottom > top + 1 and is_border_strip(gray[bottom - 1:bottom, :]):
        bottom -= 1
    left = 0
    while left < max_left and is_border_strip(gray[:, left:left + 1]):
        left += 1
    right = w
    while (w - right) < max_left and right > left + 1 and is_border_strip(gray[:, right - 1:right]):
        right -= 1

    cropped_w = right - left
    cropped_h = bottom - top
    if cropped_w < 64 or cropped_h < 64:
        return None
    if top + (h - bottom) + left + (w - right) < 4:
        return None
    return (left, top, right, bottom)


def _trim_image_borders_inplace(path: Path):
    try:
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            try:
                import numpy as np
            except Exception:
                return False, "numpy_unavailable"
            arr = np.array(rgb)
            box = _detect_uniform_border_crop_box_from_array(arr)
            if not box:
                return False, "no_border"
            cropped = rgb.crop(box)
            ext = path.suffix.lower()
            save_kwargs = {}
            if ext in {".jpg", ".jpeg"}:
                save_kwargs = {"quality": 95, "optimize": True}
            elif ext == ".webp":
                save_kwargs = {"quality": 95, "method": 6}
            tmp = path.with_suffix(path.suffix + ".trimtmp")
            cropped.save(tmp, **save_kwargs)
            os.replace(tmp, path)
            return True, None
    except UnidentifiedImageError:
        return False, "invalid_image"
    except Exception as e:
        return False, str(e)


def _detect_video_border_crop_box(path: Path):
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total <= 0:
        cap.release()
        return None
    sample_count = min(12, max(4, total // 20))
    sample_indices = sorted(set(int((i / max(1, sample_count - 1)) * (total - 1)) for i in range(sample_count)))
    boxes = []
    for idx in sample_indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        box = _detect_uniform_border_crop_box_from_array(frame_rgb)
        if box:
            boxes.append(box)
    cap.release()
    if not boxes:
        return None
    arr = np.array(boxes, dtype=np.int32)
    left = int(np.median(arr[:, 0]))
    top = int(np.median(arr[:, 1]))
    right = int(np.median(arr[:, 2]))
    bottom = int(np.median(arr[:, 3]))
    if right - left < 64 or bottom - top < 64:
        return None
    return (left, top, right, bottom)


def _trim_video_borders_inplace(path: Path):
    ext = path.suffix.lower()
    if ext not in {".mp4", ".mov", ".m4v", ".webm"}:
        return False, "unsupported_video_ext"
    box = _detect_video_border_crop_box(path)
    if not box:
        return False, "no_border"
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return False, "invalid_crop"
    tmp = path.with_suffix(path.suffix + ".trimtmp" + path.suffix)
    if ext == ".webm":
        cmd = [
            "ffmpeg", "-y", "-i", str(path),
            "-vf", f"crop={width}:{height}:{left}:{top}",
            "-c:v", "libvpx-vp9", "-crf", "34", "-b:v", "0",
            "-c:a", "libopus",
            str(tmp),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", str(path),
            "-vf", f"crop={width}:{height}:{left}:{top}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy",
            str(tmp),
        ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if proc.returncode != 0 or not tmp.exists():
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return False, "ffmpeg_failed"
        os.replace(tmp, path)
        return True, None
    except Exception as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return False, str(e)


@require_POST
def trim_source_borders(request, bucket_name):
    source_dir = (LOCAL_SOURCES_ROOT / bucket_name).resolve()
    root_dir = LOCAL_SOURCES_ROOT.resolve()
    if root_dir not in source_dir.parents or not source_dir.exists() or not source_dir.is_dir():
        messages.error(request, "Source not found.")
        return redirect("list_buckets")

    image_trimmed = 0
    video_trimmed = 0
    no_border = 0
    failed = 0
    for p in sorted(source_dir.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if not _local_source_path_allowed(p):
            continue
        ext = p.suffix.lower()
        if ext in LOCAL_SOURCE_IMAGE_EXTS:
            changed, reason = _trim_image_borders_inplace(p)
            if changed:
                image_trimmed += 1
            elif reason == "no_border":
                no_border += 1
            else:
                failed += 1
        elif ext in LOCAL_SOURCE_VIDEO_EXTS:
            changed, reason = _trim_video_borders_inplace(p)
            if changed:
                video_trimmed += 1
            elif reason in {"no_border", "unsupported_video_ext"}:
                no_border += 1
            else:
                failed += 1

    total_trimmed = image_trimmed + video_trimmed
    if total_trimmed:
        messages.success(
            request,
            f"Auto-trim complete: {total_trimmed} file(s) updated ({image_trimmed} image, {video_trimmed} video)."
            + (f" {no_border} had no detectable border." if no_border else "")
            + (f" {failed} failed." if failed else ""),
        )
    else:
        if failed:
            messages.warning(request, f"No files trimmed. {no_border} had no detectable border, {failed} failed.")
        else:
            messages.info(request, "No black/white borders detected to trim in this folder.")
    return redirect("bucket_contents", bucket_name=bucket_name)


def rename_source(request, bucket_name):
    if request.method != "POST":
        return redirect("bucket_contents", bucket_name=bucket_name)

    new_name = (request.POST.get("new_name") or "").strip()
    if not new_name:
        messages.error(request, "Please provide a new source name.")
        return redirect("bucket_contents", bucket_name=bucket_name)

    normalized_new_name = new_name.lower().replace(" ", "-")
    source_dir = (LOCAL_SOURCES_ROOT / bucket_name).resolve()
    new_source_dir = (LOCAL_SOURCES_ROOT / normalized_new_name).resolve()
    root_dir = LOCAL_SOURCES_ROOT.resolve()

    if root_dir not in source_dir.parents or source_dir == root_dir:
        messages.error(request, "Invalid source path.")
        return redirect("source-library")
    if root_dir not in new_source_dir.parents or new_source_dir == root_dir:
        messages.error(request, "Invalid new source name.")
        return redirect("bucket_contents", bucket_name=bucket_name)
    if not source_dir.exists():
        messages.error(request, "Source not found.")
        return redirect("source-library")
    if new_source_dir.exists():
        messages.error(request, f"Source '{normalized_new_name}' already exists.")
        return redirect("bucket_contents", bucket_name=bucket_name)

    try:
        source_dir.rename(new_source_dir)
        Bucket.objects.filter(name=bucket_name).update(name=normalized_new_name)
        messages.success(request, f"Source renamed to '{normalized_new_name}'.")
        return redirect("bucket_contents", bucket_name=normalized_new_name)
    except Exception as e:
        messages.error(request, f"Failed to rename source: {e}")
        return redirect("bucket_contents", bucket_name=bucket_name)


def delete_source_files(request, bucket_name):
    if request.method != "POST":
        return redirect("bucket_contents", bucket_name=bucket_name)

    selected_files = request.POST.getlist("selected_files")
    if not selected_files:
        messages.error(request, "No files selected.")
        return redirect("bucket_contents", bucket_name=bucket_name)

    source_dir = (LOCAL_SOURCES_ROOT / bucket_name).resolve()
    if not source_dir.exists():
        messages.error(request, "Source not found.")
        return redirect("source-library")

    deleted_count = 0
    for file_name in selected_files:
        try:
            target_file = (source_dir / file_name).resolve()
            if source_dir in target_file.parents and target_file.exists() and target_file.is_file():
                target_file.unlink()
                deleted_count += 1
        except Exception:
            continue

    if deleted_count:
        messages.success(request, f"Deleted {deleted_count} file(s).")
    else:
        messages.error(request, "No files were deleted.")
    return redirect("bucket_contents", bucket_name=bucket_name)


@require_POST
def remove_stray_source_files(request, bucket_name):
    """Delete non-media files (.txt, .json, .zip, etc.) from a composition source folder."""
    source_dir = (LOCAL_SOURCES_ROOT / bucket_name).resolve()
    root_dir = LOCAL_SOURCES_ROOT.resolve()
    if root_dir not in source_dir.parents or not source_dir.exists() or not source_dir.is_dir():
        messages.error(request, "Source not found.")
        return redirect("list_buckets")
    removed = 0
    for p in list(source_dir.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if _local_source_path_allowed(p):
            continue
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        messages.success(request, f"Removed {removed} non-media file(s).")
    else:
        messages.info(request, "No stray files to remove.")
    return redirect("bucket_contents", bucket_name=bucket_name)


def delete_duplicate_source_images(request, bucket_name):
    if request.method != "POST":
        return redirect("bucket_contents", bucket_name=bucket_name)

    source_dir = (LOCAL_SOURCES_ROOT / bucket_name).resolve()
    if not source_dir.exists():
        messages.error(request, "Source not found.")
        return redirect("source-library")

    seen_hashes = {}
    deleted_count = 0
    duplicate_groups = 0

    for file in sorted(source_dir.iterdir()):
        if not file.is_file() or file.name.startswith("."):
            continue
        if file.suffix.lower() not in LOCAL_SOURCE_IMAGE_EXTS:
            continue
        try:
            hasher = hashlib.sha256()
            with open(file, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
            if digest in seen_hashes:
                file.unlink()
                deleted_count += 1
                duplicate_groups += 1
            else:
                seen_hashes[digest] = file.name
        except Exception:
            continue

    if deleted_count:
        messages.success(
            request,
            f"Deleted {deleted_count} duplicate image(s) across {duplicate_groups} duplicate match(es).",
        )
    else:
        messages.info(request, "No duplicate images found.")
    return redirect("bucket_contents", bucket_name=bucket_name)


def download_source_files(request, bucket_name):
    if request.method != "POST":
        return redirect("bucket_contents", bucket_name=bucket_name)

    selected_files = request.POST.getlist("selected_files")
    if not selected_files:
        messages.error(request, "No files selected.")
        return redirect("bucket_contents", bucket_name=bucket_name)

    source_dir = (LOCAL_SOURCES_ROOT / bucket_name).resolve()
    if not source_dir.exists():
        messages.error(request, "Source not found.")
        return redirect("source-library")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zip_file:
        for file_name in selected_files:
            target_file = (source_dir / file_name).resolve()
            if source_dir in target_file.parents and target_file.exists() and target_file.is_file():
                zip_file.write(target_file, arcname=target_file.name)

    zip_buffer.seek(0)
    return FileResponse(
        zip_buffer,
        as_attachment=True,
        filename=f"{bucket_name}_selected_files.zip",
    )

@csrf_exempt
def delete_bucket(request):
    if request.method == 'POST':
        bucket_name = request.POST.get('bucket_name')
        if bucket_name:
            try:
                source_dir = LOCAL_SOURCES_ROOT / bucket_name
                if source_dir.exists():
                    shutil.rmtree(source_dir)
                messages.success(request, f"Bucket '{bucket_name}' deleted successfully.")
            except Exception as e:
                messages.error(request, f"Error deleting bucket: {e}")
    return redirect('list_buckets')  # replace with your actual bucket list view name
     
@csrf_exempt
def delete_buckets(request):
    if request.method == "POST":
        selected = request.POST.getlist("buckets")
        for bucket_name in selected:
            try:
                source_dir = LOCAL_SOURCES_ROOT / bucket_name
                if source_dir.exists():
                    shutil.rmtree(source_dir)
                Bucket.objects.filter(name=bucket_name).delete()

            except Exception as e:
                messages.error(request, f"Error deleting {bucket_name}: {e}")
        messages.success(request, "Selected buckets deleted successfully.")
    return HttpResponseRedirect(request.META.get('HTTP_REFERER'))


@require_POST
def dedupe_source_folders(request):
    """
    Merge duplicate source folders like foo-2_search into foo_search by file hash.
    Updates Composition and Bucket references from merged folders to canonical folders.
    """
    source_dirs = [p for p in get_local_sources() if p.is_dir()]
    grouped = defaultdict(list)
    for d in source_dirs:
        base = re.sub(r"-\d+(_(?:search|insta|pinterest))$", r"\1", d.name)
        grouped[base].append(d)

    merged_groups = 0
    moved_files = 0
    skipped_dupes = 0
    removed_dirs = 0
    rename_map = {}

    for base, dirs in grouped.items():
        if len(dirs) < 2:
            continue
        dirs = sorted(dirs, key=lambda p: p.name)
        canonical = dirs[0]
        canonical_hashes = {}
        for f in canonical.iterdir():
            if f.is_file():
                try:
                    with open(f, "rb") as fh:
                        canonical_hashes[hashlib.sha256(fh.read()).hexdigest()] = f.name
                except Exception:
                    continue
        for dup_dir in dirs[1:]:
            rename_map[dup_dir.name] = canonical.name
            for f in list(dup_dir.iterdir()):
                if not f.is_file():
                    continue
                if not _local_source_path_allowed(f):
                    continue
                try:
                    with open(f, "rb") as fh:
                        digest = hashlib.sha256(fh.read()).hexdigest()
                except Exception:
                    continue
                if digest in canonical_hashes:
                    skipped_dupes += 1
                    continue
                target = canonical / f.name
                if target.exists():
                    stem, ext = target.stem, target.suffix
                    n = 2
                    while (canonical / f"{stem}-{n}{ext}").exists():
                        n += 1
                    target = canonical / f"{stem}-{n}{ext}"
                try:
                    shutil.move(str(f), str(target))
                    moved_files += 1
                    canonical_hashes[digest] = target.name
                except Exception:
                    continue
            # Remove merged directory if no files remain.
            if not any(x.is_file() for x in dup_dir.iterdir()):
                try:
                    for x in list(dup_dir.iterdir()):
                        if x.is_dir():
                            shutil.rmtree(x, ignore_errors=True)
                    dup_dir.rmdir()
                    removed_dirs += 1
                except Exception:
                    pass
        merged_groups += 1

    if rename_map:
        for old_name, new_name in rename_map.items():
            Bucket.objects.filter(name=old_name).update(name=new_name)
        for comp in Composition.objects.all():
            bg = [rename_map.get(x, x) for x in (comp.background_sources or [])]
            fg = [rename_map.get(x, x) for x in (comp.foreground_sources or [])]
            ov = [rename_map.get(x, x) for x in (comp.overlay_sources or [])]
            if bg != (comp.background_sources or []) or fg != (comp.foreground_sources or []) or ov != (comp.overlay_sources or []):
                comp.background_sources = bg
                comp.foreground_sources = fg
                comp.overlay_sources = ov
                comp.save(update_fields=["background_sources", "foreground_sources", "overlay_sources"])

    if merged_groups:
        messages.success(
            request,
            (
                f"Merged {merged_groups} duplicate source group(s). "
                f"Moved {moved_files} unique file(s), skipped {skipped_dupes} duplicate file(s), "
                f"removed {removed_dirs} merged folder(s)."
            ),
        )
    else:
        messages.info(request, "No duplicate source folder groups found.")
    return redirect("source-library")


@csrf_exempt
def download_buckets(request):
    if request.method == "POST":
        selected = request.POST.getlist("buckets")

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            for bucket_name in selected:
                try:
                    source_dir = LOCAL_SOURCES_ROOT / bucket_name
                    if not source_dir.exists():
                        continue
                    for file in _local_source_dir_media_files(source_dir):
                        zip_path = f"{bucket_name}/{file.name}"
                        zip_file.write(file, arcname=zip_path)
                except Exception as e:
                    messages.error(request, f"Failed to download from {bucket_name}: {e}")

        zip_buffer.seek(0)
        return FileResponse(zip_buffer, as_attachment=True, filename="buckets.zip")

@csrf_exempt
def upload_file(request, bucket_name):
    if request.method == 'POST':
        files = request.FILES.getlist('files') or request.FILES.getlist('file')

        if len(files) > 50:
            messages.error(request, "You can upload a maximum of 50 files at once.")
            return redirect(request.path)
        source_dir = LOCAL_SOURCES_ROOT / bucket_name
        source_dir.mkdir(parents=True, exist_ok=True)

        io_error = False
        uploaded_n = 0
        skipped_n = 0

        for file in files:
            safe_name = Path(file.name).name
            if not _local_source_filename_allowed(safe_name):
                skipped_n += 1
                messages.error(
                    request,
                    f"Skipped (not allowed in source folders): {safe_name}",
                )
                continue
            try:
                target_path = source_dir / safe_name
                with open(target_path, "wb") as destination:
                    for chunk in file.chunks():
                        destination.write(chunk)
                uploaded_n += 1
            except Exception as e:
                io_error = True
                messages.error(request, f"Error uploading {safe_name}: {e}")

        if uploaded_n:
            messages.success(request, f"Uploaded {uploaded_n} file(s).")
        if skipped_n and not uploaded_n:
            messages.error(
                request,
                "Only image and video files are allowed (e.g. jpg, png, webp, gif, mp4, mov, webm).",
            )

        if not io_error:
            return redirect("bucket_contents", bucket_name=bucket_name)

    return render(request, "admin/upload.html", {"bucket_name": bucket_name})



@csrf_exempt
def delete_file_from_bucket(request, bucket_name, file_name):
    if request.method == 'POST':
        try:
            source_dir = (LOCAL_SOURCES_ROOT / bucket_name).resolve()
            target_file = (source_dir / file_name).resolve()
            if source_dir in target_file.parents and target_file.exists():
                target_file.unlink()
            messages.success(request, f"{file_name} deleted.")
        except Exception as e:
            messages.error(request, f"Failed to delete {file_name}: {e}")
        
        return redirect('bucket_contents', bucket_name=bucket_name)
     
class S3BucketAjaxView(AjaxDatatableView):
    model = S3Bucket
    initial_order = [["created_at", "desc"]]

    column_defs = [
        {"name": "id", "title": "ID"},
        {"name": "name", "title": "Bucket Name"},
        {"name": "created_at", "title": "Created At"},
    ]

### **User Registration & Authentication**

def register(request):
    if request.method == "POST":
        email = request.POST["email"]
        username = request.POST["username"]
        password = request.POST["password"]
        confirm_password = request.POST["confirm_password"]

        if password != confirm_password:
            messages.error(request, "Passwords do not match!")
            return redirect("register")

        if User.objects.filter(username=username).exists():
            messages.error(request, "Username already taken!")
            return redirect("register")

        if User.objects.filter(email=email).exists():
            messages.error(request, "Email already registered!")
            return redirect("register")

        user = User.objects.create_user(username=username, email=email, password=password)
        Profile.objects.get_or_create(user=user)
        messages.success(request, "Account created successfully! You can now log in.")
        return redirect("login")

    return render(request, "register.html")
### **Admin & Composition Management**
 
def home(request):
    return render(request, 'home.html')

def service(request):
    return render(request, 'service-detail.html')

def portfolio(request):
    return render(request, 'profile-detail.html')

def admin_login(request):
    if request.method == "POST":
        username = request.POST["username"]
        password = request.POST["password"]
        user = authenticate(request, username=username, password=password)

        if user is not None and user.is_staff:
            login(request, user)
            Profile.objects.get_or_create(user=user)
            return redirect("admin-dashboard")
        else:
            messages.error(request, "Invalid admin credentials!")
            return redirect("admin-login")

    return render(request, "admin/admin_login.html")

def admin_dashboard(request):
    return render(request, "admin/dashboard.html")


def mint_collection_settings(request):
    collection_settings = MintCollectionSettings.get_solo()
    if request.method == "POST":
        form = MintCollectionSettingsForm(request.POST, request.FILES, instance=collection_settings)
        if form.is_valid():
            form.save()
            messages.success(request, "Mint collection settings saved.")
            return redirect("mint-collection-settings")
    else:
        form = MintCollectionSettingsForm(instance=collection_settings)
    return render(
        request,
        "admin/mint_collection_settings.html",
        {
            "form": form,
            "collection_settings": collection_settings,
        },
    )

def convert_media_to_video(media_files, output_path, fps=24, duration_per_frame=2):
    """
    Converts a mix of images, GIFs, and videos into a video sequence.
    Each image is displayed for 1 second, and each frame of a video is displayed for 0.5 seconds.
    """
    clips = []

    for file in media_files:
        if file.lower().endswith((".mp4", ".mov", ".avi", ".mkv", ".gif")):
            video_clip = VideoFileClip(file)
            video_clip = video_clip.set_fps(2)  # 0.5 seconds per frame (2 FPS)
            clips.append(video_clip)
        else:
            img_clip = ImageSequenceClip([file], durations=[1])  # 1 second per image
            clips.append(img_clip)

    if not clips:
        return None

    final_clip = concatenate_videoclips(clips, method="compose")
    final_clip.write_videofile(output_path, fps=fps)
    
    return output_path

def ensure_even_dimensions(img):
    """Ensure image dimensions are even for FFmpeg compatibility."""
    width, height = img.size
    new_width = width if width % 2 == 0 else width + 1
    new_height = height if height % 2 == 0 else height + 1
    
    if (width, height) != (new_width, new_height):
        img = img.resize((new_width, new_height), RESAMPLING_METHOD)
    
    return img

def is_valid_image(file_path):
    """Check if the file is a valid image format."""
    try:
        with Image.open(file_path) as img:
            img.verify()  # Verify if it's an actual image
        return True
    except (IOError, UnidentifiedImageError):
        return False  # Not a valid image

def merge_images(left_files, right_files, output_folder):
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    
    left_files = [f for f in left_files if is_valid_image(f)]
    right_files = [f for f in right_files if is_valid_image(f)]
    
    merged_image_paths = []
    for i, (left_path, right_path) in enumerate(zip(left_files, right_files)):
        left_img = Image.open(left_path).convert("RGB")
        right_img = Image.open(right_path).convert("RGB")
        
        left_crop = left_img.crop((left_img.width // 2, 0, left_img.width, left_img.height))
        right_crop = right_img.crop((0, 0, right_img.width // 2, right_img.height))
        
        merged_img = Image.new('RGB', (left_crop.width + right_crop.width, left_img.height))
        merged_img.paste(left_crop, (0, 0))
        merged_img.paste(right_crop, (left_crop.width, 0))
        
        merged_img = ensure_even_dimensions(merged_img)
        merged_img_path = os.path.join(output_folder, f"merged_{i:03d}.png")  
        merged_img.save(merged_img_path)
        merged_image_paths.append(merged_img_path)
    
    return merged_image_paths

def create_video_ffmpeg(image_folder, output_video, fps=1):
    files = sorted([f for f in os.listdir(image_folder) if f.endswith('.png')])
    
    if not files:
        print("❌ No images found in merged folder. Exiting.")
        return None  # Return None if no images exist
    
    command = [
        "ffmpeg", "-y", "-framerate", str(fps), "-i", os.path.join(image_folder, "merged_%03d.png"),
        "-vf", "format=yuv420p", "-c:v", "libx264", "-r", "30", "-movflags", "+faststart", output_video
    ]
    
    result = subprocess.run(command, capture_output=True, text=True)
    
    if result.returncode == 0:
        print(f"✅ Video created successfully: {output_video}")
        return output_video
    else:
        print("❌ Error creating video. FFmpeg output:")
        print(result.stderr)
        return None  # Return None if video creation fails
   


def add_composition(request, composition_id=None):
    # Define paths for downloaded images & videos 
    TEMP_BG_FOLDER = "media/temp_s3_back_files"
    TEMP_FG_FOLDER = "media/temp_s3_fore_files"
    VIDEO_DIR = "media/videos"
    TEMP_IMAGE_FOLDER = "media/temp_images"
    THUMBNAIL_DIR = "static/composition_thumbnails"
    AUDIO_DIR = "compositions/audios"
    MERGED_IMAGE_DIR = "media/merged_images"
    
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

    def redirect_to_composition_view_with_page(preserve_page: bool = True):
        if preserve_page and return_page:
            return redirect(f"{reverse('composition-view')}?page={return_page}")
        return redirect("composition-view")

    if request.method == "POST":
        def normalize_transition_mode(raw_value: str, fallback: str = "none") -> str:
            raw = (raw_value or fallback or "none").strip().lower()
            compact = raw.replace(" ", "").replace("-", "").replace("_", "")
            if compact == "crossfade":
                return "crossfade"
            if compact == "fade":
                return "fade"
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
                return redirect("composition-add")
        if composition_type in {"cylinder-room", "triple-cylinder-room"} and source_type != "upload":
            if not foreground_sources:
                messages.error(request, "Please select at least one foreground source.")
                return redirect("composition-add")
        if composition_type in {"single", "vibrate", "morph-fast", "eye-morph", "center-shift", "star-shift", "prism-burst", "prism-burst-neon", "sleep-drift", "calm-wave", "anger-surge", "anxiety-loop", "box-room"} and source_type != "upload":
            if not background_sources:
                messages.error(request, "Please select at least one background source.")
                return redirect("composition-add")
        if composition_type in {"psychedelic", "psychedelic-calm", "psychedelic-intense"} and source_type != "upload":
            if not background_sources:
                messages.error(request, "Please select at least one background source.")
                return redirect("composition-add")
        if composition_type == "kaleidoscope" and source_type != "upload":
            if not background_sources:
                messages.error(request, "Please select at least one background source.")
                return redirect("composition-add")
        if composition_type == "strobe" and source_type != "upload":
            if not background_sources:
                messages.error(request, "Please select at least one background source.")
                return redirect("composition-add")
        if composition_type in {"tunnel", "psychedelic-tunnel", "tunnel-burst", "swirl", "quad", "mash", "mash-fine", "mash-fine-flux", "mash-superfine", "mash-superfine-flux", "vertical-stripes", "horizontal-stripes", "vertical-stripes-thick", "horizontal-stripes-thick", "social-scroll", "dirty-scroll", "scrollhole", "strobe", "vibrate", "morph-fast", "eye-morph", "center-shift", "star-shift", "prism-burst", "prism-burst-neon", "sleep-drift", "calm-wave", "anger-surge", "anxiety-loop", "box-room"} and not background_sources:
            messages.error(request, "Please select at least one background source.")
            return redirect("composition-add")
        if composition_type == "associations":
            has_seed_url = bool((request.POST.get("associations_seed_url") or "").strip())
            has_seed_source = bool((request.POST.get("associations_seed_source") or "").strip())
            has_seed_upload = bool(request.FILES.get("associations_seed_upload"))
            has_local_pool = bool(background_sources or foreground_sources)
            if not (has_seed_url or has_seed_source or has_seed_upload or has_local_pool):
                messages.error(request, "Associations mode needs a seed image (URL, upload, or source folder) or local sources.")
                return redirect("composition-add")

        base_url = request.POST.get("base_url", "").rstrip("/") or f"http://{request.get_host()}"
        url_slug = request.POST.get("url_slug", "").strip().strip("/")
        requested_name = (request.POST.get("name") or "").strip()
        if not url_slug and requested_name:
            url_slug = slugify(requested_name)
        full_url = f"{base_url}/{url_slug}" if base_url and url_slug else None
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
        elif overlay_effect_alias in {"rotate3d", "spin3d", "rotate"}:
            overlay_effect = "rotate_3d"
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
                return redirect("composition-add")
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
            messages.success(request, "Composition updated successfully.")
            return redirect_to_composition_view_with_page(preserve_page=True)
        else:
            new_comp = Composition.objects.create(**create_kwargs)
            new_comp.allowed_personas.set(
                MonologuePersona.objects.filter(id__in=allowed_persona_ids)
            )
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
                full_url = f"{base_url}/{url_slug}" if base_url and url_slug else None
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
            full_url = f"{base_url}/{url_slug}" if base_url and url_slug else None
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
            full_url = f"{base_url}/{url_slug}" if base_url and url_slug else None
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
            full_url = f"{base_url}/{url_slug}" if base_url and url_slug else None
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
        "return_page": return_page,
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
            rows.append(
                {
                    "relative_path": rel_path,
                    "source_name": source_name,
                    "name": media_path.name,
                    "size": size,
                    "included": rel_path not in excluded,
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


def download_s3_files(bucket_name, download_folder):
    downloaded_files = []
    source_dir = LOCAL_SOURCES_ROOT / bucket_name
    if not source_dir.exists():
        return downloaded_files

    os.makedirs(download_folder, exist_ok=True)
    for file in _local_source_dir_media_files(source_dir):
        dest_path = os.path.join(download_folder, file.name)
        shutil.copy2(file, dest_path)
        downloaded_files.append(dest_path)
    return downloaded_files
    

def convert_images_to_video(images, output_path, duration_per_image=2, target_size=(1280, 720)):
    """Convert a list of images into a video, ensuring all images are the same size."""
    if not images:
        return None  # No images, return None

    resized_images = []

    for img_path in images:
        img = Image.open(img_path)
        img = img.resize(target_size, Image.LANCZOS)  # ✅ Resize image
        temp_path = img_path.replace(".jpg", "_resized.jpg").replace(".png", "_resized.png")
        img.save(temp_path)
        resized_images.append(temp_path)

    if len(resized_images) == 1:
        # ✅ If only one image, use ImageClip instead of ImageSequenceClip
        video = mp.ImageClip(resized_images[0], duration=duration_per_image)
    else:
        # ✅ Use ImageSequenceClip for multiple images
        video = mp.ImageSequenceClip(resized_images, fps=1 / duration_per_image)

    # ✅ Write video file with correct format
    video.write_videofile(output_path, fps=24, codec="libx264")

    return output_path


def validate_images(image_list):
    """Check if images exist and are valid."""
    valid_images = []
    for img in image_list:
        if not os.path.exists(img):
            print(f"❌ Error: File {img} not found")
        else:
            try:
                with Image.open(img) as img_obj:
                    img_obj.verify()  # Verify the image
                    valid_images.append(img)  # Add to valid list
                    print(f"✅ Valid image: {img}")
            except Exception as e:
                print(f"❌ Invalid image: {img}, Error: {e}")

    return valid_images  # ✅ Return only valid images



def delete_temp_files(folder_path):
    """Delete all files in a given folder."""
    for file in os.listdir(folder_path):
        file_path = os.path.join(folder_path, file)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Error deleting file {file_path}: {e}")



def generate_video_thumbnail(video_path, thumbnail_path, time="00:00:01"):
    try:
        (
            ffmpeg
            .input(video_path, ss=time)
            .output(thumbnail_path, vframes=1)
            .run(capture_stdout=True, capture_stderr=True)
        )
        print(f"✅ Thumbnail created at: {thumbnail_path}")
    except ffmpeg.Error as e:
        print(f"❌ Error generating thumbnail: {e.stderr.decode()}")



def upload_to_s3(file_path, s3_key, bucket_name="your-s3-bucket-name"):
    """Legacy helper kept for compatibility in local mode."""
    if not os.path.exists(file_path):
        return None
    return file_path




def composition_view(request):
    search_query = (request.GET.get("q") or "").strip()
    compositions_list = Composition.objects.all().prefetch_related("media_assets")
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
        comp.nft_online_video_ready = any(
            asset.kind == "preview_15s" and asset.status == "ready" and bool(getattr(asset, "file", None))
            for asset in comp.media_assets.all()
        )
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
    full_url = f"{host_origin.rstrip('/')}/{slug}/" if host_origin else f"/{slug}/"

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
        "overlay_scale": round(random.uniform(0.7, 1.3), 2),
        "overlay_rotate": overlay_effect == "rotate_3d",
        "overlay_fit": "free",
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


@require_POST
def seed_character_compositions(request):
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


def user_logout(request):
    logout(request)
    messages.success(request, "You have been logged out successfully.")
    return redirect('admin-login')  # Replace 'login' with the name of your login page URL


def delete_compositions(request):
    """Delete selected compositions from the database."""

    if request.method == 'POST':
        try:
            # ✅ Get selected composition IDs from request
            ids_to_delete = json.loads(request.POST.get('compositions_to_delete', '[]'))
            print(f"🗑️ Selected IDs for deletion: {ids_to_delete}")  # Debugging log
            
            if not ids_to_delete:
                messages.error(request, "No compositions selected for deletion.")
                return redirect('composition-view')

            # ✅ Retrieve compositions from the database
            compositions = Composition.objects.filter(id__in=ids_to_delete)
            print(f"✅ Found {compositions.count()} compositions in the database.")  # Debugging log

            if not compositions.exists():
                messages.error(request, "No matching compositions found.")
                return redirect('composition-view')

            purge_ids = list(compositions.values_list("id", flat=True))
            with transaction.atomic():
                deleted_count = compositions.delete()[0]
                print(f"✅ Deleted {deleted_count} compositions from DB")
            _purge_composition_preview_cache(purge_ids)
            messages.success(request, f"✅ {deleted_count} selected compositions deleted successfully!")

        except Exception as e:
            print(f"❌ Error: {e}")
            messages.error(request, f"An error occurred: {e}")

    return redirect('composition-view') 
    
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


def _build_nft_metadata_payload(request, composition: Composition) -> dict:
    title = (composition.nft_name or "").strip() or (composition.name or f"Composition #{composition.id}")
    description = (composition.nft_description or "").strip() or (
        f"Dynamic composition artwork generated from evolving source pools. "
        f"Mode: {composition.nft_mode or 'live'}."
    )
    generated_assets = composition_media_assets(composition)
    poster_asset = generated_assets.get("poster")
    preview_asset = generated_assets.get("preview_15s")
    collector_asset = generated_assets.get("collector_45s")
    image_url = (
        _storage_file_url(request, poster_asset.file.name)
        if poster_asset and poster_asset.file and poster_asset.status == "ready"
        else request.build_absolute_uri(reverse("composition_nft_image", kwargs={"composition_id": int(composition.id)}))
    )
    animation_url = (
        _storage_file_url(request, preview_asset.file.name)
        if preview_asset and preview_asset.file and preview_asset.status == "ready"
        else request.build_absolute_uri(reverse("composition_nft_video", kwargs={"composition_id": int(composition.id)}))
    )
    collector_video_url = (
        _storage_file_url(request, collector_asset.file.name)
        if collector_asset and collector_asset.file and collector_asset.status == "ready"
        else ""
    )
    mode = (composition.nft_mode or "live").strip().lower()
    external_url = (composition.nft_external_url or "").strip() or (composition.page_url or "").strip() or (composition.url or "").strip()
    attributes = [
        {"trait_type": "Composition ID", "value": int(composition.id)},
        {"trait_type": "Mode", "value": mode},
        {"trait_type": "Type", "value": (composition.type or "classic")},
        {"trait_type": "Mood", "value": (composition.mood_rating or "mid")},
        {"trait_type": "Duration Seconds", "display_type": "number", "value": 10},
        {"trait_type": "Collector Video Seconds", "display_type": "number", "value": 45},
        {"trait_type": "Playback Speed", "display_type": "number", "value": float(composition.playback_speed or 1.0)},
        {"trait_type": "Source Playback", "value": (composition.source_playback_mode or "chronological")},
        {"trait_type": "Brightness", "display_type": "number", "value": int(composition.brightness or 50)},
        {"trait_type": "Saturation", "display_type": "number", "value": int(composition.saturation or 50)},
        {"trait_type": "Opacity", "display_type": "number", "value": int(composition.opacity or 100)},
    ]
    hashtags = [str(t).strip() for t in (composition.composition_hashtags or []) if str(t).strip()]
    if hashtags:
        attributes.append({"trait_type": "Themes", "value": ", ".join(hashtags[:12])})
    return {
        "name": title,
        "description": description,
        "image": image_url,
        "animation_url": animation_url,
        "external_url": external_url,
        "attributes": attributes,
        "properties": {
            "composition_id": int(composition.id),
            "composition_url": (composition.url or "").strip(),
            "page_url": (composition.page_url or "").strip(),
            "metadata_generated_at": timezone.now().isoformat(),
            "nft_enabled": bool(composition.nft_enabled),
            "mode": mode,
            "collector_video_url": collector_video_url,
        },
    }


def _composition_nft_settings_snapshot(composition: Composition) -> dict:
    return {
        "composition_id": int(composition.id),
        "name": composition.name,
        "type": composition.type,
        "url": composition.url or "",
        "page_url": composition.page_url or "",
        "background_sources": composition.background_sources or [],
        "foreground_sources": composition.foreground_sources or [],
        "overlay_sources": composition.overlay_sources or [],
        "brightness": composition.brightness,
        "saturation": composition.saturation,
        "opacity": composition.opacity,
        "transition": composition.transition,
        "playback_speed": composition.playback_speed,
        "source_playback_mode": composition.source_playback_mode,
        "filter_preset": composition.filter_preset,
        "filter_intensity": composition.filter_intensity,
        "filter_param_1": composition.filter_param_1,
        "filter_param_2": composition.filter_param_2,
        "filter_param_3": composition.filter_param_3,
        "filter_settings": composition.filter_settings or {},
        "composition_hashtags": composition.composition_hashtags or [],
        "nft_mode": composition.nft_mode or "live",
        "snapshotted_at": timezone.now().isoformat(),
    }


def _storage_copy(src: str, dst: str) -> str:
    if default_storage.exists(dst):
        default_storage.delete(dst)
    with default_storage.open(src, "rb") as fh:
        default_storage.save(dst, ContentFile(fh.read()))
    return dst


def _storage_file_url(request, file_name: str) -> str:
    if not file_name:
        return ""
    try:
        return request.build_absolute_uri(default_storage.url(file_name))
    except Exception:
        return ""


def _composition_media_context(request, composition: Composition) -> dict:
    current_signature = composition_source_signature(composition)
    assets = composition_media_assets(composition)
    rows = {}
    for kind, label in (
        ("poster", "Poster still"),
        ("preview_15s", "10s marketplace preview"),
        ("collector_45s", "45s collector video"),
    ):
        asset = assets.get(kind)
        rows[kind] = {
            "label": label,
            "asset": asset,
            "ready": bool(asset and asset.status == "ready" and asset.file),
            "status": asset.status if asset else "missing",
            "url": _storage_file_url(request, asset.file.name) if asset and asset.file and asset.status == "ready" else "",
            "stale": bool(asset and asset.source_signature and asset.source_signature != current_signature),
            "generated_at": asset.generated_at if asset else None,
            "error": asset.error_message if asset else "",
        }
    return {
        "assets": rows,
        "stale": any(row["stale"] for row in rows.values()),
        "ready": all(row["ready"] for row in rows.values()),
    }


def _build_nft_version_metadata_payload(request, nft: CompositionNFT) -> dict:
    composition = nft.composition
    title = (composition.nft_name or "").strip() or composition.name or f"Composition #{composition.id}"
    name = f"{title} v{int(nft.version_number)}"
    description = (composition.nft_description or "").strip() or (
        "A live composition with a durable Ethereum NFT checkpoint. "
        "The token stores a recent rendered video snapshot and links back to the living composition."
    )
    image_url = nft.image_uri or request.build_absolute_uri(
        reverse("composition_nft_version_image", kwargs={"nft_id": int(nft.id)})
    )
    animation_url = nft.animation_uri or request.build_absolute_uri(
        reverse("composition_nft_version_video", kwargs={"nft_id": int(nft.id)})
    )
    collector_video_url = (
        request.build_absolute_uri(reverse("composition_nft_version_collector_video", kwargs={"nft_id": int(nft.id)}))
        if nft.local_collector_video_file
        else ""
    )
    external_url = nft.live_url or (composition.url or "")
    attributes = [
        {"trait_type": "Composition ID", "value": int(composition.id)},
        {"trait_type": "Version", "display_type": "number", "value": int(nft.version_number)},
        {"trait_type": "Chain", "value": "Ethereum"},
        {"trait_type": "Mode", "value": (composition.nft_mode or "live")},
        {"trait_type": "Type", "value": (composition.type or "classic")},
        {"trait_type": "Source Playback", "value": (composition.source_playback_mode or "random")},
        {"trait_type": "Duration Seconds", "display_type": "number", "value": 10},
        {"trait_type": "Collector Video Seconds", "display_type": "number", "value": 45},
    ]
    if nft.parent_id:
        attributes.append({"trait_type": "Parent Version", "display_type": "number", "value": int(nft.parent.version_number)})
    hashtags = [str(t).strip() for t in (composition.composition_hashtags or []) if str(t).strip()]
    if hashtags:
        attributes.append({"trait_type": "Themes", "value": ", ".join(hashtags[:12])})
    return {
        "name": name,
        "description": description,
        "image": image_url,
        "animation_url": animation_url,
        "external_url": external_url,
        "attributes": attributes,
        "properties": {
            "composition_id": int(composition.id),
            "composition_url": composition.url or "",
            "version": int(nft.version_number),
            "parent_version": int(nft.parent.version_number) if nft.parent_id else None,
            "chain": nft.chain,
            "chain_id": int(nft.chain_id or 1),
            "contract_address": nft.contract_address,
            "token_id": nft.token_id,
            "settings_snapshot": nft.settings_snapshot or {},
            "metadata_generated_at": timezone.now().isoformat(),
            "collector_video_url": collector_video_url,
        },
    }


def _prepare_composition_nft_version(request, composition: Composition, wallet: str = "") -> CompositionNFT:
    latest = composition.nft_versions.order_by("-version_number").first()
    next_version = int(latest.version_number) + 1 if latest else 1
    nft = CompositionNFT.objects.create(
        composition=composition,
        parent=latest if latest and latest.status == "minted" else None,
        version_number=next_version,
        status="rendering",
        chain="ethereum",
        chain_id=int(getattr(settings, "NFT_ETH_CHAIN_ID", 1) or 1),
        contract_address=(getattr(settings, "NFT_ETH_CONTRACT_ADDRESS", "") or "").strip(),
        live_url=(composition.url or "").strip(),
        minter_wallet=wallet,
        owner_wallet=wallet,
        settings_snapshot=_composition_nft_settings_snapshot(composition),
        prepared_at=timezone.now(),
    )
    try:
        media_assets = composition_media_assets(composition)
        required_kinds = ["poster", "preview_15s", "collector_45s"]
        missing_required = [
            kind
            for kind in required_kinds
            if not media_assets.get(kind)
            or media_assets[kind].status != "ready"
            or not media_assets[kind].file
            or not default_storage.exists(media_assets[kind].file.name)
        ]
        if missing_required:
            media_assets.update(generate_composition_media_assets(composition, force=False, kinds=missing_required))
        poster_asset = media_assets.get("poster")
        preview_asset = media_assets.get("preview_15s")
        collector_asset = media_assets.get("collector_45s")
        if not poster_asset or not poster_asset.file or poster_asset.status != "ready":
            raise RuntimeError("Poster still is not ready.")
        if not preview_asset or not preview_asset.file or preview_asset.status != "ready":
            raise RuntimeError("10s preview video is not ready.")
        if not collector_asset or not collector_asset.file or collector_asset.status != "ready":
            raise RuntimeError("45s collector video is not ready.")
        base = f"nft/versions/composition_{int(composition.id)}/v{int(nft.version_number)}"
        nft.local_image_file = _storage_copy(poster_asset.file.name, f"{base}/image.jpg")
        nft.local_video_file = _storage_copy(preview_asset.file.name, f"{base}/animation.mp4")
        nft.local_collector_video_file = _storage_copy(collector_asset.file.name, f"{base}/collector-45s.mp4")
        metadata_payload = _build_nft_version_metadata_payload(request, nft)
        nft.local_metadata_file = f"{base}/metadata.json"
        if default_storage.exists(nft.local_metadata_file):
            default_storage.delete(nft.local_metadata_file)
        default_storage.save(nft.local_metadata_file, ContentFile(json.dumps(metadata_payload, indent=2).encode("utf-8")))
        nft.token_uri = request.build_absolute_uri(reverse("composition_nft_version_metadata", kwargs={"nft_id": int(nft.id)}))
        nft.status = "ready"
        nft.error_message = ""
    except Exception as exc:
        nft.status = "failed"
        nft.error_message = str(exc)[:1000]
    nft.save()
    composition.nft_enabled = True
    composition.nft_last_generated_at = timezone.now()
    composition.nft_metadata_file = nft.local_metadata_file
    composition.save(update_fields=["nft_enabled", "nft_last_generated_at", "nft_metadata_file"])
    return nft


def _nft_public_state(request, composition: Composition, wallet: str = "") -> dict:
    versions = list(composition.nft_versions.order_by("version_number"))
    latest = versions[-1] if versions else None
    latest_minted = next((n for n in reversed(versions) if n.status == "minted"), None)
    current_signature = composition_source_signature(composition)
    generated_assets = composition_media_assets(composition)
    media_state = {}
    def _media_size_label(file_name: str) -> str:
        try:
            size = int(default_storage.size(file_name))
        except Exception:
            return ""
        if size >= 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        if size >= 1024:
            return f"{size / 1024:.0f} KB"
        return f"{size} B"

    def _date_label(value) -> str:
        if not value:
            return ""
        try:
            return timezone.localtime(value).strftime("%b %-d, %Y %-I:%M %p")
        except Exception:
            return value.isoformat() if hasattr(value, "isoformat") else str(value)

    for kind in ["poster", "preview_15s", "collector_45s"]:
        asset = generated_assets.get(kind)
        file_name = asset.file.name if asset and asset.file and asset.status == "ready" else ""
        media_state[kind] = {
            "ready": bool(asset and asset.status == "ready" and asset.file),
            "status": asset.status if asset else "",
            "url": _storage_file_url(request, file_name) if file_name else "",
            "stale": bool(asset and asset.source_signature and asset.source_signature != current_signature),
            "generated_at": asset.generated_at.isoformat() if asset and asset.generated_at else "",
            "generated_at_label": _date_label(asset.generated_at) if asset and asset.generated_at else "",
            "error": asset.error_message if asset else "",
            "size_label": _media_size_label(file_name) if file_name else "",
        }
    wallet_clean = str(wallet or "").strip().lower()
    owner_wallet = str((latest_minted or latest).owner_wallet if (latest_minted or latest) else "").strip().lower()
    minted_owner_wallet = str(latest_minted.owner_wallet if latest_minted else "").strip().lower()
    wallet_owns_latest_minted = bool(wallet_clean and minted_owner_wallet and wallet_clean == minted_owner_wallet)
    ready_for_mint = bool(getattr(composition, "ready_for_deployment", False))
    owner_can_prepare = not latest_minted or (wallet_clean and owner_wallet and wallet_clean == owner_wallet)
    can_prepare = ready_for_mint and owner_can_prepare
    return {
        "enabled": ready_for_mint,
        "ready_for_mint": ready_for_mint,
        "network_name": getattr(settings, "NFT_ETH_NETWORK_NAME", "Ethereum"),
        "chain_id": int(getattr(settings, "NFT_ETH_CHAIN_ID", 1) or 1),
        "contract_address": (getattr(settings, "NFT_ETH_CONTRACT_ADDRESS", "") or "").strip(),
        "contract_configured": bool((getattr(settings, "NFT_ETH_CONTRACT_ADDRESS", "") or "").strip()),
        "marketplace_base_url": getattr(settings, "NFT_ETH_MARKETPLACE_BASE_URL", ""),
        "storage_guidance_url": getattr(settings, "NFT_STORAGE_GUIDANCE_URL", ""),
        "latest_version": int(latest.version_number) if latest else 0,
        "latest_status": latest.status if latest else "",
        "latest_token_id": latest.token_id if latest else "",
        "latest_owner_wallet": (latest_minted or latest).owner_wallet if (latest_minted or latest) else "",
        "latest_metadata_url": (
            request.build_absolute_uri(reverse("composition_nft_version_metadata", kwargs={"nft_id": int(latest.id)}))
            if latest and latest.local_metadata_file
            else ""
        ),
        "latest_video_url": (
            request.build_absolute_uri(reverse("composition_nft_version_video", kwargs={"nft_id": int(latest.id)}))
            if latest and latest.local_video_file
            else ""
        ),
        "latest_collector_video_url": (
            request.build_absolute_uri(reverse("composition_nft_version_collector_video", kwargs={"nft_id": int(latest.id)}))
            if latest and latest.local_collector_video_file
            else ""
        ),
        "owns_latest_minted": wallet_owns_latest_minted,
        "owner_download_url": (
            request.build_absolute_uri(reverse("composition_nft_version_collector_video", kwargs={"nft_id": int(latest_minted.id)})) + "?download=1"
            if wallet_owns_latest_minted and latest_minted and latest_minted.local_collector_video_file
            else ""
        ),
        "media": media_state,
        "media_stale": any(item["stale"] for item in media_state.values()),
        "can_prepare": can_prepare,
        "blocked_reason": "" if can_prepare else (
            "Composition is not marked ready in the composition bank."
            if not ready_for_mint
            else "Only the current NFT owner can prepare the next version."
        ),
        "next_version": (int(latest.version_number) + 1) if latest else 1,
        "versions": [
            {
                "id": int(n.id),
                "version": int(n.version_number),
                "status": n.status,
                "token_id": n.token_id,
                "owner_wallet": n.owner_wallet,
                "tx_hash": n.tx_hash,
                "metadata_url": request.build_absolute_uri(reverse("composition_nft_version_metadata", kwargs={"nft_id": int(n.id)})) if n.local_metadata_file else "",
                "video_url": request.build_absolute_uri(reverse("composition_nft_version_video", kwargs={"nft_id": int(n.id)})) if n.local_video_file else "",
                "collector_video_url": request.build_absolute_uri(reverse("composition_nft_version_collector_video", kwargs={"nft_id": int(n.id)})) if n.local_collector_video_file else "",
                "collector_download_url": request.build_absolute_uri(reverse("composition_nft_version_collector_video", kwargs={"nft_id": int(n.id)})) + "?download=1" if n.local_collector_video_file else "",
                "collector_size_label": _media_size_label(n.local_collector_video_file) if n.local_collector_video_file else "",
                "updated_at_label": _date_label(n.updated_at),
            }
            for n in versions[-8:]
        ],
    }


def _storage_local_path_or_temp(name: str, suffix: str = "") -> tuple[str | None, str | None]:
    try:
        p = default_storage.path(name)
        if p and os.path.exists(p):
            return p, None
    except Exception:
        pass
    if not default_storage.exists(name):
        return None, None
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    try:
        with default_storage.open(name, "rb") as fh:
            tmp.write(fh.read())
        tmp.flush()
    finally:
        tmp.close()
    return tmp_path, tmp_path


def _ensure_square_nft_image(composition: Composition, size: int = 1080) -> str:
    rel_path = f"nft/images/composition_{int(composition.id)}_square.jpg"
    src_image = None
    if composition.img:
        try:
            img_name = str(composition.img.name or "").strip()
            if img_name and default_storage.exists(img_name):
                with default_storage.open(img_name, "rb") as fh:
                    src_image = Image.open(fh).convert("RGB")
                    src_image.load()
        except Exception:
            src_image = None

    if src_image is None:
        # Last-resort fallback: deterministic placeholder, still NFT-safe 1:1.
        src_image = Image.new("RGB", (size, size), (12, 12, 12))

    out = ImageOps.fit(src_image, (size, size), Image.Resampling.LANCZOS)
    blob = io.BytesIO()
    out.save(blob, format="JPEG", quality=92, optimize=True)
    if default_storage.exists(rel_path):
        default_storage.delete(rel_path)
    default_storage.save(rel_path, ContentFile(blob.getvalue()))
    return rel_path


def _ensure_square_nft_video(
    composition: Composition,
    duration_seconds: int = 30,
    size: int = 1080,
    force_regen: bool = False,
) -> str:
    rel_path = f"nft/video/composition_{int(composition.id)}_square_{int(duration_seconds)}s.mp4"
    if not force_regen and default_storage.exists(rel_path):
        return rel_path

    input_path = None
    input_tmp = None
    static_image_mode = False
    try:
        if composition.final_video and default_storage.exists(composition.final_video.name):
            input_path, input_tmp = _storage_local_path_or_temp(
                composition.final_video.name,
                suffix=".mp4",
            )
        if not input_path:
            image_rel = _ensure_square_nft_image(composition, size=size)
            input_path, input_tmp = _storage_local_path_or_temp(image_rel, suffix=".jpg")
            static_image_mode = True
        if not input_path or not os.path.exists(input_path):
            raise FileNotFoundError("No source media available for NFT video export.")

        out_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        out_path = out_tmp.name
        out_tmp.close()
        vf = (
            f"scale={size}:{size}:force_original_aspect_ratio=increase,"
            f"crop={size}:{size},fps=30,format=yuv420p"
        )
        cmd = ["ffmpeg", "-y"]
        if static_image_mode:
            cmd += ["-loop", "1", "-i", input_path]
        else:
            cmd += ["-stream_loop", "-1", "-i", input_path]
        cmd += [
            "-t",
            str(int(duration_seconds)),
            "-an",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            out_path,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError(proc.stderr or "ffmpeg failed")

        if default_storage.exists(rel_path):
            default_storage.delete(rel_path)
        with open(out_path, "rb") as fh:
            default_storage.save(rel_path, ContentFile(fh.read()))
        try:
            os.remove(out_path)
        except Exception:
            pass
        return rel_path
    finally:
        if input_tmp:
            try:
                os.remove(input_tmp)
            except Exception:
                pass


def composition_nft_image(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    rel_path = _ensure_square_nft_image(composition, size=1080)
    if not default_storage.exists(rel_path):
        raise Http404("NFT image is unavailable.")
    try:
        fh = default_storage.open(rel_path, "rb")
    except OSError:
        raise Http404("Unable to read NFT image.")
    resp = FileResponse(fh, content_type="image/jpeg")
    resp["Cache-Control"] = "no-cache"
    resp["Content-Disposition"] = 'inline; filename="composition-nft-square.jpg"'
    return resp


def composition_nft_video(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    rel_path = _ensure_square_nft_video(composition, duration_seconds=30, size=1080, force_regen=False)
    if not default_storage.exists(rel_path):
        raise Http404("NFT video is unavailable.")
    try:
        fh = default_storage.open(rel_path, "rb")
    except OSError:
        raise Http404("Unable to read NFT video.")
    resp = FileResponse(fh, content_type="video/mp4")
    resp["Cache-Control"] = "no-cache"
    resp["Content-Disposition"] = 'inline; filename="composition-nft-loop-30s.mp4"'
    return resp


def composition_nft_metadata(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    _ensure_square_nft_image(composition, size=1080)
    _ensure_square_nft_video(composition, duration_seconds=30, size=1080, force_regen=False)
    payload = _build_nft_metadata_payload(request, composition)
    return JsonResponse(payload)


def composition_nft_version_image(request, nft_id):
    nft = get_object_or_404(CompositionNFT, id=nft_id)
    if not nft.local_image_file or not default_storage.exists(nft.local_image_file):
        raise Http404("NFT version image is unavailable.")
    resp = FileResponse(default_storage.open(nft.local_image_file, "rb"), content_type="image/jpeg")
    resp["Cache-Control"] = "public, max-age=3600"
    resp["Content-Disposition"] = f'inline; filename="composition-{nft.composition_id}-v{nft.version_number}.jpg"'
    return resp


def composition_nft_version_video(request, nft_id):
    nft = get_object_or_404(CompositionNFT, id=nft_id)
    if not nft.local_video_file or not default_storage.exists(nft.local_video_file):
        raise Http404("NFT version video is unavailable.")
    resp = FileResponse(default_storage.open(nft.local_video_file, "rb"), content_type="video/mp4")
    resp["Cache-Control"] = "public, max-age=3600"
    resp["Content-Disposition"] = f'inline; filename="composition-{nft.composition_id}-v{nft.version_number}.mp4"'
    return resp


def composition_nft_version_collector_video(request, nft_id):
    nft = get_object_or_404(CompositionNFT, id=nft_id)
    if not nft.local_collector_video_file or not default_storage.exists(nft.local_collector_video_file):
        raise Http404("NFT version collector video is unavailable.")
    resp = FileResponse(default_storage.open(nft.local_collector_video_file, "rb"), content_type="video/mp4")
    resp["Cache-Control"] = "public, max-age=3600"
    disposition = "attachment" if request.GET.get("download") == "1" else "inline"
    resp["Content-Disposition"] = f'{disposition}; filename="composition-{nft.composition_id}-v{nft.version_number}-collector-45s.mp4"'
    return resp


def composition_nft_version_metadata(request, nft_id):
    nft = get_object_or_404(CompositionNFT, id=nft_id)
    payload = _build_nft_version_metadata_payload(request, nft)
    return JsonResponse(payload)


def composition_nft_status(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    wallet = (request.GET.get("wallet") or "").strip()
    return JsonResponse({"ok": True, "nft": _nft_public_state(request, composition, wallet=wallet)})


@require_POST
def composition_prepare_nft_version(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    wallet = (request.POST.get("wallet") or "").strip()
    state = _nft_public_state(request, composition, wallet=wallet)
    if not state.get("can_prepare"):
        return JsonResponse({"ok": False, "error": state.get("blocked_reason") or "NFT version cannot be prepared yet."}, status=403)
    nft = _prepare_composition_nft_version(request, composition, wallet=wallet)
    status = 200 if nft.status == "ready" else 500
    return JsonResponse(
        {
            "ok": nft.status == "ready",
            "nft_id": int(nft.id),
            "version": int(nft.version_number),
            "status": nft.status,
            "error": nft.error_message,
            "metadata_url": request.build_absolute_uri(reverse("composition_nft_version_metadata", kwargs={"nft_id": int(nft.id)})),
            "video_url": request.build_absolute_uri(reverse("composition_nft_version_video", kwargs={"nft_id": int(nft.id)})) if nft.local_video_file else "",
            "token_uri": nft.token_uri,
            "nft": _nft_public_state(request, composition, wallet=wallet),
        },
        status=status,
    )


@require_POST
def composition_record_nft_mint(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    nft_id = payload.get("nft_id") or request.POST.get("nft_id")
    nft = get_object_or_404(CompositionNFT, id=nft_id, composition=composition)
    wallet = str(payload.get("owner_wallet") or payload.get("wallet") or request.POST.get("owner_wallet") or "").strip()
    tx_hash = str(payload.get("tx_hash") or request.POST.get("tx_hash") or "").strip()
    token_id = str(payload.get("token_id") or request.POST.get("token_id") or "").strip()
    metadata_uri = str(payload.get("metadata_uri") or request.POST.get("metadata_uri") or "").strip()
    animation_uri = str(payload.get("animation_uri") or request.POST.get("animation_uri") or "").strip()
    image_uri = str(payload.get("image_uri") or request.POST.get("image_uri") or "").strip()
    if not wallet or not tx_hash:
        return JsonResponse({"ok": False, "error": "owner_wallet and tx_hash are required."}, status=400)
    nft.owner_wallet = wallet
    nft.minter_wallet = nft.minter_wallet or wallet
    nft.tx_hash = tx_hash
    nft.token_id = token_id
    nft.metadata_uri = metadata_uri
    nft.token_uri = metadata_uri or nft.token_uri
    nft.animation_uri = animation_uri
    nft.image_uri = image_uri
    nft.status = "minted"
    nft.minted_at = timezone.now()
    nft.save()
    return JsonResponse({"ok": True, "nft": _nft_public_state(request, composition, wallet=wallet)})


def composition_generate_nft(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    generate_composition_media_assets(composition, force=True)
    payload = _build_nft_metadata_payload(request, composition)
    rel_path = f"nft/metadata/composition_{int(composition.id)}.json"
    if default_storage.exists(rel_path):
        default_storage.delete(rel_path)
    default_storage.save(rel_path, ContentFile(json.dumps(payload, indent=2).encode("utf-8")))
    composition.nft_enabled = True
    composition.nft_last_generated_at = timezone.now()
    composition.nft_metadata_file = rel_path
    composition.save(update_fields=["nft_enabled", "nft_last_generated_at", "nft_metadata_file"])
    messages.success(
        request,
        "NFT metadata generated. Use the metadata URL for minting tokenURI.",
    )
    return redirect("composition-edit", composition_id=composition.id)


@require_POST
def composition_generate_nft_media(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    force = request.POST.get("force") == "1"
    results = generate_composition_media_assets(composition, force=force)
    failed = [asset for asset in results.values() if asset.status != "ready"]
    if failed:
        messages.error(
            request,
            "Some NFT media failed: " + "; ".join(f"{asset.kind}: {asset.error_message}" for asset in failed),
        )
    else:
        messages.success(request, "NFT media generated: poster still, 10s preview, and 45s collector video.")
    return redirect("composition-edit", composition_id=composition.id)


@require_POST
def generate_all_nft_media(request):
    force = request.POST.get("force") == "1"
    compositions = (
        Composition.objects.filter(ready_for_deployment=True)
        .exclude(url__isnull=True)
        .exclude(url__exact="")
        .order_by("id")
    )
    processed = 0
    failed = []
    for composition in compositions:
        processed += 1
        results = generate_composition_media_assets(composition, force=force)
        for asset in results.values():
            if asset.status != "ready":
                failed.append(f"{composition.name} {asset.kind}: {asset.error_message}")
    if failed:
        messages.error(request, f"Generated media for {processed} compositions with {len(failed)} failures. First: {failed[0]}")
    else:
        messages.success(request, f"Generated NFT media for {processed} ready compositions.")
    return redirect("composition-view")


@require_POST
def generate_selected_nft_media(request):
    try:
        selected_ids = json.loads(request.POST.get("composition_ids", "[]"))
        selected_ids = [int(cid) for cid in selected_ids]
    except (TypeError, ValueError):
        selected_ids = []

    if not selected_ids:
        messages.error(request, "Select at least one composition before generating media.")
        return redirect("composition-view")

    force = request.POST.get("force") == "1"
    compositions = Composition.objects.filter(id__in=selected_ids).exclude(url__isnull=True).exclude(url__exact="").order_by("id")
    processed = 0
    failed = []
    for composition in compositions:
        processed += 1
        results = generate_composition_media_assets(composition, force=force)
        for asset in results.values():
            if asset.status != "ready":
                failed.append(f"{composition.name} {asset.kind}: {asset.error_message}")

    missing_url_count = len(selected_ids) - processed
    if failed:
        messages.error(request, f"Generated media for {processed} selected composition(s) with {len(failed)} failure(s). First: {failed[0]}")
    else:
        suffix = f" {missing_url_count} selected composition(s) were skipped because they do not have a URL." if missing_url_count else ""
        messages.success(request, f"Generated NFT media for {processed} selected composition(s).{suffix}")
    return redirect("composition-view")


@require_POST
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


def monologue_public_page(request, slug):
    persona = get_object_or_404(monologue_persona_queryset(), slug=slug, is_published=True)
    segments = build_segments(persona)
    context = {
        "persona": persona,
        "segment_count": len(segments),
        "typing_min_ms": persona.typing_min_ms,
        "typing_max_ms": persona.typing_max_ms,
        "pause_between_segments_ms": persona.pause_between_segments_ms,
        "stream_endpoint": reverse("monologue_segment", kwargs={"slug": persona.slug}),
        "bg_endpoint": reverse("monologue_background_images", kwargs={"slug": persona.slug}),
        "has_openai": bool(getattr(settings, "OPENAI_API_KEY", None) or ""),
    }
    return render(request, "monologue_public.html", context)


@csrf_exempt
def monologue_next_segment(request, slug):
    persona = get_object_or_404(monologue_persona_queryset(), slug=slug, is_published=True)

    # Legacy index mode (?i=) for old clients / tests — cycles curated segments
    if request.method == "GET" and request.GET.get("i") is not None:
        segments = build_segments(persona)
        if not segments:
            return JsonResponse({"segment": "", "index": 0, "total": 0, "mode": "legacy"})
        try:
            idx = int(request.GET.get("i", 0))
        except ValueError:
            idx = 0
        n = len(segments)
        idx = idx % n
        return JsonResponse(
            {
                "segment": segments[idx],
                "index": idx,
                "total": n,
                "mode": "legacy",
            }
        )

    recent: list[str] = []
    if request.method == "POST":
        try:
            body = json.loads(request.body.decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        raw_recent = body.get("recent")
        if isinstance(raw_recent, list):
            recent = [str(x)[:650] for x in raw_recent if isinstance(x, (str, int, float))]

    text, err = generate_stream_segment(persona, recent)
    if err is None and text:
        return JsonResponse({"segment": text, "mode": "generated"})

    text_fb = pick_fallback_segment(persona, recent)
    payload = {
        "segment": text_fb,
        "mode": "fallback",
        "hint": (
            "Set OPENAI_API_KEY for endless, non-repeating lines driven by this character’s corpus."
            if err == "no_api_key"
            else "Using curated lines until generation recovers."
        ),
    }
    if err and err != "no_api_key":
        payload["detail"] = err[:500]
    if not text_fb:
        payload["mode"] = "error"
        payload["segment"] = ""
    return JsonResponse(payload)


@csrf_exempt
def monologue_background_images(request, slug):
    """Return (and optionally refresh) og:image URLs harvested from source_urls."""
    persona = get_object_or_404(monologue_persona_queryset(), slug=slug, is_published=True)
    if not monologue_visual_columns_ready():
        return JsonResponse({"urls": [], "cache_ready": False})
    urls = list(persona.cached_visual_urls or [])
    stale = True
    if persona.visual_cache_updated_at:
        stale = (timezone.now() - persona.visual_cache_updated_at).total_seconds() > 7 * 86400
    need = len(urls) < 4 or stale
    if need and persona.source_urls:
        try:
            urls = refresh_persona_visual_cache(persona, max_pages=10)
        except Exception:
            urls = list(persona.cached_visual_urls or [])
    return JsonResponse({"urls": urls, "cache_ready": True})


def _monologue_unique_slug(desired: str, exclude_pk=None) -> str:
    base = (desired or "").strip() or "thought"
    slug = base
    counter = 2
    while True:
        qs = monologue_persona_queryset().filter(slug=slug)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        if not qs.exists():
            return slug
        slug = f"{base}-{counter}"
        counter += 1


def monologue_list(request):
    personas = monologue_persona_queryset().order_by("-updated_at")
    return render(request, "admin/monologue_list.html", {"personas": personas})


def monologue_edit(request, persona_id=None):
    persona = get_object_or_404(monologue_persona_queryset(), id=persona_id) if persona_id else None
    if request.method == "POST":
        form = MonologuePersonaForm(request.POST, instance=persona)
        if form.is_valid():
            obj = form.save(commit=False)
            raw_slug = (obj.slug or "").strip()
            if not raw_slug:
                raw_slug = slugify((obj.title or "").strip()) or "thought"
            obj.slug = _monologue_unique_slug(raw_slug, exclude_pk=obj.pk)
            obj.save()
            messages.success(
                request,
                f'Saved "{obj.title}". Open: /thoughts/{obj.slug}/',
            )
            return redirect("monologue_list")
    else:
        form = MonologuePersonaForm(instance=persona)
    return render(
        request,
        "admin/monologue_form.html",
        {
            "form": form,
            "persona": persona,
            "is_new": persona is None,
        },
    )


def _scenario_unique_slug(raw: str, exclude_pk=None) -> str:
    base = slugify(raw or "") or "advanced-thought"
    slug = base
    n = 2
    while True:
        qs = AdvancedThoughtScenario.objects.filter(slug=slug)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        if not qs.exists():
            return slug
        slug = f"{base}-{n}"
        n += 1


def advanced_thoughts(request):
    scenarios = AdvancedThoughtScenario.objects.all().order_by("-updated_at")
    return render(request, "admin/advanced_thoughts.html", {"scenarios": scenarios})


def advanced_thoughts_edit(request, scenario_id=None):
    scenario = get_object_or_404(AdvancedThoughtScenario, id=scenario_id) if scenario_id else None
    if request.method == "POST":
        title = (request.POST.get("title") or "").strip()
        character_name = (request.POST.get("character_name") or "").strip()
        archetype = (request.POST.get("archetype") or "").strip()
        character_brief = (request.POST.get("character_brief") or "").strip()
        world_context = (request.POST.get("world_context") or "").strip()
        tone_hint = (request.POST.get("tone_hint") or "").strip()
        current_location = (request.POST.get("current_location") or "").strip()
        try:
            sim_minutes = int(request.POST.get("sim_minutes") or (8 * 60))
        except ValueError:
            sim_minutes = 8 * 60
        try:
            tick_interval_seconds = int(request.POST.get("tick_interval_seconds") or 7)
        except ValueError:
            tick_interval_seconds = 7
        is_published = bool(request.POST.get("is_published"))
        desires_text = (request.POST.get("seed_desires") or "").strip()
        subconscious_text = (request.POST.get("seed_subconscious") or "").strip()
        anchors_text = (request.POST.get("scene_anchors") or "").strip()
        relationships_text = (request.POST.get("relationship_state") or "").strip()
        desires = [x.strip() for x in desires_text.splitlines() if x.strip()]
        subconscious = [x.strip() for x in subconscious_text.splitlines() if x.strip()]
        anchors = [x.strip() for x in anchors_text.splitlines() if x.strip()]
        relationship_state = [x.strip() for x in relationships_text.splitlines() if x.strip()]
        if not title:
            messages.error(request, "Title is required.")
            return redirect("advanced_thoughts_add")

        if scenario is None:
            scenario = AdvancedThoughtScenario()
        scenario.title = title
        scenario.slug = _scenario_unique_slug((request.POST.get("slug") or title), exclude_pk=scenario.pk)
        scenario.character_name = character_name
        scenario.archetype = archetype
        scenario.character_brief = character_brief
        scenario.world_context = world_context
        scenario.tone_hint = tone_hint
        scenario.current_location = current_location or scenario.current_location or "bedroom"
        scenario.sim_minutes = max(0, min(24 * 60 - 1, sim_minutes))
        scenario.tick_interval_seconds = max(3, min(60, tick_interval_seconds))
        scenario.is_published = is_published
        scenario.seed_desires = desires
        scenario.seed_subconscious = subconscious
        scenario.scene_bible = {
            "location": scenario.current_location,
            "anchors": anchors,
        }
        scenario.relationship_graph = relationship_state
        scenario.save()
        messages.success(request, f'Saved advanced scenario "{scenario.title}".')
        return redirect("advanced_thoughts")

    context = {
        "scenario": scenario,
        "is_new": scenario is None,
    }
    return render(request, "admin/advanced_thoughts_form.html", context)


def advanced_thoughts_delete(request, scenario_id):
    if request.method != "POST":
        return redirect("advanced_thoughts")
    scenario = get_object_or_404(AdvancedThoughtScenario, id=scenario_id)
    title = scenario.title
    scenario.delete()
    messages.success(request, f'Deleted "{title}".')
    return redirect("advanced_thoughts")


def advanced_thoughts_public(request, slug):
    scenario = get_object_or_404(AdvancedThoughtScenario, slug=slug, is_published=True)
    context = {
        "scenario": scenario,
        "tick_url": reverse("advanced_thoughts_tick", kwargs={"slug": scenario.slug}),
    }
    return render(request, "advanced_thoughts_public.html", context)


@csrf_exempt
def advanced_thoughts_tick(request, slug):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    scenario = get_object_or_404(AdvancedThoughtScenario, slug=slug, is_published=True)
    payload, err = generate_advanced_tick(scenario)
    if not isinstance(payload, dict):
        payload = {}
    if not payload.get("thought"):
        payload["thought"] = scenario.last_thought or "I hold still and reassess."
    if "subconscious" not in payload:
        payload["subconscious"] = scenario.last_subconscious or scenario.seed_subconscious or []
    if "desires" not in payload:
        payload["desires"] = scenario.last_desires or scenario.seed_desires or []
    if "cognitive_trace" not in payload:
        payload["cognitive_trace"] = scenario.last_subconscious or []
    if "scene_anchors" not in payload:
        payload["scene_anchors"] = (scenario.scene_bible or {}).get("anchors", [])
    if "relationship_state" not in payload:
        payload["relationship_state"] = scenario.relationship_graph or []
    if "sim_minutes" not in payload:
        payload["sim_minutes"] = int(scenario.sim_minutes or (8 * 60))
    if "location" not in payload:
        payload["location"] = scenario.current_location or "bedroom"
    if "backup_image_url" not in payload:
        payload["backup_image_url"] = "https://loremflickr.com/1600/1024/interior,room,cinematic?lock=1"
    payload["next_tick_ms"] = max(3, int(scenario.tick_interval_seconds or 7)) * 1000

    # Strict coupling mode: only advance cognition when a *new* image is produced.
    new_img = (payload.get("image_url") or "").strip()
    image_generated = bool(payload.get("image_generated"))
    if (not image_generated) or (not new_img):
        payload["thought"] = scenario.last_thought or payload.get("thought") or "Holding state."
        payload["subconscious"] = scenario.last_subconscious or payload.get("subconscious") or []
        payload["desires"] = scenario.last_desires or payload.get("desires") or []
        payload["cognitive_trace"] = payload.get("cognitive_trace") or ["Holding prior state until next new frame."]
        payload["minutes_advance"] = 0
        payload["sim_minutes"] = int(scenario.sim_minutes or (8 * 60))
        payload["location"] = scenario.current_location or payload.get("location") or "bedroom"
        payload["continuity_note"] = scenario.continuity_note or payload.get("continuity_note") or ""
        payload["image_generated"] = False
        warn = payload.get("warning")
        payload["warning"] = (
            (f"{warn} " if warn else "")
            + "Strict mode: waiting for a fresh image before advancing thought."
        ).strip()
    if err:
        payload["warning"] = err

    scenario.last_thought = payload.get("thought", "")[:1000]
    scenario.last_image_url = payload.get("image_url", "")[:1000]
    scenario.last_visual_prompt = payload.get("visual_prompt", "")[:4000]
    scenario.last_desires = payload.get("desires", [])
    scenario.last_subconscious = payload.get("subconscious", [])
    scenario.current_location = (payload.get("location") or scenario.current_location or "bedroom")[:255]
    scenario.continuity_note = (payload.get("continuity_note") or "")[:1200]
    try:
        scenario.sim_minutes = int(payload.get("sim_minutes") or scenario.sim_minutes or (8 * 60))
    except (TypeError, ValueError):
        pass
    anchors = payload.get("scene_anchors")
    if isinstance(anchors, list):
        scenario.scene_bible = {
            "location": scenario.current_location,
            "anchors": [str(x).strip() for x in anchors if str(x).strip()][:8],
        }
    rel = payload.get("relationship_state")
    if isinstance(rel, list):
        scenario.relationship_graph = [str(x).strip() for x in rel if str(x).strip()][:8]
    scenario.save()
    return JsonResponse(payload)


def monologue_delete(request, persona_id):
    if request.method != "POST":
        return redirect("monologue_list")
    persona = get_object_or_404(monologue_persona_queryset(), id=persona_id)
    title = persona.title
    persona.delete()
    messages.success(request, f'Deleted "{title}".')
    return redirect("monologue_list")


@require_POST
def monologue_ai_research(request, persona_id):
    """
    Merge curated URLs from research_packs/<archetype>.json, then optionally call OpenAI
    (OPENAI_API_KEY) for extra URLs + tone notes. Does not browse the live web.
    """
    persona = get_object_or_404(monologue_persona_queryset(), id=persona_id)
    pack = load_pack(persona.archetype)
    new_urls = pack.get("suggested_urls") or []
    existing = list(persona.source_urls or [])
    if not isinstance(existing, list):
        existing = []
    seen = {str(u).strip() for u in existing}
    added_pack = 0
    for u in new_urls:
        u = (u or "").strip()
        if u and u not in seen:
            existing.append(u)
            seen.add(u)
            added_pack += 1
    persona.source_urls = existing

    tag = (pack.get("corpus_marker") or "").strip()
    addition = (pack.get("corpus_addition") or "").strip()
    corpus = (persona.style_corpus or "").strip()
    if addition:
        marker_block = f"[{tag}]" if tag else None
        if marker_block and marker_block in corpus:
            pass
        elif addition in corpus:
            pass
        else:
            if tag:
                block = f"\n\n{marker_block}\n{addition}"
            else:
                block = f"\n\n{addition}"
            persona.style_corpus = (corpus + block).strip() if corpus else addition.strip()
            corpus = (persona.style_corpus or "").strip()

    added_ai_urls = 0
    ai_note = ""
    if (getattr(settings, "OPENAI_API_KEY", None) or "").strip():
        ai_urls, corpus_add, sys_frag, err = run_monologue_research(persona)
        if err:
            messages.warning(request, f"OpenAI: {err}")
        else:
            for u in ai_urls:
                u = (u or "").strip()
                if u and u not in seen:
                    persona.source_urls.append(u)
                    seen.add(u)
                    added_ai_urls += 1
            if corpus_add and corpus_add not in (persona.style_corpus or ""):
                block = f"\n\n[OpenAI research]\n{corpus_add}"
                persona.style_corpus = ((persona.style_corpus or "").strip() + block).strip()
            if sys_frag and sys_frag not in (persona.system_prompt_hint or ""):
                hint = (persona.system_prompt_hint or "").strip()
                persona.system_prompt_hint = (
                    f"{hint}\n\n{sys_frag}".strip() if hint else sys_frag
                )
            ai_note = f" OpenAI added {added_ai_urls} URL(s) and tone notes."
    else:
        messages.info(
            request,
            "Set OPENAI_API_KEY in your .env for AI-suggested URLs and tone notes (static pack merged).",
        )

    persona.save()
    if persona.source_urls and monologue_visual_columns_ready():
        try:
            refresh_persona_visual_cache(persona, max_pages=10)
        except Exception:
            pass
    messages.success(
        request,
        f"Research updated: {added_pack} new URL(s) from curated pack.{ai_note}",
    )
    return redirect("monologue_edit", persona_id=persona.id)


def _find_composition_by_slug(page_slug: str) -> Composition | None:
    compositions = Composition.objects.exclude(url__isnull=True).exclude(url__exact="")
    for comp in compositions:
        normalized = comp.url.rstrip("/")
        if normalized.endswith(f"/{page_slug}"):
            return comp
    return None


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
    matched = _find_composition_by_slug(page_slug)
    if not matched:
        fallback_redirect = _fallback_redirect_for_public_slug(page_slug)
        if fallback_redirect:
            return fallback_redirect
        raise Http404("Composition page not found")
    candidates = Composition.objects.exclude(url__isnull=True).exclude(url__exact="").exclude(id=matched.id)
    random_links = [comp.url for comp in candidates if comp.url]
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
    }
    return render(request, "composition_public.html", context)


def _mintable_compositions_queryset():
    return (
        Composition.objects.filter(ready_for_deployment=True)
        .select_related("series")
        .exclude(url__isnull=True)
        .exclude(url__exact="")
        .order_by("-created_at", "-id")
    )


def _mint_navigation(page_slug: str) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    rows: list[dict[str, str]] = []
    for comp in _mintable_compositions_queryset():
        slug = _composition_slug_from_url(comp.url or "")
        if not slug:
            continue
        rows.append({"slug": slug, "name": comp.name, "url": reverse("mint_composition", kwargs={"page_slug": slug})})
    idx = next((i for i, row in enumerate(rows) if row["slug"] == page_slug), None)
    if idx is None:
        return None, None
    prev_row = rows[idx - 1] if idx > 0 else rows[-1] if len(rows) > 1 else None
    next_row = rows[(idx + 1) % len(rows)] if len(rows) > 1 else None
    return prev_row, next_row


def _related_mint_rows(request, page_slug: str, limit: int = 5) -> list[dict]:
    rows = []
    for comp in _mintable_compositions_queryset():
        slug = _composition_slug_from_url(comp.url or "")
        if not slug or slug == page_slug:
            continue
        state = _nft_public_state(request, comp)
        rows.append(
            {
                "composition": comp,
                "slug": slug,
                "mint_url": reverse("mint_composition", kwargs={"page_slug": slug}),
                "render_url": (comp.url or "").strip() + ("&render=1" if "?" in (comp.url or "") else "?render=1") if (comp.url or "").strip() else "",
                "preview_url": reverse("composition-preview", kwargs={"composition_id": int(comp.id)}),
                "nft": state,
            }
        )
    random.shuffle(rows)
    return rows[:limit]


def mint_site(request):
    compositions = _mintable_compositions_queryset()
    collection_settings = MintCollectionSettings.get_solo()
    rows = []
    for comp in compositions:
        slug = _composition_slug_from_url(comp.url or "")
        if not slug:
            continue
        state = _nft_public_state(request, comp)
        metadata_terms = [
            comp.name or "",
            comp.type or "",
            slug,
            comp.series.name if comp.series else "",
            comp.mood_rating or "",
            *[str(item) for item in (comp.composition_hashtags or [])],
            *[str(item) for item in (comp.composition_emotions or [])],
            *[str(item) for item in (comp.composition_themes or [])],
            *[str(item) for item in (comp.composition_characters or [])],
        ]
        association_labels = [
            f"#{str(item).lstrip('#')}" for item in (comp.composition_hashtags or []) if str(item).strip()
        ]
        state_labels = [
            str(item).strip() for item in (comp.composition_emotions or []) if str(item).strip()
        ]
        rows.append(
            {
                "composition": comp,
                "slug": slug,
                "search_text": " ".join(str(term).strip().lower() for term in metadata_terms if str(term).strip()),
                "association_label": " · ".join(association_labels),
                "state_label": " · ".join(state_labels),
                "live_url": comp.url or "",
                "render_url": (comp.url or "").strip() + ("&render=1" if "?" in (comp.url or "") else "?render=1") if (comp.url or "").strip() else "",
                "mint_url": reverse("mint_composition", kwargs={"page_slug": slug}),
                "preview_url": reverse("composition-preview", kwargs={"composition_id": int(comp.id)}),
                "nft": state,
            }
        )
    return render(
        request,
        "mint/site.html",
        {
            "rows": rows,
            "collection_settings": collection_settings,
            "network_name": getattr(settings, "NFT_ETH_NETWORK_NAME", "Ethereum"),
            "contract_address": (getattr(settings, "NFT_ETH_CONTRACT_ADDRESS", "") or "").strip(),
        },
    )


def mint_random_page(request):
    try:
        count = int(request.GET.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(10, count))
    rows = []
    compositions = list(_mintable_compositions_queryset())
    random.shuffle(compositions)
    for comp in compositions[:count]:
        slug = _composition_slug_from_url(comp.url or "")
        if not slug:
            continue
        state = _nft_public_state(request, comp)
        rows.append(
            {
                "composition": comp,
                "slug": slug,
                "live_url": comp.url or "",
                "render_url": (comp.url or "").strip() + ("&render=1" if "?" in (comp.url or "") else "?render=1") if (comp.url or "").strip() else "",
                "mint_url": reverse("mint_composition", kwargs={"page_slug": slug}),
                "preview_url": reverse("composition-preview", kwargs={"composition_id": int(comp.id)}),
                "nft": state,
            }
        )
    return render(
        request,
        "mint/random.html",
        {
            "rows": rows,
            "count": count,
            "count_options": range(1, 11),
            "holder_price_eth": "0.05",
            "public_price_eth": "0.07",
            "last_phase_price_eth": "0.1",
            "active_price_eth": "0.07",
            "total_eth": f"{count * 0.07:.2f}".rstrip("0").rstrip("."),
            "contract_address": (getattr(settings, "NFT_ETH_CONTRACT_ADDRESS", "") or "").strip(),
            "network_name": getattr(settings, "NFT_ETH_NETWORK_NAME", "Ethereum"),
        },
    )


def mint_composition_page(request, page_slug):
    matched = _find_composition_by_slug(page_slug)
    if not matched or not matched.ready_for_deployment:
        raise Http404("Composition is not ready to mint")
    live_url = (matched.url or "").strip()
    render_url = live_url + ("&render=1" if "?" in live_url else "?render=1") if live_url else ""
    prev_mint, next_mint = _mint_navigation(page_slug)
    return render(
        request,
        "mint/composition.html",
        {
            "composition": matched,
            "page_slug": page_slug,
            "live_url": live_url,
            "render_url": render_url,
            "nft_state": _nft_public_state(request, matched),
            "nft_status_url": reverse("composition_nft_status", kwargs={"composition_id": int(matched.id)}),
            "nft_prepare_url": reverse("composition_prepare_nft_version", kwargs={"composition_id": int(matched.id)}),
            "nft_record_mint_url": reverse("composition_record_nft_mint", kwargs={"composition_id": int(matched.id)}),
            "prev_mint": prev_mint,
            "next_mint": next_mint,
            "related_mint_rows": _related_mint_rows(request, page_slug),
        },
    )


def composition_collect_page(request, page_slug):
    matched = _find_composition_by_slug(page_slug)
    if not matched:
        raise Http404("Composition page not found")
    live_url = (matched.url or "").strip()
    if live_url:
        live_url = live_url + ("&render=1" if "?" in live_url else "?render=1")
    metadata = _build_nft_metadata_payload(request, matched)
    context = {
        "composition": matched,
        "live_url": live_url,
        "nft_name": metadata.get("name") or matched.name,
        "nft_description": metadata.get("description") or "",
        "nft_attributes": metadata.get("attributes") or [],
        "nft_enabled": bool(matched.nft_enabled),
        "nft_mode": (matched.nft_mode or "live"),
        "nft_image_url": reverse("composition_nft_image", kwargs={"composition_id": int(matched.id)}),
        "nft_video_url": reverse("composition_nft_video", kwargs={"composition_id": int(matched.id)}),
        "nft_metadata_url": reverse("composition_nft_metadata", kwargs={"composition_id": int(matched.id)}),
        "nft_external_url": (matched.nft_external_url or "").strip(),
    }
    return render(request, "composition_collect.html", context)


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


def _thoughtscape_words(raw: str) -> list[str]:
    txt = (raw or "").lower().strip()
    if not txt:
        return []
    words = re.findall(r"[a-z0-9_#-]+", txt)
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        w = w.strip().lstrip("#")
        if len(w) < 2:
            continue
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _thoughtscape_intensity_bucket(thought_text: str, scenario_state: str, scene_context: str = "") -> str:
    st = (scenario_state or "").strip().lower()
    if st in {"asleep", "sleep", "calm"}:
        return "low"
    if st in {"distracted", "intense", "agitated", "urgent"}:
        return "high"
    words = set(_thoughtscape_words(f"{thought_text} {scene_context}"))
    high_markers = {"panic", "fight", "sex", "desire", "obsession", "power", "rush", "danger", "rage", "fear"}
    low_markers = {"sleep", "dream", "calm", "rest", "soft", "quiet", "still"}
    hi = len(words & high_markers)
    lo = len(words & low_markers)
    if hi >= max(1, lo + 1):
        return "high"
    if lo > hi:
        return "low"
    return "mid"


def _thoughtscape_cadence_for_bucket(bucket: str, tick: int) -> tuple[int, int]:
    t = max(0, int(tick or 0))
    if bucket == "low":
        cadence = 24 + (t % 7)  # 24..30
        replacements = 1
    elif bucket == "high":
        cadence = 4 + (t % 3)  # 4..6
        replacements = 2 + (t % 2)  # 2..3
    else:
        cadence = 10 + (t % 5)  # 10..14
        replacements = 1 + (t % 2)  # 1..2
    cadence = max(3, min(30, cadence))
    replacements = max(1, min(3, replacements))
    return cadence, replacements


def _thoughtscape_layer_for_index(rows: int, cols: int, idx: int, has_thought: bool, thought_idx: int | None) -> str:
    if has_thought and thought_idx is not None and idx == thought_idx:
        return "thought"
    # locked v1 3x3 mapping
    if rows == 3 and cols == 3 and has_thought and thought_idx == 4:
        r = idx // cols
        c = idx % cols
        if r == 0:
            return "conscious"
        if r == 2:
            return "subconscious"
        if c in {0, 2}:
            return "transitional"
        return "transitional"
    # generic row-band mapping
    r = idx // cols
    if rows <= 2:
        return "conscious" if r == 0 else "subconscious"
    top_cut = max(1, rows // 3)
    bottom_cut = rows - max(1, rows // 3)
    if r < top_cut:
        return "conscious"
    if r >= bottom_cut:
        return "subconscious"
    return "transitional"


def _thoughtscape_weight(layer: str) -> float:
    if layer == "conscious":
        return 1.0
    if layer == "transitional":
        return 0.65
    if layer == "subconscious":
        return 0.4
    return 0.0


def _thoughtscape_candidate_compositions(limit: int = 240) -> list[dict]:
    comps = Composition.objects.exclude(url__isnull=True).exclude(url__exact="").order_by("-date")[:limit]
    out = []
    for c in comps:
        tags = [str(t).strip().lower() for t in (c.composition_hashtags or []) if str(t).strip()]
        out.append({
            "id": int(c.id),
            "name": c.name or f"Composition {c.id}",
            "url": (c.url or "").strip(),
            "live_preview_url": (c.url or "").strip(),
            "admin_url": reverse("composition_detail", kwargs={"composition_id": int(c.id)}),
            "preview_image_url": f"/composition-preview/{int(c.id)}/",
            "loop_video_url": (
                reverse("composition_latest_render", kwargs={"composition_id": int(c.id)})
                if bool(getattr(c, "final_video", None))
                else ""
            ),
            "hashtags": tags,
        })
    return out


def _thoughtscape_persona_presets(limit: int = 80) -> list[dict]:
    presets: list[dict] = []
    personas = MonologuePersona.objects.order_by("-updated_at")[:limit]
    for p in personas:
        sample = ""
        try:
            segs = build_segments(p)
            if segs:
                sample = str(segs[0]).strip()
        except Exception:
            sample = ""
        if not sample:
            sample = (p.stream_seed_text or "").strip().split("\n\n")[0].strip()
        if not sample:
            bits = [x for x in [p.character_name, p.archetype, p.title] if (x or "").strip()]
            sample = " ".join(bits).strip() or "Holding the room, recalibrating."
        presets.append(
            {
                "id": int(p.id),
                "kind": "persona",
                "title": p.title or f"Persona {p.id}",
                "characterName": (p.character_name or "").strip(),
                "archetype": (p.archetype or "").strip(),
                "state": "neutral",
                "seedThought": sample[:900],
            }
        )
    return presets


def _thoughtscape_scenario_presets(limit: int = 80) -> list[dict]:
    presets: list[dict] = []
    scenarios = AdvancedThoughtScenario.objects.order_by("-updated_at")[:limit]
    for s in scenarios:
        anchors = (s.scene_bible or {}).get("anchors", [])
        anchor_text = ", ".join(str(a).strip() for a in anchors[:3] if str(a).strip())
        thought = (s.last_thought or "").strip()
        if not thought:
            desires = [str(x).strip() for x in (s.seed_desires or []) if str(x).strip()]
            subconscious = [str(x).strip() for x in (s.seed_subconscious or []) if str(x).strip()]
            parts = desires[:2] + subconscious[:2]
            if anchor_text:
                parts.append(f"space: {anchor_text}")
            thought = "; ".join(parts).strip()
        if not thought:
            thought = f"{s.character_name or s.title} scans the scene and updates intent."
        state = "neutral"
        blob = f"{s.tone_hint} {thought}".lower()
        if any(k in blob for k in ["panic", "rush", "urgent", "fight", "desire", "power"]):
            state = "intense"
        elif any(k in blob for k in ["sleep", "dream", "calm", "quiet", "still"]):
            state = "calm"
        presets.append(
            {
                "id": int(s.id),
                "kind": "scenario",
                "title": s.title or f"Scenario {s.id}",
                "characterName": (s.character_name or "").strip(),
                "archetype": (s.archetype or "").strip(),
                "state": state,
                "seedThought": thought[:900],
            }
        )
    return presets


def thoughtscape_lab(request):
    comps = _thoughtscape_candidate_compositions(limit=320)
    personas = MonologuePersona.objects.order_by("-updated_at")[:40]
    persona_presets = _thoughtscape_persona_presets(limit=100)
    scenario_presets = _thoughtscape_scenario_presets(limit=100)
    return render(
        request,
        "admin/thoughtscape_lab.html",
        {
            "default_rows": 3,
            "default_cols": 3,
            "default_has_thought": True,
            "default_thought_tile": 4,
            "compositions": comps,
            "personas": personas,
            "persona_presets": persona_presets,
            "scenario_presets": scenario_presets,
        },
    )


@csrf_exempt
def thoughtscape_lab_simulate(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}

    try:
        rows = max(2, min(5, int(payload.get("rows") or 3)))
    except Exception:
        rows = 3
    try:
        cols = max(2, min(5, int(payload.get("cols") or 3)))
    except Exception:
        cols = 3
    total = rows * cols

    has_thought = bool(payload.get("hasThoughtTile", True))
    try:
        thought_idx = int(payload.get("thoughtTileIndex") if payload.get("thoughtTileIndex") is not None else 4)
    except Exception:
        thought_idx = 4
    if not (0 <= thought_idx < total):
        thought_idx = min(total - 1, (rows // 2) * cols + (cols // 2))
    if not has_thought:
        thought_idx = -1

    thought_text = (payload.get("thoughtText") or "").strip()
    scenario_state = (payload.get("scenarioState") or "neutral").strip().lower()
    scene_location = (payload.get("sceneLocation") or "").strip()
    scene_people = [str(x).strip() for x in (payload.get("scenePeople") or []) if str(x).strip()]
    scene_context = " ".join([scene_location] + scene_people)
    tick = int(payload.get("tick") or 0)

    words = _thoughtscape_words(thought_text)
    context_words = _thoughtscape_words(scene_context)
    thought_word_set = set(words)
    context_word_set = set(context_words)
    combined_words = set(words) | set(context_words)
    intensity = _thoughtscape_intensity_bucket(thought_text, scenario_state, scene_context=scene_context)
    cadence_s, replacements = _thoughtscape_cadence_for_bucket(intensity, tick=tick)

    all_comps = _thoughtscape_candidate_compositions(limit=500)
    selected_ids = payload.get("poolCompositionIds") or []
    character_preset_key = str(payload.get("characterPresetKey") or "").strip()
    pool_id_set = set()
    for x in selected_ids:
        try:
            pool_id_set.add(int(x))
        except Exception:
            continue
    if pool_id_set:
        pool = [c for c in all_comps if c["id"] in pool_id_set]
    else:
        pool = all_comps

    character_restriction_applied = False
    character_restriction_label = ""
    if character_preset_key.startswith("persona:"):
        try:
            persona_id = int(character_preset_key.split(":", 1)[1])
        except Exception:
            persona_id = 0
        if persona_id > 0 and pool:
            from django.db.models import Q
            persona = MonologuePersona.objects.filter(id=persona_id).only("title", "character_name").first()
            if persona:
                character_restriction_label = persona.title or persona.character_name or f"persona:{persona_id}"
            allowed_ids = set(
                Composition.objects.filter(id__in=[c["id"] for c in pool])
                .filter(Q(allowed_personas__isnull=True) | Q(allowed_personas__id=persona_id))
                .values_list("id", flat=True)
            )
            if allowed_ids:
                pool = [c for c in pool if c["id"] in allowed_ids]
                character_restriction_applied = True

    prev = payload.get("previousAssignments") or []
    prev_map: dict[int, int] = {}
    for row in prev:
        try:
            i = int(row.get("tileIndex"))
            cid = int(row.get("compositionId"))
            if 0 <= i < total:
                prev_map[i] = cid
        except Exception:
            continue

    tiles = []
    comp_by_id = {c["id"]: c for c in pool}

    comp_slots = [i for i in range(total) if not (has_thought and i == thought_idx)]
    if comp_slots:
        rot = tick % len(comp_slots)
        ordered = comp_slots[rot:] + comp_slots[:rot]
        to_replace = set(ordered[: min(replacements, len(comp_slots))])
    else:
        to_replace = set()

    used_ids = set(v for k, v in prev_map.items() if k not in to_replace)

    def pick_for(tile_index: int, layer: str) -> tuple[dict | None, str]:
        layer_w = _thoughtscape_weight(layer)
        best = None
        best_score = -1e9
        for c in pool:
            cid = c["id"]
            if cid in used_ids:
                continue
            tags = c["hashtags"]
            thought_overlap = len(set(tags) & thought_word_set)
            context_overlap = len(set(tags) & context_word_set)
            overlap = thought_overlap + context_overlap
            score = (overlap * (2.4 * layer_w)) + (0.25 * layer_w)
            # small deterministic tie-break based on tick and id
            score += ((cid * 31 + tick * 17 + tile_index * 13) % 19) / 1000.0
            if cid == prev_map.get(tile_index):
                score -= 0.9  # recency penalty
            if score > best_score:
                best_score = score
                best = c
        if best is None:
            return None, "no_candidate"
        used_ids.add(best["id"])
        bt = set(best.get("hashtags") or [])
        ov_thought = len(bt & thought_word_set)
        ov_context = len(bt & context_word_set)
        reason = (
            f"layer={layer}; weight={layer_w:.2f}; thought_overlap={ov_thought}; "
            f"context_overlap={ov_context}; intensity={intensity}"
        )
        return best, reason

    for i in range(total):
        layer = _thoughtscape_layer_for_index(rows, cols, i, has_thought, thought_idx if has_thought else None)
        if has_thought and i == thought_idx:
            tiles.append({
                "tileIndex": i,
                "kind": "thought",
                "layer": layer,
                "thoughtText": thought_text,
            })
            continue

        chosen = None
        reason = ""
        prev_id = prev_map.get(i)
        if i not in to_replace and prev_id in comp_by_id:
            chosen = comp_by_id[prev_id]
            used_ids.add(prev_id)
            reason = "kept_previous"
        if chosen is None:
            chosen, reason = pick_for(i, layer)
        if chosen is None and pool:
            chosen = pool[(tick + i) % len(pool)]
            reason = "fallback_round_robin"

        if chosen is None:
            tiles.append({"tileIndex": i, "kind": "composition", "layer": layer, "composition": None, "reason": "empty_pool"})
        else:
            tiles.append(
                {
                    "tileIndex": i,
                    "kind": "composition",
                    "layer": layer,
                    "composition": chosen,
                    "reason": reason,
                }
            )

    return JsonResponse(
        {
            "ok": True,
            "rows": rows,
            "cols": cols,
            "hasThoughtTile": has_thought,
            "thoughtTileIndex": thought_idx if has_thought else None,
            "intensity": intensity,
            "cadenceSeconds": cadence_s,
            "replacementsPerTick": replacements,
            "words": words[:24],
            "contextWords": context_words[:24],
            "sceneLocation": scene_location,
            "scenePeople": scene_people[:12],
            "characterRestrictionApplied": character_restriction_applied,
            "characterRestrictionLabel": character_restriction_label,
            "tiles": tiles,
        }
    )


def _wall_now_ms() -> int:
    return int(time.time() * 1000)


def _wall_get_or_create_run() -> tuple[WallProfile, WallRun]:
    profile, _ = WallProfile.objects.get_or_create(
        name="Gallery 9-Screen",
        defaults={
            "rows": 3,
            "cols": 3,
            "strict_live": True,
            "cadence_seconds": 12,
            "start_lead_ms": 2000,
            "is_active": True,
        },
    )
    run = (
        WallRun.objects.filter(profile=profile).order_by("-updated_at", "-id").first()
    )
    if not run:
        run = WallRun.objects.create(profile=profile, status="stopped")
    return profile, run


def _wall_assignments_for_tick(run: WallRun, tick_id: int) -> list[dict]:
    rows = list(
        WallAssignment.objects.filter(run=run, tick_id=tick_id)
        .select_related("composition")
        .order_by("tile_index")
    )
    out = []
    for a in rows:
        out.append(
            {
                "tile_index": int(a.tile_index),
                "composition_id": int(a.composition_id) if a.composition_id else None,
                "composition_name": (a.composition.name if a.composition_id else ""),
                "composition_url": a.composition_url or "",
                "start_at_unix_ms": int(a.start_at_unix_ms or 0),
                "duration_ms": int(a.duration_ms or 0),
                "reason": a.reason or "",
                "layer": a.layer or "transitional",
            }
        )
    return out


def _wall_materialize_tick(run: WallRun, tick_id: int, start_at_unix_ms: int) -> dict:
    computed = compute_wall_tick(run, tick_id=tick_id)
    duration_ms = int(max(1000, computed["cadence_seconds"] * 1000))

    existing = _wall_assignments_for_tick(run, tick_id=tick_id)
    if existing:
        run.tick = max(int(run.tick or 0), int(tick_id))
        run.current_tick_id = int(tick_id)
        run.current_start_at_unix_ms = int(start_at_unix_ms)
        run.current_duration_ms = duration_ms
        run.previous_assignments = computed["assignments"]
        run.save(
            update_fields=[
                "tick",
                "current_tick_id",
                "current_start_at_unix_ms",
                "current_duration_ms",
                "previous_assignments",
                "updated_at",
            ]
        )
        return {
            "tick_id": int(tick_id),
            "start_at_unix_ms": int(start_at_unix_ms),
            "duration_ms": duration_ms,
            "intensity": computed["intensity"],
            "cadence_seconds": int(computed["cadence_seconds"]),
            "assignments": existing,
        }

    comp_ids = []
    for t in computed["tiles"]:
        c = t.get("composition")
        if c and c.get("id"):
            comp_ids.append(int(c["id"]))
    comp_map = {
        int(c.id): c
        for c in Composition.objects.filter(id__in=comp_ids).only("id", "name", "url")
    }

    created = []
    with transaction.atomic():
        for t in computed["tiles"]:
            idx = int(t.get("tileIndex", 0))
            layer = str(t.get("layer") or "transitional")
            reason = str(t.get("reason") or "")
            comp_blob = t.get("composition") or {}
            comp_id = int(comp_blob["id"]) if comp_blob and comp_blob.get("id") else None
            comp_obj = comp_map.get(comp_id) if comp_id else None
            comp_url = ""
            if comp_obj and comp_obj.url:
                comp_url = str(comp_obj.url).strip()
            elif comp_blob and comp_blob.get("url"):
                comp_url = str(comp_blob.get("url") or "").strip()
            rec, _ = WallAssignment.objects.get_or_create(
                run=run,
                tick_id=tick_id,
                tile_index=idx,
                defaults={
                    "layer": layer,
                    "composition": comp_obj,
                    "composition_url": comp_url,
                    "reason": reason[:120],
                    "start_at_unix_ms": int(start_at_unix_ms),
                    "duration_ms": duration_ms,
                },
            )
            created.append(
                {
                    "tile_index": int(rec.tile_index),
                    "composition_id": int(rec.composition_id) if rec.composition_id else None,
                    "composition_name": (rec.composition.name if rec.composition_id else ""),
                    "composition_url": rec.composition_url or "",
                    "start_at_unix_ms": int(rec.start_at_unix_ms or 0),
                    "duration_ms": int(rec.duration_ms or 0),
                    "reason": rec.reason or "",
                    "layer": rec.layer or "transitional",
                }
            )

    run.tick = max(int(run.tick or 0), int(tick_id))
    run.current_tick_id = int(tick_id)
    run.current_start_at_unix_ms = int(start_at_unix_ms)
    run.current_duration_ms = duration_ms
    run.previous_assignments = computed["assignments"]
    run.save(
        update_fields=[
            "tick",
            "current_tick_id",
            "current_start_at_unix_ms",
            "current_duration_ms",
            "previous_assignments",
            "updated_at",
        ]
    )
    return {
        "tick_id": int(tick_id),
        "start_at_unix_ms": int(start_at_unix_ms),
        "duration_ms": duration_ms,
        "intensity": computed["intensity"],
        "cadence_seconds": int(computed["cadence_seconds"]),
        "assignments": sorted(created, key=lambda row: row["tile_index"]),
    }


def _wall_ensure_current_tick(run: WallRun, now_ms: int, force_next: bool = False) -> dict | None:
    if run.status != "running":
        return None
    lead_ms = int(max(500, run.profile.start_lead_ms or 2000))

    if force_next:
        next_tick = int(run.current_tick_id or run.tick or 0) + 1
        start_at = max(now_ms + lead_ms, int(run.current_start_at_unix_ms or 0) + int(run.current_duration_ms or 0))
        return _wall_materialize_tick(run, tick_id=next_tick, start_at_unix_ms=start_at)

    if int(run.current_tick_id or 0) <= 0 or int(run.current_start_at_unix_ms or 0) <= 0:
        return _wall_materialize_tick(run, tick_id=max(1, int(run.tick or 0) + 1), start_at_unix_ms=now_ms + lead_ms)

    # Advance as needed if the current tick window has already passed.
    guard = 0
    while now_ms >= int(run.current_start_at_unix_ms or 0) + int(run.current_duration_ms or 0) and guard < 10:
        next_tick = int(run.current_tick_id or run.tick or 0) + 1
        next_start = int(run.current_start_at_unix_ms or 0) + int(run.current_duration_ms or 0)
        _wall_materialize_tick(run, tick_id=next_tick, start_at_unix_ms=next_start)
        run.refresh_from_db(fields=["tick", "current_tick_id", "current_start_at_unix_ms", "current_duration_ms"])
        guard += 1

    return {
        "tick_id": int(run.current_tick_id or 0),
        "start_at_unix_ms": int(run.current_start_at_unix_ms or 0),
        "duration_ms": int(run.current_duration_ms or 0),
        "assignments": _wall_assignments_for_tick(run, int(run.current_tick_id or 0)),
    }


def wall_operator(request):
    profile, run = _wall_get_or_create_run()
    return render(
        request,
        "admin/wall_operator.html",
        {
            "wall_profile": profile,
            "wall_run": run,
            "persona_presets": _thoughtscape_persona_presets(limit=120),
            "scenario_presets": _thoughtscape_scenario_presets(limit=120),
        },
    )


def wall_player(request, screen_id):
    sid = str(screen_id or "1").strip()
    return render(request, "wall_player.html", {"screen_id": sid})


def wall_state(request):
    profile, run = _wall_get_or_create_run()
    screen_id = str(request.GET.get("screen_id") or "1").strip()
    total = max(1, int(profile.rows or 3) * int(profile.cols or 3))
    now_ms = _wall_now_ms()
    state = _wall_ensure_current_tick(run, now_ms=now_ms)
    heartbeats = list(
        WallHeartbeat.objects.filter(run=run).order_by("screen_id").values(
            "screen_id", "clock_offset_ms", "last_applied_tick", "drift_ms", "last_seen"
        )
    )
    if not state:
        return JsonResponse(
            {
                "ok": True,
                "running": False,
                "screen_id": screen_id,
                "screen_index": parse_screen_tile_index(screen_id, total),
                "total_screens": total,
                "server_now_ms": now_ms,
                "tick_id": 0,
                "assignments": [],
                "heartbeats": heartbeats,
                "profile": {
                    "name": profile.name,
                    "rows": int(profile.rows or 3),
                    "cols": int(profile.cols or 3),
                    "strict_live": bool(profile.strict_live),
                },
            }
        )

    screen_index = parse_screen_tile_index(screen_id, total)
    assignments = state["assignments"] or []
    selected = next((a for a in assignments if int(a["tile_index"]) == int(screen_index)), None)
    return JsonResponse(
        {
            "ok": True,
            "running": True,
            "screen_id": screen_id,
            "screen_index": screen_index,
            "total_screens": total,
            "server_now_ms": now_ms,
            "tick_id": int(state["tick_id"]),
            "start_at_unix_ms": int(state["start_at_unix_ms"]),
            "duration_ms": int(state["duration_ms"]),
            "assignment": selected,
            "assignments": assignments,
            "heartbeats": heartbeats,
            "profile": {
                "name": profile.name,
                "rows": int(profile.rows or 3),
                "cols": int(profile.cols or 3),
                "strict_live": bool(profile.strict_live),
            },
        }
    )


@csrf_exempt
def wall_heartbeat(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    _, run = _wall_get_or_create_run()
    screen_id = str(payload.get("screen_id") or payload.get("screenId") or "").strip()
    if not screen_id:
        return JsonResponse({"ok": False, "error": "screen_id required"}, status=400)

    hb, _ = WallHeartbeat.objects.update_or_create(
        run=run,
        screen_id=screen_id,
        defaults={
            "clock_offset_ms": int(payload.get("clock_offset_ms") or payload.get("clockOffsetMs") or 0),
            "last_applied_tick": int(payload.get("last_applied_tick") or payload.get("lastAppliedTick") or 0),
            "drift_ms": int(payload.get("drift_ms") or payload.get("driftMs") or 0),
            "last_error": str(payload.get("last_error") or payload.get("lastError") or "")[:255],
            "last_payload": payload if isinstance(payload, dict) else {},
        },
    )
    return JsonResponse(
        {
            "ok": True,
            "screen_id": hb.screen_id,
            "last_seen": hb.last_seen.isoformat(),
        }
    )


@csrf_exempt
def wall_control(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    profile, run = _wall_get_or_create_run()
    action = str(payload.get("action") or "").strip().lower()
    now_ms = _wall_now_ms()

    if action in {"start", "restart"}:
        profile.is_active = True
        profile.save(update_fields=["is_active", "updated_at"])
        run.status = "running"
        run.tick = 0
        run.current_tick_id = 0
        run.current_start_at_unix_ms = 0
        run.current_duration_ms = int(max(1000, (profile.cadence_seconds or 12) * 1000))
        run.previous_assignments = []
        run.active_character_keys = list(payload.get("character_keys") or payload.get("characterKeys") or [])
        run.thought_text = str(payload.get("thought_text") or payload.get("thoughtText") or run.thought_text or "").strip()
        run.scenario_state = str(payload.get("scenario_state") or payload.get("scenarioState") or run.scenario_state or "neutral").strip().lower()
        run.scene_location = str(payload.get("scene_location") or payload.get("sceneLocation") or run.scene_location or "").strip()
        run.scene_people = [str(x).strip() for x in (payload.get("scene_people") or payload.get("scenePeople") or run.scene_people or []) if str(x).strip()]
        pool_ids = []
        for x in (payload.get("pool_composition_ids") or payload.get("poolCompositionIds") or run.pool_composition_ids or []):
            try:
                pool_ids.append(int(x))
            except (TypeError, ValueError):
                continue
        run.pool_composition_ids = pool_ids
        run.save()
        WallAssignment.objects.filter(run=run).delete()
        state = _wall_ensure_current_tick(run, now_ms=now_ms)
        return JsonResponse({"ok": True, "running": True, "state": state})

    if action == "stop":
        run.status = "stopped"
        run.save(update_fields=["status", "updated_at"])
        return JsonResponse({"ok": True, "running": False})

    if action == "next":
        if run.status != "running":
            return JsonResponse({"ok": False, "error": "run is not active"}, status=400)
        state = _wall_ensure_current_tick(run, now_ms=now_ms, force_next=True)
        return JsonResponse({"ok": True, "running": True, "state": state})

    if action == "pin":
        try:
            tile_index = int(payload.get("tile_index"))
            composition_id = int(payload.get("composition_id"))
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "tile_index and composition_id required"}, status=400)
        pins = dict(run.pinned_assignments or {})
        pins[str(tile_index)] = int(composition_id)
        run.pinned_assignments = pins
        run.save(update_fields=["pinned_assignments", "updated_at"])
        return JsonResponse({"ok": True, "pinned_assignments": run.pinned_assignments})

    if action == "unpin":
        try:
            tile_index = int(payload.get("tile_index"))
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "tile_index required"}, status=400)
        pins = dict(run.pinned_assignments or {})
        pins.pop(str(tile_index), None)
        run.pinned_assignments = pins
        run.save(update_fields=["pinned_assignments", "updated_at"])
        return JsonResponse({"ok": True, "pinned_assignments": run.pinned_assignments})

    if action in {"set_characters", "set_context"}:
        if "character_keys" in payload or "characterKeys" in payload:
            run.active_character_keys = list(payload.get("character_keys") or payload.get("characterKeys") or [])
        if "thought_text" in payload or "thoughtText" in payload:
            run.thought_text = str(payload.get("thought_text") or payload.get("thoughtText") or "").strip()
        if "scenario_state" in payload or "scenarioState" in payload:
            run.scenario_state = str(payload.get("scenario_state") or payload.get("scenarioState") or "neutral").strip().lower()
        if "scene_location" in payload or "sceneLocation" in payload:
            run.scene_location = str(payload.get("scene_location") or payload.get("sceneLocation") or "").strip()
        if "scene_people" in payload or "scenePeople" in payload:
            run.scene_people = [str(x).strip() for x in (payload.get("scene_people") or payload.get("scenePeople") or []) if str(x).strip()]
        run.save(update_fields=["active_character_keys", "thought_text", "scenario_state", "scene_location", "scene_people", "updated_at"])
        return JsonResponse({"ok": True})

    return JsonResponse({"ok": False, "error": "unknown action"}, status=400)


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


def training_set_media_file(request, file_name):
    target = (ASSOCIATIONS_TRAINING_SET_DIR / file_name).resolve()
    root = ASSOCIATIONS_TRAINING_SET_DIR.resolve()
    if root != target and root not in target.parents:
        raise Http404("Training image not found")
    if not target.exists() or not target.is_file() or not _local_source_path_allowed(target):
        raise Http404("Training image not found")
    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(open(target, "rb"), content_type=content_type)


def source_media_file(request, source_name, file_name):
    source_dir = (LOCAL_SOURCES_ROOT / source_name).resolve()
    target = _resolve_source_media_file(source_dir, file_name)
    if not target:
        raise Http404("Source file not found")
    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
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
    if ext in LOCAL_SOURCE_IMAGE_EXTS:
        return source_media_file(request, source_name, file_name)
    if ext not in LOCAL_SOURCE_VIDEO_EXTS:
        raise Http404("Unsupported thumbnail type")

    frame = _extract_video_frame_image(target)
    if frame is None:
        raise Http404("Unable to generate thumbnail")
    output = io.BytesIO()
    frame.convert("RGB").save(output, format="JPEG", quality=86)
    return HttpResponse(output.getvalue(), content_type="image/jpeg")


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
    return JsonResponse({"assets": assets})


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


@csrf_exempt
def generate_video(request, comp_id):
    if request.method == 'POST':
        try:
            comp = Composition.objects.get(id=comp_id)
            comp.status = "Processing"
            comp.save()
            data = json.loads(request.body)
            selected_type = data.get("selected_type", "").lower().replace("-", "_")
            ids = int(data.get("id"))
            background = data.get("background")
            foreground = data.get("foreground")
            audio_path = data.get("audio_path")
            print("🔥 TYPE RECEIVED:", foreground)

            if selected_type == "classic":
                classic_task.delay(
                    ids=ids,
                    selected_type=selected_type,
                    selected_background_bucket=background,
                    selected_foreground_bucket=foreground,
                    audio_file_path=audio_path
                )

            elif selected_type == "tunnel":
                tunnel_task.delay({
                    "ids": ids,
                    "selected_background_buckets": background if isinstance(background, list) else [background],
                    "audio_file_path": audio_path
                })

            elif selected_type == "left_to_right":
                left_to_right_task.delay({
                    "ids": ids,
                    "background_bucket": background,
                    "foreground_bucket": foreground,
                    "audio_file_path": audio_path
                })

            else:
                return JsonResponse({"success": False, "error": f"Invalid type: {selected_type}"})
            return JsonResponse({'success': comp.id})
        except Composition.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Composition not found'})
    return JsonResponse({'success': False, 'error': 'Invalid request'})