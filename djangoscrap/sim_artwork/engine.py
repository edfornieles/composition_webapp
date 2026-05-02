from __future__ import annotations

import random
from dataclasses import dataclass

from django.db import transaction

from djangoscrap.models import (
    SocialAgent,
    SocialEvent,
    SocialLocation,
    SocialMemory,
    SocialRelationship,
    SocialSimWorld,
)


@dataclass
class TickResult:
    world_slug: str
    day: int
    minute: int
    moved_agents: int
    events_written: int
    render_candidates: list[dict]


def bootstrap_sigma_chi_world(slug: str = "berkeley-sigma-chi-prototype") -> SocialSimWorld:
    with transaction.atomic():
        world, _ = SocialSimWorld.objects.get_or_create(
            slug=slug,
            defaults={
                "title": "Berkeley Sigma Chi Prototype",
                "weather_state": {"condition": "clear", "temperature_c": 17},
                "rhythm_state": {"term_phase": "mid-semester", "day_type": "weekday"},
                "render_policy": {"base_tick_minutes": 5, "min_significance_for_render": 0.58},
            },
        )
        _ensure_locations(world)
        _ensure_agents(world)
        _ensure_relationships(world)
    return world


def run_world_tick(world: SocialSimWorld, step_minutes: int = 5) -> TickResult:
    step = max(1, min(30, int(step_minutes)))
    locations_by_slug = {loc.slug: loc for loc in world.locations.all()}
    moved_agents = 0
    events_written = 0
    render_candidates: list[dict] = []

    with transaction.atomic():
        world.sim_minute += step
        while world.sim_minute >= 24 * 60:
            world.sim_day += 1
            world.sim_minute -= 24 * 60
        world.save(update_fields=["sim_day", "sim_minute", "updated_at"])

        agents = list(world.agents.select_related("current_location").all())
        for agent in agents:
            start_location = agent.current_location
            target_slug = _select_location_for_time(agent, world.sim_minute)
            target = locations_by_slug.get(target_slug) or start_location
            if target and start_location != target:
                agent.current_location = target
                moved_agents += 1
            _apply_body_and_mood_drift(agent, world.sim_minute)
            agent.save(update_fields=["current_location", "body_state", "mood_state", "updated_at"])

        by_location: dict[int, list[SocialAgent]] = {}
        for agent in agents:
            if agent.current_location_id:
                by_location.setdefault(agent.current_location_id, []).append(agent)

        for location_id, present in by_location.items():
            if len(present) < 2:
                continue
            pair_significance = _location_significance(present)
            if pair_significance < 0.45:
                continue
            loc = present[0].current_location
            actors = [a.slug for a in present]
            ev = SocialEvent.objects.create(
                world=world,
                day_index=world.sim_day,
                minute_index=world.sim_minute,
                event_type="encounter",
                location=loc,
                actor_slugs=actors,
                payload={
                    "summary": f"{', '.join(a.name for a in present[:3])} share space at {loc.title}.",
                    "ambient": list(loc.ambient_tags or [])[:4],
                },
                significance=pair_significance,
            )
            events_written += 1
            for a in present:
                SocialMemory.objects.create(
                    world=world,
                    agent=a,
                    event=ev,
                    objective_summary=ev.payload.get("summary", ""),
                    subjective_summary=_subjective_memory(a, present, loc.title),
                    emotional_weight=max(0.2, round(pair_significance - random.uniform(0.0, 0.2), 2)),
                    distortion=round(random.uniform(0.03, 0.22), 2),
                    unresolved=[],
                    consolidated=False,
                )
                if pair_significance >= world.render_policy.get("min_significance_for_render", 0.58):
                    render_candidates.append(
                        {
                            "agent_slug": a.slug,
                            "location_slug": a.current_location.slug if a.current_location else "",
                            "event_id": ev.id,
                            "reason": "high-significance encounter",
                            "significance": pair_significance,
                        }
                    )

    return TickResult(world.slug, world.sim_day, world.sim_minute, moved_agents, events_written, render_candidates[:8])


