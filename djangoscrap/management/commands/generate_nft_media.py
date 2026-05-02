from django.core.management.base import BaseCommand, CommandError

from djangoscrap.models import Composition
from djangoscrap.nft_media import MEDIA_KINDS, generate_composition_media_assets


class Command(BaseCommand):
    help = "Generate NFT poster, 10s preview, and 45s collector media for compositions."

    def add_arguments(self, parser):
        parser.add_argument("--composition-id", type=int, action="append", dest="composition_ids")
        parser.add_argument("--ready-only", action="store_true", help="Only process ready-for-deployment compositions.")
        parser.add_argument("--force", action="store_true", help="Regenerate even when generated media is fresh.")
        parser.add_argument(
            "--kinds",
            default="poster,preview_15s,collector_45s",
            help="Comma-separated media kinds to generate.",
        )
        parser.add_argument("--poster", action="store_true", help="Compatibility flag; poster is included by default.")
        parser.add_argument("--durations", default="15,45", help="Compatibility flag for 15 and 45 second videos.")
        parser.add_argument("--skip-fresh", action="store_true", help="Compatibility flag; fresh assets are skipped by default.")

    def handle(self, *args, **options):
        if options.get("kinds"):
            kinds = [kind.strip() for kind in (options["kinds"] or "").split(",") if kind.strip()]
        else:
            kinds = []
            if options.get("poster"):
                kinds.append("poster")
            durations = {value.strip() for value in (options.get("durations") or "").split(",")}
            if "15" in durations:
                kinds.append("preview_15s")
            if "45" in durations:
                kinds.append("collector_45s")
        unknown = [kind for kind in kinds if kind not in MEDIA_KINDS]
        if unknown:
            raise CommandError(f"Unknown media kind(s): {', '.join(unknown)}")

        queryset = Composition.objects.exclude(url__isnull=True).exclude(url__exact="")
        if options.get("ready_only"):
            queryset = queryset.filter(ready_for_deployment=True)
        if options.get("composition_ids"):
            queryset = queryset.filter(id__in=options["composition_ids"])
        queryset = queryset.order_by("id")

        total = queryset.count()
        self.stdout.write(f"Generating NFT media for {total} composition(s).")
        for index, composition in enumerate(queryset, start=1):
            self.stdout.write(f"[{index}/{total}] {composition.id}: {composition.name}")
            results = generate_composition_media_assets(
                composition,
                force=bool(options.get("force")),
                kinds=kinds,
            )
            for kind in kinds:
                asset = results.get(kind)
                if not asset:
                    self.stdout.write(f"  {kind}: skipped")
                    continue
                if getattr(asset, "_nft_media_skipped", False):
                    self.stdout.write(f"  {kind}: fresh, skipped -> {asset.file.name}")
                elif asset.status == "ready":
                    self.stdout.write(f"  {kind}: ready -> {asset.file.name}")
                else:
                    self.stdout.write(self.style.ERROR(f"  {kind}: {asset.status} {asset.error_message}"))
