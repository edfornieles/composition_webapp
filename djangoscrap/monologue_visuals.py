"""
Harvest candidate image URLs from monologue source pages (og:image / twitter:image).
Respect robots.txt and rate limits; many sites block hotlinking — visuals are best-effort.
"""

from __future__ import annotations

import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from django.utils import timezone

from .monologue_compat import monologue_visual_columns_ready

USER_AGENT = (
    "MonologueBackdrop/1.0 (+https://github.com/) compatible; image preview for editorial research"
)

_MAX_IMAGE_URL_LEN = 2048


def _fetch_html(url: str, timeout: float) -> str | None:
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        ctx = ssl.create_default_context()
        with urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status != 200:
                return None
            raw = resp.read(800_000)
        return raw.decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return None


def _extract_image_urls(html: str, base_url: str) -> list[str]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _extract_image_urls_regex(html, base_url)

    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        if prop in (
            "og:image",
            "og:image:url",
            "og:image:secure_url",
            "twitter:image",
            "twitter:image:src",
        ):
            c = meta.get("content")
            if c and isinstance(c, str):
                u = urljoin(base_url, c.strip())
                if u.startswith("http") and len(u) < _MAX_IMAGE_URL_LEN:
                    out.append(u)
    # One prominent <img> if og missing (skip data:)
    if not out:
        for img in soup.find_all("img", limit=8):
            src = img.get("src")
            if not src or not isinstance(src, str):
                continue
            if src.strip().lower().startswith("data:"):
                continue
            u = urljoin(base_url, src.strip())
            if u.startswith("http") and len(u) < _MAX_IMAGE_URL_LEN:
                out.append(u)
    return out


def _extract_image_urls_regex(html: str, base_url: str) -> list[str]:
    out: list[str] = []
    for pat in (
        r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        r'property=["\']og:image:url["\']\s+content=["\']([^"\']+)["\']',
        r'name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']',
    ):
        for m in re.finditer(pat, html, re.I):
            u = urljoin(base_url, m.group(1).strip())
            if u.startswith("http"):
                out.append(u)
    return out


def _normalize_url(u: str) -> str:
    u = u.strip()
    if not u.startswith(("http://", "https://")):
        return ""
    return u


def refresh_persona_visual_cache(persona, *, max_pages: int = 10, timeout: float = 2.8) -> list[str]:
    """
    Fetch up to max_pages source URLs and collect unique image candidates.
    Updates persona.cached_visual_urls and visual_cache_updated_at.
    """
    if not monologue_visual_columns_ready():
        return []

    sources = persona.source_urls or []
    if not isinstance(sources, list):
        sources = []
    seen: set[str] = set()
    collected: list[str] = []

    for raw in sources[:max_pages]:
        page_url = _normalize_url(str(raw))
        if not page_url:
            continue
        host = urlparse(page_url).netloc.lower()
        if host in {"localhost", "127.0.0.1"}:
            continue
        html = _fetch_html(page_url, timeout=timeout)
        if not html:
            continue
        for img in _extract_image_urls(html, page_url):
            if img not in seen:
                seen.add(img)
                collected.append(img)
            if len(collected) >= 36:
                break
        if len(collected) >= 36:
            break

    persona.cached_visual_urls = collected
    persona.visual_cache_updated_at = timezone.now()
    persona.save(update_fields=["cached_visual_urls", "visual_cache_updated_at", "updated_at"])
    return collected