def get_agent_snapshot(world_slug: str, agent_slug: str) -> dict:
    world = SocialSimWorld.objects.get(slug=world_slug)
    agent = SocialAgent.objects.select_related("current_location").get(world=world, slug=agent_slug)
    relationships = SocialRelationship.objects.filter(world=world, source=agent).select_related("target")[:8]
    return {
        "world": world.slug,
        "sim_day": world.sim_day,
        "sim_minute": world.sim_minute,
        "agent": {
            "slug": agent.slug,
            "name": agent.name,
            "location": agent.current_location.slug if agent.current_location else "",
            "body_state": agent.body_state,
            "mood_state": agent.mood_state,
            "goals_state": agent.goals_state,
            "subconscious_state": agent.subconscious_state,
            "working_self": agent.working_self,
        },
        "relationships": [
            {
                "target_slug": rel.target.slug,
                "target_name": rel.target.name,
                "metrics": rel.metrics,
                "summary": rel.summary,
                "unresolved_tensions": rel.unresolved_tensions,
            }
            for rel in relationships
        ],
    }


def _ensure_locations(world: SocialSimWorld) -> None:
    nodes = [
        ("sigma-chi-porch", "Sigma Chi Porch", "outdoor", 0, 0, ["sigma-chi-chapter-room", "southside-street"], ["porch", "voices", "night air"]),
        ("sigma-chi-chapter-room", "Chapter Room", "interior", 1, 0, ["sigma-chi-porch", "sigma-chi-kitchen", "sigma-chi-hallway"], ["composites wall", "old couch"]),
        ("sigma-chi-kitchen", "House Kitchen", "interior", 2, 0, ["sigma-chi-chapter-room", "sigma-chi-basement"], ["red cups", "snack wrappers"]),
        ("sigma-chi-basement", "Basement Party Room", "interior", 2, -1, ["sigma-chi-kitchen"], ["string lights", "sticky floor"]),
        ("sigma-chi-hallway", "Upstairs Hallway", "interior", 1, 1, ["sigma-chi-chapter-room", "luke-bedroom", "evan-bedroom"], ["creaky stairs", "framed photos"]),
        ("luke-bedroom", "Luke's Bedroom", "interior", 2, 1, ["sigma-chi-hallway"], ["desk clutter", "lifting straps", "mirror photo"]),
        ("evan-bedroom", "Evan's Bedroom", "interior", 0, 1, ["sigma-chi-hallway"], ["gaming monitor", "laundry pile"]),
        ("southside-street", "Southside Street", "outdoor", -1, 0, ["sigma-chi-porch", "sather-gate-path"], ["traffic hum", "bike lights"]),
        ("sather-gate-path", "Sather Gate Path", "campus", -2, 0, ["southside-street", "doe-library-steps"], ["students crossing", "bells"]),
        ("doe-library-steps", "Doe Library Steps", "campus", -3, 0, ["sather-gate-path", "sproul-plaza"], ["stone steps", "study chatter"]),
        ("sproul-plaza", "Sproul Plaza", "campus", -4, 0, ["doe-library-steps"], ["tables", "flyers", "megaphone echo"]),
    ]
    for slug, title, kind, x, y, neighbors, tags in nodes:
        SocialLocation.objects.get_or_create(
            world=world,
            slug=slug,
            defaults={
                "title": title,
                "location_type": kind,
                "x": x,
                "y": y,
                "adjacent_slugs": neighbors,
                "access_rules": {},
                "ambient_tags": tags,
            },
        )


def _ensure_agents(world: SocialSimWorld) -> None:
    default_location = SocialLocation.objects.get(world=world, slug="sigma-chi-chapter-room")
    roster = [
        ("luke-mercer", "Luke Mercer", "luke-bedroom"),
        ("evan-kim", "Evan Kim", "evan-bedroom"),
        ("maya-patel", "Maya Patel", "southside-street"),
        ("noah-ramirez", "Noah Ramirez", "sigma-chi-kitchen"),
        ("tyler-brooks", "Tyler Brooks", "sigma-chi-porch"),
        ("diego-martinez", "Diego Martinez", "sigma-chi-hallway"),
        ("sara-ng", "Sara Ng", "doe-library-steps"),
        ("jamie-ward", "Jamie Ward", "sproul-plaza"),
    ]
    locs = {l.slug: l for l in SocialLocation.objects.filter(world=world)}
    for slug, name, start_slug in roster:
        SocialAgent.objects.get_or_create(
            world=world,
            slug=slug,
            defaults={
                "name": name,
                "identity_packet": {"appearance": "college-age, everyday campus clothes", "voice": "guarded but expressive"},
                "body_state": {"energy": 0.68, "stress": 0.34},
                "mood_state": {"valence": 0.12, "arousal": 0.48},
                "goals_state": ["maintain belonging", "protect social image", "pursue intimacy without losing face"],
                "subconscious_state": ["status scan", "fear of exclusion"],
                "working_self": "I am trying to belong without becoming fake.",
                "routine_profile": {
                    "morning": ["sather-gate-path", "doe-library-steps"],
                    "afternoon": ["sproul-plaza", "southside-street"],
                    "night": ["sigma-chi-kitchen", "sigma-chi-chapter-room", "luke-bedroom"],
                },
                "tendency_profile": {"avoidance_when_stressed": 0.44, "social_gravity": 0.71},
                "current_location": locs.get(start_slug, default_location),
            },
        )


