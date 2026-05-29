"""Mocked tests for the 4chan client. No network."""
import io
import json
from unittest.mock import patch

import pytest

from djangoscrap.imageboard_ingestion import fourchan_client as api


class _FakeResp:
    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _patch_urlopen(payload: bytes, headers: dict | None = None):
    return patch(
        "djangoscrap.imageboard_ingestion.fourchan_client.urllib.request.urlopen",
        return_value=_FakeResp(payload, headers or {}),
    )


def test_fetch_catalog_parses_json():
    body = json.dumps([{"page": 1, "threads": [{"no": 1, "replies": 5}]}]).encode()
    with _patch_urlopen(body):
        pages, _headers = api.fetch_catalog("fit")
    assert isinstance(pages, list)
    assert pages[0]["threads"][0]["no"] == 1


def test_fetch_thread_parses_posts():
    body = json.dumps({"posts": [{"no": 99, "com": "hello"}]}).encode()
    with _patch_urlopen(body):
        thread, _ = api.fetch_thread("fit", 99)
    assert thread["posts"][0]["no"] == 99


def test_iter_catalog_threads_yields_all():
    pages = [
        {"page": 1, "threads": [{"no": 1}, {"no": 2}]},
        {"page": 2, "threads": [{"no": 3}]},
    ]
    out = list(api.iter_catalog_threads(pages))
    assert [t["no"] for _, t in out] == [1, 2, 3]


def test_throttle_does_not_explode_under_repeated_calls(monkeypatch):
    # Ensure two sequential mocked calls work without raising.
    body = json.dumps({"posts": []}).encode()
    monkeypatch.setattr(api, "_MIN_INTERVAL_S", 0.0)  # don't sleep in tests
    with _patch_urlopen(body):
        api.fetch_thread("fit", 1)
        api.fetch_thread("fit", 2)


def test_404_raises_fourchan_error(monkeypatch):
    import urllib.error
    monkeypatch.setattr(api, "_MIN_INTERVAL_S", 0.0)

    def raise_404(*a, **kw):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    with patch("djangoscrap.imageboard_ingestion.fourchan_client.urllib.request.urlopen", side_effect=raise_404):
        with pytest.raises(api.FourChanError):
            api.fetch_thread("fit", 999999999)
