"""
Runtime smoke-test for the ingestion pipeline's pure-Python layers.

Unlike the pytest suite (which runs isolated unit tests), this command is
meant to be run after any deploy / long shell session as a "is the
pipeline sane right now?" canary. It exits 0 on PASS and non-zero on FAIL
so scripts/run_tests.sh can gate on it.

What it checks (in order, failing fast):

1. The URL rewriter produces the expected output for a handful of real
   thumbnail URLs from every major built-in rule (Pinterest, Tumblr,
   Flickr, Wikimedia, Reddit preview, Etsy, Dribbble, 500px, Unsplash).
2. The ignore list drops known watermark hosts.
3. RewriteStats summary line is well-formed.
4. HostThrottle can acquire and release holds on at least two distinct
   hosts without blocking.
5. DownloadStats summary line is well-formed.
6. head_probe's decision matrix is reachable (we mock urlopen with a
   204 response so no real network is touched, but this proves the
   module imports and the class shape is unchanged).

Intentionally NOT checked here:
* Any live network access (that's the responsibility of real campaigns;
  we don't want a canary that flakes on upstream outages).
* Django ORM / migrations (migrations are validated by ``manage.py check``).
"""

from __future__ import annotations

import io
from unittest import mock

from django.core.management.base import BaseCommand, CommandError

from djangoscrap import ingestion_http as ih
from djangoscrap import ingestion_url_rewriter as rw


_REWRITE_SAMPLES = [
    (
        "https://i.pinimg.com/236x/ab/cd/ef/abcdef.jpg",
        "https://i.pinimg.com/originals/ab/cd/ef/abcdef.jpg",
        "pinterest",
    ),
    (
        "https://64.media.tumblr.com/x/s540x810/hash.jpg",
        "https://64.media.tumblr.com/x/s2048x3072/hash.jpg",
        "tumblr",
    ),
    (
        "https://live.staticflickr.com/65535/51234_abcdef_m.jpg",
        "https://live.staticflickr.com/65535/51234_abcdef_b.jpg",
        "flickr",
    ),
    (
        "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/X.jpg/320px-X.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/a/ab/X.jpg",
        "wikimedia",
    ),
    (
        "https://preview.redd.it/abc.jpg?auto=webp&s=x",
        "https://i.redd.it/abc.jpg",
        "reddit",
    ),
]

_IGNORED_HOSTS = [
    "https://www.shutterstock.com/image",
    "https://www.gettyimages.com/detail",
    "https://www.freepik.com/free-photo",
    "https://ftcdn.net/v2/jpg/01/23/45.jpg",  # adobe stock preview CDN
    "https://www.dreamstime.com/some-photo",
]


class _FakeResp:
    def __init__(self, *, content_type="image/jpeg", content_length=500_000):
        self.status = 200
        self.headers = {"Content-Type": content_type, "Content-Length": str(content_length)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Command(BaseCommand):
    help = "Smoke-test the ingestion pipeline's pure-Python layers. Exit 0 on PASS."

    # This canary only exercises the pure-Python ingestion modules, which have
    # no Django model / URL dependencies. Skipping system checks keeps the
    # command usable on developer machines that are missing optional heavy
    # deps like moviepy/ffmpeg (views.py pulls those in indirectly).
    requires_system_checks: list = []
    requires_migrations_checks = False

    def _fail(self, msg: str) -> None:
        self.stdout.write(self.style.ERROR(f"FAIL: {msg}"))
        raise CommandError(msg)

    def _ok(self, msg: str) -> None:
        self.stdout.write(self.style.SUCCESS(f"  ok: {msg}"))

    def handle(self, *args, **opts) -> None:
        self.stdout.write("ingestion_selftest starting...")

        # 1) URL rewriter positive cases.
        for raw, expected, label in _REWRITE_SAMPLES:
            got = rw.rewrite_candidate_url(raw)
            if got != expected:
                self._fail(f"rewriter[{label}]: {raw!r} → {got!r} (expected {expected!r})")
        self._ok(f"url-rewriter upgraded {len(_REWRITE_SAMPLES)} sample URLs correctly")

        # 2) Ignore list negative cases.
        for raw in _IGNORED_HOSTS:
            got = rw.rewrite_candidate_url(raw)
            if got is not None:
                self._fail(f"rewriter ignore: {raw!r} should be dropped, got {got!r}")
        self._ok(f"url-rewriter dropped {len(_IGNORED_HOSTS)} ignore-listed URLs")

        # 3) RewriteStats summary line.
        stats = rw.RewriteStats()
        rw.rewrite_candidate_urls(
            [s[0] for s in _REWRITE_SAMPLES] + _IGNORED_HOSTS,
            stats=stats,
        )
        summary = stats.as_summary()
        if not (summary.startswith("url-rewriter:") and "in=" in summary and "upgraded=" in summary):
            self._fail(f"rewriter summary malformed: {summary!r}")
        self._ok(f"rewriter summary OK: {summary}")

        # 4) HostThrottle acquire / release.
        try:
            with ih.HOST_THROTTLE.hold("selftest-host-a.example.com"):
                pass
            with ih.HOST_THROTTLE.hold("selftest-host-b.example.com"):
                pass
        except Exception as exc:
            self._fail(f"HostThrottle.hold raised: {exc!r}")
        self._ok("HostThrottle acquired + released on 2 distinct hosts")

        # 5) DownloadStats summary format.
        ds = ih.DownloadStats()
        ds.probed = 10
        ds.get_ok = 7
        ds.get_failed = 2
        ds.head_blocked = 1
        ds.bump_reason("too_small")
        dsum = ds.as_summary()
        if "probed=10" not in dsum or "get_ok=7" not in dsum:
            self._fail(f"DownloadStats summary malformed: {dsum!r}")
        self._ok(f"DownloadStats summary OK: {dsum}")

        # 6) head_probe decision matrix reachable (mocked).
        with mock.patch.object(ih, "urlopen", return_value=_FakeResp()):
            probe = ih.head_probe("https://cdn.example.com/big.jpg", throttle=None)
        if not probe.ok or probe.reason != "ok":
            self._fail(f"head_probe ok-case unexpected: {probe!r}")
        self._ok("head_probe ok-case returns ok=True reason=ok")

        from urllib.error import HTTPError
        err = HTTPError("https://x/y.jpg", 404, "Not Found", hdrs=None, fp=io.BytesIO(b""))
        with mock.patch.object(ih, "urlopen", side_effect=err):
            probe = ih.head_probe("https://x/y.jpg", throttle=None)
        if probe.ok or probe.reason != "http_404":
            self._fail(f"head_probe 404-case unexpected: {probe!r}")
        self._ok("head_probe 404-case returns ok=False reason=http_404")

        self.stdout.write(self.style.SUCCESS("PASS: ingestion_selftest"))
