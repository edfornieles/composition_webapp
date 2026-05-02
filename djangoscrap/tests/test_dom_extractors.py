"""Tests for per-site DOM extractors."""

from __future__ import annotations

from djangoscrap.ingestion_dom_extractors import (
    BingImagesExtractor,
    FlickrExtractor,
    GoogleImagesExtractor,
    PinterestExtractor,
    TumblrExtractor,
    YandexImagesExtractor,
    extract_generic,
    extract_urls,
    pick_extractor,
)


# --------------------------------------------------------------------------- #
#  Generic fallback                                                           #
# --------------------------------------------------------------------------- #


def test_generic_extracts_og_image_and_img_src():
    html = """
    <html><head>
      <meta property="og:image" content="https://a.com/og.jpg">
      <meta name="twitter:image" content="https://a.com/tw.jpg">
    </head><body>
      <img src="https://a.com/body.jpg">
      <img data-src="https://a.com/lazy.png">
    </body></html>
    """
    urls = extract_generic(html)
    assert "https://a.com/og.jpg" in urls
    assert "https://a.com/tw.jpg" in urls
    assert "https://a.com/body.jpg" in urls
    assert "https://a.com/lazy.png" in urls


def test_generic_dedupes():
    html = '<img src="https://a.com/x.jpg"><img src="https://a.com/x.jpg">'
    assert extract_generic(html).count("https://a.com/x.jpg") == 1


def test_generic_filters_non_image_urls():
    html = '<img src="https://a.com/script.js">'
    assert extract_generic(html) == []


def test_generic_unescapes_html_entities():
    html = '<meta property="og:image" content="https://a.com/x.jpg?a=1&amp;b=2">'
    urls = extract_generic(html)
    assert "https://a.com/x.jpg?a=1&b=2" in urls


# --------------------------------------------------------------------------- #
#  Google Images                                                              #
# --------------------------------------------------------------------------- #


def test_google_images_can_handle():
    ex = GoogleImagesExtractor()
    assert ex.can_handle("https://www.google.com/search?q=cats&tbm=isch")
    assert not ex.can_handle("https://www.google.com/search?q=cats")
    assert not ex.can_handle("https://bing.com/images/search")


def test_google_images_extract_filters_encrypted_thumbnails():
    html = """
    ["https://encrypted-tbn0.gstatic.com/thumb.jpg",80,80],
    ["https://real.example.com/photo.jpg",800,600],
    ["https://www.gstatic.com/other.jpg",120,120],
    ["https://another.example.com/photo2.png",400,300]
    """
    urls = GoogleImagesExtractor().extract(html)
    assert "https://real.example.com/photo.jpg" in urls
    assert "https://another.example.com/photo2.png" in urls
    assert not any("encrypted-tbn" in u for u in urls)
    assert not any("gstatic.com" in u for u in urls)


# --------------------------------------------------------------------------- #
#  Bing Images                                                                #
# --------------------------------------------------------------------------- #


def test_bing_images_can_handle():
    ex = BingImagesExtractor()
    assert ex.can_handle("https://www.bing.com/images/search?q=cats")
    assert not ex.can_handle("https://www.bing.com/search?q=cats")


def test_bing_images_extracts_murl_from_raw_json():
    html = '<a class="iusc" m=\'{"murl":"https://cdn.example.com/big.jpg","turl":"..."}\'></a>'
    # The literal JSON form
    html += 'data-something={"murl":"https://other.example.com/second.png"}'
    urls = BingImagesExtractor().extract(html)
    assert "https://cdn.example.com/big.jpg" in urls
    assert "https://other.example.com/second.png" in urls


def test_bing_images_extracts_murl_from_html_escaped_json():
    html = (
        'm=&quot;{&quot;murl&quot;:&quot;https://cdn.example.com/big.jpg&quot;,'
        '&quot;turl&quot;:&quot;...&quot;}&quot;'
    )
    urls = BingImagesExtractor().extract(html)
    assert "https://cdn.example.com/big.jpg" in urls


# --------------------------------------------------------------------------- #
#  Yandex Images                                                              #
# --------------------------------------------------------------------------- #


def test_yandex_images_can_handle():
    ex = YandexImagesExtractor()
    assert ex.can_handle("https://yandex.com/images/search?text=cats")
    assert ex.can_handle("https://yandex.ru/images/search?text=koshki")
    assert not ex.can_handle("https://yandex.com/")


def test_yandex_images_extracts_origUrl():
    html = (
        '{"origUrl":"https://upload.example.com/large/cat.jpg","thumb":"..."}'
        '{"origUrl":"https:\\/\\/other.example.com\\/second.png"}'
    )
    urls = YandexImagesExtractor().extract(html)
    assert "https://upload.example.com/large/cat.jpg" in urls
    assert "https://other.example.com/second.png" in urls


