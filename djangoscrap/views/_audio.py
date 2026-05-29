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


def voiceover_composer(request):
    """Multi-track voiceover composer: layer beds under a voice track with
    sidechain ducking, preview live, render to a single WAV, save to library."""
    (LOCAL_AUDIO_SOURCES_ROOT / "voiceover_tracks").mkdir(parents=True, exist_ok=True)
    return render(
        request,
        "admin/voiceover_composer.html",
        {
            "audio_source_map": collect_audio_source_files(),
        },
    )


def voiceover_composer_save(request):
    """Save the rendered voiceover track WAV to the voiceover_tracks library."""
    from django.utils.text import get_valid_filename
    if request.method != "POST":
        return redirect("voiceover-composer")
    upload = request.FILES.get("file")
    if not upload:
        messages.error(request, "No audio file received.")
        return redirect("voiceover-composer")
    dest_dir = LOCAL_AUDIO_SOURCES_ROOT / "voiceover_tracks"
    dest_dir.mkdir(parents=True, exist_ok=True)
    preferred = (request.POST.get("save_name") or "").strip()
    if preferred:
        base_name = get_valid_filename(preferred) or "voiceover-track.wav"
    else:
        base_name = get_valid_filename(upload.name) or "voiceover-track.wav"
    if not base_name.lower().endswith((".wav", ".mp3", ".m4a", ".flac", ".ogg")):
        base_name += ".wav"
    target = dest_dir / base_name
    overwrite = (request.POST.get("overwrite", "").lower() in {"1", "true", "on"})
    if target.exists() and not overwrite:
        stem, ext = os.path.splitext(base_name)
        i = 2
        while (dest_dir / f"{stem}_{i}{ext}").exists():
            i += 1
        target = dest_dir / f"{stem}_{i}{ext}"
    with open(target, "wb") as f:
        for chunk in upload.chunks():
            f.write(chunk)
    messages.success(request, f"Saved “{target.name}” to Audio Library → voiceover_tracks.")
    return redirect("voiceover-composer")


# ──────────────────────────────────────────────────────────────────────────────
# Composition Studio: multi-project save / load / delete with bundled assets.
#
# Each project lives in its own directory under
#   media/composition_projects/<slug>/
# containing:
#   project.json       — full serialized state (tracks, FX, settings, asset refs)
#   voice.wav          — recorded voice audio (if present)
#   assets/<key>.wav   — uploaded bed audio (non-library beds)
#
# This guarantees the user's recorded/uploaded audio is preserved across
# browser-cache clears and machine moves, and that a project can be restored
# byte-for-byte.
# ──────────────────────────────────────────────────────────────────────────────


def _composition_projects_root() -> Path:
    """Return (and create) the project storage root."""
    root = Path(settings.BASE_DIR) / "media" / "composition_projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_project_slug(name: str) -> str:
    """Sanitize a project name into a directory-safe slug. Empty input -> 'untitled'."""
    from django.utils.text import get_valid_filename
    slug = get_valid_filename((name or "").strip())
    # get_valid_filename allows dots; collapse leading dots to avoid hidden dirs.
    slug = slug.lstrip(".") or "untitled"
    return slug[:80]  # length cap


