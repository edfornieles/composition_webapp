"""
InstagrapiAdapter — Instagram posts/reels/profile adapter.

Uses the `instagrapi` Python library (optional dependency). Supports:
* ``https://www.instagram.com/<handle>/``         → profile media
* ``https://www.instagram.com/p/<shortcode>/``    → single post or carousel
* ``https://www.instagram.com/reel/<shortcode>/`` → reel

Credentials
===========
Two supported modes, in this priority order:

1. **Session ID cookie** (preferred). Set ``INSTAGRAM_SESSIONID`` to the
   value of the ``sessionid`` cookie copied from a logged-in browser
   session. This bypasses ``accounts/login`` entirely — no password is
   ever sent — so Meta's "suspicious login" / IP-blacklist gate never
   fires. Home IPs and residential ISPs are routinely blocked from the
   private mobile-API login path; the cookie path is what actually
   works in practice. Rotate the value whenever your browser session
   expires.

2. **Username / password fallback**. ``INSTAGRAM_USER`` +
   ``INSTAGRAM_PASS``. Works on fresh, non-flagged accounts from IPs
   Meta hasn't already fingerprinted. Expect login challenges on first
   use and occasional hard blocks thereafter.

Sessions are cached at ``~/.config/composition_webapp/ig_session.json``
in both modes so subsequent invocations reuse the authenticated state.
If neither credential set is present, ``can_handle`` returns False and
the adapter silently defers to ``instaloader`` (via the legacy engine
chain) or ``yt-dlp``.

Resumable sync
==============
``state["last_post_pk"]`` is the highwater mark: pagination stops once
we see a post with pk ≤ that value. On first run, state is empty and we
take everything up to ``max_urls``.

Testing notes
=============
The live ``instagrapi.Client`` is never touched by pytest. Unit tests
mock the client and assert on our glue code (state management,
post→URLs extraction, carousel unrolling). Live verification requires
``RUN_SOCIAL_TESTS=1`` and valid credentials.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from djangoscrap.ingestion_adapters.base import Adapter, AdapterResult

_LOGGER = logging.getLogger("djangoscrap.adapters.instagrapi")


# --------------------------------------------------------------------------- #
#  URL classification                                                         #
# --------------------------------------------------------------------------- #


def classify_instagram_url(url: str) -> Optional[tuple[str, str]]:
    """
    Return ``(kind, key)`` where kind is one of ``profile|post|reel`` and
    key is the handle / shortcode. Returns None if the URL isn't a
    recognized Instagram content URL.

    Accepts www.instagram.com + instagram.com, http + https, with or
    without trailing slash.
    """
    if not url:
        return None
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return None
    netloc = (parsed.netloc or "").lower().removeprefix("www.")
    if netloc != "instagram.com":
        return None
    parts = [p for p in (parsed.path or "").strip("/").split("/") if p]
    if not parts:
        return None
    if parts[0] == "p" and len(parts) >= 2:
        return "post", parts[1]
    if parts[0] == "reel" and len(parts) >= 2:
        return "reel", parts[1]
    if parts[0] == "reels" and len(parts) >= 2:
        return "reel", parts[1]
    if parts[0] == "tv" and len(parts) >= 2:
        return "reel", parts[1]  # IGTV → treat as reel
    # /<handle>/ — profile
    if len(parts) == 1 and not parts[0].startswith("_"):
        return "profile", parts[0]
    return None


# --------------------------------------------------------------------------- #
#  Client wrapper (dependency-injectable for tests)                           #
# --------------------------------------------------------------------------- #


class _InstagrapiClientProvider:
    """
    Lazy loader for an authenticated ``instagrapi.Client``. Split out so
    tests can inject a fake client without installing instagrapi.
    """

    def __init__(
        self,
        *,
        user_env: str = "INSTAGRAM_USER",
        pass_env: str = "INSTAGRAM_PASS",
        sessionid_env: str = "INSTAGRAM_SESSIONID",
        session_path: Optional[str] = None,
    ) -> None:
        self._user_env = user_env
        self._pass_env = pass_env
        self._sessionid_env = sessionid_env
        self._session_path = Path(
            session_path
            or os.path.expanduser("~/.config/composition_webapp/ig_session.json")
        )
        self._client = None

    # Preferred path: sessionid cookie. Returns the stripped value (or "") so
    # both ``available()`` and ``get()`` share one code-path for "is the cookie
    # present and non-whitespace".
    def _sessionid(self) -> str:
        return (os.environ.get(self._sessionid_env) or "").strip()

    def _has_userpass(self) -> bool:
        return bool(os.environ.get(self._user_env) and os.environ.get(self._pass_env))

    def available(self) -> bool:
        """True if instagrapi importable AND at least one credential mode is configured."""
        try:
            import instagrapi  # noqa: F401
        except Exception:
            return False
        return bool(self._sessionid() or self._has_userpass())

    def get(self) -> Any:
        """Return a logged-in client, reusing the session cache when possible.

        Auth resolution:
        1. If a cached session file exists, load it and trust it. instagrapi
           re-verifies on the next API call, so a stale cookie still produces
           a clear error downstream rather than silently returning junk.
        2. Otherwise, if INSTAGRAM_SESSIONID is set, use login_by_sessionid —
           no password ever leaves this process.
        3. Otherwise fall back to classic username/password login.
        """
        if self._client is not None:
            return self._client
        from instagrapi import Client  # type: ignore

        client = Client()
        if self._session_path.exists():
            try:
                client.load_settings(str(self._session_path))
                # load_settings alone does not confirm auth; but many endpoints
                # work purely off the stored cookies so we skip a synchronous
                # login round-trip here. Callers that hit auth-gated endpoints
                # will get a clear ClientLoginRequired if the session expired.
                self._client = client
                return client
            except Exception as exc:
                _LOGGER.warning("instagrapi session load failed, re-logging in: %r", exc)

        sid = self._sessionid()
        try:
            if sid:
                client.login_by_sessionid(sid)
            else:
                user = os.environ.get(self._user_env, "")
                pw = os.environ.get(self._pass_env, "")
                if not (user and pw):
                    raise RuntimeError(
                        "instagrapi: no credentials available "
                        "(set INSTAGRAM_SESSIONID, or INSTAGRAM_USER+INSTAGRAM_PASS)"
                    )
                client.login(user, pw)
        except Exception as exc:
            _LOGGER.warning("instagrapi login failed: %r", exc)
            raise

        try:
            self._session_path.parent.mkdir(parents=True, exist_ok=True)
            client.dump_settings(str(self._session_path))
        except Exception:
            pass
        self._client = client
        return client


# --------------------------------------------------------------------------- #
#  Post → URL extraction                                                      #
# --------------------------------------------------------------------------- #


def _urls_from_media(media: Any) -> list[str]:
    """Pull image / video URLs off an instagrapi Media object.

    Supports single image, single video, and carousel. Handles the fact
    that instagrapi returns Pydantic-like objects where URLs are
    attributes (``media.thumbnail_url``, ``media.video_url``,
    ``media.resources``). We extract all of them; the dedupe layer later
    collapses duplicates if the same CDN URL appears twice.
    """
    urls: list[str] = []

    # Carousel (media_type=8) — recurse into resources.
    resources = getattr(media, "resources", None) or []
    if resources:
        for res in resources:
            urls.extend(_urls_from_media(res))

    # Top-level media URL (image or video).
    video_url = getattr(media, "video_url", None)
    if video_url:
        urls.append(str(video_url))
    thumbnail_url = getattr(media, "thumbnail_url", None)
    if thumbnail_url:
        urls.append(str(thumbnail_url))

    # Filter http(s) and dedupe.
    seen: set[str] = set()
    deduped: list[str] = []
    for u in urls:
        if not (u.startswith("http://") or u.startswith("https://")):
            continue
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
    return deduped


# --------------------------------------------------------------------------- #
#  Adapter                                                                    #
# --------------------------------------------------------------------------- #


class InstagrapiAdapter:
    """Priority-100 adapter for Instagram content URLs."""

    name: str = "instagrapi"
    priority: int = 100

    def __init__(self, *, provider: Optional[_InstagrapiClientProvider] = None):
        self._provider = provider or _InstagrapiClientProvider()

    def can_handle(self, url: str) -> bool:
        if not self._provider.available():
            return False
        return classify_instagram_url(url) is not None

    def extract(self, url: str, *, max_urls: int, state: dict) -> AdapterResult:
        classification = classify_instagram_url(url)
        if classification is None:
            return AdapterResult(error="instagrapi: url not recognized")
        kind, key = classification
        try:
            client = self._provider.get()
        except Exception as exc:
            return AdapterResult(error=f"instagrapi: login failed: {exc!r}")

        try:
            if kind == "profile":
                user_id = client.user_id_from_username(key)
                amount = min(max_urls, 50)  # each profile page call is expensive
                last_pk = int((state or {}).get("last_post_pk") or 0)
                medias = client.user_medias(user_id, amount)
                urls: list[str] = []
                highest_pk = last_pk
                for media in medias:
                    try:
                        pk = int(getattr(media, "pk", 0))
                    except Exception:
                        pk = 0
                    if last_pk and pk and pk <= last_pk:
                        continue
                    if pk and pk > highest_pk:
                        highest_pk = pk
                    urls.extend(_urls_from_media(media))
                    if len(urls) >= max_urls:
                        break
                next_state = {"last_post_pk": highest_pk} if highest_pk else dict(state or {})
                return AdapterResult(
                    urls=urls[:max_urls],
                    next_state=next_state,
                    diagnostics={"kind": kind, "handle": key, "media_count": str(len(medias))},
                )
            # post / reel — single-call
            media = client.media_info(client.media_pk_from_code(key))
            urls = _urls_from_media(media)
            return AdapterResult(
                urls=urls[:max_urls],
                next_state=dict(state or {}),
                diagnostics={"kind": kind, "shortcode": key, "carousel_count": str(len(urls))},
            )
        except Exception as exc:
            _LOGGER.warning("instagrapi extract failed for %s: %r", url, exc)
            return AdapterResult(error=f"instagrapi: {exc!r}")
