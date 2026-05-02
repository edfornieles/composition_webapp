"""
Upgrade candidate URLs extracted from scrape pages to their highest-resolution
variant (thumbnail → full-res), and drop URLs that are known never to yield a
usable full-size image.

Why this exists
===============
Yandex, Google and Bing search results, Pinterest search, Tumblr tag pages,
and most other discovery surfaces hand us **thumbnail** URLs. Downloading
those gives us ``i.pinimg.com/236x/…jpg`` — a 236-pixel-wide thumbnail —
which then passes our dedupe + QC checks and lands in the folder as
low-resolution fill. The vast majority of the repetition and "why does my
grid look like a blurry mosaic" feeling on batches like /ingestion/1/
traces back to this: we're ingesting the thumbnail, not the source.

The Bulk Image Downloader extension solves this with two user-editable
lists plus a heuristic scorer:
  * an **Include** list of patterns that ALWAYS correspond to full-res, and
  * an **Ignore** list of patterns to silently drop,
  * and per-host "site scripts" that know how to rewrite a thumbnail URL
    to the full-resolution variant on that specific CDN.

This module is the pure-Python version of that third piece. Each ``Rule``
is a small data object with:
  * a host matcher (suffix match, e.g. ``i.pinimg.com``),
  * an optional ``rewrite`` callable that takes a ``urllib.parse.ParseResult``
    and returns a replacement URL (or ``None`` to leave the URL alone), and
  * an ``ignore`` flag that discards the URL outright.

Rules are evaluated in order; the first matching rule wins. Unknown hosts
fall through unchanged, so adding a new site is purely additive.

Integration
-----------
See ``views._extract_candidates_from_page``: every list returned by the
extractor is passed through :func:`rewrite_candidate_urls` before the
caller ever sees it. That's the single chokepoint for Yandex-derived URLs,
user-pasted URLs, gallery-dl output, and playwright output.

Adding new rules
----------------
Prefer to add rules here rather than touching the extractors. A rule needs
at most ~5 lines and a comment explaining what low-res pattern you saw.
Keep the comment — in a year nobody will remember why we rewrite
``/s250x400/`` to ``/s2048x3072/`` on tumblr.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import ParseResult, urlparse, urlunparse

_LOGGER = logging.getLogger("djangoscrap.url_rewriter")


# --------------------------------------------------------------------------- #
#  Rule container                                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Rule:
    """
    One URL-rewrite rule. A rule matches on ``host_suffix`` (case-insensitive
    suffix match against ``parsed.netloc``, so ``i.pinimg.com`` also matches
    ``www.i.pinimg.com`` and ``s-media-cache-ak0.pinimg.com``).

    Exactly one of ``rewrite`` or ``ignore`` should be meaningful. If ``ignore``
    is True, matching URLs are dropped entirely. Otherwise ``rewrite(parsed)``
    is called; if it returns a string, that's the new URL. Returning None
    leaves the URL unchanged (useful when a rule matches the host but not the
    specific path pattern that needs rewriting — the host's other patterns
    stay in play for other rules if we didn't register the suffix, but since
    rules are order-sensitive we short-circuit on first match).
    """

    host_suffix: str
    name: str = ""
    rewrite: Optional[Callable[[ParseResult], Optional[str]]] = None
    ignore: bool = False
    # Per-rule tag for diagnostic counters — defaults to ``name`` when unset.
    counter_key: str = ""

    def matches(self, netloc: str) -> bool:
        if not netloc:
            return False
        h = netloc.lower()
        if h.startswith("www."):
            h = h[4:]
        suffix = self.host_suffix.lower()
        return h == suffix or h.endswith("." + suffix)

    @property
    def tag(self) -> str:
        return self.counter_key or self.name or self.host_suffix


# --------------------------------------------------------------------------- #
#  Per-site rewrite functions                                                  #
# --------------------------------------------------------------------------- #
#
# Each of these takes a ParseResult and returns a replacement URL string or
# None. They are intentionally defensive — if the path shape doesn't match
# what we expect, we return None so the URL stays as-is rather than producing
# a broken rewrite.


# Pinterest CDN: path shape is ``/<size>/<xx>/<yy>/<zz>/<hash>.<ext>``. The
# ``/originals/`` bucket holds the uploader's source image at maximum
# resolution available; every other size (``/236x/``, ``/474x/``, ``/564x/``,
# ``/736x/``, ``/1200x/`` …) is a downsized JPEG re-encode. Yandex image
# search almost always links to one of the smaller ones.
_PINTEREST_SIZE_RE = re.compile(r"^/\d+x\d*/")


def _pinterest_upgrade(p: ParseResult) -> Optional[str]:
    if not _PINTEREST_SIZE_RE.match(p.path):
        return None
    new_path = _PINTEREST_SIZE_RE.sub("/originals/", p.path, count=1)
    # Pinterest's /originals/ bucket tends to serve the uploader's original
    # format. For URLs that came in as ``.jpg`` the original may well be a
    # ``.png`` or ``.gif``; we can't know without probing, so we try ``.jpg``
    # first (covers the common case) and accept a 404 fallthrough to the
    # scraper's existing error handling if Pinterest returns 404 for that
    # extension — the old thumbnail URL is lost, but the original was never
    # accessible anyway.
    return urlunparse(p._replace(path=new_path))


# Tumblr media CDN: ``/<base>/s{W}x{H}/<hash>/...``. Bumping the size segment
# to ``s2048x3072`` returns the largest variant Tumblr will serve publicly.
_TUMBLR_SIZE_RE = re.compile(r"(/s)\d+x\d+(/|$)")


def _tumblr_upgrade(p: ParseResult) -> Optional[str]:
    if not _TUMBLR_SIZE_RE.search(p.path):
        return None
    new_path = _TUMBLR_SIZE_RE.sub(r"\g<1>2048x3072\g<2>", p.path, count=1)
    if new_path == p.path:
        return None
    return urlunparse(p._replace(path=new_path))


# Squarespace image CDN: resolution is controlled by ``?format=500w`` (or
# ``?format=original``) style query params. The default response (no query)
# serves the original upload.
def _squarespace_upgrade(p: ParseResult) -> Optional[str]:
    if not p.query:
        return None
    return urlunparse(p._replace(query=""))


# WordPress / Jetpack photon: ``i0.wp.com/foo.jpg?w=300&h=200&resize=…``.
# Stripping the query returns the full-res upstream.
def _wp_photon_upgrade(p: ParseResult) -> Optional[str]:
    if not p.query:
        return None
    return urlunparse(p._replace(query=""))


# Flickr static farm: ``live.staticflickr.com/{server}/{id}_{secret}_{size}.jpg``
# where size is a letter: s/q/t/m/n/w/z/c (small), b (large), h (1600), k
# (2048), 3k/4k/5k/6k, and o (original, often a PNG). We pick ``b`` as the
# best guaranteed public size — ``o`` requires the owner to have enabled
# "view original" and often 404s.
_FLICKR_SIZE_RE = re.compile(
    r"^(/\d+/\d+_[0-9a-f]+)(?:_(?:[sqtmnwzc]|b|h|k|3k|4k|5k|6k))?(\.(?:jpe?g|png|gif))$",
    re.IGNORECASE,
)


def _flickr_upgrade(p: ParseResult) -> Optional[str]:
    m = _FLICKR_SIZE_RE.match(p.path)
    if not m:
        return None
    stem, ext = m.group(1), m.group(2)
    new_path = f"{stem}_b{ext}"
    if new_path == p.path:
        return None
    return urlunparse(p._replace(path=new_path))


# Etsy product images: ``il_340x270.xxx_abc.jpg`` / ``il_570xN.xxx_abc.jpg``
# → ``il_fullxfull.xxx_abc.jpg``.
_ETSY_SIZE_RE = re.compile(r"/il_\d+x(?:\d+|N)\.", re.IGNORECASE)


def _etsy_upgrade(p: ParseResult) -> Optional[str]:
    if not _ETSY_SIZE_RE.search(p.path):
        return None
    new_path = _ETSY_SIZE_RE.sub("/il_fullxfull.", p.path, count=1)
    return urlunparse(p._replace(path=new_path))


# Dribbble CDN: shots have suffixes like ``_teaser``, ``_1x_min``, ``_1x``,
# ``_2x``, ``_4x``. The ``_4x`` variant is the highest quality that Dribbble
# reliably serves without the uploader's paid plan; fall back to stripping the
# suffix entirely if ``_4x`` isn't likely to exist.
_DRIBBBLE_SIZE_RE = re.compile(
    r"_(teaser|1x_min|1x|2x)(\.(?:jpe?g|png|gif|webp))$",
    re.IGNORECASE,
)


def _dribbble_upgrade(p: ParseResult) -> Optional[str]:
    m = _DRIBBBLE_SIZE_RE.search(p.path)
    if not m:
        return None
    new_path = _DRIBBBLE_SIZE_RE.sub(r"_4x\2", p.path, count=1)
    return urlunparse(p._replace(path=new_path))


# Wikimedia Commons thumbnails: ``/wikipedia/commons/thumb/a/ab/Foo.jpg/320px-Foo.jpg``
# where the full-resolution file is at ``/wikipedia/commons/a/ab/Foo.jpg``.
_WIKIMEDIA_THUMB_RE = re.compile(
    r"^(/wikipedia/[^/]+)/thumb/([a-f0-9])/([a-f0-9]{2})/([^/]+)/[^/]+$"
)


def _wikimedia_upgrade(p: ParseResult) -> Optional[str]:
    m = _WIKIMEDIA_THUMB_RE.match(p.path)
    if not m:
        return None
    proj, shard1, shard2, fname = m.groups()
    new_path = f"{proj}/{shard1}/{shard2}/{fname}"
    return urlunparse(p._replace(path=new_path))


# Reddit preview CDN: ``preview.redd.it/<hash>.jpg?auto=webp&s=…``. The
# original asset is at ``i.redd.it/<hash>.jpg`` with no query string.
def _reddit_preview_upgrade(p: ParseResult) -> Optional[str]:
    if not p.path:
        return None
    # Strip query + rehome to i.redd.it. The path stays the same.
    return urlunparse(p._replace(netloc="i.redd.it", query=""))


# 500px CDN: ``drscdn.500px.org/.../v2?webp=1&w=440&h=…&sig=…`` variants
# include a size in the query. Removing the query returns 404 on 500px; the
# safer upgrade is to swap the size params to the largest they publish
# publicly (``q=80&w=2048``).
# Use a capture group for the boundary so we preserve ``&`` or string-start
# when substituting. Otherwise ``sub("w=2048", …)`` would drop the ``&``
# separator and produce ``?webp=1w=2048h=2048`` (caught by
# tests/test_url_rewriter.py::test_builtin_upgrades).
_500PX_QUERY_W = re.compile(r"(^|&)w=\d+")
_500PX_QUERY_H = re.compile(r"(^|&)h=\d+")


def _fivehundredpx_upgrade(p: ParseResult) -> Optional[str]:
    if not p.query:
        return None
    new_query = _500PX_QUERY_W.sub(r"\1w=2048", p.query)
    new_query = _500PX_QUERY_H.sub(r"\1h=2048", new_query)
    if new_query == p.query:
        return None
    return urlunparse(p._replace(query=new_query))


# Unsplash: ``images.unsplash.com/photo-{id}?...&w=400``. Swap to a large
# variant with a reasonable quality setting.
def _unsplash_upgrade(p: ParseResult) -> Optional[str]:
    if "/photo-" not in p.path:
        return None
    # Drop any ``w=``/``h=``/``fit=``/``q=`` and request a large auto-format.
    new_query = "auto=format&q=80&w=2400"
    return urlunparse(p._replace(query=new_query))


# --------------------------------------------------------------------------- #
#  Rule registry                                                               #
# --------------------------------------------------------------------------- #
#
# Order matters: first match wins. Put ``ignore`` rules for known-bad hosts
# before any upgrade rules so we never waste bandwidth fetching stock-site
# watermark previews that can't ever yield a usable image.


_RULES: list[Rule] = [
    # ----- ignore: known watermark/preview-only hosts ---------------------- #
    # Dreamstime previews are always watermarked; the full res is paywalled.
    Rule(
        host_suffix="dreamstime.com",
        name="dreamstime-preview",
        ignore=True,
    ),
    Rule(
        host_suffix="alamy.com",
        name="alamy-preview",
        ignore=True,
    ),
    Rule(
        host_suffix="shutterstock.com",
        name="shutterstock-preview",
        ignore=True,
    ),
    Rule(
        host_suffix="gettyimages.com",
        name="gettyimages-preview",
        ignore=True,
    ),
    Rule(
        host_suffix="istockphoto.com",
        name="istock-preview",
        ignore=True,
    ),
    Rule(
        host_suffix="123rf.com",
        name="123rf-preview",
        ignore=True,
    ),
    # Freemium stock CDNs that serve watermark-branded previews to anonymous
    # clients. Even when the URL resolves, the image has a "Freepik" /
    # "Vecteezy" / "PngTree" watermark baked in that ruins composition tiles.
    # Real discovery of these brands requires a logged-in paid account, which
    # the ingestion pipeline isn't set up to do.
    Rule(
        host_suffix="freepik.com",
        name="freepik-preview",
        ignore=True,
    ),
    Rule(
        host_suffix="vecteezy.com",
        name="vecteezy-preview",
        ignore=True,
    ),
    Rule(
        host_suffix="pngtree.com",
        name="pngtree-preview",
        ignore=True,
    ),
    Rule(
        host_suffix="ftcdn.net",  # Adobe Stock preview CDN
        name="adobe-stock-preview",
        ignore=True,
    ),
    Rule(
        host_suffix="depositphotos.com",
        name="depositphotos-preview",
        ignore=True,
    ),
    # Adobe Stock browse/search pages — the real browse host is stock.adobe.com;
    # previews are watermarked 256-wide JPEGs. Dropping the browse-page URL
    # prevents us scraping HTML for content we can't legally use anyway.
    Rule(
        host_suffix="stock.adobe.com",
        name="stock-adobe-preview",
        ignore=True,
    ),

    # ----- upgrade: known thumbnail → full-res rewriters ------------------- #
    Rule(
        host_suffix="pinimg.com",
        name="pinterest",
        rewrite=_pinterest_upgrade,
    ),
    Rule(
        host_suffix="tumblr.com",  # matches *.media.tumblr.com
        name="tumblr",
        rewrite=_tumblr_upgrade,
    ),
    Rule(
        host_suffix="squarespace-cdn.com",
        name="squarespace",
        rewrite=_squarespace_upgrade,
    ),
    Rule(
        host_suffix="wp.com",  # matches i0/i1/i2.wp.com (Jetpack photon)
        name="wp-photon",
        rewrite=_wp_photon_upgrade,
    ),
    Rule(
        host_suffix="staticflickr.com",
        name="flickr",
        rewrite=_flickr_upgrade,
    ),
    Rule(
        host_suffix="etsystatic.com",
        name="etsy",
        rewrite=_etsy_upgrade,
    ),
    Rule(
        host_suffix="dribbble.com",
        name="dribbble",
        rewrite=_dribbble_upgrade,
    ),
    Rule(
        host_suffix="wikimedia.org",
        name="wikimedia",
        rewrite=_wikimedia_upgrade,
    ),
    Rule(
        host_suffix="redd.it",  # matches preview.redd.it and i.redd.it
        name="reddit-preview",
        rewrite=_reddit_preview_upgrade,
    ),
    Rule(
        host_suffix="500px.org",
        name="500px",
        rewrite=_fivehundredpx_upgrade,
    ),
    Rule(
        host_suffix="unsplash.com",
        name="unsplash",
        rewrite=_unsplash_upgrade,
    ),
]


# --------------------------------------------------------------------------- #
#  Optional JSON overlay                                                       #
# --------------------------------------------------------------------------- #
#
# Operators can drop a ``ingestion_site_rules.json`` file at the project
# root (or point ``INGESTION_SITE_RULES`` at any path) to extend the rule
# set without editing Python. The JSON schema is:
#
#   {
#     "ignore": ["example.com", "*.preview-cdn.com"],
#     "upgrade": [
#       {
#         "name": "my-site",
#         "host_suffix": "cdn.mysite.com",
#         "pattern": "/thumb/(\\d+)/",
#         "replacement": "/original/"
#       }
#     ]
#   }
#
# JSON rules are prepended to the built-in registry so they take precedence,
# meaning you can override a built-in behavior by declaring a JSON rule with
# the same host_suffix.
#
# Format notes:
# * ``ignore`` is a list of host suffixes (no protocol, no path).
# * ``upgrade[].pattern`` is a Python regex applied to the *path* portion of
#   the URL (``p.path``) — protocol, host, and query are preserved unless
#   ``apply_to_query`` is true, in which case the regex is applied to the
#   full URL including query string.
# * ``upgrade[].replacement`` may use regex backreferences like ``\1``.
# * Parsing errors are logged and the offending rule is skipped; bad JSON
#   never blocks ingestion.


def _json_rule_candidates() -> list[Path]:
    """Return candidate paths to check for the JSON overlay, in priority order."""
    paths: list[Path] = []
    env_path = os.environ.get("INGESTION_SITE_RULES", "").strip()
    if env_path:
        paths.append(Path(env_path))
    here = Path(__file__).resolve().parent
    project_root = here.parent
    paths.append(project_root / "ingestion_site_rules.json")
    paths.append(here / "ingestion_site_rules.json")
    return paths


def _compile_json_upgrade(entry: dict) -> Optional[Rule]:
    """Turn one ``upgrade`` JSON entry into a :class:`Rule` or return None on error."""
    host_suffix = (entry.get("host_suffix") or entry.get("host") or "").strip().lower()
    pattern = entry.get("pattern") or ""
    replacement = entry.get("replacement")
    apply_to_query = bool(entry.get("apply_to_query", False))
    name = entry.get("name") or host_suffix
    if not host_suffix or not pattern or replacement is None:
        _LOGGER.warning("skipping upgrade rule with missing host/pattern/replacement: %r", entry)
        return None
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        _LOGGER.warning("skipping upgrade rule %s: bad regex %r (%s)", name, pattern, exc)
        return None

    def _rewriter(p: ParseResult, _re=compiled, _repl=replacement, _q=apply_to_query) -> Optional[str]:
        if _q:
            full = urlunparse(p)
            new_full, n = _re.subn(_repl, full)
            if not n or new_full == full:
                return None
            return new_full
        new_path, n = _re.subn(_repl, p.path)
        if not n or new_path == p.path:
            return None
        return urlunparse(p._replace(path=new_path))

    return Rule(
        host_suffix=host_suffix,
        name=f"json:{name}",
        rewrite=_rewriter,
        counter_key=f"json:{name}",
    )


def _load_json_rules() -> list[Rule]:
    """
    Load JSON overlay rules from the first candidate path that exists. Silently
    returns ``[]`` when no file is found, logs a warning for malformed files.
    """
    for path in _json_rule_candidates():
        if not path or not path.exists():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception as exc:
            _LOGGER.warning("ingestion_site_rules.json at %s failed to parse: %s", path, exc)
            return []
        if not isinstance(data, dict):
            _LOGGER.warning("ingestion_site_rules.json at %s: top level must be an object", path)
            return []
        rules: list[Rule] = []
        ignore_entries = data.get("ignore") or []
        if isinstance(ignore_entries, list):
            for raw in ignore_entries:
                host_suffix = str(raw or "").strip().lower().lstrip("*.")
                if not host_suffix:
                    continue
                rules.append(
                    Rule(
                        host_suffix=host_suffix,
                        name=f"json-ignore:{host_suffix}",
                        ignore=True,
                        counter_key=f"json-ignore:{host_suffix}",
                    )
                )
        upgrade_entries = data.get("upgrade") or []
        if isinstance(upgrade_entries, list):
            for entry in upgrade_entries:
                if not isinstance(entry, dict):
                    continue
                compiled = _compile_json_upgrade(entry)
                if compiled is not None:
                    rules.append(compiled)
        if rules:
            _LOGGER.info(
                "loaded %d ingestion URL overlay rule(s) from %s", len(rules), path
            )
        return rules
    return []


# Load once at import. JSON rules are prepended so they win tie-breaks
# against the built-in registry (first match wins in _find_matching_rule).
_RULES = _load_json_rules() + _RULES


def reload_rules() -> int:
    """
    Re-read ``ingestion_site_rules.json`` and rebuild the rule registry.
    Returns the number of JSON rules loaded. Safe to call from a management
    command or admin view; no coordination with live requests needed because
    ``_RULES`` is only read, never mutated in-place, during a rewrite.
    """
    global _RULES
    # Strip any previous JSON rules (anything tagged ``json:`` / ``json-ignore:``)
    # before re-prepending the fresh load.
    builtins = [r for r in _RULES if not r.name.startswith(("json:", "json-ignore:"))]
    fresh = _load_json_rules()
    _RULES = fresh + builtins
    return len(fresh)


# --------------------------------------------------------------------------- #
#  Public API                                                                  #
# --------------------------------------------------------------------------- #


def _find_matching_rule(netloc: str) -> Optional[Rule]:
    """First matching rule (any kind). Kept for stats-labeling compat."""
    for rule in _RULES:
        if rule.matches(netloc):
            return rule
    return None


def _find_matching_rules(netloc: str) -> list[Rule]:
    """All rules whose host suffix matches, in registry order.

    Unlike :func:`_find_matching_rule`, this returns every candidate.
    ``rewrite_candidate_url`` iterates through them so that multiple
    patterns for the same host (e.g. Twitter ``:size`` suffix AND
    ``?name=size`` query) can each take a shot at upgrading the URL.
    Required once the JSON overlay is allowed to register several
    rules per host — see test_overlay_upgrade_produces_expected_url
    for the twimg case that originally surfaced this.
    """
    return [rule for rule in _RULES if rule.matches(netloc)]


def rewrite_candidate_url(url: str) -> Optional[str]:
    """
    Return the full-resolution variant of ``url``, or ``None`` if the URL
    should be dropped entirely (matched an ``ignore`` rule).

    Evaluation semantics:

    * The first ``ignore`` rule whose host matches short-circuits and drops
      the URL.
    * Otherwise every matching ``upgrade`` rule is tried in registry order;
      the first one that actually produces a replacement wins. Rules whose
      callable returns ``None`` are skipped so that a later rule with a
      different pattern on the same host still gets a chance.
    * If no rule matches or no rule rewrites, the URL is returned unchanged.
    """
    if not url:
        return url
    try:
        p = urlparse(url)
    except Exception:
        return url
    if p.scheme not in {"http", "https"}:
        return url
    matches = _find_matching_rules(p.netloc or "")
    if not matches:
        return url
    for rule in matches:
        if rule.ignore:
            return None
        if rule.rewrite is None:
            continue
        try:
            replacement = rule.rewrite(p)
        except Exception:
            replacement = None
        if replacement:
            return replacement
    return url


@dataclass
class RewriteStats:
    """Per-call diagnostic counters so campaigns can report what happened."""

    total_input: int = 0
    upgraded: int = 0
    dropped: int = 0
    per_rule: dict = field(default_factory=dict)

    def _bump(self, key: str) -> None:
        self.per_rule[key] = self.per_rule.get(key, 0) + 1

    def as_summary(self) -> str:
        if not self.total_input:
            return "url-rewriter: no input"
        parts = [
            f"in={self.total_input}",
            f"upgraded={self.upgraded}",
            f"dropped={self.dropped}",
        ]
        top = sorted(self.per_rule.items(), key=lambda kv: -kv[1])[:6]
        if top:
            parts.append("rules=" + ",".join(f"{k}:{v}" for k, v in top))
        return "url-rewriter: " + " ".join(parts)


def rewrite_candidate_urls(
    urls: list[str],
    *,
    stats: Optional[RewriteStats] = None,
) -> list[str]:
    """
    Apply :func:`rewrite_candidate_url` to every URL in ``urls``.

    * URLs matched by an ``ignore`` rule are dropped.
    * URLs rewritten by an ``upgrade`` rule are replaced in place.
    * Unknown URLs pass through unchanged.
    * The returned list preserves the original order but deduplicates on the
      post-rewrite value, so two different thumbnail URLs that upgrade to the
      same ``/originals/`` path are only emitted once. This is important:
      without this, the campaign cycle's URL-seen set would still treat the
      two thumbnails as distinct candidates even though they resolve to the
      same image, wasting a download slot.
    """
    out: list[str] = []
    seen_post_rewrite: set[str] = set()
    for raw in urls:
        if stats is not None:
            stats.total_input += 1
        replacement = rewrite_candidate_url(raw)
        if replacement is None:
            # Matched an ignore rule.
            if stats is not None:
                stats.dropped += 1
                try:
                    host = (urlparse(raw).netloc or "").lower()
                except Exception:
                    host = ""
                rule = _find_matching_rule(host)
                if rule is not None:
                    stats._bump(rule.tag)
            continue
        if replacement in seen_post_rewrite:
            continue
        seen_post_rewrite.add(replacement)
        if stats is not None and replacement != raw:
            stats.upgraded += 1
            try:
                host = (urlparse(raw).netloc or "").lower()
            except Exception:
                host = ""
            rule = _find_matching_rule(host)
            if rule is not None:
                stats._bump(rule.tag)
        out.append(replacement)
    return out
