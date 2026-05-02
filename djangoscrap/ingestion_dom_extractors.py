"""
Per-site DOM extractors.

Separate from adapters (which decide *how to fetch*). These are pure
HTML → URLs helpers, used by both the legacy scraper and the Playwright
scroll-harvester to pull the *right* image URLs out of a given page
instead of picking up every ``<img>`` on the document.

Why per-site?
=============
Search-result SERPs and content platforms embed thumbnails, avatars,
and logos in the same HTML as the content we want. A naïve
``<img src>`` sweep brings back 10× more junk than signal. Per-site
extractors know which attribute and which container hold the real
image (e.g. Pinterest stores the high-res URL in ``src`` inside
``div[data-test-id="pin"]``; Google hides the real image URLs inside
inline JSON blobs).

Extractor protocol
==================
Each extractor implements :class:`DomExtractor`:

    name: str
    can_handle(url: str) -> bool
    extract(html: str, page_url: str) -> list[str]

Registry ordering is: most specific first. The first ``can_handle``
match wins; unknown sites fall back to :func:`extract_generic` which
does the classic ``og:image`` / ``<img src>`` sweep used today.

Testing
=======
Every extractor is tested against a fixture HTML blob (representative
of what the live SERP returns, minus auth-gated bits). No live network.
"""

from __future__ import annotations

import html as _html
import json as _json
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


_IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif|avif)(?:$|\?)", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


# --------------------------------------------------------------------------- #
#  Base / helpers                                                             #
# --------------------------------------------------------------------------- #


@dataclass
class DomExtractor:
    name: str

    def can_handle(self, url: str) -> bool:
        raise NotImplementedError

    def extract(self, html: str, page_url: str) -> list[str]:
        raise NotImplementedError


def _unique(urls):
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if not u or u in seen:
            continue
        if not _IMG_EXT_RE.search(u):
            continue
        seen.add(u)
        out.append(u)
    return out


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _host_matches(url: str, suffixes: tuple[str, ...]) -> bool:
    h = _host(url)
    return any(h == s or h.endswith("." + s) for s in suffixes)


# --------------------------------------------------------------------------- #
#  Generic fallback                                                           #
# --------------------------------------------------------------------------- #


def extract_generic(html: str, page_url: str = "") -> list[str]:
    """Fallback sweep: ``og:image`` / ``twitter:image`` + bare ``<img src>``.

    Intentionally permissive — used when no site-specific extractor
    matches. The caller is expected to run the results through the URL
    rewriter so thumbnails get upgraded to full-res.
    """
    found: list[str] = []
    # OpenGraph / Twitter card hero images (highest quality on most sites).
    for m in re.finditer(
        r'<meta[^>]+(?:property|name)="(?:og:image|og:image:secure_url|twitter:image)"[^>]+content="([^"]+)"',
        html,
        re.IGNORECASE,
    ):
        found.append(_html.unescape(m.group(1)))
    for m in re.finditer(
        r'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="(?:og:image|og:image:secure_url|twitter:image)"',
        html,
        re.IGNORECASE,
    ):
        found.append(_html.unescape(m.group(1)))
    # Bare <img src> + common lazy-load attributes.
    for m in re.finditer(
        r'<img\b[^>]*\b(?:src|data-src|data-original|data-lazy)="([^"]+)"',
        html,
        re.IGNORECASE,
    ):
        found.append(_html.unescape(m.group(1)))
    return _unique(found)


# --------------------------------------------------------------------------- #
#  Google Images SERP                                                         #
# --------------------------------------------------------------------------- #


class GoogleImagesExtractor(DomExtractor):
    name = "google-images"
    _HOSTS = ("google.com", "google.co.uk", "google.ca", "google.de")

    def __init__(self) -> None:
        super().__init__(name=self.name)

    def can_handle(self, url: str) -> bool:
        return _host_matches(url, self._HOSTS) and "tbm=isch" in (url or "")

    def extract(self, html: str, page_url: str = "") -> list[str]:
        urls: list[str] = []
        # Google hides the real image URLs inside inline JSON arrays that
        # look like ``["https://….jpg", 800, 600]``. Walk every URL we can
        # find in the raw text and keep the ones with image extensions.
        for u in _URL_RE.findall(html):
            clean = u.rstrip("\\")  # strings in the JSON are quoted, escape crud
            # Drop Google's own static hosting.
            h = _host(clean)
            if h.startswith("encrypted-tbn") or "gstatic.com" in h:
                continue
            urls.append(clean)
        return _unique(urls)


# --------------------------------------------------------------------------- #
#  Bing Images SERP                                                           #
# --------------------------------------------------------------------------- #


class BingImagesExtractor(DomExtractor):
    name = "bing-images"
    _HOSTS = ("bing.com",)

    def __init__(self) -> None:
        super().__init__(name=self.name)

    def can_handle(self, url: str) -> bool:
        return _host_matches(url, self._HOSTS) and "images/search" in (url or "")

    def extract(self, html: str, page_url: str = "") -> list[str]:
        # Bing embeds ``"murl":"<URL>"`` inside each thumbnail card's JSON
        # payload. Depending on how the HTML was served, the JSON may be
        # raw (``"murl":"..."``) or HTML-escaped (``&quot;murl&quot;:&quot;...&quot;``).
        # We match both variants directly rather than try to re-decode the
        # whole JSON blob, since partial HTML-escaping makes JSON parsing
        # fragile.
        urls: list[str] = []
        for m in re.finditer(r'"murl":"([^"]+?)"', html):
            urls.append(m.group(1).replace("\\/", "/"))
        for m in re.finditer(
            r'&quot;murl&quot;:&quot;([^&]+?)&quot;', html
        ):
            urls.append(_html.unescape(m.group(1)))
        return _unique(urls)


