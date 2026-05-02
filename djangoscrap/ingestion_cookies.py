"""
Netscape-format cookie files for ingestion scrapers.

Operators export cookies from a logged-in browser (e.g. "Get cookies.txt LOCALLY")
after completing any bot check once; we reuse that session on HTTP and Playwright
requests — same pattern as ``INSTAGRAM_COOKIES_FILE`` for yt-dlp.
"""

from __future__ import annotations

import logging
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPCookieProcessor, build_opener

logger = logging.getLogger(__name__)


def resolve_netscape_cookie_file(raw: str | None) -> Path | None:
    """Return path only when ``raw`` is non-empty and the file exists."""
    r = (raw or "").strip()
    if not r:
        return None
    p = Path(r).expanduser()
    return p if p.is_file() else None


def mozilla_cookie_jar(path: Path) -> MozillaCookieJar | None:
    """Load cookies.txt (Mozilla / Netscape format)."""
    try:
        cj = MozillaCookieJar(str(path))
        cj.load(ignore_discard=True, ignore_expires=True)
        return cj
    except OSError:
        logger.warning("Cookie file unreadable: %s", path)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cookie file parse failed (%s): %s", path, exc)
        return None


def urllib_opener_with_netscape(path: Path | None):
    """Return urllib opener with cookie processor, or None if path missing/invalid."""
    if path is None:
        return None
    cj = mozilla_cookie_jar(path)
    if not cj:
        return None
    return build_opener(HTTPCookieProcessor(cj))


def _host_matches_cookie_domain(hostname: str, cookie_domain: str) -> bool:
    hn = (hostname or "").lower().strip(".")
    raw = (cookie_domain or "").strip().lower()
    if not hn or not raw:
        return False
    dom = raw.lstrip(".")
    return hn == dom or hn.endswith("." + dom)


def yandex_playwright_cookie_list(path: Path | None, page_url: str) -> list[dict]:
    """Load Netscape cookies from disk and return Playwright payloads for ``page_url``."""
    if path is None:
        return []
    jar = mozilla_cookie_jar(path)
    if not jar:
        return []
    return playwright_cookies_from_jar(jar, page_url)


def playwright_cookies_from_jar(jar: MozillaCookieJar, page_url: str) -> list[dict]:
    """
    Convert a MozillaCookieJar into Playwright ``add_cookies`` payloads for ``page_url``.

    Only cookies whose domain matches the page host are included (RFC-style domain match).
    """
    parsed = urlparse(page_url)
    host = (parsed.hostname or "").lower()
    if not host:
        return []
    out: list[dict] = []
    for c in jar:
        if not isinstance(c, Cookie):
            continue
        if not _host_matches_cookie_domain(host, c.domain or ""):
            continue
        dom = (c.domain or "").strip()
        if not dom:
            continue
        entry: dict = {
            "name": c.name,
            "value": c.value,
            "domain": dom if dom.startswith(".") else f".{dom.lstrip('.')}",
            "path": c.path or "/",
        }
        if c.secure:
            entry["secure"] = True
        exp = getattr(c, "expires", None)
        if exp not in (None, 0):
            try:
                entry["expires"] = float(exp)
            except (TypeError, ValueError):
                pass
        rest = getattr(c, "_rest", None)
        if isinstance(rest, dict) and rest.get("HttpOnly"):
            entry["httpOnly"] = True
        out.append(entry)
    return out
