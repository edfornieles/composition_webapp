"""
Tests for the Adapter framework (djangoscrap.ingestion_adapters).

Covers:
* AdapterResult.ok semantics.
* Registry insertion, replacement on re-register, priority ordering.
* pick_for / dispatch behavior: highest-priority match wins; fall
  through on empty/error results; enabled_adapters feature-flag.
* Reference PassthroughAdapter can_handle + extract.

Each test wipes the registry before running so we never pick up
adapters registered by other test modules or production code.
"""

from __future__ import annotations

import pytest

from djangoscrap.ingestion_adapters import (
    Adapter,
    AdapterResult,
    register,
    unregister,
    pick_for,
    list_registered,
)
from djangoscrap.ingestion_adapters import registry as reg
from djangoscrap.ingestion_adapters.passthrough import PassthroughAdapter


@pytest.fixture(autouse=True)
def _clean_registry():
    """Wipe the registry before and after each test for isolation."""
    reg._REGISTRY.clear()
    yield
    reg._REGISTRY.clear()


# --------------------------------------------------------------------------- #
#  Minimal fake adapters for testing                                          #
# --------------------------------------------------------------------------- #


class _FakeAdapter:
    def __init__(self, name: str, priority: int, handles_hosts: tuple[str, ...] = (), urls=None, error=None):
        self.name = name
        self.priority = priority
        self._handles_hosts = tuple(h.lower() for h in handles_hosts)
        self._urls = urls if urls is not None else [f"https://out.example.com/{name}.jpg"]
        self._error = error

    def can_handle(self, url: str) -> bool:
        if not self._handles_hosts:
            return True
        low = url.lower()
        return any(h in low for h in self._handles_hosts)

    def extract(self, url: str, *, max_urls: int, state: dict) -> AdapterResult:
        return AdapterResult(
            urls=list(self._urls[:max_urls]),
            next_state={"last_url": url},
            diagnostics={"who": self.name},
            error=self._error,
        )


# --------------------------------------------------------------------------- #
#  AdapterResult                                                              #
# --------------------------------------------------------------------------- #


def test_adapter_result_ok_requires_urls_and_no_error():
    assert AdapterResult(urls=["u"]).ok is True
    assert AdapterResult(urls=[]).ok is False
    assert AdapterResult(urls=["u"], error="boom").ok is False


# --------------------------------------------------------------------------- #
#  Registry                                                                   #
# --------------------------------------------------------------------------- #


def test_register_adds_and_orders_by_priority():
    a = _FakeAdapter("low", priority=10)
    b = _FakeAdapter("high", priority=100)
    register(a)
    register(b)
    items = list_registered()
    assert [i.name for i in items] == ["high", "low"]


def test_re_register_replaces_existing():
    register(_FakeAdapter("x", priority=10))
    register(_FakeAdapter("x", priority=99))
    items = list_registered()
    assert len(items) == 1
    assert items[0].priority == 99


def test_unregister_removes_by_name():
    register(_FakeAdapter("alpha", priority=50))
    register(_FakeAdapter("beta", priority=50))
    assert unregister("alpha") is True
    assert unregister("ghost") is False
    assert [i.name for i in list_registered()] == ["beta"]


# --------------------------------------------------------------------------- #
#  pick_for                                                                   #
# --------------------------------------------------------------------------- #


def test_pick_for_returns_matches_in_priority_order():
    register(_FakeAdapter("generic", priority=50))
    register(_FakeAdapter("specific", priority=100, handles_hosts=("pinimg",)))
    matches = pick_for("https://i.pinimg.com/originals/x.jpg")
    assert [a.name for a in matches] == ["specific", "generic"]


def test_pick_for_empty_when_none_handle():
    register(_FakeAdapter("tightly-scoped", priority=100, handles_hosts=("pinimg",)))
    matches = pick_for("https://other.example.com/x.jpg")
    assert matches == []


# --------------------------------------------------------------------------- #
#  Dispatcher                                                                 #
# --------------------------------------------------------------------------- #


def test_dispatch_picks_highest_priority_adapter():
    generic = _FakeAdapter("generic", priority=50, urls=["https://out/generic.jpg"])
    specific = _FakeAdapter("specific", priority=100, handles_hosts=("pinimg",),
                             urls=["https://out/specific.jpg"])
    register(generic)
    register(specific)
    adapter, result = reg.dispatch(
        "https://i.pinimg.com/originals/x.jpg", max_urls=5, state={}
    )
    assert adapter.name == "specific"
    assert result.urls == ["https://out/specific.jpg"]


def test_dispatch_falls_through_empty_result():
    """If the high-priority adapter returns empty URLs, the dispatcher tries the next."""
    empty_specific = _FakeAdapter("empty-specific", priority=100, urls=[])  # returns empty
    fallback = _FakeAdapter("fallback", priority=50, urls=["https://out/fb.jpg"])
    register(empty_specific)
    register(fallback)
    adapter, result = reg.dispatch("https://anywhere.example.com/", max_urls=5, state={})
    assert adapter is not None and adapter.name == "fallback"
    assert result.urls == ["https://out/fb.jpg"]


def test_dispatch_falls_through_error_result():
    err = _FakeAdapter("err", priority=100, error="network down",
                        urls=["https://out/ignored.jpg"])
    fallback = _FakeAdapter("fb", priority=50, urls=["https://out/fb.jpg"])
    register(err)
    register(fallback)
    adapter, result = reg.dispatch("https://anywhere/", max_urls=5, state={})
    assert adapter.name == "fb"
    assert result.urls == ["https://out/fb.jpg"]


def test_dispatch_enabled_adapters_flag_skips_disabled():
    """enabled_adapters set forces a specific name; others are skipped."""
    a = _FakeAdapter("A", priority=100, urls=["https://out/a.jpg"])
    b = _FakeAdapter("B", priority=50, urls=["https://out/b.jpg"])
    register(a)
    register(b)
    adapter, result = reg.dispatch(
        "https://anywhere/", max_urls=5, state={}, enabled_adapters={"B"}
    )
    assert adapter.name == "B"


def test_dispatch_no_match_returns_none_tuple():
    adapter, result = reg.dispatch("https://zero/", max_urls=5, state={})
    assert adapter is None and result is None


# --------------------------------------------------------------------------- #
#  PassthroughAdapter                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://cdn.example.com/foo.jpg", True),
        ("https://cdn.example.com/foo.PNG", True),
        ("https://cdn.example.com/clip.mp4", True),
        ("https://cdn.example.com/photo.jpg?size=large", True),  # query preserved
        ("https://cdn.example.com/page.html", False),
        ("ftp://cdn.example.com/foo.jpg", False),
        ("", False),
    ],
)
def test_passthrough_can_handle(url, expected):
    assert PassthroughAdapter().can_handle(url) is expected


def test_passthrough_extract_echoes_url():
    adapter = PassthroughAdapter()
    result = adapter.extract(
        "https://cdn.example.com/foo.jpg", max_urls=10, state={}
    )
    assert result.urls == ["https://cdn.example.com/foo.jpg"]
    assert result.diagnostics == {"source": "direct_link"}
    assert result.ok is True


# --------------------------------------------------------------------------- #
#  Protocol conformance                                                       #
# --------------------------------------------------------------------------- #


def test_passthrough_conforms_to_adapter_protocol():
    """isinstance check against the runtime-checkable Adapter protocol."""
    assert isinstance(PassthroughAdapter(), Adapter)
