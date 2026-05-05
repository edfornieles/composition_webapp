"""Seed simple anime-inspired mask assets so Studio has things to compose with.

Usage:
    python manage.py seed_masks            # idempotent; skips existing names
    python manage.py seed_masks --reset    # wipe seeded assets first

Drawing is intentionally simple PIL primitives — meant as starter art the user
can replace by uploading real PNGs in Studio.
"""
from __future__ import annotations

import io
import math
import os

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from PIL import Image, ImageDraw

from djangoscrap.models import MaskAsset, Tribe


CANVAS = 512
SEED_TAG = "[seed]"


# ---------- drawing helpers ----------

def _new(size=CANVAS):
    return Image.new("RGBA", (size, size), (0, 0, 0, 0))


def _save(img: Image.Image) -> ContentFile:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return ContentFile(buf.getvalue())


def _eye(d: ImageDraw.ImageDraw, cx, cy, r=22, sparkle=True, color=(30, 30, 30, 255)):
    # whites
    d.ellipse((cx - r, cy - r * 1.15, cx + r, cy + r * 1.15), fill=(250, 250, 250, 255), outline=(20, 20, 20, 255), width=3)
    # iris
    d.ellipse((cx - r * 0.7, cy - r * 0.85, cx + r * 0.7, cy + r * 0.85), fill=color)
    # pupil
    d.ellipse((cx - r * 0.35, cy - r * 0.45, cx + r * 0.35, cy + r * 0.45), fill=(0, 0, 0, 255))
    if sparkle:
        d.ellipse((cx - r * 0.6, cy - r * 0.85, cx - r * 0.25, cy - r * 0.5), fill=(255, 255, 255, 255))


def _mouth_smile(d, cx, cy, w=70, h=24):
    d.arc((cx - w, cy - h, cx + w, cy + h), start=10, end=170, fill=(60, 30, 40, 255), width=6)


def _mouth_o(d, cx, cy, w=28, h=40):
    d.ellipse((cx - w, cy - h, cx + w, cy + h), fill=(120, 30, 40, 255), outline=(50, 20, 25, 255), width=4)


def _mouth_flat(d, cx, cy, w=60):
    d.line((cx - w, cy, cx + w, cy), fill=(80, 30, 40, 255), width=6)


def _mouth_grin(d, cx, cy, w=80, h=30):
    d.arc((cx - w, cy - h, cx + w, cy + h), start=0, end=180, fill=(60, 30, 40, 255), width=7)
    d.line((cx - w, cy, cx + w, cy), fill=(60, 30, 40, 255), width=4)


def draw_face(skin=(255, 224, 196, 255), hair=(60, 40, 30, 255), mood="neutral", tier=0):
    img = _new()
    d = ImageDraw.Draw(img)
    cx = cy = CANVAS // 2

    # hair back
    d.ellipse((cx - 200, cy - 230, cx + 200, cy + 130), fill=hair)

    # face oval
    d.ellipse((cx - 150, cy - 170, cx + 150, cy + 190), fill=skin, outline=(60, 40, 30, 255), width=4)

    # cheeks (more pink as tier rises)
    if tier >= 1:
        blush = (255, 150, 170, 110 + tier * 40)
        d.ellipse((cx - 120, cy + 20, cx - 60, cy + 60), fill=blush)
        d.ellipse((cx + 60, cy + 20, cx + 120, cy + 60), fill=blush)

    # eyes — bigger and shinier at higher tiers
    eye_r = 26 + tier * 6
    eye_y = cy - 20
    iris = [(30, 30, 30), (60, 110, 200), (160, 60, 180), (220, 120, 60)][min(tier, 3)] + (255,)
    _eye(d, cx - 55, eye_y, r=eye_r, color=iris, sparkle=True)
    _eye(d, cx + 55, eye_y, r=eye_r, color=iris, sparkle=True)

    # eyebrows
    d.line((cx - 85, cy - 70, cx - 25, cy - 80), fill=hair, width=6)
    d.line((cx + 25, cy - 80, cx + 85, cy - 70), fill=hair, width=6)

    # mouth varies by mood/tier
    if mood == "smile" or tier >= 1:
        _mouth_smile(d, cx, cy + 90, w=50 + tier * 10)
    elif mood == "o":
        _mouth_o(d, cx, cy + 90)
    elif mood == "grin":
        _mouth_grin(d, cx, cy + 90)
    else:
        _mouth_flat(d, cx, cy + 90)

    # hair fringe over forehead
    for i, x in enumerate(range(cx - 140, cx + 140, 28)):
        d.polygon([(x, cy - 170), (x + 14, cy - 100), (x + 28, cy - 170)], fill=hair)

    # tier-3 sparkle aura
    if tier >= 3:
        for ang in range(0, 360, 30):
            ax = cx + int(220 * math.cos(math.radians(ang)))
            ay = cy + int(220 * math.sin(math.radians(ang)))
            d.polygon([(ax, ay - 8), (ax + 5, ay), (ax, ay + 8), (ax - 5, ay)], fill=(255, 230, 120, 220))

    return img


