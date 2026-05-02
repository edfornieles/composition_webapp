"""
Tests for the Playwright scroll-harvester adapters.

Live browser-driven tests are gated behind ``RUN_BROWSER_TESTS=1`` so CI
doesn't hang. The core logic we care about — HTML→URL extraction and
adapter dispatch — is covered by fixture-based tests here.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from djangoscrap.ingestion_adapters.playwright_scroll import (
    PinterestBoardAdapter,
    TumblrTagAdapter,
    _PlaywrightScroller,
    extract_image_urls_from_html,
)


# --------------------------------------------------------------------------- #
#  extract_image_urls_from_html                                               #
# --------------------------------------------------------------------------- #


_PIN_HTML = """
<html><body>
  <div class="pin"><img src="https://i.pinimg.com/236x/ab/cd/ef/1.jpg" alt="pin"></div>
  <div class="pin"><img src="https://i.pinimg.com/236x/bb/cd/ef/2.jpg"
                         srcset="https://i.pinimg.com/474x/bb/cd/ef/2.jpg 474w,
                                 https://i.pinimg.com/736x/bb/cd/ef/2.jpg 736w"></div>
  <div class="lazy-pin"><img data-src="https://i.pinimg.com/564x/cc/cd/ef/3.jpg"></div>
  <footer><img src="https://static.pinterest.com/logo.svg"></footer>
  <img src="https://other.cdn.example.com/photo.jpg">
</body></html>
"""


def test_extract_image_urls_basic_src_and_srcset():
    urls = extract_image_urls_from_html(_PIN_HTML)
    # src attributes
    assert "https://i.pinimg.com/236x/ab/cd/ef/1.jpg" in urls
    assert "https://i.pinimg.com/236x/bb/cd/ef/2.jpg" in urls
    # srcset candidates
    assert "https://i.pinimg.com/474x/bb/cd/ef/2.jpg" in urls
    assert "https://i.pinimg.com/736x/bb/cd/ef/2.jpg" in urls
    # data-src lazy-load
    assert "https://i.pinimg.com/564x/cc/cd/ef/3.jpg" in urls
    # unrelated CDN is included (no host filter)
    assert "https://other.cdn.example.com/photo.jpg" in urls
    # SVG logo is filtered out
    assert not any(u.endswith(".svg") for u in urls)


def test_extract_image_urls_host_filter_narrows_output():
    urls = extract_image_urls_from_html(_PIN_HTML, host_filter="pinimg.com")
    for u in urls:
        assert "pinimg.com" in u
    # other.cdn.example.com is filtered out
    assert not any("example.com" in u for u in urls)


def test_extract_image_urls_dedupes():
    html = '<img src="https://a.com/x.jpg"><img src="https://a.com/x.jpg">'
    urls = extract_image_urls_from_html(html)
    assert urls.count("https://a.com/x.jpg") == 1


def test_extract_image_urls_no_matches_returns_empty():
    html = '<html><body>no images here</body></html>'
    assert extract_image_urls_from_html(html) == []


# --------------------------------------------------------------------------- #
#  Adapter can_handle                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.pinterest.com/user/board/", True),
        ("https://pinterest.com/search/pins/?q=cats", True),
        ("https://pinterest.co.uk/user/", True),
        ("https://www.tumblr.com/tagged/vaporwave", False),  # wrong adapter
        ("https://www.example.com/", False),
    ],
)
def test_pinterest_can_handle(url, expected):
    assert PinterestBoardAdapter().can_handle(url) is expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.tumblr.com/tagged/vaporwave", True),
        ("https://some-blog.tumblr.com/", True),
        ("https://www.pinterest.com/", False),
    ],
)
def test_tumblr_can_handle(url, expected):
    assert TumblrTagAdapter().can_handle(url) is expected


# --------------------------------------------------------------------------- #
#  Adapter extract (mocked scroller)                                          #
# --------------------------------------------------------------------------- #


def test_pinterest_extract_uses_scroller_output():
    scroller = MagicMock(spec=_PlaywrightScroller)
    scroller.collect.return_value = [
        "https://i.pinimg.com/originals/a.jpg",
        "https://i.pinimg.com/originals/b.jpg",
    ]
    adapter = PinterestBoardAdapter(scroller=scroller)
    result = adapter.extract(
        "https://www.pinterest.com/user/board/", max_urls=20, state={}
    )
    assert result.ok is True
    assert len(result.urls) == 2
    scroller.collect.assert_called_once()
    kwargs = scroller.collect.call_args.kwargs
    assert kwargs["max_urls"] == 20
    assert kwargs["host_filter"] == "pinimg.com"


def test_extract_empty_scroller_output_is_error():
    scroller = MagicMock(spec=_PlaywrightScroller)
    scroller.collect.return_value = []
    adapter = PinterestBoardAdapter(scroller=scroller)
    result = adapter.extract(
        "https://www.pinterest.com/user/board/", max_urls=20, state={}
    )
    assert result.ok is False
    assert "0 urls collected" in result.error
