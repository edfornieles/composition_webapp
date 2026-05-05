"""Mask battle resolution.

Off-chain stat math; on-chain settlement is left to a separate submitter
(see Phase 3 plan). Phase 1 only resolves and updates fun + face tier.
"""
import hashlib
import random

from django.db import transaction
from django.utils import timezone

from djangoscrap.models import Mask, MaskAsset, MaskBattle


FUN_TIER_THRESHOLDS = [(40, 3), (15, 2), (5, 1), (0, 0)]


def _tier_for_fun(fun_level: int) -> int:
    for threshold, tier in FUN_TIER_THRESHOLDS:
        if fun_level >= threshold:
            return tier
    return 0


def apply_fun_tier(mask: Mask) -> bool:
    """Swap face asset to match current fun tier. Returns True if changed."""
    target_tier = _tier_for_fun(mask.fun_level)
    if mask.face and mask.face.tier == target_tier:
        return False
    candidates = MaskAsset.objects.filter(slot="face", tier=target_tier)
    if mask.tribe_id:
        tribe_match = candidates.filter(tribe_id=mask.tribe_id).order_by("id")
        if tribe_match.exists():
            candidates = tribe_match
        else:
            candidates = candidates.order_by("id")
    else:
        candidates = candidates.order_by("id")
    new_face = candidates.first()
    if new_face is None or new_face.id == mask.face_id:
        return False
    mask.face = new_face
    return True


def _score(mask: Mask, rng: random.Random) -> int:
    return mask.power * 2 + mask.speed + mask.wit + mask.charm + rng.randint(0, 10)


def _resolve_stats(battle: MaskBattle):
    seed_bytes = hashlib.sha256(
        f"{battle.id}:{battle.escrow_account}:{battle.started_at.isoformat()}".encode()
    ).digest()
    rng = random.Random(seed_bytes)
    a, d = battle.attacker, battle.defender
    a_score = _score(a, rng)
    d_score = _score(d, rng)
    winner = a if a_score >= d_score else d
    return winner, seed_bytes.hex(), {
        "source": "stats",
        "attacker_score": a_score, "defender_score": d_score,
        "attacker_id": a.id, "defender_id": d.id, "winner_id": winner.id,
    }


def _resolve_polymarket(battle: MaskBattle):
    """Returns (winner, seed, log) or None if the market hasn't resolved yet."""
    from .polymarket import fetch_market, PolymarketError
    if not battle.polymarket_market_id:
        raise ValueError(f"Battle {battle.id} is polymarket-sourced but has no market id.")
    try:
        market = fetch_market(battle.polymarket_market_id)
    except PolymarketError as exc:
        return None, str(exc)
    if not market["closed"] or not market["winning_outcome"]:
        return None, "market still open"
    attacker_outcome = (battle.polymarket_attacker_outcome or "").strip()
    winning = market["winning_outcome"]
    if attacker_outcome and winning == attacker_outcome:
        winner = battle.attacker
    elif attacker_outcome:
        winner = battle.defender
    else:
        # No mapping configured: first outcome ⇒ attacker, second ⇒ defender.
        outcomes = market["outcomes"]
        if outcomes and winning == outcomes[0]:
            winner = battle.attacker
        else:
            winner = battle.defender
    log = {
        "source": "polymarket",
        "market_id": market["id"],
        "question": market["question"],
        "outcomes": market["outcomes"],
        "prices": market["prices"],
        "winning_outcome": winning,
        "attacker_outcome": attacker_outcome or (market["outcomes"][0] if market["outcomes"] else ""),
        "winner_id": winner.id,
    }
    seed = hashlib.sha256(f"polymarket:{market['id']}:{winning}".encode()).hexdigest()
    return (winner, seed, log), None


def resolve_battle(battle_id: int) -> MaskBattle:
    """Compute winner, persist roll log, bump fun, swap face tier if needed.

    Idempotent: only runs on battles in `pending` or `escrowed` status.
    Polymarket-sourced battles stay pending until the market closes.
    """
    with transaction.atomic():
        battle = (
            MaskBattle.objects
            .select_for_update()
            .select_related("attacker", "defender")
            .get(id=battle_id)
        )
        if battle.status not in ("pending", "escrowed"):
            return battle

        if battle.resolution_source == "polymarket":
            outcome, reason = _resolve_polymarket(battle)
            if outcome is None:
                # Stash the reason in roll_log for visibility but stay pending.
                battle.roll_log = {**(battle.roll_log or {}), "last_check_error": reason or ""}
                battle.save(update_fields=["roll_log"])
                return battle
            winner, seed, log = outcome
        else:
            winner, seed, log = _resolve_stats(battle)

        a, d = battle.attacker, battle.defender
        battle.winner = winner
        battle.seed = seed
        battle.roll_log = log
        battle.status = "resolved"
        battle.resolved_at = timezone.now()
        battle.save(update_fields=[
            "winner", "seed", "roll_log", "status", "resolved_at",
        ])

        for mask in (a, d):
            mask.fun_level += 2 if mask.id == winner.id else 1
            face_changed = apply_fun_tier(mask)
            update_fields = ["fun_level", "updated_at"]
            if face_changed:
                update_fields.append("face")
            mask.save(update_fields=update_fields)

    return battle