def composition_project_save(request):
    """Save (or overwrite) a named composition project, bundling all assets.

    Expects multipart POST with:
      - name:           project display name (slug derived from this)
      - project_json:   JSON string of the project state
      - voice_audio:    optional voice WAV blob (file part)
      - asset_<bedId>:  zero or more bed WAV blobs (file parts), one per
                        uploaded/recorded bed referenced in project_json.assets

    Returns JSON: { ok, slug, name, saved_at, asset_count, voice: bool }
    """
    from django.http import JsonResponse
    import json as _json
    import time

    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    name = (request.POST.get("name") or "").strip()
    slug = _safe_project_slug(name)
    proj_json = request.POST.get("project_json") or ""
    if not proj_json:
        return JsonResponse({"ok": False, "error": "missing project_json"}, status=400)
    try:
        proj_data = _json.loads(proj_json)
    except ValueError as e:
        return JsonResponse({"ok": False, "error": f"invalid project_json: {e}"}, status=400)
    if not isinstance(proj_data, dict):
        return JsonResponse({"ok": False, "error": "project_json must be a JSON object"}, status=400)

    proj_data["name"] = name or slug
    proj_data["slug"] = slug
    proj_data["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    root = _composition_projects_root()
    pdir = root / slug
    # NON-DESTRUCTIVE SAVE:
    # We copy existing assets (and voice.wav) into tmp_dir FIRST, then
    # overwrite with anything uploaded by this request. This means clients
    # that re-save a project without re-attaching every bundled asset (for
    # example, because a clip's audio buffer was nulled out by an edit before
    # the save) don't accidentally destroy their previous recording. Any
    # asset that is no longer referenced by the new project.json's clip
    # uuids/assetKeys is pruned at the end so the dir doesn't accumulate
    # forever.
    tmp_dir = root / f".{slug}.tmp.{os.getpid()}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True)
    try:
        assets_dir = tmp_dir / "assets"
        assets_dir.mkdir()

        # 1) Carry forward voice + assets from the previous save (if any).
        # Use shutil.copy (not copy2) to avoid macOS AppleDouble metadata
        # quirks where copy2 fails on certain HFS+/SMB shares trying to
        # preserve xattrs. We also skip any "._*" resource-fork files that
        # macOS may have written alongside the real ones.
        def _is_real_file(p):
            return p.is_file() and not p.name.startswith("._")
        if pdir.is_dir():
            prev_voice = pdir / "voice.wav"
            if _is_real_file(prev_voice):
                try: shutil.copy(prev_voice, tmp_dir / "voice.wav")
                except OSError: pass
            prev_assets = pdir / "assets"
            if prev_assets.is_dir():
                for f in prev_assets.iterdir():
                    if not _is_real_file(f):
                        continue
                    try: shutil.copy(f, assets_dir / f.name)
                    except OSError: pass

        # 2) Overwrite voice if uploaded with this request.
        voice_saved = False
        voice = request.FILES.get("voice_audio")
        if voice:
            with open(tmp_dir / "voice.wav", "wb") as f:
                for chunk in voice.chunks():
                    f.write(chunk)
            voice_saved = True

        # 3) Overwrite/add each uploaded asset.
        asset_count = 0
        for field_name, upload in request.FILES.items():
            if not field_name.startswith("asset_") or field_name == "voice_audio":
                continue
            key = field_name[len("asset_"):]
            safe_key = "".join(c for c in key if c.isalnum() or c in "-_")
            if not safe_key:
                continue
            with open(assets_dir / f"{safe_key}.wav", "wb") as f:
                for chunk in upload.chunks():
                    f.write(chunk)
            asset_count += 1

        # 4) Prune orphaned assets — files whose keys aren't referenced by any
        # clip in the new project.json. Walks beds[].clips[].assetKey to build
        # the referenced-set, plus legacy bed.assetKey at the top level.
        referenced = set()
        for b in (proj_data.get("beds") or []):
            if b.get("assetKey"):
                referenced.add(b["assetKey"])
            for c in (b.get("clips") or []):
                if c.get("assetKey"):
                    referenced.add(c["assetKey"])
        pruned = 0
        for f in assets_dir.iterdir():
            if not f.is_file():
                continue
            stem = f.stem  # strip .wav
            if stem not in referenced:
                try:
                    f.unlink()
                    pruned += 1
                except OSError:
                    pass

        # 5) Write project.json + atomic rename.
        with open(tmp_dir / "project.json", "w") as f:
            _json.dump(proj_data, f, indent=2)
        # ignore_errors=True so macOS AppleDouble files (._*) on the mounted
        # volume don't sink the save — those resource forks sometimes vanish
        # mid-walk when their counterpart real file is removed first.
        if pdir.exists():
            shutil.rmtree(pdir, ignore_errors=True)
        tmp_dir.rename(pdir)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return JsonResponse({"ok": False, "error": f"save failed: {e}"}, status=500)

    return JsonResponse({
        "ok": True,
        "slug": slug,
        "name": proj_data["name"],
        "saved_at": proj_data["saved_at"],
        "asset_count": asset_count,
        "voice": voice_saved,
    })


def composition_project_list(request):
    """Return a JSON list of all saved composition projects (most recent first)."""
    from django.http import JsonResponse
    import json as _json

    root = _composition_projects_root()
    items = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        proj_file = child / "project.json"
        if not proj_file.is_file():
            continue
        try:
            with open(proj_file) as f:
                meta = _json.load(f)
        except Exception:
            continue
        items.append({
            "slug": child.name,
            "name": meta.get("name") or child.name,
            "saved_at": meta.get("saved_at") or "",
            "bed_count": len(meta.get("beds") or []),
            "has_voice": (child / "voice.wav").is_file(),
            "size_bytes": sum(p.stat().st_size for p in child.rglob("*") if p.is_file()),
        })
    items.sort(key=lambda it: it["saved_at"], reverse=True)
    return JsonResponse({"ok": True, "projects": items})


def composition_project_load(request, slug):
    """Return project.json for the given slug. Asset URLs are emitted as
    absolute paths that the client can fetch directly."""
    from django.http import JsonResponse, Http404
    import json as _json

    slug = _safe_project_slug(slug)
    pdir = _composition_projects_root() / slug
    proj_file = pdir / "project.json"
    if not proj_file.is_file():
        return JsonResponse({"ok": False, "error": "project not found"}, status=404)
    with open(proj_file) as f:
        data = _json.load(f)
    # Attach asset URLs for the client.
    data["_voice_url"] = (f"/audio-sources/composition-projects/{slug}/voice.wav"
                          if (pdir / "voice.wav").is_file() else None)
    assets_dir = pdir / "assets"
    data["_asset_urls"] = {}
    if assets_dir.is_dir():
        for p in assets_dir.glob("*.wav"):
            data["_asset_urls"][p.stem] = f"/audio-sources/composition-projects/{slug}/assets/{p.name}"
    return JsonResponse({"ok": True, "project": data})


def composition_project_asset(request, slug, *, filename=None, sub=None):
    """Serve an asset file from a project directory. Restricts paths so users
    can't escape the project root."""
    from django.http import FileResponse, Http404

    slug = _safe_project_slug(slug)
    pdir = _composition_projects_root() / slug
    if sub:
        candidate = pdir / "assets" / sub
    else:
        candidate = pdir / filename
    # Resolve and ensure the file is genuinely inside pdir.
    try:
        candidate_real = candidate.resolve()
        pdir_real = pdir.resolve()
    except FileNotFoundError:
        raise Http404
    if not str(candidate_real).startswith(str(pdir_real)):
        raise Http404("not in project dir")
    if not candidate_real.is_file():
        raise Http404
    return FileResponse(open(candidate_real, "rb"), content_type="audio/wav")


def composition_project_delete(request):
    """Delete a saved project (POST { slug })."""
    from django.http import JsonResponse

    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)
    slug = _safe_project_slug(request.POST.get("slug") or "")
    if not slug:
        return JsonResponse({"ok": False, "error": "missing slug"}, status=400)
    pdir = _composition_projects_root() / slug
    if not pdir.is_dir():
        return JsonResponse({"ok": False, "error": "not found"}, status=404)
    shutil.rmtree(pdir)
    return JsonResponse({"ok": True, "slug": slug})


