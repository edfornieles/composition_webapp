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
    from ..models import Composition, IngestionBatch

    import random
    ready_qs = Composition.objects.filter(ready_for_deployment=True).order_by("-id")
    ready_list = list(ready_qs)

    character_preview_sections = []
    if len(ready_list) >= 9:
        sample = random.sample(ready_list, 9)
        character_preview_sections.append({"label": "Random Selection", "compositions": sample})
    elif ready_list:
        character_preview_sections.append({"label": "Random Selection", "compositions": ready_list})

    context = {
        "total_compositions": Composition.objects.count(),
        "ready_count": ready_qs.count(),
        "draft_count": Composition.objects.filter(ready_for_deployment=False).count(),
        "sources_count": IngestionBatch.objects.count(),
        "character_preview_sections": character_preview_sections,
    }
    return render(request, "admin/dashboard.html", context)



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


def user_logout(request):
    logout(request)
    messages.success(request, "You have been logged out successfully.")
    return redirect('admin-login')  # Replace 'login' with the name of your login page URL



__all__ = [n for n in list(vars().keys()) if not n.startswith('__')]
