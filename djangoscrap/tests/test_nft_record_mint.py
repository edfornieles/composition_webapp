"""Tests for NFT record-mint authorization (signed capability from prepare)."""

from __future__ import annotations

import json
import os

import django
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")
django.setup()


@pytest.mark.django_db
def test_record_mint_requires_signed_token():
    from djangoscrap.models import Composition, CompositionNFT

    comp = Composition.objects.create(
        name="test-comp",
        type="classic",
        background_video=SimpleUploadedFile("bg.mp4", b"x", content_type="video/mp4"),
        foreground_video=SimpleUploadedFile("fg.mp4", b"y", content_type="video/mp4"),
        background_sources=[],
        foreground_sources=[],
        overlay_sources=[],
        ready_for_deployment=True,
        url="https://example.com/work",
    )
    nft = CompositionNFT.objects.create(
        composition=comp,
        version_number=1,
        status="ready",
        chain="ethereum",
        chain_id=1,
    )
    client = Client()
    url = reverse("composition_record_nft_mint", kwargs={"composition_id": comp.id})
    body = {
        "nft_id": nft.id,
        "owner_wallet": "0xabc",
        "tx_hash": "0xdead",
    }
    resp = client.post(
        url,
        data=json.dumps(body),
        content_type="application/json",
        HTTP_HOST="localhost",
    )
    assert resp.status_code == 403
    data = resp.json()
    assert data.get("ok") is False


@pytest.mark.django_db
def test_record_mint_accepts_prepare_issued_token():
    from djangoscrap.models import Composition, CompositionNFT
    from djangoscrap.views import _make_record_mint_token

    comp = Composition.objects.create(
        name="test-comp-2",
        type="classic",
        background_video=SimpleUploadedFile("bg.mp4", b"x", content_type="video/mp4"),
        foreground_video=SimpleUploadedFile("fg.mp4", b"y", content_type="video/mp4"),
        background_sources=[],
        foreground_sources=[],
        overlay_sources=[],
        ready_for_deployment=True,
        url="https://example.com/work",
    )
    nft = CompositionNFT.objects.create(
        composition=comp,
        version_number=1,
        status="ready",
        chain="ethereum",
        chain_id=1,
    )
    token = _make_record_mint_token(nft_id=int(nft.id), composition_id=int(comp.id))
    client = Client()
    url = reverse("composition_record_nft_mint", kwargs={"composition_id": comp.id})
    body = {
        "nft_id": nft.id,
        "record_mint_token": token,
        "owner_wallet": "0xowner",
        "tx_hash": "0xtxhash",
        "token_id": "1",
    }
    resp = client.post(
        url,
        data=json.dumps(body),
        content_type="application/json",
        HTTP_HOST="localhost",
    )
    assert resp.status_code == 200
    assert resp.json().get("ok") is True
    nft.refresh_from_db()
    assert nft.status == "minted"
    assert nft.owner_wallet == "0xowner"
    assert nft.tx_hash == "0xtxhash"


@pytest.mark.django_db
def test_record_mint_rejects_token_for_wrong_nft():
    from djangoscrap.models import Composition, CompositionNFT
    from djangoscrap.views import _make_record_mint_token

    comp = Composition.objects.create(
        name="test-comp-3",
        type="classic",
        background_video=SimpleUploadedFile("bg.mp4", b"x", content_type="video/mp4"),
        foreground_video=SimpleUploadedFile("fg.mp4", b"y", content_type="video/mp4"),
        background_sources=[],
        foreground_sources=[],
        overlay_sources=[],
        ready_for_deployment=True,
        url="https://example.com/work",
    )
    nft_a = CompositionNFT.objects.create(
        composition=comp,
        version_number=1,
        status="ready",
        chain="ethereum",
        chain_id=1,
    )
    nft_b = CompositionNFT.objects.create(
        composition=comp,
        version_number=2,
        status="ready",
        chain="ethereum",
        chain_id=1,
    )
    token_for_b = _make_record_mint_token(nft_id=int(nft_b.id), composition_id=int(comp.id))
    client = Client()
    url = reverse("composition_record_nft_mint", kwargs={"composition_id": comp.id})
    body = {
        "nft_id": nft_a.id,
        "record_mint_token": token_for_b,
        "owner_wallet": "0xabc",
        "tx_hash": "0xdead",
    }
    resp = client.post(
        url,
        data=json.dumps(body),
        content_type="application/json",
        HTTP_HOST="localhost",
    )
    assert resp.status_code == 403