def noise_studio_render_soundfont(request):
    """Render a posted MIDI file through a SoundFont via FluidSynth.

    Currently exposes the Salamander Grand Piano (Yamaha C5) — far more realistic
    than the browser's additive piano synthesis. POST a .mid file in the `midi`
    field. Optional `bpm` form field rewrites the MIDI's tempo before rendering.
    Returns a stereo 44.1kHz WAV.
    """
    from django.http import HttpResponse, JsonResponse
    import subprocess
    import tempfile
    import time

    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    upload = request.FILES.get("midi")
    if not upload:
        return JsonResponse({"error": "no MIDI file in 'midi' field"}, status=400)
    if upload.size > 8 * 1024 * 1024:
        return JsonResponse({"error": "MIDI file exceeds 8 MB limit"}, status=413)

    # The SoundFont lives in the project's data/ dir. If absent, the user hasn't
    # downloaded it yet — surface a useful error instead of a cryptic crash.
    sf2_path = Path(settings.BASE_DIR) / "data" / "soundfonts" / "SalamanderC5.sf2"
    if not sf2_path.is_file():
        return JsonResponse({"error": f"SoundFont not found at {sf2_path}. Re-run the setup."}, status=500)

    target_bpm = 0.0
    try:
        target_bpm = float(request.POST.get("bpm") or 0)
    except ValueError:
        target_bpm = 0.0

    with tempfile.TemporaryDirectory(prefix="ns_sf_") as tmpdir:
        tmp = Path(tmpdir)
        mid_in = tmp / "input.mid"
        wav_out = tmp / "output.wav"
        with open(mid_in, "wb") as f:
            for chunk in upload.chunks():
                f.write(chunk)

        # Optional tempo rewrite: replace all tempo events with one at the start.
        # FluidSynth respects the file's set_tempo events, so rewriting the file
        # is the cleanest way to lock to a target BPM.
        if target_bpm > 0:
            try:
                import mido
                mf = mido.MidiFile(str(mid_in))
                new_tempo = int(60_000_000 / max(1, target_bpm))
                # Strip existing tempo events.
                for track in mf.tracks:
                    track[:] = [m for m in track if m.type != "set_tempo"]
                # Insert one tempo event at the head of track 0.
                if mf.tracks:
                    mf.tracks[0].insert(0, mido.MetaMessage("set_tempo", tempo=new_tempo, time=0))
                mf.save(str(mid_in))
            except Exception as exc:
                return JsonResponse({"error": f"tempo rewrite failed: {exc}"}, status=500)

        # Render: -ni = non-interactive, -F = output file. FluidSynth picks a
        # sensible audio config for offline rendering (44.1k stereo by default).
        fluidsynth_path = "/opt/homebrew/bin/fluidsynth"
        if not os.path.exists(fluidsynth_path):
            fluidsynth_path = "fluidsynth"  # let PATH resolve it
        t0 = time.time()
        try:
            result = subprocess.run(
                [fluidsynth_path, "-ni", "-F", str(wav_out), str(sf2_path), str(mid_in)],
                check=True, capture_output=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            return JsonResponse({"error": "fluidsynth timed out (>5 min)"}, status=504)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="ignore")[:500]
            return JsonResponse({"error": f"fluidsynth failed: {stderr}"}, status=500)
        elapsed = time.time() - t0

        if not wav_out.is_file() or wav_out.stat().st_size == 0:
            return JsonResponse({"error": "fluidsynth produced no output"}, status=500)
        with open(wav_out, "rb") as f:
            data = f.read()

    response = HttpResponse(data, content_type="audio/wav")
    response["Content-Disposition"] = 'attachment; filename="salamander_render.wav"'
    response["X-Render-Seconds"] = f"{elapsed:.1f}"
    return response