# --------------------------------------------------------------------------- #
#  Yandex Images SERP                                                         #
# --------------------------------------------------------------------------- #


class YandexImagesExtractor(DomExtractor):
    name = "yandex-images"
    _HOSTS = ("yandex.com", "yandex.ru")

    def __init__(self) -> None:
        super().__init__(name=self.name)

    def can_handle(self, url: str) -> bool:
        return _host_matches(url, self._HOSTS) and "images/" in (url or "")

    def extract(self, html: str, page_url: str = "") -> list[str]:
        urls: list[str] = []
        # Yandex stores the full image URL in a JSON blob `"origUrl":"…"`
        for m in re.finditer(r'"origUrl":"([^"]+)"', html):
            urls.append(m.group(1).replace("\\/", "/"))
        # Secondary: encoded inside data-bem JSON on each card.
        for m in re.finditer(r'&quot;origUrl&quot;:&quot;([^"]+?)&quot;', html):
            urls.append(_html.unescape(m.group(1)))
        return _unique(urls)


# --------------------------------------------------------------------------- #
#  Pinterest boards / search                                                  #
# --------------------------------------------------------------------------- #


class PinterestExtractor(DomExtractor):
    name = "pinterest"
    _HOSTS = ("pinterest.com", "pinterest.co.uk", "pinterest.ca")

    def __init__(self) -> None:
        super().__init__(name=self.name)

    def can_handle(self, url: str) -> bool:
        return _host_matches(url, self._HOSTS)

    def extract(self, html: str, page_url: str = "") -> list[str]:
        urls: list[str] = []
        # Pinterest's board HTML exposes images via <img src> AND via JSON
        # islands in <script id="__PWS_DATA__"> — both patterns captured.
        for m in re.finditer(r'<img\b[^>]+src="(https://i\.pinimg\.com/[^"]+)"', html):
            urls.append(m.group(1))
        for m in re.finditer(r'"url":"(https://i\.pinimg\.com/[^"]+)"', html):
            urls.append(m.group(1).replace("\\/", "/"))
        return _unique(urls)


# --------------------------------------------------------------------------- #
#  Tumblr tag / blog pages                                                    #
# --------------------------------------------------------------------------- #


class TumblrExtractor(DomExtractor):
    name = "tumblr"
    _HOSTS = ("tumblr.com",)

    def __init__(self) -> None:
        super().__init__(name=self.name)

    def can_handle(self, url: str) -> bool:
        return _host_matches(url, self._HOSTS)

    def extract(self, html: str, page_url: str = "") -> list[str]:
        urls: list[str] = []
        for m in re.finditer(
            r'(https://\d+\.media\.tumblr\.com/[^\s"\'<>]+)', html
        ):
            urls.append(m.group(1))
        # Also ``assets.tumblr.com`` post images.
        for m in re.finditer(
            r'(https://assets\.tumblr\.com/[^\s"\'<>]+)', html
        ):
            urls.append(m.group(1))
        return _unique(urls)


# --------------------------------------------------------------------------- #
#  Flickr photo pages                                                         #
# --------------------------------------------------------------------------- #


class FlickrExtractor(DomExtractor):
    name = "flickr"
    _HOSTS = ("flickr.com",)

    def __init__(self) -> None:
        super().__init__(name=self.name)

    def can_handle(self, url: str) -> bool:
        return _host_matches(url, self._HOSTS)

    def extract(self, html: str, page_url: str = "") -> list[str]:
        urls: list[str] = []
        # Flickr exposes multiple sizes; pick the `_b.jpg` (large) if present.
        for m in re.finditer(
            r'(https://live\.staticflickr\.com/\d+/\d+_[a-f0-9]+(?:_[a-z])?\.jpg)',
            html,
        ):
            urls.append(m.group(1))
        return _unique(urls)


# --------------------------------------------------------------------------- #
#  Registry                                                                   #
# --------------------------------------------------------------------------- #


EXTRACTORS: tuple[DomExtractor, ...] = (
    GoogleImagesExtractor(),
    BingImagesExtractor(),
    YandexImagesExtractor(),
    PinterestExtractor(),
    TumblrExtractor(),
    FlickrExtractor(),
)


def pick_extractor(url: str) -> Optional[DomExtractor]:
    for ex in EXTRACTORS:
        try:
            if ex.can_handle(url):
                return ex
        except Exception:
            continue
    return None


def extract_urls(html: str, page_url: str = "") -> tuple[list[str], str]:
    """Pick the right extractor for ``page_url`` and run it.

    Returns ``(urls, extractor_name)``. ``extractor_name`` is "generic"
    when no site-specific match applied, for easy observability.
    """
    ex = pick_extractor(page_url)
    if ex is None:
        return extract_generic(html, page_url), "generic"
    try:
        urls = ex.extract(html, page_url)
    except Exception:
        return extract_generic(html, page_url), f"{ex.name}+fallback"
    if not urls:
        # If the specific extractor found nothing, fall back to the generic
        # sweep rather than return empty — some pages legitimately match a
        # host but the concrete pattern didn't hit (e.g. a Pinterest profile
        # page before the JS has hydrated).
        fallback = extract_generic(html, page_url)
        if fallback:
            return fallback, f"{ex.name}+fallback"
    return urls, ex.name
