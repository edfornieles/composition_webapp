from django.core.management.base import BaseCommand

from djangoscrap.models import Composition
from djangoscrap.nft_media import (
    MEDIA_KINDS,
    generate_composition_media_assets,
    media_asset_is_fresh,
    composition_source_signature,
)

VALID_KINDS = set(MEDIA_KINDS.keys())


class Command(BaseCommand):
    help = "Generate media assets for every composition that has a public URL."

    def add_arguments(self, parser):
        parser.add_argument(
            "--kinds",
            default="poster,preview_10s",
            help="Comma-separated list of media kinds to generate (default: poster,preview_10s).",
        )
        parser.add_argument("--force", action="store_true", help="Regenerate even when an up-to-date asset already exists.")
        parser.add_argument("--ready-only", action="store_true", help="Only process compositions marked ready for deployment.")

    def handle(self, *args, **options):
        kinds = [k.strip() for k in options["kinds"].split(",") if k.strip()]
        invalid = [k for k in kinds if k not in VALID_KINDS]
        if invalid:
            self.stderr.write(self.style.ERROR(f"Unknown kind(s): {', '.join(invalid)}. Valid: {', '.join(sorted(VALID_KINDS))}"))
            return

        force = options["force"]
        qs = Composition.objects.prefetch_related("media_assets").filter(url__isnull=False).exclude(url="")
        if options["ready_only"]:
            qs = qs.filter(ready_for_deployment=True)
        compositions = list(qs.order_by("id"))
        total = len(compositions)
        self.stdout.write(f"Found {total} composition(s) with a public URL. Generating: {', '.join(kinds)}")

        done = skipped = failed = 0
        for i, comp in enumerate(compositions, 1):
            sig = composition_source_signature(comp)
            existing = {a.kind: a for a in comp.media_assets.all()}
            needs = [k for k in kinds if force or not media_asset_is_fresh(existing.get(k), sig)]
            if not needs:
                self.stdout.write(f"  [{i}/{total}] #{comp.id} {comp.name} — skipped (all fresh)")
                skipped += 1
                continue
            self.stdout.write(f"  [{i}/{total}] #{comp.id} {comp.name} — generating {', '.join(needs)}…")
            self.stdout.flush()
            try:
                results = generate_composition_media_assets(comp, force=force, kinds=needs)
                comp_failed = []
                for kind, asset in results.items():
                    if getattr(asset, "_nft_media_skipped", False):
                        continue
                    if asset.status == "ready":
                        done += 1
                    else:
                        err = asset.error_message or "unknown error"
                        comp_failed.append(f"{kind}: {err}")
                        failed += 1
                if comp_failed:
                    self.stdout.write(self.style.ERROR(f"    ✗ {'; '.join(comp_failed)}"))
                else:
                    self.stdout.write(self.style.SUCCESS(f"    ✓ done"))
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"    ✗ exception: {exc}"))
                failed += len(needs)

        self.stdout.write(f"\nDone. Generated: {done}, Skipped: {skipped}, Failed: {failed}")
