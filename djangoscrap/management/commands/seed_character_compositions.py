from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Seed a batch of compositions for a character (e.g. goth) using curated source palettes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--character",
            default="goth",
            help="Character key from _CHARACTER_PALETTES in views.py (default: goth).",
        )
        parser.add_argument(
            "--count",
            type=int,
            default=12,
            help="How many compositions to create (1-100). Default: 12.",
        )
        parser.add_argument(
            "--host-origin",
            default="http://127.0.0.1:8000",
            help="Origin used in composition.url so /<slug>/ resolves locally.",
        )

    def handle(self, *args, **opts):
        from djangoscrap.views import _seed_character_compositions

        character = opts["character"]
        count = opts["count"]
        host_origin = opts["host_origin"]
        created, errors = _seed_character_compositions(character, count, host_origin=host_origin)
        self.stdout.write(self.style.SUCCESS(f"Created {created} '{character}' composition(s)."))
        for err in errors:
            self.stdout.write(self.style.WARNING(err))
