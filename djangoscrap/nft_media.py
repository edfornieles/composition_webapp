import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone


MEDIA_KINDS = {
    "poster": {"duration": 0, "extension": "jpg", "content_type": "image/jpeg"},
    "preview_10s": {
        "duration": 10,
        "extension": "mp4",
        "content_type": "video/mp4",
        "size": (720, 720),
        "fps": 24,
        "crf": 32,
        "maxrate": "700k",
        "bufsize": "1400k",
        "include_audio": True,
        "audio_bitrate": "64k",
        "use_screencast": True,
    },
    "collector_45s": {
        "duration": 45,
        "extension": "mp4",
        "content_type": "video/mp4",
        "size": (1080, 1080),
        "fps": 25,
        "crf": 18,
        "preset": "slow",
        "include_audio": True,
        "use_screencast": True,
    },
}


def _source_root() -> Path:
    return (Path(settings.BASE_DIR).parent / "composition_sources_unprocessed").resolve()


def _source_folder_state(source_name: str) -> dict:
    source_dir = (_source_root() / str(source_name)).resolve()
    if not source_dir.exists() or not source_dir.is_dir():
        return {"name": source_name, "exists": False, "files": 0, "bytes": 0, "latest_mtime_ns": 0}

    file_count = 0
    total_bytes = 0
    latest_mtime_ns = 0
    sample = []
    for path in sorted(p for p in source_dir.rglob("*") if p.is_file()):
        try:
            stat = path.stat()
        except OSError:
            continue
        file_count += 1
        total_bytes += int(stat.st_size)
        latest_mtime_ns = max(latest_mtime_ns, int(stat.st_mtime_ns))
        if len(sample) < 80:
            sample.append(
                {
                    "path": str(path.relative_to(source_dir)),
                    "size": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                }
            )

    return {
        "name": source_name,
        "exists": True,
        "files": file_count,
        "bytes": total_bytes,
        "latest_mtime_ns": latest_mtime_ns,
        "sample": sample,
    }


def _storage_file_state(file_field) -> dict:
    name = str(getattr(file_field, "name", "") or "").strip()
    if not name:
        return {"name": "", "exists": False}
    state = {"name": name, "exists": False, "size": 0, "mtime": ""}
    try:
        state["exists"] = bool(default_storage.exists(name))
        if state["exists"]:
            state["size"] = int(default_storage.size(name))
            try:
                state["mtime"] = default_storage.get_modified_time(name).isoformat()
            except Exception:
                state["mtime"] = ""
    except Exception:
        pass
    return state


def composition_source_signature(composition) -> str:
    source_names = []
    for values in (
        composition.background_sources or [],
        composition.foreground_sources or [],
        composition.overlay_sources or [],
    ):
        source_names.extend(str(v).strip() for v in values if str(v).strip())

    payload = {
        "composition_id": int(composition.id),
        "type": composition.type,
        "transition": composition.transition,
        "playback_speed": float(composition.playback_speed or 1),
        "source_playback_mode": composition.source_playback_mode,
        "landscape_only": bool(composition.landscape_only),
        "brightness": int(composition.brightness or 0),
        "saturation": int(composition.saturation or 0),
        "opacity": int(composition.opacity or 0),
        "filter_preset": composition.filter_preset,
        "filter_intensity": int(composition.filter_intensity or 0),
        "filter_settings": composition.filter_settings or {},
        "overlay_sources": composition.overlay_sources or [],
        "audio_file": _storage_file_state(composition.audio_file),
        "overlay_landscape_only": bool(composition.overlay_landscape_only),
        "overlay_speed": float(composition.overlay_speed or 1),
        "overlay_scale": float(composition.overlay_scale or 1),
        "overlay_rotate": bool(composition.overlay_rotate),
        "overlay_fit": composition.overlay_fit,
        "overlay_frame_margin": float(composition.overlay_frame_margin or 0),
        "background_sources": composition.background_sources or [],
        "foreground_sources": composition.foreground_sources or [],
        "source_folders": [_source_folder_state(name) for name in sorted(set(source_names))],
    }
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def composition_media_assets(composition) -> dict:
    return {asset.kind: asset for asset in composition.media_assets.all()}