def draw_hat(kind="beret", color=(180, 30, 50, 255)):
    img = _new()
    d = ImageDraw.Draw(img)
    cx = CANVAS // 2
    top_y = 90
    if kind == "beret":
        d.ellipse((cx - 180, top_y, cx + 180, top_y + 130), fill=color, outline=(30, 30, 30, 255), width=4)
        d.ellipse((cx + 90, top_y - 20, cx + 130, top_y + 20), fill=color, outline=(30, 30, 30, 255), width=3)
    elif kind == "bow":
        d.polygon([(cx - 110, top_y + 20), (cx - 20, top_y + 50), (cx - 110, top_y + 80)], fill=color)
        d.polygon([(cx + 110, top_y + 20), (cx + 20, top_y + 50), (cx + 110, top_y + 80)], fill=color)
        d.ellipse((cx - 25, top_y + 30, cx + 25, top_y + 75), fill=color, outline=(30, 30, 30, 255), width=3)
    elif kind == "crown":
        pts = []
        for i in range(7):
            x = cx - 140 + i * 47
            pts.append((x, top_y + 110))
            pts.append((x + 23, top_y + 40))
        pts.append((cx + 140, top_y + 110))
        d.polygon(pts, fill=color, outline=(80, 60, 0, 255))
        d.ellipse((cx - 20, top_y + 70, cx + 20, top_y + 110), fill=(220, 50, 80, 255))
    elif kind == "cap":
        d.pieslice((cx - 170, top_y - 30, cx + 170, top_y + 200), start=180, end=360, fill=color, outline=(30, 30, 30, 255), width=4)
        d.rectangle((cx - 200, top_y + 130, cx + 50, top_y + 160), fill=color, outline=(30, 30, 30, 255), width=3)
    return img


def draw_clothing(kind="hoodie", color=(70, 110, 200, 255)):
    img = _new()
    d = ImageDraw.Draw(img)
    cx = CANVAS // 2
    y = 340
    if kind == "hoodie":
        d.polygon([(cx - 220, CANVAS), (cx - 180, y), (cx + 180, y), (cx + 220, CANVAS)], fill=color, outline=(20, 20, 20, 255))
        d.ellipse((cx - 90, y - 30, cx + 90, y + 60), fill=color, outline=(20, 20, 20, 255), width=3)
    elif kind == "sailor":
        d.polygon([(cx - 220, CANVAS), (cx - 170, y), (cx + 170, y), (cx + 220, CANVAS)], fill=(245, 245, 245, 255), outline=(20, 20, 20, 255))
        d.polygon([(cx - 130, y), (cx, y + 100), (cx + 130, y), (cx + 90, y + 30), (cx, y + 60), (cx - 90, y + 30)], fill=color)
        d.line((cx - 30, y + 80, cx + 30, y + 130), fill=(220, 30, 50, 255), width=8)
    elif kind == "tee":
        d.polygon([(cx - 200, CANVAS), (cx - 170, y + 20), (cx + 170, y + 20), (cx + 200, CANVAS)], fill=color, outline=(20, 20, 20, 255))
    elif kind == "kimono":
        d.polygon([(cx - 230, CANVAS), (cx - 180, y), (cx + 180, y), (cx + 230, CANVAS)], fill=color, outline=(20, 20, 20, 255))
        d.polygon([(cx - 30, y - 5), (cx + 30, y - 5), (cx + 80, CANVAS), (cx - 80, CANVAS)], fill=(245, 245, 245, 255), outline=(20, 20, 20, 255))
        d.rectangle((cx - 110, y + 90, cx + 110, y + 130), fill=(220, 30, 50, 255), outline=(60, 10, 20, 255))
    return img


