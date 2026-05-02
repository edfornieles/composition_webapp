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



from ._compositions import _unique_slug
from ._utils import create_video_ffmpeg
from django.http import HttpResponseNotAllowed
from django.utils.log import log_response
__all__ = [n for n in list(vars().keys()) if not n.startswith('__')]
