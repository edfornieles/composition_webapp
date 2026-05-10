"""Standalone Three.js viewer for the anon wojak 3D model.

Eventual integration: render into a tab on the character page, lip-sync to TTS,
drive pose/expression from mood/topic tags emitted by each character's thought
stream. For now: prove the mesh loads + orbits + shades correctly.
"""
from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET


@require_GET
def avatar_page(request):
    return render(request, "avatar.html")
