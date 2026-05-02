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



from ._compositions import _unique_slug
__all__ = [n for n in list(vars().keys()) if not n.startswith('__')]
