from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from djangoscrap.models import Bucket, Composition


SEARCH_TOKENS = {"google", "bing", "yandex", "search"}
INSTA_TOKENS = {"insta", "instagram"}
PINTEREST_TOKENS = {"pinterest"}
OTHER_SOURCE_TOKENS = {"tumblr", "reddit", "flickr", "unsplash", "x", "twitter", "tiktok"}
THEME_PREFIX_HINTS = [
    "champagne",
    "wealth",
    "food",
    "beach",
    "wedding",
    "luxury",
    "animal",
    "beard",
    "nail",
    "weed",
    "sport",
]

STOPWORDS = {
    "images",
    "image",
    "pics",
    "photo",
    "photos",
    "set",
    "collection",
    "folder",
    "new",
    "final",
    "mix",
}


def _slug_token(raw: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
    return token[:36] if token else ""


def _tokenize(name: str) -> list[str]:
    raw = re.split(r"[\s_\-/]+", (name or "").strip().lower())
    return [t for t in (_slug_token(x) for x in raw) if t]


def _infer_parts(folder_name: str) -> tuple[str, str, str]:
    tokens = _tokenize(folder_name)
    raw = (folder_name or "").strip().lower()
    platform_suffix = "search"
    if any(t in SEARCH_TOKENS for t in tokens):
        platform_suffix = "search"
    elif any(t in INSTA_TOKENS for t in tokens) or "@" in raw:
        platform_suffix = "insta"
    elif any(t in PINTEREST_TOKENS for t in tokens):
        platform_suffix = "pinterest"
    else:
        found = next((t for t in tokens if t in OTHER_SOURCE_TOKENS), "")
        platform_suffix = found or "search"

    account_name = ""
    m = re.search(r"@([a-z0-9._-]+)", raw)
    if m:
        account_name = _slug_token(m.group(1))
    if not account_name and platform_suffix == "insta":
        for i, t in enumerate(tokens):
            if t in {"acc", "account"} and i + 1 < len(tokens):
                candidate = _slug_token(tokens[i + 1])
                if candidate not in {"acc", "account", "insta"}:
                    account_name = candidate
                    break
    # Existing canonical forms like "..._account-foo_insta" or "..._acc_foo_insta"
    if not account_name and platform_suffix == "insta":
        m2 = re.search(r"(?:^|[_-])account-([a-z0-9-]+)(?:$|[_-])", raw)
        if m2:
            account_name = _slug_token(m2.group(1))
    if not account_name and platform_suffix == "insta":
        m3 = re.search(r"(?:^|[_-])acc[_-]([a-z0-9-]+)(?:$|[_-])", raw)
        if m3:
            candidate = _slug_token(m3.group(1))
            if candidate not in {"acc", "account"}:
                account_name = candidate

    meaningful = [t for t in tokens if t not in STOPWORDS and t not in SEARCH_TOKENS and t not in INSTA_TOKENS and t not in PINTEREST_TOKENS]
    category = meaningful[0] if meaningful else "misc"
    # Canonical account forms should preserve a thematic category before acc/account.
    mcat = re.match(r"^([a-z0-9-]+)_(?:acc|account)[_-]", _slug_token(raw.replace("__", "_").replace(" ", "_")))
    if mcat:
        category = _slug_token(mcat.group(1)) or category
    elif "acc" in tokens:
        acc_idx = tokens.index("acc")
        if acc_idx > 0:
            category = tokens[acc_idx - 1]
    elif "account" in tokens:
        acc_idx = tokens.index("account")
        if acc_idx > 0:
            category = tokens[acc_idx - 1]
    if category in {"acc", "account"} and account_name:
        # Try themed prefix hints first (e.g. champagnelaurentperrier -> champagne).
        lowered_acc = account_name.lower().replace("-", "")
        for hint in THEME_PREFIX_HINTS:
            if lowered_acc.startswith(hint):
                category = hint
                break
        else:
            category = account_name.split("-")[0]
    if account_name and platform_suffix == "insta":
        exact = f"acc_{account_name}"
    else:
        exact_tokens = meaningful[:5] if meaningful else [category]
        exact = "-".join(exact_tokens) if exact_tokens else category
    return category, exact, platform_suffix


def _canonical_name(folder_name: str) -> str:
    category, exact, suffix = _infer_parts(folder_name)
    category = _slug_token(category) or "misc"
    exact = _slug_token(exact.replace("_", "-")) or category
    # Remove duplicate prefix repetition: food + food-hotdogs => food-hotdogs
    if exact.startswith(f"{category}-{category}-"):
        exact = exact[len(category) + 1 :]
    if exact == f"{category}-{category}":
        exact = category
    if exact == category:
        base = category
    elif exact.startswith(f"{category}-"):
        base = exact
    elif exact.startswith("acc-"):
        # category_acc_name_insta
        base = f"{category}_{exact.replace('acc-', 'acc_', 1)}"
    else:
        base = f"{category}-{exact}"
    if suffix in {"search", "insta", "pinterest"}:
        return f"{base}_{suffix}"
    return f"{base}_{_slug_token(suffix) or 'search'}"


class Command(BaseCommand):
    help = (
        "Normalize source folder naming to category_exact_suffix and "
        "optionally apply renames while updating DB references."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply renames and update DB references. Without this flag, runs in dry-run mode.",
        )

    def handle(self, *args, **options):
        root = (Path(settings.BASE_DIR).parent / "composition_sources_unprocessed").resolve()
        root.mkdir(parents=True, exist_ok=True)
        dirs = sorted([p for p in root.iterdir() if p.is_dir()])
        if not dirs:
            self.stdout.write(self.style.WARNING(f"No source folders found under {root}"))
            return

        mapping: list[tuple[Path, str]] = []
        occupied = {d.name for d in dirs}
        reserved = set(occupied)
        for d in dirs:
            target = _canonical_name(d.name)
            if not target or target == d.name:
                continue
            unique_target = target
            n = 2
            while unique_target in reserved:
                unique_target = f"{target}-{n}"
                n += 1
            reserved.add(unique_target)
            mapping.append((d, unique_target))

        if not mapping:
            self.stdout.write(self.style.SUCCESS("All source folders already match canonical format."))
            return

        self.stdout.write(self.style.WARNING(f"Root: {root}"))
        self.stdout.write(self.style.WARNING("Proposed folder rename plan:"))
        for old_path, new_name in mapping:
            self.stdout.write(f"  - {old_path.name} -> {new_name}")

        if not options["apply"]:
            self.stdout.write(
                self.style.SUCCESS("Dry run complete. Re-run with --apply to execute and update DB references.")
            )
            return

        with transaction.atomic():
            rename_map: dict[str, str] = {}
            for old_path, new_name in mapping:
                target_path = old_path.parent / new_name
                old_name = old_path.name
                old_path.rename(target_path)
                rename_map[old_name] = new_name

            for old_name, new_name in rename_map.items():
                Bucket.objects.filter(name=old_name).update(name=new_name)

            compositions = Composition.objects.all()
            updated = 0
            for comp in compositions:
                bg = [rename_map.get(x, x) for x in (comp.background_sources or [])]
                fg = [rename_map.get(x, x) for x in (comp.foreground_sources or [])]
                ov = [rename_map.get(x, x) for x in (comp.overlay_sources or [])]
                if (
                    bg != (comp.background_sources or [])
                    or fg != (comp.foreground_sources or [])
                    or ov != (comp.overlay_sources or [])
                ):
                    comp.background_sources = bg
                    comp.foreground_sources = fg
                    comp.overlay_sources = ov
                    comp.save(update_fields=["background_sources", "foreground_sources", "overlay_sources"])
                    updated += 1

        self.stdout.write(self.style.SUCCESS(f"Applied {len(mapping)} folder renames."))
        self.stdout.write(self.style.SUCCESS(f"Updated {updated} composition source references."))