def media_asset_is_fresh(asset, signature: str) -> bool:
    if not asset or asset.status != "ready" or not asset.file:
        return False
    if asset.source_signature != signature:
        return False
    try:
        return default_storage.exists(asset.file.name)
    except Exception:
        return False


def capture_url_for_composition(composition, *, capture_kind: str = "video") -> str:
    """Build the capture URL for Playwright.

    capture_kind="still" appends &capture=still — motion-modes like eye-wake
    use this to freeze on a useful frame for the poster. Video captures use
    the default and let motion run normally.
    """
    if not composition.url:
        raise ValueError("Composition has no public URL to capture.")
    path = composition.url.rstrip("/")
    suffix = "/?render=1" if "?" not in path else "&render=1"
    if capture_kind == "still":
        suffix += "&capture=still"
    # Playwright requires a fully-qualified URL; composition.url is a path like
    # "/foo". Default to the local gunicorn/runserver, overridable via env.
    base = os.getenv("NFT_CAPTURE_BASE_URL", "http://localhost:8765").rstrip("/")
    return f"{base}{path}{suffix}"


def _playwright_headless_value() -> bool:
    value = os.getenv("PLAYWRIGHT_HEADLESS", "true").strip().lower()
    return value not in ("0", "false", "no", "off")


def _launch_chromium(playwright):
    args = [
        "--autoplay-policy=no-user-gesture-required",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=IsolateOrigins,site-per-process",
    ]
    kwargs = {"headless": _playwright_headless_value(), "args": args}
    channel = os.getenv("PLAYWRIGHT_CHROME_CHANNEL", "").strip()
    if channel:
        kwargs["channel"] = channel
        return playwright.chromium.launch(**kwargs)
    try:
        kwargs["channel"] = "chrome"
        return playwright.chromium.launch(**kwargs)
    except Exception:
        kwargs.pop("channel", None)
    return playwright.chromium.launch(**kwargs)


