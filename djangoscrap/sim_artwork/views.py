from __future__ import annotations

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from djangoscrap.models import SocialSimWorld
from djangoscrap.sim_artwork.engine import bootstrap_sigma_chi_world, get_agent_snapshot, run_world_tick


@require_POST
@csrf_exempt
def social_sim_bootstrap(request):
    slug = (request.GET.get("slug") or "berkeley-sigma-chi-prototype").strip()
    world = bootstrap_sigma_chi_world(slug=slug)
    return JsonResponse(
        {
            "world_slug": world.slug,
            "title": world.title,
            "locations": world.locations.count(),
            "agents": world.agents.count(),
            "relationships": world.relationships.count(),
            "sim_day": world.sim_day,
            "sim_minute": world.sim_minute,
        }
    )


@require_POST
@csrf_exempt
def social_sim_tick(request, slug):
    try:
        world = SocialSimWorld.objects.get(slug=slug)
    except SocialSimWorld.DoesNotExist:
        return JsonResponse({"error": "world not found"}, status=404)

    step = 5
    if request.body:
        try:
            body = json.loads(request.body.decode("utf-8"))
            step = int(body.get("step_minutes") or 5)
        except Exception:
            step = 5

    result = run_world_tick(world, step_minutes=step)
    return JsonResponse(
        {
            "world_slug": result.world_slug,
            "sim_day": result.day,
            "sim_minute": result.minute,
            "moved_agents": result.moved_agents,
            "events_written": result.events_written,
            "render_candidates": result.render_candidates,
        }
    )


@require_GET
def social_sim_agent_snapshot(request, slug, agent_slug):
    try:
        snapshot = get_agent_snapshot(slug, agent_slug)
    except SocialSimWorld.DoesNotExist:
        return JsonResponse({"error": "world not found"}, status=404)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=404)
    return JsonResponse(snapshot)
