"""3D Studio: browse + view models from the local "3d studio/3d_assets" library.

Loads Three.js-compatible formats (OBJ, FBX, GLB/GLTF, STL, DAE, PLY) — other
files in the library (Maya .ma, .max, XPS) are listed but flagged unloadable.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from django.conf import settings as dj_settings
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

# library lives outside the repo (sibling of composition_webapp-main)
ASSETS_ROOT = Path(dj_settings.BASE_DIR).parent / "3d studio" / "3d_assets"

LOADABLE_EXTS = {".glb", ".gltf", ".obj", ".fbx", ".stl", ".dae", ".ply"}
ANY_3D_EXTS = LOADABLE_EXTS | {".ma", ".max", ".blend", ".xps", ".mesh", ".c4d"}
TEXTURE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp"}


def _is_hidden(p: Path) -> bool:
    return p.name.startswith(".") or p.name.startswith("._")


def _is_anim_path(rel: Path) -> bool:
    """A file under any segment named 'animations' (or '*_anim*') is an animation clip."""
    parts = [p.lower() for p in rel.parts]
    if any(p in {"animations", "anim"} for p in parts):
        return True
    if any("anim" in p for p in parts[:-1]):
        return True
    return False


def _scan_library() -> list[dict]:
    """Group loadable models by top-level category folder. Animations split out."""
    if not ASSETS_ROOT.exists():
        return []
    categories: dict[str, list[dict]] = {}
    for top in sorted(ASSETS_ROOT.iterdir()):
        if not top.is_dir() or _is_hidden(top):
            continue
        cat = top.name
        models: list[dict] = []
        for f in top.rglob("*"):
            if not f.is_file() or _is_hidden(f):
                continue
            ext = f.suffix.lower()
            if ext not in LOADABLE_EXTS:
                continue
            rel = f.relative_to(ASSETS_ROOT)
            if _is_anim_path(rel):
                continue  # animations live in their own list
            models.append({
                "name": f.stem,
                "ext": ext.lstrip("."),
                "rel": str(rel),
                "url": f"/api/studio3d/file/?p={quote(str(rel))}",
                "size_mb": round(f.stat().st_size / 1_048_576, 2),
                "subdir": str(rel.parent),
            })
        if models:
            models.sort(key=lambda m: (m["subdir"], m["name"]))
            categories[cat] = models
    return [{"category": k, "models": v} for k, v in categories.items()]


def _scan_animations() -> list[dict]:
    """Find animation-only files. Convention: any FBX/GLB in a folder named
    'animations' (case-insensitive) at any depth, OR named '*_anim*'.
    """
    if not ASSETS_ROOT.exists():
        return []
    out: list[dict] = []
    for f in ASSETS_ROOT.rglob("*"):
        if not f.is_file() or _is_hidden(f):
            continue
        ext = f.suffix.lower()
        if ext not in {".fbx", ".glb", ".gltf"}:
            continue
        rel = f.relative_to(ASSETS_ROOT)
        if not _is_anim_path(rel):
            continue
        out.append({
            "name": f.stem,
            "ext": ext.lstrip("."),
            "rel": str(rel),
            "url": f"/api/studio3d/file/?p={quote(str(rel))}",
            "size_mb": round(f.stat().st_size / 1_048_576, 2),
            "subdir": str(rel.parent),
        })
    out.sort(key=lambda m: (m["subdir"], m["name"]))
    return out


@require_GET
def template_play_page(request):
    """Reference playground using the pre-rigged Three.js Soldier.glb (Idle/Walk/Run baked in)."""
    return render(request, "template_play.html", {})


@require_GET
def wojak_play_page(request):
    """Playable wojak — keyboard movement + animation state machine."""
    base = ASSETS_ROOT / "wojak model [meme war]"
    glb = base / "wojak_clean.glb"
    glb_url = (
        f"/api/studio3d/file/?p={quote(str(glb.relative_to(ASSETS_ROOT)))}"
        if glb.exists() else ""
    )
    anim_dir = base / "wojak_animations"
    clips: list[dict] = []
    if anim_dir.exists():
        for f in sorted(anim_dir.iterdir()):
            if not f.is_file() or _is_hidden(f):
                continue
            if f.suffix.lower() != ".fbx":
                continue
            rel = f.relative_to(ASSETS_ROOT)
            clips.append({
                "name": f.stem,
                "url": f"/api/studio3d/file/?p={quote(str(rel))}",
            })
    return render(request, "wojak_play.html", {
        "glb_url": glb_url,
        "clips": clips,
    })


@require_GET
def studio_3d_page(request):
    library = _scan_library()
    animations = _scan_animations()
    return render(request, "studio_3d.html", {
        "library": library,
        "animations": animations,
        "model_count": sum(len(c["models"]) for c in library),
        "category_count": len(library),
        "anim_count": len(animations),
    })


@require_GET
def studio_3d_library(request):
    return JsonResponse({"library": _scan_library()})


@require_GET
def studio_3d_file(request):
    """Serve a file from the assets library, scoped to ASSETS_ROOT."""
    rel = request.GET.get("p", "")
    if not rel or ".." in rel:
        raise Http404
    target = (ASSETS_ROOT / rel).resolve()
    try:
        target.relative_to(ASSETS_ROOT.resolve())
    except ValueError:
        raise Http404
    if not target.is_file():
        raise Http404
    ext = target.suffix.lower()
    ctype = {
        ".obj": "model/obj",
        ".fbx": "application/octet-stream",
        ".glb": "model/gltf-binary",
        ".gltf": "model/gltf+json",
        ".stl": "model/stl",
        ".dae": "model/vnd.collada+xml",
        ".ply": "model/ply",
        ".mtl": "text/plain",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".bmp": "image/bmp", ".webp": "image/webp", ".tga": "image/x-tga",
    }.get(ext, "application/octet-stream")
    return FileResponse(open(target, "rb"), content_type=ctype)
