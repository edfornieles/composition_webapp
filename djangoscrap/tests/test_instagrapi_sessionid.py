"""
Tests for the Instagrapi adapter's dual-auth (sessionid cookie OR
username+password) provider. The sessionid path exists because Meta's
anti-bot system aggressively blacklists home IPs that attempt the
private mobile-API login; a browser session cookie bypasses that gate
entirely. These tests ensure the adapter prefers the cookie when both
are configured and fails cleanly when neither is.
"""

from __future__ import annotations

import os
from unittest import mock

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")
django.setup()


def _fresh_provider(**env):
    """Return a provider with a clean env so test state doesn't leak."""
    from djangoscrap.ingestion_adapters.instagrapi_adapter import _InstagrapiClientProvider

    with mock.patch.dict(os.environ, env, clear=True):
        prov = _InstagrapiClientProvider()
        yield prov


def test_provider_available_requires_creds_or_sessionid():
    from djangoscrap.ingestion_adapters.instagrapi_adapter import _InstagrapiClientProvider

    # No env at all → unavailable
    with mock.patch.dict(os.environ, {}, clear=True):
        prov = _InstagrapiClientProvider()
        assert not prov.available()

    # Just sessionid → available
    with mock.patch.dict(os.environ, {"INSTAGRAM_SESSIONID": "abc123"}, clear=True):
        prov = _InstagrapiClientProvider()
        assert prov.available()

    # Just user+pass → available
    with mock.patch.dict(
        os.environ,
        {"INSTAGRAM_USER": "u", "INSTAGRAM_PASS": "p"},
        clear=True,
    ):
        prov = _InstagrapiClientProvider()
        assert prov.available()

    # Only user (no pass) and no sessionid → unavailable (regression guard)
    with mock.patch.dict(os.environ, {"INSTAGRAM_USER": "u"}, clear=True):
        prov = _InstagrapiClientProvider()
        assert not prov.available()


def test_provider_prefers_sessionid_over_password(tmp_path):
    """When both modes are configured, never call password login."""
    from djangoscrap.ingestion_adapters import instagrapi_adapter as ig_mod

    fake_client = mock.MagicMock()

    class _FakeClientCls:
        def __init__(self):
            pass

        def __call__(self, *a, **kw):
            return fake_client

    with (
        mock.patch.dict(
            os.environ,
            {
                "INSTAGRAM_SESSIONID": "deadbeef",
                "INSTAGRAM_USER": "u",
                "INSTAGRAM_PASS": "p",
            },
            clear=True,
        ),
        mock.patch.object(
            ig_mod,
            "_InstagrapiClientProvider",
            wraps=ig_mod._InstagrapiClientProvider,
        ),
    ):
        prov = ig_mod._InstagrapiClientProvider(
            session_path=str(tmp_path / "no_cache.json"),
        )
        # The real ``from instagrapi import Client`` happens inside .get();
        # patch the exact attribute lookup path.
        with mock.patch("instagrapi.Client", return_value=fake_client):
            client = prov.get()

    assert client is fake_client
    fake_client.login_by_sessionid.assert_called_once_with("deadbeef")
    fake_client.login.assert_not_called()


def test_provider_falls_back_to_password_when_no_sessionid(tmp_path):
    from djangoscrap.ingestion_adapters import instagrapi_adapter as ig_mod

    fake_client = mock.MagicMock()

    with mock.patch.dict(
        os.environ,
        {"INSTAGRAM_USER": "u", "INSTAGRAM_PASS": "p"},
        clear=True,
    ):
        prov = ig_mod._InstagrapiClientProvider(
            session_path=str(tmp_path / "no_cache.json"),
        )
        with mock.patch("instagrapi.Client", return_value=fake_client):
            prov.get()

    fake_client.login.assert_called_once_with("u", "p")
    fake_client.login_by_sessionid.assert_not_called()


def test_provider_raises_when_no_credentials(tmp_path):
    from djangoscrap.ingestion_adapters import instagrapi_adapter as ig_mod

    fake_client = mock.MagicMock()

    with mock.patch.dict(os.environ, {}, clear=True):
        prov = ig_mod._InstagrapiClientProvider(
            session_path=str(tmp_path / "no_cache.json"),
        )
        with mock.patch("instagrapi.Client", return_value=fake_client):
            import pytest
            with pytest.raises(RuntimeError, match="no credentials available"):
                prov.get()

    fake_client.login.assert_not_called()
    fake_client.login_by_sessionid.assert_not_called()


def test_provider_reuses_cached_session_file(tmp_path):
    """If a session file exists, skip login entirely and trust the cache."""
    from djangoscrap.ingestion_adapters import instagrapi_adapter as ig_mod

    session_file = tmp_path / "ig_session.json"
    session_file.write_text("{\"cookies\": {}}", encoding="utf-8")

    fake_client = mock.MagicMock()

    with mock.patch.dict(
        os.environ,
        {"INSTAGRAM_SESSIONID": "ignored_when_cache_present"},
        clear=True,
    ):
        prov = ig_mod._InstagrapiClientProvider(session_path=str(session_file))
        with mock.patch("instagrapi.Client", return_value=fake_client):
            client = prov.get()

    assert client is fake_client
    fake_client.load_settings.assert_called_once_with(str(session_file))
    fake_client.login_by_sessionid.assert_not_called()
    fake_client.login.assert_not_called()


def test_classify_instagram_url_roundtrip():
    """Sanity: URL classifier still handles all the shapes archive mode cares about."""
    from djangoscrap.ingestion_adapters.instagrapi_adapter import classify_instagram_url

    assert classify_instagram_url("https://www.instagram.com/asmr__slice/") == ("profile", "asmr__slice")
    assert classify_instagram_url("https://instagram.com/asmr__slice") == ("profile", "asmr__slice")
    assert classify_instagram_url("https://www.instagram.com/p/ABC123/") == ("post", "ABC123")
    assert classify_instagram_url("https://www.instagram.com/reel/DEF456/") == ("reel", "DEF456")
    assert classify_instagram_url("https://www.instagram.com/reels/XYZ789/") == ("reel", "XYZ789")
    assert classify_instagram_url("https://www.instagram.com/tv/OLD111/") == ("reel", "OLD111")
    assert classify_instagram_url("https://example.com/notig/") is None