def _ensure_relationships(world: SocialSimWorld) -> None:
    agents = {a.slug: a for a in SocialAgent.objects.filter(world=world)}
    edges = [
        ("luke-mercer", "maya-patel", {"trust": 0.66, "attraction": 0.83, "resentment": 0.2}, ["missed calls", "jealousy"]),
        ("luke-mercer", "evan-kim", {"trust": 0.81, "status_attention": 0.61, "resentment": 0.18}, ["competition under jokes"]),
        ("luke-mercer", "tyler-brooks", {"trust": 0.31, "status_attention": 0.89, "resentment": 0.64}, ["status rivalry"]),
        ("evan-kim", "noah-ramirez", {"trust": 0.6, "dependency": 0.32}, ["party-planning stress"]),
        ("maya-patel", "sara-ng", {"trust": 0.78, "dependency": 0.29}, ["advice about Luke"]),
        ("diego-martinez", "luke-mercer", {"trust": 0.72, "status_attention": 0.8}, ["seeking approval"]),
    ]
    for src, tgt, metrics, unresolved in edges:
        s = agents.get(src)
        t = agents.get(tgt)
        if not s or not t:
            continue
        SocialRelationship.objects.get_or_create(
            world=world,
            source=s,
            target=t,
            defaults={
                "metrics": metrics,
                "unresolved_tensions": unresolved,
                "shared_history": "",
                "summary": "Relationship seeded for persistent simulation.",
            },
        )


def _select_location_for_time(agent: SocialAgent, minute: int) -> str:
    routine = agent.routine_profile or {}
    if 6 * 60 <= minute < 12 * 60:
        key = "morning"
    elif 12 * 60 <= minute < 18 * 60:
        key = "afternoon"
    else:
        key = "night"
    options = [str(x).strip() for x in routine.get(key, []) if str(x).strip()]
    if not options and agent.current_location:
        return agent.current_location.slug
    return random.choice(options) if options else "sigma-chi-chapter-room"


def _apply_body_and_mood_drift(agent: SocialAgent, minute: int) -> None:
    body = dict(agent.body_state or {})
    mood = dict(agent.mood_state or {})
    energy = float(body.get("energy", 0.6))
    stress = float(body.get("stress", 0.4))
    valence = float(mood.get("valence", 0.1))
    arousal = float(mood.get("arousal", 0.4))

    if minute >= 23 * 60 or minute < 6 * 60:
        energy -= 0.02
    else:
        energy -= 0.005
    if stress > 0.65:
        valence -= 0.01
    if agent.current_location and "party" in (agent.current_location.slug or ""):
        arousal += 0.03
        stress += 0.02
    else:
        arousal -= 0.01

    body["energy"] = round(max(0.05, min(1.0, energy)), 3)
    body["stress"] = round(max(0.0, min(1.0, stress)), 3)
    mood["valence"] = round(max(-1.0, min(1.0, valence)), 3)
    mood["arousal"] = round(max(0.0, min(1.0, arousal)), 3)
    agent.body_state = body
    agent.mood_state = mood


def _location_significance(present: list[SocialAgent]) -> float:
    stress_avg = sum(float((a.body_state or {}).get("stress", 0.4)) for a in present) / len(present)
    tension = sum(len((a.subconscious_state or [])[:2]) * 0.02 for a in present)
    return round(min(1.0, 0.35 + stress_avg * 0.5 + tension), 2)


def _subjective_memory(agent: SocialAgent, present: list[SocialAgent], location_title: str) -> str:
    others = [a.name for a in present if a.id != agent.id]
    if not others:
        return f"I was in {location_title}, trying to steady myself."
    return f"I noticed {', '.join(others[:2])} in {location_title}; I kept reading their tone for signs of threat or care."
