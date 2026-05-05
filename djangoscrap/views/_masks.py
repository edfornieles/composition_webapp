"""Views for the Masks feature (Phase 1 — Web2 only).

Wallet/escrow flows are stubs that will be filled in Phase 2/3.
"""
import json
import secrets

from django.contrib import messages
from django.http import JsonResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from ..models import Mask, MaskAsset, MaskBattle, Tribe
from ..services.mask_battle import resolve_battle


__all__ = [
    "mask_admin_view",
    "mask_edit_view",
    "mask_studio_view",
    "mask_asset_delete_view",
    "tribe_create_view",
    "mask_public_view",
    "mask_arena_view",
    "mask_detail_api",
    "battle_intent_view",
    "battle_resolve_view",
    "wallet_link_view",
    "resolve_mask_slug",
]


# Slugs reserved by other top-level routes; mirror routes that already live
# under `/<page_slug>/` so we don't shadow them.
_RESERVED_MASK_SLUGS = {
    "arena", "admin", "django-admin", "login", "logout", "register",
    "mint", "dashboard", "service", "portfolio", "folder", "grid",
    "collect", "compositions", "composition", "media", "nft", "s3",
    "thoughts", "character-thoughts", "advanced-thoughts", "sim-artwork",
    "audio-sources", "ingestion", "associations-studio", "associations-compare",
    "aggregation-library", "source-library", "wall", "api",
}


def resolve_mask_slug(slug: str):
    if slug in _RESERVED_MASK_SLUGS:
        return None
    try:
        return Mask.objects.select_related("tribe", "face", "hat", "clothing", "accessory").get(slug=slug)
    except Mask.DoesNotExist:
        return None


def mask_admin_view(request):
    masks = Mask.objects.select_related("tribe", "face").order_by("tribe__name", "name")
    tribes = Tribe.objects.all()
    return render(request, "admin/mask-view.html", {"masks": masks, "tribes": tribes})


def mask_edit_view(request, mask_id=None):
    mask = get_object_or_404(Mask, id=mask_id) if mask_id else None
    if request.method == "POST":
        data = request.POST
        try:
            tribe = Tribe.objects.get(id=int(data.get("tribe")))
            face = MaskAsset.objects.get(id=int(data.get("face")), slot="face")
        except (Tribe.DoesNotExist, MaskAsset.DoesNotExist, ValueError, TypeError):
            messages.error(request, "Tribe and face are required.")
            return redirect(request.path)

        def _opt_asset(field, slot):
            raw = data.get(field) or ""
            if not raw:
                return None
            try:
                return MaskAsset.objects.get(id=int(raw), slot=slot)
            except (MaskAsset.DoesNotExist, ValueError):
                return None

        name = data.get("name", "").strip() or "Untitled"
        slug_raw = (data.get("slug") or slugify(name)).strip()
        if mask is None:
            mask = Mask(name=name, slug=slug_raw, tribe=tribe, face=face)
        mask.name = name
        mask.slug = slug_raw
        mask.tribe = tribe
        mask.face = face
        mask.hat = _opt_asset("hat", "hat")
        mask.clothing = _opt_asset("clothing", "clothing")
        mask.accessory = _opt_asset("accessory", "accessory")
        mask.background_source_name = data.get("background_source_name", "").strip()
        for stat in ("power", "speed", "wit", "charm"):
            try:
                setattr(mask, stat, max(1, min(10, int(data.get(stat, 5)))))
            except (ValueError, TypeError):
                pass
        try:
            mask.save()
        except Exception as exc:
            messages.error(request, f"Save failed: {exc}")
            return redirect(request.path)
        messages.success(request, f"Saved {mask.name}.")
        return redirect("mask-edit", mask_id=mask.id)

    return render(request, "admin/mask.html", {
        "mask": mask,
        "tribes": Tribe.objects.all(),
        "faces": MaskAsset.objects.filter(slot="face"),
        "hats": MaskAsset.objects.filter(slot="hat"),
        "clothings": MaskAsset.objects.filter(slot="clothing"),
        "accessories": MaskAsset.objects.filter(slot="accessory"),
    })


def mask_public_view(request, mask):
    """Render the public mask page. Called by the slug catch-all."""
    recent_battles = (
        MaskBattle.objects
        .filter(status__in=["resolved", "settled"])
        .filter(attacker=mask)
        .union(MaskBattle.objects.filter(status__in=["resolved", "settled"]).filter(defender=mask))
        .order_by("-resolved_at")[:10]
    )
    return render(request, "mask_public.html", {
        "mask": mask,
        "recent_battles": list(recent_battles),
    })


def mask_arena_view(request):
    masks = Mask.objects.select_related("tribe", "face").all()[:60]
    pending = MaskBattle.objects.filter(status__in=["pending", "escrowed"]).select_related("attacker", "defender")[:25]
    recent = (
        MaskBattle.objects
        .filter(status__in=["resolved", "settled"])
        .select_related("attacker", "defender", "winner")
        .order_by("-resolved_at")[:25]
    )
    return render(request, "mask-arena.html", {
        "masks": masks, "pending": pending, "recent": recent,
    })