# --------------------------------------------------------------------------- #
#  Pinterest                                                                  #
# --------------------------------------------------------------------------- #


def test_pinterest_can_handle():
    ex = PinterestExtractor()
    assert ex.can_handle("https://www.pinterest.com/user/board/")
    assert ex.can_handle("https://pinterest.co.uk/search/pins/?q=x")
    assert not ex.can_handle("https://tumblr.com/")


def test_pinterest_extracts_pinimg_urls():
    html = """
    <img src="https://i.pinimg.com/236x/ab/cd/ef/1.jpg">
    <script>{"url":"https://i.pinimg.com/originals/gh/ij/kl/2.jpg"}</script>
    <img src="https://static.pinterest.com/logo.svg">
    """
    urls = PinterestExtractor().extract(html)
    assert "https://i.pinimg.com/236x/ab/cd/ef/1.jpg" in urls
    assert "https://i.pinimg.com/originals/gh/ij/kl/2.jpg" in urls
    # SVG and non-pinimg hosts excluded
    for u in urls:
        assert "pinimg.com" in u


# --------------------------------------------------------------------------- #
#  Tumblr                                                                     #
# --------------------------------------------------------------------------- #


def test_tumblr_can_handle():
    ex = TumblrExtractor()
    assert ex.can_handle("https://www.tumblr.com/tagged/vaporwave")
    assert ex.can_handle("https://myblog.tumblr.com/")
    assert not ex.can_handle("https://example.com/")


def test_tumblr_extracts_media_and_assets():
    html = """
    <img src="https://64.media.tumblr.com/abcd/1234.jpg">
    <a href="https://assets.tumblr.com/images/x/photo.png">link</a>
    <img src="https://not-tumblr.com/photo.jpg">
    """
    urls = TumblrExtractor().extract(html)
    assert "https://64.media.tumblr.com/abcd/1234.jpg" in urls
    assert "https://assets.tumblr.com/images/x/photo.png" in urls
    assert not any("not-tumblr.com" in u for u in urls)


# --------------------------------------------------------------------------- #
#  Flickr                                                                     #
# --------------------------------------------------------------------------- #


def test_flickr_extracts_static_urls():
    html = """
    <img src="https://live.staticflickr.com/65535/51234_abcdef_b.jpg">
    <img src="https://live.staticflickr.com/65535/51234_abcdef_m.jpg">
    """
    urls = FlickrExtractor().extract(html)
    assert "https://live.staticflickr.com/65535/51234_abcdef_b.jpg" in urls
    assert "https://live.staticflickr.com/65535/51234_abcdef_m.jpg" in urls


# --------------------------------------------------------------------------- #
#  pick_extractor / extract_urls dispatcher                                   #
# --------------------------------------------------------------------------- #


def test_pick_extractor_picks_most_specific():
    assert pick_extractor("https://www.pinterest.com/x/").name == "pinterest"
    assert (
        pick_extractor("https://yandex.com/images/search?text=x").name
        == "yandex-images"
    )
    assert pick_extractor("https://unknown.example.com/") is None


def test_extract_urls_falls_back_to_generic_for_unknown_site():
    html = '<meta property="og:image" content="https://a.com/x.jpg">'
    urls, engine = extract_urls(html, "https://unknown.example.com/")
    assert "https://a.com/x.jpg" in urls
    assert engine == "generic"


def test_extract_urls_returns_specific_engine_name():
    html = '<img src="https://i.pinimg.com/originals/a.jpg">'
    urls, engine = extract_urls(html, "https://www.pinterest.com/user/")
    assert urls == ["https://i.pinimg.com/originals/a.jpg"]
    assert engine == "pinterest"


def test_extract_urls_falls_back_when_specific_extractor_empty():
    # Pinterest URL but HTML contains only og:image — specific extractor
    # returns nothing, fallback should kick in.
    html = '<meta property="og:image" content="https://somecdn.com/hero.jpg">'
    urls, engine = extract_urls(html, "https://www.pinterest.com/user/")
    assert "https://somecdn.com/hero.jpg" in urls
    assert "fallback" in engine


def test_extract_urls_swallows_extractor_exceptions_and_falls_back():
    class _Explode(GoogleImagesExtractor):
        def extract(self, html, page_url=""):  # type: ignore[override]
            raise RuntimeError("boom")

    # Monkey-patch by swapping the first registry entry inline for this test.
    from djangoscrap import ingestion_dom_extractors as dom

    original = dom.EXTRACTORS
    dom.EXTRACTORS = (_Explode(),) + original[1:]
    try:
        html = '<meta property="og:image" content="https://a.com/x.jpg">'
        urls, engine = extract_urls(html, "https://www.google.com/search?tbm=isch")
        assert "https://a.com/x.jpg" in urls
        assert "fallback" in engine
    finally:
        dom.EXTRACTORS = original