def draw_accessory(kind="star"):
    img = _new()
    d = ImageDraw.Draw(img)
    cx = CANVAS // 2 - 140
    cy = 160
    if kind == "star":
        pts = []
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            r = 40 if i % 2 == 0 else 18
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        d.polygon(pts, fill=(255, 220, 60, 255), outline=(120, 90, 0, 255))
    elif kind == "heart":
        d.ellipse((cx - 30, cy - 20, cx, cy + 20), fill=(220, 30, 80, 255))
        d.ellipse((cx, cy - 20, cx + 30, cy + 20), fill=(220, 30, 80, 255))
        d.polygon([(cx - 28, cy + 8), (cx + 28, cy + 8), (cx, cy + 50)], fill=(220, 30, 80, 255))
    elif kind == "earring":
        for x_off in (-160, 160):
            ex = CANVAS // 2 + x_off
            ey = CANVAS // 2 + 40
            d.line((ex, ey, ex, ey + 30), fill=(180, 160, 0, 255), width=4)
            d.ellipse((ex - 12, ey + 22, ex + 12, ey + 46), fill=(255, 215, 0, 255), outline=(120, 90, 0, 255), width=2)
    elif kind == "halo":
        d.ellipse((CANVAS // 2 - 110, 30, CANVAS // 2 + 110, 90), outline=(255, 230, 120, 255), width=10)
    return img


# ---------- seed plan ----------

FACES = [
    ("Sora", 0, "neutral", (255, 224, 196), (60, 40, 30)),
    ("Mei",  0, "smile",   (255, 220, 200), (200, 70, 130)),
    ("Kai",  0, "flat",    (240, 200, 170), (30, 30, 60)),
    ("Yuki", 0, "smile",   (255, 230, 220), (220, 220, 240)),
    ("Sora-fun",  1, "smile", (255, 224, 196), (60, 40, 30)),
    ("Mei-fun",   1, "grin",  (255, 220, 200), (200, 70, 130)),
    ("Kai-blissed", 2, "smile", (240, 200, 170), (30, 30, 60)),
    ("Yuki-radiant", 3, "grin", (255, 230, 220), (220, 220, 240)),
]

HATS = [
    ("Red Beret",   "beret", (200, 40, 60, 255)),
    ("Pink Bow",    "bow",   (240, 120, 170, 255)),
    ("Gold Crown",  "crown", (240, 200, 60, 255)),
    ("Blue Cap",    "cap",   (60, 110, 200, 255)),
]

CLOTHING = [
    ("Sky Hoodie",   "hoodie", (90, 150, 230, 255)),
    ("Sailor Top",   "sailor", (40, 80, 160, 255)),
    ("Black Tee",    "tee",    (30, 30, 30, 255)),
    ("Pink Kimono",  "kimono", (230, 130, 170, 255)),
]

ACCESSORIES = [
    ("Gold Star", "star"),
    ("Pink Heart", "heart"),
    ("Gold Earrings", "earring"),
    ("Halo", "halo"),
]

TRIBES = [
    ("Neon Soft", "neon-soft", "Pastel-coded dreamers — wits and charm over raw power."),
    ("Iron Bloom", "iron-bloom", "Hardcore brawlers with a flower hidden in the gauntlet."),
]


class Command(BaseCommand):
    help = "Generate basic anime-inspired mask assets and tribes."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing seeded assets first.")

    def handle(self, *args, **opts):
        if opts["reset"]:
            removed = MaskAsset.objects.filter(name__startswith=SEED_TAG).count()
            for a in MaskAsset.objects.filter(name__startswith=SEED_TAG):
                try:
                    a.image.delete(save=False)
                except Exception:
                    pass
                a.delete()
            self.stdout.write(self.style.WARNING(f"Removed {removed} seeded assets."))

        # tribes
        tribes = {}
        for name, slug, desc in TRIBES:
            tribe, created = Tribe.objects.get_or_create(slug=slug, defaults={"name": name, "description": desc})
            tribes[slug] = tribe
            if created:
                self.stdout.write(f"+ tribe {name}")
        tribe_cycle = list(tribes.values())

        # faces
        for i, (label, tier, mood, skin, hair) in enumerate(FACES):
            full_name = f"{SEED_TAG} face/{label}"
            if MaskAsset.objects.filter(name=full_name).exists():
                continue
            img = draw_face(skin=tuple(list(skin) + [255]), hair=tuple(list(hair) + [255]), mood=mood, tier=tier)
            asset = MaskAsset(slot="face", tier=tier, name=full_name, tribe=tribe_cycle[i % len(tribe_cycle)])
            asset.image.save(f"seed_face_{label.lower()}.png", _save(img), save=True)
            self.stdout.write(f"+ face {label} (tier {tier})")

        for label, kind, color in HATS:
            full = f"{SEED_TAG} hat/{label}"
            if MaskAsset.objects.filter(name=full).exists():
                continue
            asset = MaskAsset(slot="hat", tier=0, name=full)
            asset.image.save(f"seed_hat_{kind}.png", _save(draw_hat(kind, color)), save=True)
            self.stdout.write(f"+ hat {label}")

        for label, kind, color in CLOTHING:
            full = f"{SEED_TAG} clothing/{label}"
            if MaskAsset.objects.filter(name=full).exists():
                continue
            asset = MaskAsset(slot="clothing", tier=0, name=full)
            asset.image.save(f"seed_clothing_{kind}.png", _save(draw_clothing(kind, color)), save=True)
            self.stdout.write(f"+ clothing {label}")

        for label, kind in ACCESSORIES:
            full = f"{SEED_TAG} accessory/{label}"
            if MaskAsset.objects.filter(name=full).exists():
                continue
            asset = MaskAsset(slot="accessory", tier=0, name=full)
            asset.image.save(f"seed_accessory_{kind}.png", _save(draw_accessory(kind)), save=True)
            self.stdout.write(f"+ accessory {label}")

        self.stdout.write(self.style.SUCCESS("Seed complete. Open Mask Studio to see them."))
