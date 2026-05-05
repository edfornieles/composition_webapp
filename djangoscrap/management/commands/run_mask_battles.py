"""Pair idle masks and resolve zero-wager background battles.

Usage:
    python manage.py run_mask_battles --pairs 4
    python manage.py run_mask_battles --once
"""
import random

from django.core.management.base import BaseCommand

from djangoscrap.models import Mask, MaskBattle
from djangoscrap.services.mask_battle import resolve_battle


class Command(BaseCommand):
    help = "Schedule and resolve zero-wager background battles between idle masks."

    def add_arguments(self, parser):
        parser.add_argument("--pairs", type=int, default=4, help="How many pairs to fight this tick.")
        parser.add_argument("--once", action="store_true", help="Run a single tick and exit (no-op flag, default behaviour).")

    def handle(self, *args, **options):
        pairs = max(1, options["pairs"])
        idle = list(Mask.objects.order_by("updated_at")[: pairs * 2])
        if len(idle) < 2:
            self.stdout.write(self.style.WARNING("Need at least 2 masks to battle."))
            return
        random.shuffle(idle)
        fought = 0
        while len(idle) >= 2 and fought < pairs:
            a = idle.pop()
            d = idle.pop()
            battle = MaskBattle.objects.create(
                attacker=a, defender=d,
                wager_lamports=0, wager_mint="", nft_at_stake=False,
                escrow_account="", status="pending",
            )
            resolve_battle(battle.id)
            fought += 1
            self.stdout.write(f"  battle {battle.id}: {a.name} vs {d.name}")
        self.stdout.write(self.style.SUCCESS(f"Resolved {fought} battle(s)."))