def _wait_for_render_ready(page, timeout_ms: int = 120000):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    page.wait_for_timeout(800)
    # Stage 1: wait until isCompositionReadyForCapture() signals all assets loaded.
    try:
        page.wait_for_function(
            "() => window.__compositionCaptureReady === true",
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        pass

    # Stage 2: force-play every video so they produce frames, not frozen first frames.
    page.evaluate(
        """() => {
            document.querySelectorAll('video').forEach((v) => {
                try { v.muted = true; v.play(); } catch (e) {}
            });
        }"""
    )

    # Stage 3: wait until every visible video has actually advanced (currentTime > 0),
    # proving animation frames are being produced — not just a loaded-but-paused clip.
    try:
        page.wait_for_function(
            """() => {
                const videos = Array.from(document.querySelectorAll('video'));
                if (!videos.length) return true;
                return videos.every(v => v.paused || v.currentTime > 0.05);
            }""",
            timeout=8000,
        )
    except PlaywrightTimeoutError:
        pass

    # Stage 4: a short settle window for CSS transitions and first-frame rendering.
    page.wait_for_timeout(800)


def _store_file(local_path: Path, storage_path: str) -> str:
    if default_storage.exists(storage_path):
        default_storage.delete(storage_path)
    with open(local_path, "rb") as fh:
        default_storage.save(storage_path, ContentFile(fh.read()))
    return storage_path


def capture_composition_still(composition, *, storage_path: str | None = None, size: int = 1080, random_offset: bool = True) -> str:
    import random as _random
    from playwright.sync_api import sync_playwright

    storage_path = storage_path or f"nft/generated/composition_{int(composition.id)}/poster_square.jpg"
    renders_dir = Path(settings.MEDIA_ROOT) / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = renders_dir / f"poster_{composition.id}_{uuid.uuid4().hex[:8]}.jpg"

    try:
        with sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            context = browser.new_context(viewport={"width": size, "height": size})
            page = context.new_page()
            page.goto(capture_url_for_composition(composition, capture_kind="still"), wait_until="domcontentloaded", timeout=120000)
            _wait_for_render_ready(page)
            if random_offset:
                offset_ms = _random.randint(3000, 20000)
                page.wait_for_timeout(offset_ms)
            page.screenshot(path=str(tmp_path), type="jpeg", quality=92)
            context.close()
            browser.close()
        return _store_file(tmp_path, storage_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def capture_composition_still_png(
    composition,
    *,
    size: int = 1080,
    random_offset: bool = True,
    transparent: bool = True,
) -> bytes:
    """Capture a composition still as PNG bytes.

    When transparent=True (default) the page's solid backdrop fills (body and
    .stage are #000) are stripped and Playwright omits its default canvas, so
    the result has a genuine alpha channel wherever no image layer covers the
    frame — the PNG opens in Photoshop with an unlocked transparent layer
    rather than a locked flat fill.
    """
    import random as _random
    from playwright.sync_api import sync_playwright

    renders_dir = Path(settings.MEDIA_ROOT) / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = renders_dir / f"still_png_{composition.id}_{uuid.uuid4().hex[:8]}.png"

    try:
        with sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            context = browser.new_context(viewport={"width": size, "height": size})
            if transparent:
                # Strip the page's solid black background-COLOR (body/.stage use
                # background:#000) so the screenshot's alpha shows through where no
                # image layer covers the frame. Use an init script (runs before
                # page scripts on EVERY navigation) rather than a post-load
                # add_style_tag: these pages auto-navigate / refresh, which
                # destroys the execution context and makes add_style_tag race and
                # throw ("Execution context was destroyed"). Leave background-image
                # intact — some composition types paint imagery into .stage that
                # way, and removing it would yield an empty (all-alpha-0) PNG.
                context.add_init_script(
                    "(()=>{const c='html,body{background-color:transparent !important}"
                    ".stage{background-color:transparent !important}';"
                    "const f=()=>{if(document.getElementById('__pngxf'))return;"
                    "const s=document.createElement('style');s.id='__pngxf';"
                    "s.textContent=c;(document.head||document.documentElement).appendChild(s);};"
                    "if(document.readyState==='loading')"
                    "document.addEventListener('DOMContentLoaded',f);else f();})();"
                )
            page = context.new_page()
            page.goto(
                capture_url_for_composition(composition, capture_kind="still"),
                wait_until="domcontentloaded",
                timeout=120000,
            )
            _wait_for_render_ready(page)
            if random_offset:
                offset_ms = _random.randint(3000, 20000)
                page.wait_for_timeout(offset_ms)
            page.screenshot(
                path=str(tmp_path),
                type="png",
                omit_background=transparent,
            )
            context.close()
            browser.close()
        raw = tmp_path.read_bytes()
        if transparent:
            # Guarantee an alpha channel so the PNG always opens in Photoshop as
            # an unlocked layer (Layer 0) rather than a locked Background. PNG
            # screenshots can come back as RGB when the frame is fully covered,
            # in which case omit_background alone leaves no alpha; force RGBA.
            try:
                from PIL import Image as _Image
                import io as _io2
                im = _Image.open(_io2.BytesIO(raw))
                if im.mode != "RGBA":
                    buf = _io2.BytesIO()
                    im.convert("RGBA").save(buf, format="PNG")
                    raw = buf.getvalue()
            except Exception:
                pass
        return raw
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def capture_composition_video(
    composition,
    *,
    duration_seconds: int,
    storage_path: str | None = None,
    aspect_preset: str = "square",
    include_audio: bool = False,
    trim_start_seconds: float = 0.7,
    size_override: tuple[int, int] | None = None,
    fps: int | None = None,
    crf: int | None = None,
    maxrate: str | None = None,
    bufsize: str | None = None,
    audio_bitrate: str = "128k",
    oversample_factor: int = 1,
    preset: str = "medium",
    use_screencast: bool = False,
) -> str:
    import base64 as _base64
    from playwright.sync_api import sync_playwright

    preset_sizes = {
        "landscape": (1920, 1080),
        "laptop": (1440, 900),
        "portrait": (1080, 1920),
        "square": (1080, 1080),
    }
    size = size_override or preset_sizes.get(aspect_preset, preset_sizes["square"])
    duration_seconds = max(3, min(600, int(duration_seconds)))
    trim_start_seconds = max(0, float(trim_start_seconds or 0))
    fps = max(12, min(60, int(fps or 25)))
    oversample_factor = max(1, min(4, int(oversample_factor or 1)))
    preset = preset or "medium"
    storage_path = storage_path or (
        f"nft/generated/composition_{int(composition.id)}/"
        f"{aspect_preset}_{duration_seconds}s.mp4"
    )

    renders_dir = Path(settings.MEDIA_ROOT) / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)
    temp_capture_dir = Path(tempfile.mkdtemp(prefix="capture_", dir=str(renders_dir)))
    output_path = renders_dir / f"render_{composition.id}_{aspect_preset}_{duration_seconds}s_{uuid.uuid4().hex[:8]}.mp4"
    audio_temp_path = None

    try:
        # --- audio setup (shared by both capture modes) ---
        audio_input = None
        if include_audio and composition.audio_file:
            af_name = composition.audio_file.name
            if af_name and default_storage.exists(af_name):
                audio_temp_path = tempfile.NamedTemporaryFile(
                    suffix=Path(af_name).suffix or ".mp3",
                    delete=False,
                ).name
                with default_storage.open(af_name, "rb") as src, open(audio_temp_path, "wb") as dst:
                    dst.write(src.read())
                audio_input = audio_temp_path

        if use_screencast:
            # ------------------------------------------------------------------
            # CDP screencast mode — JPEG frames captured after page is fully
            # ready, assembled by ffmpeg.  Eliminates the VP8 intermediate step
            # so source-image detail is preserved all the way to the final H264.
            # Dead space is impossible here because recording only begins after
            # _wait_for_render_ready() returns.
            # ------------------------------------------------------------------
            frames_dir = temp_capture_dir / "frames"
            frames_dir.mkdir()
            frame_list: list[tuple[str, float]] = []  # (abs_path, monotonic_time)

            with sync_playwright() as playwright:
                browser = _launch_chromium(playwright)
                context = browser.new_context(
                    viewport={"width": size[0], "height": size[1]},
                )
                page = context.new_page()
                cdp = context.new_cdp_session(page)

                page.goto(
                    capture_url_for_composition(composition),
                    wait_until="domcontentloaded",
                    timeout=120000,
                )
                _wait_for_render_ready(page)

                def _on_frame(event):
                    raw = _base64.b64decode(event.get("data", ""))
                    t = time.monotonic()
                    idx = len(frame_list)
                    fpath = frames_dir / f"frame_{idx:06d}.jpg"
                    fpath.write_bytes(raw)
                    frame_list.append((str(fpath), t))
                    try:
                        cdp.send("Page.screencastFrameAck", {"sessionId": event.get("sessionId", 0)})
                    except Exception:
                        pass

                cdp.on("Page.screencastFrame", _on_frame)
                cdp.send("Page.startScreencast", {
                    "format": "jpeg",
                    "quality": 92,
                    "maxWidth": size[0],
                    "maxHeight": size[1],
                    "everyNthFrame": 1,
                })

                page.wait_for_timeout(int((duration_seconds + 2.0) * 1000))

                try:
                    cdp.send("Page.stopScreencast")
                except Exception:
                    pass
                context.close()
                browser.close()

            if not frame_list:
                raise RuntimeError("CDP screencast produced no frames")

            # Build a concat file so ffmpeg respects each frame's actual capture time.
            concat_file = temp_capture_dir / "frames.txt"
            timestamps = [t for _, t in frame_list]
            with open(concat_file, "w") as cf:
                for i, (fpath, _) in enumerate(frame_list):
                    next_t = timestamps[i + 1] if i + 1 < len(timestamps) else timestamps[i] + 1.0 / fps
                    dur = max(0.001, next_t - timestamps[i])
                    cf.write(f"file '{fpath}'\n")
                    cf.write(f"duration {dur:.6f}\n")
                # ffmpeg concat demuxer requires a trailing entry without duration
                cf.write(f"file '{frame_list[-1][0]}'\n")

            ffmpeg_cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_file),
            ]
            if audio_input:
                ffmpeg_cmd += ["-stream_loop", "-1", "-i", str(audio_input)]
            ffmpeg_cmd += [
                "-map", "0:v:0",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-r", str(fps), "-preset", preset,
                "-crf", str(int(crf if crf is not None else 23)),
                "-movflags", "+faststart",
            ]
            if maxrate:
                ffmpeg_cmd += ["-maxrate", str(maxrate)]
            if bufsize:
                ffmpeg_cmd += ["-bufsize", str(bufsize)]
            if audio_input:
                ffmpeg_cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", audio_bitrate]
            else:
                ffmpeg_cmd += ["-an"]
            # No -ss trim needed — recording began after page was ready.
            ffmpeg_cmd += ["-t", str(duration_seconds), str(output_path)]

            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(result.stderr or result.stdout or "ffmpeg encode failed (screencast)")
            return _store_file(output_path, storage_path)

        else:
            # ------------------------------------------------------------------
            # record_video_dir mode — original Playwright VP8/WebM capture.
            # Used for preview_10s and as fallback.
            # ------------------------------------------------------------------
            capture_path = None
            load_elapsed_seconds = trim_start_seconds  # fallback if measurement fails

            with sync_playwright() as playwright:
                browser = _launch_chromium(playwright)
                rec_w = size[0] * oversample_factor
                rec_h = size[1] * oversample_factor
                ctx_kwargs: dict = {
                    "viewport": {"width": size[0], "height": size[1]},
                    "record_video_dir": str(temp_capture_dir),
                    "record_video_size": {"width": rec_w, "height": rec_h},
                }
                if oversample_factor > 1:
                    ctx_kwargs["device_scale_factor"] = float(oversample_factor)
                context = browser.new_context(**ctx_kwargs)
                page = context.new_page()
                t_nav = time.monotonic()
                page.goto(
                    capture_url_for_composition(composition),
                    wait_until="domcontentloaded",
                    timeout=120000,
                )
                _wait_for_render_ready(page)
                load_elapsed_seconds = time.monotonic() - t_nav
                page.wait_for_timeout(max(1000, int((duration_seconds + 1.0) * 1000)))
                page_video = page.video
                context.close()
                browser.close()
                capture_path = Path(page_video.path())

            ffmpeg_cmd = ["ffmpeg", "-y", "-i", str(capture_path)]
            if audio_input:
                ffmpeg_cmd += ["-stream_loop", "-1", "-i", str(audio_input)]
            vf_filters = []
            if oversample_factor > 1:
                vf_filters.append(f"scale={size[0]}:{size[1]}:flags=lanczos")
            ffmpeg_cmd += [
                "-map", "0:v:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-r", str(fps), "-preset", preset,
                "-crf", str(int(crf if crf is not None else 23)),
                "-movflags", "+faststart",
            ]
            if vf_filters:
                ffmpeg_cmd += ["-vf", ",".join(vf_filters)]
            if maxrate:
                ffmpeg_cmd += ["-maxrate", str(maxrate)]
            if bufsize:
                ffmpeg_cmd += ["-bufsize", str(bufsize)]
            if audio_input:
                ffmpeg_cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", audio_bitrate]
            else:
                ffmpeg_cmd += ["-an"]

            effective_trim = load_elapsed_seconds + 0.3
            try:
                dur_probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(capture_path)],
                    capture_output=True, text=True, timeout=30,
                )
                total_rec_duration = float((dur_probe.stdout or "").strip() or "0") or (duration_seconds + effective_trim + 30)
            except Exception:
                total_rec_duration = duration_seconds + effective_trim + 30

            def _last_black_end_from_probe(stderr: str) -> float:
                val = 0.0
                for line in stderr.splitlines():
                    if "black_end:" in line:
                        for part in line.split():
                            if part.startswith("black_end:"):
                                try:
                                    val = max(val, float(part.split(":")[1]))
                                except (ValueError, IndexError):
                                    pass
                return val

            try:
                probe_dark = subprocess.run(
                    ["ffmpeg", "-i", str(capture_path),
                     "-vf", "crop=iw:ih/4:0:ih*3/4,blackdetect=d=0.1:pix_th=0.10",
                     "-f", "null", "-"],
                    capture_output=True, text=True, timeout=90,
                )
                last_dark_end = _last_black_end_from_probe(probe_dark.stderr or "")
                probe_bright = subprocess.run(
                    ["ffmpeg", "-i", str(capture_path),
                     "-vf", "crop=iw:ih/4:0:ih*3/4,negate,blackdetect=d=0.1:pix_th=0.10",
                     "-f", "null", "-"],
                    capture_output=True, text=True, timeout=90,
                )
                last_bright_end = _last_black_end_from_probe(probe_bright.stderr or "")
                last_uniform_end = max(last_dark_end, last_bright_end)
                if last_uniform_end > effective_trim:
                    max_auto_trim = max(effective_trim, total_rec_duration - duration_seconds - 0.5)
                    effective_trim = min(last_uniform_end + 0.15, max_auto_trim)
            except Exception:
                pass

            if effective_trim:
                ffmpeg_cmd += ["-ss", f"{effective_trim:.3f}"]
            ffmpeg_cmd += ["-t", str(duration_seconds), str(output_path)]
            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(result.stderr or result.stdout or "ffmpeg encode failed")
            return _store_file(output_path, storage_path)

    finally:
        if audio_temp_path:
            try:
                os.unlink(audio_temp_path)
            except OSError:
                pass
        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass
        try:
            shutil.rmtree(temp_capture_dir, ignore_errors=True)
        except OSError:
            pass


