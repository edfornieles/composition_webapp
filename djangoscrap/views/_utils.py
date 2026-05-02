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
from django.views.decorators.http import require_POST, require_http_methods
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



def _is_stock_image_candidate(filename: str) -> bool:
    """Best-effort stock image detection based on common filename markers."""
    lowered = (filename or "").lower()
    if not lowered:
        return False
    # Ignore non-image files; this detector is intended for still images.
    if Path(lowered).suffix not in LOCAL_SOURCE_IMAGE_EXTS:
        return False
    return any(marker in lowered for marker in _STOCK_FILENAME_MARKERS)


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

class S3BucketAjaxView(AjaxDatatableView):
    model = S3Bucket
    initial_order = [["created_at", "desc"]]

    column_defs = [
        {"name": "id", "title": "ID"},
        {"name": "name", "title": "Bucket Name"},
        {"name": "created_at", "title": "Created At"},
    ]

### **User Registration & Authentication**


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
from django.http import HttpResponseNotAllowed
from django.utils.log import log_response
__all__ = [n for n in list(vars().keys()) if not n.startswith('__')]
