"""Phase 1 tests for the Masks feature."""
import django

django.setup()

from django.test import TestCase  # noqa: E402

from djangoscrap.models import Mask, MaskAsset, MaskBattle, Tribe  # noqa: E402
from djangoscrap.services.mask_battle import (  # noqa: E402
    resolve_battle, apply_fun_tier, _tier_for_fun,
)


def _make_face(tier=0, tribe=None):
    return MaskAsset.objects.create(
        slot="face", tier=tier, image=f"mask_assets/face_t{tier}.png",
        name=f"face-t{tier}-{tier}", tribe=tribe,
    )


def _make_mask(name, slug, tribe, face=None, **stats):
    return Mask.objects.create(
        name=name, slug=slug, tribe=tribe, face=face or _make_face(0, tribe),
        power=stats.get("power", 5), speed=stats.get("speed", 5),
        wit=stats.get("wit", 5), charm=stats.get("charm", 5),
    )


class FunTierTests(TestCase):
    def test_tier_for_fun(self):
        self.assertEqual(_tier_for_fun(0), 0)
        self.assertEqual(_tier_for_fun(4), 0)
        self.assertEqual(_tier_for_fun(5), 1)
        self.assertEqual(_tier_for_fun(15), 2)
        self.assertEqual(_tier_for_fun(40), 3)
        self.assertEqual(_tier_for_fun(999), 3)


class ResolveBattleTests(TestCase):
    def test_resolve_picks_stronger_winner(self):
        tribe = Tribe.objects.create(slug="t1", name="T1")
        a = _make_mask("Alpha", "alpha", tribe, power=10, speed=10, wit=10, charm=10)
        d = _make_mask("Bravo", "bravo", tribe, power=1, speed=1, wit=1, charm=1)
        battle = MaskBattle.objects.create(attacker=a, defender=d, status="pending", escrow_account="x")
        resolved = resolve_battle(battle.id)
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.winner_id, a.id)
        a.refresh_from_db(); d.refresh_from_db()
        self.assertEqual(a.fun_level, 2)
        self.assertEqual(d.fun_level, 1)

    def test_resolve_is_idempotent(self):
        tribe = Tribe.objects.create(slug="t2", name="T2")
        a = _make_mask("A", "a", tribe)
        d = _make_mask("D", "d", tribe)
        battle = MaskBattle.objects.create(attacker=a, defender=d, status="pending")
        resolve_battle(battle.id)
        a.refresh_from_db()
        fun_after_first = a.fun_level
        resolve_battle(battle.id)
        a.refresh_from_db()
        self.assertEqual(a.fun_level, fun_after_first)


class FaceTierSwapTests(TestCase):
    def test_swap_when_higher_asset_exists(self):
        tribe = Tribe.objects.create(slug="t3", name="T3")
        t0 = _make_face(0, tribe)
        t1 = _make_face(1, tribe)
        mask = _make_mask("Mask", "mask", tribe, face=t0)
        mask.fun_level = 5
        self.assertTrue(apply_fun_tier(mask))
        self.assertEqual(mask.face_id, t1.id)

    def test_no_swap_when_no_higher_asset(self):
        tribe = Tribe.objects.create(slug="t4", name="T4")
        t0 = _make_face(0, tribe)
        mask = _make_mask("Mask", "mask4", tribe, face=t0)
        mask.fun_level = 99
        self.assertFalse(apply_fun_tier(mask))
        self.assertEqual(mask.face_id, t0.id)

    def test_face_tier_property(self):
        tribe = Tribe.objects.create(slug="t5", name="T5")
        m = _make_mask("M", "m5", tribe)
        self.assertEqual(m.face_tier, 0)
        m.fun_level = 15
        self.assertEqual(m.face_tier, 2)
        m.fun_level = 41
        self.assertEqual(m.face_tier, 3)


class PolymarketResolveTests(TestCase):
    def _make_battle(self, market_id="m1", attacker_outcome="Yes"):
        tribe = Tribe.objects.create(slug=f"poly-{market_id}", name="P")
        a = _make_mask("A", f"poly-a-{market_id}", tribe)
        d = _make_mask("D", f"poly-d-{market_id}", tribe)
        return MaskBattle.objects.create(
            attacker=a, defender=d, status="pending",
            resolution_source="polymarket",
            polymarket_market_id=market_id,
            polymarket_attacker_outcome=attacker_outcome,
        )

    def test_open_market_stays_pending(self):
        from djangoscrap.services import polymarket as pm
        battle = self._make_battle()
        orig = pm.fetch_market
        pm.fetch_market = lambda mid: {
            "id": mid, "question": "?", "closed": False,
            "outcomes": ["Yes", "No"], "prices": [0.4, 0.6],
            "winning_outcome": None, "raw": {},
        }
        try:
            resolved = resolve_battle(battle.id)
            self.assertEqual(resolved.status, "pending")
            self.assertIn("last_check_error", resolved.roll_log or {})
        finally:
            pm.fetch_market = orig

    def test_closed_market_picks_winner(self):
        from djangoscrap.services import polymarket as pm
        battle = self._make_battle(market_id="m2", attacker_outcome="Yes")
        orig = pm.fetch_market
        pm.fetch_market = lambda mid: {
            "id": mid, "question": "?", "closed": True,
            "outcomes": ["Yes", "No"], "prices": [1.0, 0.0],
            "winning_outcome": "Yes", "raw": {},
        }
        try:
            resolved = resolve_battle(battle.id)
            self.assertEqual(resolved.status, "resolved")
            self.assertEqual(resolved.winner_id, battle.attacker_id)
            self.assertEqual(resolved.roll_log["source"], "polymarket")
        finally:
            pm.fetch_market = orig

    def test_closed_market_defender_wins(self):
        from djangoscrap.services import polymarket as pm
        battle = self._make_battle(market_id="m3", attacker_outcome="Yes")
        orig = pm.fetch_market
        pm.fetch_market = lambda mid: {
            "id": mid, "question": "?", "closed": True,
            "outcomes": ["Yes", "No"], "prices": [0.0, 1.0],
            "winning_outcome": "No", "raw": {},
        }
        try:
            resolved = resolve_battle(battle.id)
            self.assertEqual(resolved.winner_id, battle.defender_id)
        finally:
            pm.fetch_market = orig


class RunMaskBattlesCommandTests(TestCase):
    def test_command_resolves_pairs(self):
        from django.core.management import call_command
        tribe = Tribe.objects.create(slug="t6", name="T6")
        for i in range(4):
            _make_mask(f"M{i}", f"m6-{i}", tribe)
        call_command("run_mask_battles", "--pairs", "2")
        self.assertEqual(MaskBattle.objects.filter(status="resolved").count(), 2)
