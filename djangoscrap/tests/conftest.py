"""
Shared pytest fixtures for the ingestion test suite.

This file intentionally does the absolute minimum needed for the ingestion
modules to be testable without depending on Django's full ORM machinery.
Specifically:

* ``clean_url_rewriter`` resets the rewriter's in-memory registry around
  each test so JSON overlay tests don't leak state into rewriter tests.
* ``tmp_ingestion_root`` gives each test its own scratch directory so we
  never touch the real MEDIA_ROOT.
* ``mock_http`` enables ``responses`` globally so any accidental live
  HTTP call fails loudly instead of silently hitting the network.

We do NOT spin up a full Django DB by default — all ingestion-module
tests are pure-Python against explicit inputs. Tests that need the ORM
use the ``django_db`` marker explicitly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure the project root is on sys.path so ``import djangoscrap`` works
# regardless of where pytest was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Django needs a settings module before any app code imports.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")


@pytest.fixture
def tmp_ingestion_root(tmp_path):
    """Per-test scratch directory standing in for INGESTION_MEDIA_ROOT."""
    root = tmp_path / "ingestion_items"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def clean_url_rewriter(monkeypatch):
    """
    Wipe the rewriter's JSON overlay path so tests can load their own
    overlay without picking up a developer's local ingestion_site_rules.json.
    """
    monkeypatch.setenv("INGESTION_SITE_RULES", "/nonexistent/no-such-file.json")
    from djangoscrap import ingestion_url_rewriter as rw
    rw.reload_rules()
    yield rw
    # Restore builtins-only state after the test.
    rw.reload_rules()


@pytest.fixture
def responses_mock():
    """Shortcut: a RequestsMock session with passthrough disabled."""
    import responses as _responses
    with _responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps
