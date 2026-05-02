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


_RECORD_MINT_TOKEN_MAX_AGE = 90 * 86400
_record_mint_signer = TimestampSigner(salt="djangoscrap.nft.record_mint")



def _make_record_mint_token(*, nft_id: int, composition_id: int) -> str:
    """Signed capability issued only from composition_prepare_nft_version when status is ready."""
    return _record_mint_signer.sign(f"{int(nft_id)}:{int(composition_id)}")



def _parse_record_mint_token(token: str | None) -> tuple[int, int] | None:
    if not token or not str(token).strip():
        return None
    try:
        raw = _record_mint_signer.unsign(str(token).strip(), max_age=_RECORD_MINT_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    parts = raw.split(":", 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


@require_POST

def composition_prepare_nft_version(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    wallet = (request.POST.get("wallet") or "").strip()
    state = _nft_public_state(request, composition, wallet=wallet)
    if not state.get("can_prepare"):
        return JsonResponse({"ok": False, "error": state.get("blocked_reason") or "NFT version cannot be prepared yet."}, status=403)
    nft = _prepare_composition_nft_version(request, composition, wallet=wallet)
    status = 200 if nft.status == "ready" else 500
    payload_out = {
        "ok": nft.status == "ready",
        "nft_id": int(nft.id),
        "version": int(nft.version_number),
        "status": nft.status,
        "error": nft.error_message,
        "metadata_url": request.build_absolute_uri(reverse("composition_nft_version_metadata", kwargs={"nft_id": int(nft.id)})),
        "video_url": request.build_absolute_uri(reverse("composition_nft_version_video", kwargs={"nft_id": int(nft.id)})) if nft.local_video_file else "",
        "token_uri": nft.token_uri,
        "nft": _nft_public_state(request, composition, wallet=wallet),
    }
    if nft.status == "ready":
        payload_out["record_mint_token"] = _make_record_mint_token(
            nft_id=int(nft.id),
            composition_id=int(composition.id),
        )
    return JsonResponse(payload_out, status=status)


@require_POST

def composition_record_nft_mint(request, composition_id):
    composition = get_object_or_404(Composition, id=composition_id)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    nft_id = payload.get("nft_id") or request.POST.get("nft_id")
    nft = get_object_or_404(CompositionNFT, id=nft_id, composition=composition)
    record_token = (
        payload.get("record_mint_token")
        or payload.get("mint_token")
        or request.POST.get("record_mint_token")
        or request.POST.get("mint_token")
        or ""
    )
    parsed = _parse_record_mint_token(str(record_token).strip() or None)
    if not parsed or parsed[0] != int(nft.id) or parsed[1] != int(composition.id):
        return JsonResponse(
            {"ok": False, "error": "Valid record_mint_token from prepare is required (call prepare after connecting wallet)."},
            status=403,
        )
    if nft.status != "ready":
        return JsonResponse(
            {"ok": False, "error": "NFT version is not in ready state; record-mint is only valid after a successful prepare."},
            status=403,
        )
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

def _mintable_compositions_queryset():
    return (
        Composition.objects.filter(ready_for_deployment=True)
        .select_related("series")
        .exclude(url__isnull=True)
        .exclude(url__exact="")
        .order_by("-created_at", "-id")
    )



def _mint_navigation(page_slug: str) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    from ._associations import _composition_slug_from_url
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
    from ._associations import _composition_slug_from_url
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
    from ._associations import _composition_slug_from_url
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
    from ._associations import _composition_slug_from_url
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
    from ._compositions import _find_composition_by_slug
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
    from ._compositions import _find_composition_by_slug
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



from ._utils import create_video_ffmpeg  # noqa: F401
from django.http import HttpResponseNotAllowed
from django.utils.log import log_response
__all__ = [n for n in list(vars().keys()) if not n.startswith('__')]