def mask_detail_api(request, slug):
    mask = resolve_mask_slug(slug)
    if mask is None:
        raise Http404
    def _asset(a):
        return None if a is None else {"id": a.id, "name": a.name, "url": a.image.url, "tier": a.tier}
    return JsonResponse({
        "id": mask.id,
        "name": mask.name,
        "slug": mask.slug,
        "tribe": mask.tribe.slug,
        "stats": {"power": mask.power, "speed": mask.speed, "wit": mask.wit, "charm": mask.charm},
        "fun_level": mask.fun_level,
        "face_tier": mask.face_tier,
        "face": _asset(mask.face),
        "hat": _asset(mask.hat),
        "clothing": _asset(mask.clothing),
        "accessory": _asset(mask.accessory),
        "background_source_name": mask.background_source_name,
        "mint_address": mask.mint_address,
    })


@require_POST
def battle_intent_view(request):
    """Phase 1 stub: creates a pending no-wager battle for hot-seat play.

    In Phase 3 this returns an unsigned `open_battle` Solana tx.
    """
    try:
        body = json.loads(request.body or "{}")
        attacker = Mask.objects.get(id=int(body["attacker_id"]))
        defender = Mask.objects.get(id=int(body["defender_id"]))
    except (KeyError, ValueError, TypeError, Mask.DoesNotExist):
        return JsonResponse({"error": "invalid battle intent"}, status=400)
    battle = MaskBattle.objects.create(
        attacker=attacker,
        defender=defender,
        wager_lamports=int(body.get("wager_lamports", 0) or 0),
        wager_mint=body.get("wager_mint", "") or "",
        nft_at_stake=bool(body.get("nft_at_stake", False)),
        escrow_account=secrets.token_hex(16),
        status="pending",
    )
    return JsonResponse({"battle_id": battle.id, "escrow_account": battle.escrow_account, "status": battle.status})


@require_POST
def battle_resolve_view(request, battle_id):
    if not request.user.is_staff:
        return JsonResponse({"error": "forbidden"}, status=403)
    battle = resolve_battle(battle_id)
    return JsonResponse({"battle_id": battle.id, "status": battle.status, "winner_id": battle.winner_id})


SLOT_CHOICES = ("face", "hat", "clothing", "accessory")


def mask_studio_view(request):
    """Workspace for uploading/managing mask asset sprites and tribes."""
    if request.method == "POST":
        slot = request.POST.get("slot", "")
        if slot not in SLOT_CHOICES:
            messages.error(request, "Invalid slot.")
            return redirect("mask-studio")
        files = request.FILES.getlist("images")
        if not files:
            messages.error(request, "Pick at least one image to upload.")
            return redirect("mask-studio")
        try:
            tier = max(0, min(3, int(request.POST.get("tier", 0))))
        except (ValueError, TypeError):
            tier = 0
        tribe_id = request.POST.get("tribe") or ""
        tribe = None
        if tribe_id:
            try:
                tribe = Tribe.objects.get(id=int(tribe_id))
            except (Tribe.DoesNotExist, ValueError):
                tribe = None
        base_name = (request.POST.get("name") or "").strip()
        created = 0
        for idx, f in enumerate(files):
            name = base_name or f.name.rsplit(".", 1)[0]
            if len(files) > 1 and base_name:
                name = f"{base_name}-{idx + 1}"
            MaskAsset.objects.create(slot=slot, tier=tier, image=f, name=name, tribe=tribe)
            created += 1
        messages.success(request, f"Uploaded {created} {slot} asset{'s' if created != 1 else ''}.")
        return redirect("mask-studio")

    assets_by_slot = {slot: list(MaskAsset.objects.filter(slot=slot).select_related("tribe")) for slot in SLOT_CHOICES}
    backgrounds = []
    try:
        from ..models import S3Bucket
        backgrounds = list(S3Bucket.objects.all()[:200])
    except Exception:
        pass
    return render(request, "admin/mask-studio.html", {
        "assets_by_slot": assets_by_slot,
        "tribes": Tribe.objects.all(),
        "backgrounds": backgrounds,
        "slots": SLOT_CHOICES,
    })


@require_POST
def mask_asset_delete_view(request, asset_id):
    asset = get_object_or_404(MaskAsset, id=asset_id)
    if asset.masks_as_face.exists() or asset.masks_as_hat.exists() or asset.masks_as_clothing.exists() or asset.masks_as_accessory.exists():
        messages.error(request, f"Can't delete {asset.name} — it's used by an existing mask.")
    else:
        try:
            asset.image.delete(save=False)
        except Exception:
            pass
        asset.delete()
        messages.success(request, f"Deleted {asset.name}.")
    return redirect("mask-studio")


@require_POST
def tribe_create_view(request):
    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, "Tribe needs a name.")
        return redirect("mask-studio")
    slug = slugify(request.POST.get("slug") or name)[:64]
    if Tribe.objects.filter(slug=slug).exists():
        messages.error(request, f"Slug '{slug}' already taken.")
        return redirect("mask-studio")
    Tribe.objects.create(
        slug=slug, name=name,
        description=(request.POST.get("description") or "").strip(),
    )
    messages.success(request, f"Created tribe '{name}'.")
    return redirect("mask-studio")


@require_POST
def wallet_link_view(request):
    """Phase 2 stub. Frontend signs a nonce; we'd verify with nacl here."""
    return JsonResponse({"ok": False, "error": "wallet linking ships in phase 2"}, status=501)