def generate_composition_media_assets(composition, *, force: bool = False, kinds: list[str] | None = None) -> dict:
    from .models import CompositionMediaAsset

    kinds = kinds or ["poster", "preview_10s", "collector_45s"]
    signature = composition_source_signature(composition)
    results = {}
    for kind in kinds:
        if kind not in MEDIA_KINDS:
            raise ValueError(f"Unknown media kind: {kind}")
        spec = MEDIA_KINDS[kind]
        asset, _ = CompositionMediaAsset.objects.get_or_create(
            composition=composition,
            kind=kind,
            defaults={
                "duration_seconds": int(spec["duration"]),
                "aspect_preset": "square",
            },
        )
        asset.duration_seconds = int(spec["duration"])
        asset.aspect_preset = "square"
        if not force and media_asset_is_fresh(asset, signature):
            asset._nft_media_skipped = True
            results[kind] = asset
            continue

        asset._nft_media_skipped = False

        # Archive the existing file before overwriting
        if asset.file and asset.file.name and asset.status == "ready":
            try:
                old_name = asset.file.name
                if default_storage.exists(old_name):
                    ts = timezone.now().strftime("%Y%m%d_%H%M%S")
                    ext = Path(old_name).suffix
                    archive_path = f"nft/generated/composition_{int(composition.id)}/archive/{kind}_{ts}{ext}"
                    with default_storage.open(old_name, "rb") as src:
                        default_storage.save(archive_path, ContentFile(src.read()))
                    archive = list(asset.archive or [])
                    archive.append({
                        "path": archive_path,
                        "generated_at": (asset.generated_at or asset.updated_at).isoformat() if (asset.generated_at or asset.updated_at) else "",
                        "size": default_storage.size(archive_path),
                    })
                    asset.archive = archive
            except Exception:
                pass

        asset.status = "rendering"
        asset.error_message = ""
        asset.save(update_fields=["duration_seconds", "aspect_preset", "status", "error_message", "archive", "updated_at"])
        try:
            ext = spec["extension"]
            storage_path = f"nft/generated/composition_{int(composition.id)}/{kind}.{ext}"
            if kind == "poster":
                rel_path = capture_composition_still(composition, storage_path=storage_path)
            else:
                rel_path = capture_composition_video(
                    composition,
                    duration_seconds=int(spec["duration"]),
                    storage_path=storage_path,
                    aspect_preset="square",
                    include_audio=bool(spec.get("include_audio", True)),
                    size_override=spec.get("size"),
                    fps=int(spec.get("fps") or 25),
                    crf=int(spec.get("crf") if spec.get("crf") is not None else 23),
                    maxrate=spec.get("maxrate"),
                    bufsize=spec.get("bufsize"),
                    audio_bitrate=str(spec.get("audio_bitrate") or "128k"),
                    trim_start_seconds=float(spec.get("trim_start_seconds") or 0.7),
                    oversample_factor=int(spec.get("oversample_factor") or 1),
                    preset=str(spec.get("preset") or "medium"),
                    use_screencast=bool(spec.get("use_screencast", False)),
                )
            asset.file.name = rel_path
            asset.source_signature = signature
            asset.status = "ready"
            asset.error_message = ""
            asset.generated_at = timezone.now()
        except Exception as exc:
            asset.status = "failed"
            asset.error_message = str(exc)[:2000]
        asset.save()
        results[kind] = asset
    return results