def noise_studio_audio_to_midi(request):
    """Transcribe an uploaded audio file to MIDI via Spotify's basic-pitch.

    Returns the .mid file bytes directly. Slow (~20-30s for a 5-min track on
    Apple Silicon), so the studio shows a "transcribing…" status while waiting.
    """
    from django.http import HttpResponse, JsonResponse
    import tempfile
    import time

    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    upload = request.FILES.get("audio")
    if not upload:
        return JsonResponse({"error": "no audio file in 'audio' field"}, status=400)
    # Sanity-check file size to avoid runaway requests (basic-pitch holds the
    # whole signal in memory). 60 MB ≈ ~30 min of mp3 — plenty for any chapter.
    if upload.size > 60 * 1024 * 1024:
        return JsonResponse({"error": "audio file exceeds 60 MB limit"}, status=413)

    # Use a private temp dir so basic-pitch's auto-named output is in a known
    # spot and can be deleted on the way out.
    with tempfile.TemporaryDirectory(prefix="ns_a2m_") as tmpdir:
        tmp_path = Path(tmpdir)
        # Preserve a usable extension so basic-pitch / its dependencies (resampy,
        # librosa) can pick the right loader by sniffing the filename.
        orig_ext = os.path.splitext(upload.name)[1].lower() or ".wav"
        if orig_ext not in {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}:
            orig_ext = ".wav"
        in_path = tmp_path / f"input{orig_ext}"
        with open(in_path, "wb") as f:
            for chunk in upload.chunks():
                f.write(chunk)
        try:
            from basic_pitch.inference import predict_and_save  # noqa: WPS433
            from basic_pitch import ICASSP_2022_MODEL_PATH  # noqa: WPS433
        except ImportError as exc:
            return JsonResponse({"error": f"basic-pitch not available: {exc}"}, status=500)

        t0 = time.time()
        try:
            predict_and_save(
                [str(in_path)],
                str(tmp_path),
                save_midi=True,
                sonify_midi=False,
                save_model_outputs=False,
                save_notes=False,
                model_or_model_path=ICASSP_2022_MODEL_PATH,
            )
        except Exception as exc:
            return JsonResponse({"error": f"transcription failed: {exc}"}, status=500)
        # basic-pitch names the output "<stem>_basic_pitch.mid" next to the input.
        midi_path = tmp_path / f"{in_path.stem}_basic_pitch.mid"
        if not midi_path.is_file():
            # Fall back to any .mid in the dir (filename convention has changed
            # across basic-pitch versions).
            mids = list(tmp_path.glob("*.mid"))
            if not mids:
                return JsonResponse({"error": "transcription produced no MIDI"}, status=500)
            midi_path = mids[0]
        with open(midi_path, "rb") as f:
            data = f.read()
        elapsed = time.time() - t0

    out_name = (os.path.splitext(upload.name)[0] or "transcribed") + "_basic_pitch.mid"
    response = HttpResponse(data, content_type="audio/midi")
    response["Content-Disposition"] = f'attachment; filename="{out_name}"'
    response["X-Transcribe-Seconds"] = f"{elapsed:.1f}"
    response["X-Transcribe-Bytes"] = str(len(data))
    return response



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
