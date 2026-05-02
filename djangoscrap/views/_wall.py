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



from ._advanced_thoughts import _thoughtscape_persona_presets, _thoughtscape_scenario_presets
__all__ = [n for n in list(vars().keys()) if not n.startswith('__')]
