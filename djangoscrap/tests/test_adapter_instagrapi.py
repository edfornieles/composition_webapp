"""
Tests for InstagrapiAdapter.

The instagrapi library is never imported or invoked — the adapter is
tested with a mocked provider + mock Media objects, so these tests run
offline + without credentials.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from djangoscrap.ingestion_adapters.instagrapi_adapter import (
    InstagrapiAdapter,
    _urls_from_media,
    classify_instagram_url,
)


# --------------------------------------------------------------------------- #
#  URL classification                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.instagram.com/username/", ("profile", "username")),
        ("https://instagram.com/username", ("profile", "username")),
        ("https://www.instagram.com/p/CxYzAbC1234/", ("post", "CxYzAbC1234")),
        ("https://www.instagram.com/reel/DxYzAbC1234/", ("reel", "DxYzAbC1234")),
        ("https://www.instagram.com/reels/DxYzAbC1234/", ("reel", "DxYzAbC1234")),
        ("https://www.instagram.com/tv/CxYzAbC1234/", ("reel", "CxYzAbC1234")),
        ("https://www.youtube.com/watch?v=abc", None),
        # /accounts/login/ has 2 path segments → not a recognized content URL
        ("https://www.instagram.com/accounts/login/", None),
        ("", None),
    ],
)
def test_classify_instagram_url(url, expected):
    assert classify_instagram_url(url) == expected


# --------------------------------------------------------------------------- #
#  _urls_from_media                                                           #
# --------------------------------------------------------------------------- #


def _mock_media(*, video_url=None, thumbnail_url=None, resources=None):
    m = MagicMock()
    m.video_url = video_url
    m.thumbnail_url = thumbnail_url
    m.resources = resources or []
    return m


def test_urls_from_media_single_image():
    media = _mock_media(thumbnail_url="https://scontent.cdninstagram.com/a.jpg")
    urls = _urls_from_media(media)
    assert urls == ["https://scontent.cdninstagram.com/a.jpg"]


def test_urls_from_media_video_plus_thumbnail():
    media = _mock_media(
        video_url="https://scontent.cdninstagram.com/v.mp4",
        thumbnail_url="https://scontent.cdninstagram.com/v.jpg",
    )
    urls = _urls_from_media(media)
    assert "https://scontent.cdninstagram.com/v.mp4" in urls
    assert "https://scontent.cdninstagram.com/v.jpg" in urls


def test_urls_from_media_carousel_unrolls():
    res1 = _mock_media(thumbnail_url="https://scontent.cdninstagram.com/1.jpg")
    res2 = _mock_media(thumbnail_url="https://scontent.cdninstagram.com/2.jpg")
    carousel = _mock_media(resources=[res1, res2])
    urls = _urls_from_media(carousel)
    assert "https://scontent.cdninstagram.com/1.jpg" in urls
    assert "https://scontent.cdninstagram.com/2.jpg" in urls


def test_urls_from_media_dedupes():
    res1 = _mock_media(thumbnail_url="https://scontent.cdninstagram.com/1.jpg")
    res2 = _mock_media(thumbnail_url="https://scontent.cdninstagram.com/1.jpg")  # same
    carousel = _mock_media(resources=[res1, res2])
    urls = _urls_from_media(carousel)
    assert urls.count("https://scontent.cdninstagram.com/1.jpg") == 1


# --------------------------------------------------------------------------- #
#  Adapter can_handle + extract                                               #
# --------------------------------------------------------------------------- #


class _FakeProvider:
    def __init__(self, *, available=True, client=None):
        self._available = available
        self._client = client or MagicMock()

    def available(self) -> bool:
        return self._available

    def get(self):
        return self._client


def test_can_handle_requires_credentials():
    adapter = InstagrapiAdapter(provider=_FakeProvider(available=False))
    assert adapter.can_handle("https://www.instagram.com/foo/") is False


def test_can_handle_accepts_recognized_url_when_available():
    adapter = InstagrapiAdapter(provider=_FakeProvider(available=True))
    assert adapter.can_handle("https://www.instagram.com/foo/") is True
    assert adapter.can_handle("https://example.com/") is False


def test_extract_post_returns_carousel_urls():
    client = MagicMock()
    client.media_pk_from_code.return_value = 42
    media = _mock_media(
        thumbnail_url="https://scontent.cdninstagram.com/cover.jpg",
        resources=[
            _mock_media(thumbnail_url="https://scontent.cdninstagram.com/a.jpg"),
            _mock_media(video_url="https://scontent.cdninstagram.com/b.mp4",
                         thumbnail_url="https://scontent.cdninstagram.com/b.jpg"),
        ],
    )
    client.media_info.return_value = media
    adapter = InstagrapiAdapter(provider=_FakeProvider(client=client))

    result = adapter.extract(
        "https://www.instagram.com/p/CxYzAbC1234/", max_urls=10, state={}
    )
    assert result.ok is True
    assert len(result.urls) == 4
    assert "https://scontent.cdninstagram.com/a.jpg" in result.urls
    assert "https://scontent.cdninstagram.com/b.mp4" in result.urls
    assert result.diagnostics["kind"] == "post"


def test_extract_profile_updates_last_post_pk():
    client = MagicMock()
    client.user_id_from_username.return_value = 123456
    m1 = MagicMock()
    m1.pk = 500
    m1.video_url = None
    m1.thumbnail_url = "https://scontent.cdninstagram.com/500.jpg"
    m1.resources = []
    m2 = MagicMock()
    m2.pk = 450
    m2.video_url = None
    m2.thumbnail_url = "https://scontent.cdninstagram.com/450.jpg"
    m2.resources = []
    client.user_medias.return_value = [m1, m2]
    adapter = InstagrapiAdapter(provider=_FakeProvider(client=client))

    result = adapter.extract(
        "https://www.instagram.com/user/", max_urls=10, state={}
    )
    assert result.ok is True
    assert result.next_state["last_post_pk"] == 500
    assert len(result.urls) == 2


def test_extract_profile_honors_last_post_pk_cursor():
    """With last_post_pk=500, only posts with pk>500 should be emitted."""
    client = MagicMock()
    client.user_id_from_username.return_value = 123456
    m_old = MagicMock()
    m_old.pk = 400
    m_old.video_url = None
    m_old.thumbnail_url = "https://scontent.cdninstagram.com/400.jpg"
    m_old.resources = []
    m_new = MagicMock()
    m_new.pk = 600
    m_new.video_url = None
    m_new.thumbnail_url = "https://scontent.cdninstagram.com/600.jpg"
    m_new.resources = []
    client.user_medias.return_value = [m_new, m_old]
    adapter = InstagrapiAdapter(provider=_FakeProvider(client=client))

    result = adapter.extract(
        "https://www.instagram.com/user/", max_urls=10, state={"last_post_pk": 500}
    )
    assert result.ok is True
    assert result.urls == ["https://scontent.cdninstagram.com/600.jpg"]
    assert result.next_state["last_post_pk"] == 600


def test_extract_client_exception_returns_error_result():
    client = MagicMock()
    client.media_pk_from_code.side_effect = RuntimeError("rate limit")
    adapter = InstagrapiAdapter(provider=_FakeProvider(client=client))
    result = adapter.extract(
        "https://www.instagram.com/p/CxYzAbC1234/", max_urls=10, state={}
    )
    assert result.ok is False
    assert "rate limit" in result.error
