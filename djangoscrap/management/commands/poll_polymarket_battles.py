"""Resolve any pending Polymarket-backed battles whose markets have closed.

Usage:
    python manage.py poll_polymarket_battles
"""
from django.core.management.base import BaseCommand

from djangoscrap.models import MaskBattle
from djangoscrap.services.mask_battle import resolve_battle


class Command(BaseCommand):
    help = "Check pending Polymarket-backed battles and resolve any whose markets have closed."

    def handle(self, *args, **options):
        pending = MaskBattle.objects.filter(
            resolution_source="polymarket",
            status__in=("pending", "escrowed"),
        ).exclude(polymarket_market_id="")
        self.stdout.write(f"Polling {pending.count()} polymarket battles...")
        for battle in pending:
            before = battle.status
            updated = resolve_battle(battle.id)
            if updated.status != before:
                self.stdout.write(self.style.SUCCESS(
                    f"  battle {battle.id}: {before} → {updated.status} (winner {updated.winner_id})"
                ))
            else:
                reason = (updated.roll_log or {}).get("last_check_error", "still pending")
                self.stdout.write(f"  battle {battle.id}: {reason}")
