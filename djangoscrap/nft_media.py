import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone


MEDIA_KINDS = {
    "poster": {"duration": 0, "extension": "jpg", "content_type": "image/jpeg"},
    "preview_15s": {
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
    },
    "collector_45s": {
        "duration": 45,
        "extension": "mp4",
        "content_type": "video/mp4",
        "size": (1080, 1080),
        "fps": 25,
        "crf": 23,
        "include_audio": True,
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


def capture_url_for_composition(composition) -> str:
    if not composition.url:
        raise ValueError("Composition has no public URL to capture.")
    base = composition.url.rstrip("/")
    return base + ("/?render=1" if "?" not in base else "&render=1")


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
    try:
        page.wait_for_function(
            "() => window.__compositionCaptureReady === true",
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1200)
    page.evaluate(
        """() => {
            document.querySelectorAll('video').forEach((v) => {
                try {
                    v.muted = true;
                    v.play();
                } catch (e) {}
            });
        }"""
    )


def _store_file(local_path: Path, storage_path: str) -> str:
    if default_storage.exists(storage_path):
        default_storage.delete(storage_path)
    with open(local_path, "rb") as fh:
        default_storage.save(storage_path, ContentFile(fh.read()))
    return storage_path


def capture_composition_still(composition, *, storage_path: str | None = None, size: int = 1080) -> str:
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
            page.goto(capture_url_for_composition(composition), wait_until="load", timeout=120000)
            _wait_for_render_ready(page)
            page.screenshot(path=str(tmp_path), type="jpeg", quality=92)
            context.close()
            browser.close()
        return _store_file(tmp_path, storage_path)
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
) -> str:
    from playwright.sync_api import sync_playwright

    preset_sizes = {
        "landscape": (1920, 1080),
        "laptop": (1440, 900),
        "portrait": (1080, 1920),
        "square": (1080, 1080),
    }
    size = size_override or preset_sizes.get(aspect_preset, preset_sizes["square"])
    duration_seconds = max(3, min(600, int(duration_seconds)))
    trim_start_seconds = max(0, min(3, float(trim_start_seconds or 0)))
    fps = max(12, min(60, int(fps or 25)))
    storage_path = storage_path or (
        f"nft/generated/composition_{int(composition.id)}/"
        f"{aspect_preset}_{duration_seconds}s.mp4"
    )

    renders_dir = Path(settings.MEDIA_ROOT) / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)
    temp_capture_dir = Path(tempfile.mkdtemp(prefix="capture_", dir=str(renders_dir)))
    output_path = renders_dir / f"render_{composition.id}_{aspect_preset}_{duration_seconds}s_{uuid.uuid4().hex[:8]}.mp4"
    capture_path = None
    audio_temp_path = None

    try:
        with sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            context = browser.new_context(
                viewport={"width": size[0], "height": size[1]},
                record_video_dir=str(temp_capture_dir),
                record_video_size={"width": size[0], "height": size[1]},
            )
            page = context.new_page()
            page.goto(capture_url_for_composition(composition), wait_until="load", timeout=120000)
            _wait_for_render_ready(page)
            page.wait_for_timeout(max(1000, int((duration_seconds + trim_start_seconds) * 1000)))
            page_video = page.video
            context.close()
            browser.close()
            capture_path = Path(page_video.path())

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

        ffmpeg_cmd = ["ffmpeg", "-y", "-i", str(capture_path)]
        if audio_input:
            ffmpeg_cmd += ["-stream_loop", "-1", "-i", str(audio_input)]
        ffmpeg_cmd += [
            "-map",
            "0:v:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-preset",
            "medium",
            "-crf",
            str(int(crf if crf is not None else 23)),
            "-movflags",
            "+faststart",
        ]
        if maxrate:
            ffmpeg_cmd += ["-maxrate", str(maxrate)]
        if bufsize:
            ffmpeg_cmd += ["-bufsize", str(bufsize)]
        if audio_input:
            ffmpeg_cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", audio_bitrate]
        else:
            ffmpeg_cmd += ["-an"]
        if trim_start_seconds:
            ffmpeg_cmd += ["-ss", f"{trim_start_seconds:.3f}"]
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

    kinds = kinds or ["poster", "preview_15s", "collector_45s"]
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
        asset.status = "rendering"
        asset.error_message = ""
        asset.save(update_fields=["duration_seconds", "aspect_preset", "status", "error_message", "updated_at"])
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
