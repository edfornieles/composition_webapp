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
from ._utils import (
    _ensure_default_adapters_registered,
    _is_stock_image_candidate,
    _trim_image_borders_inplace,
    _trim_video_borders_inplace,
    create_video_ffmpeg,
)
s3 = boto3.client('s3')

_ingestion_batch_folder_locks: dict[str, 'threading.Lock'] = {}


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
    # Match collect_source_assets / ingestion: include nested legacy batch folders, not only top-level files.
    for p in sorted(
        _local_source_dir_media_files(source_dir),
        key=lambda x: str(x.relative_to(source_dir)).lower(),
    ):
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
            f"Auto-trim complete: {total_trimmed} file(s) updated ({image_trimmed} image, {video_trimmed} video; subfolders included)."
            + (f" {no_border} had no detectable border." if no_border else "")
            + (f" {failed} failed." if failed else ""),
        )
    else:
        if failed:
            messages.warning(request, f"No files trimmed. {no_border} had no detectable border, {failed} failed.")
        else:
            messages.info(
                request,
                "No black/white borders detected to trim in this source (all media files, including subfolders, were checked).",
            )
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


from django.http import HttpResponseNotAllowed
from django.utils.log import log_response
from ._compositions import _purge_composition_preview_cache
from ._media import get_sample_image_url
__all__ = [n for n in list(vars().keys()) if not n.startswith('__')]
