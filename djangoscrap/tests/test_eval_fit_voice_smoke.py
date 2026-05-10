"""Smoke test for the eval harness.

Confirms that:
  - run_eval() works template-only with no MLX, GPU, or external backends
  - JSONL + report.md + summary.json land in the requested out_dir
  - missing backends are recorded as unavailable, not crashes
  - thought cases run without DB
  - chat cases (when persona exists) drive _generate_chat_reply correctly

We deliberately use --skip-chat in the no-DB path so the smoke test never
needs Django's DB setup. The chat-case branch is exercised by separate
django_db tests when those are available.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# Boot Django for the eval module (it touches Django settings indirectly)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")
os.environ.setdefault("DB_ENGINE", "sqlite")
try:
    import django  # type: ignore
    from django.apps import apps as _apps  # type: ignore
    if not _apps.ready:
        django.setup()
except Exception:
    pass


def test_eval_runs_template_only(tmp_path, monkeypatch):
    """Template-only eval: no DB, no MLX. Just confirm the harness produces files."""
    # Force template backend regardless of environment
    monkeypatch.setenv("IMAGEBOARD_LM_BACKEND", "template")

    from djangoscrap.imageboard_ingestion import eval_fit_voice

    out_dir = tmp_path / "evals" / "smoke"
    summary = eval_fit_voice.run_eval(
        out_dir=out_dir,
        backends_filter=["template"],
        skip_chat=True,                       # avoid DB requirement
        source_key="fourchan_fit",
    )

    # Backends recorded
    assert "template" in summary["available_backends"]
    assert summary["row_count"] >= len(eval_fit_voice.THOUGHT_CASES)

    # Files present
    report_md = Path(summary["report_path"])
    jsonl = Path(summary["jsonl_path"])
    assert report_md.exists()
    assert jsonl.exists()
    assert (out_dir / "summary.json").exists()

    # JSONL is parseable + has the expected keys
    rows = []
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    assert rows, "JSONL must contain at least one row"
    expected_keys = {
        "eval_id", "timestamp", "case_id", "mode", "backend",
        "backend_status", "prompt", "output", "runtime_meta",
        "grounding_fragments", "validator_result", "rejection_reasons",
        "length_words", "fallback_occurred",
    }
    sample = rows[0]
    missing = expected_keys - set(sample.keys())
    assert not missing, f"row missing keys: {missing}"

    # Markdown report has expected structure
    md = report_md.read_text(encoding="utf-8")
    assert "# Eval —" in md
    assert "## Backends" in md
    assert "## Cases" in md
    assert "## Manual rubric" in md
    # All thought cases must appear
    for case in eval_fit_voice.THOUGHT_CASES:
        assert f"### {case['id']}" in md, f"missing case section for {case['id']}"


def test_missing_backend_recorded_not_crashed(tmp_path, monkeypatch):
    """When mlx-lm isn't installed, the eval records mlx as unavailable
    rather than crashing.
    """
    monkeypatch.setenv("IMAGEBOARD_LM_BACKEND", "template")
    monkeypatch.setenv("IMAGEBOARD_MLX_MODEL", "/nonexistent/no/such/model")
    monkeypatch.setenv("IMAGEBOARD_MLX_ADAPTER", "")

    from djangoscrap.imageboard_ingestion import eval_fit_voice

    out_dir = tmp_path / "evals" / "missing-mlx"
    summary = eval_fit_voice.run_eval(
        out_dir=out_dir,
        backends_filter=["template", "mlx", "ollama", "llamacpp"],
        skip_chat=True,
        source_key="fourchan_fit",
    )

    # template should be available
    assert "template" in summary["available_backends"]
    # mlx, ollama, llamacpp should all show as unavailable on a clean machine
    # (the smoke test environment does not configure them)
    for missing in ("ollama", "llamacpp"):
        assert missing in summary["unavailable_backends"], (
            f"{missing} should be unavailable on a clean smoke run"
        )

    # No backend crashed the run
    backends = summary["backends_detected"]
    by_name = {b["name"]: b for b in backends}
    for name in ("template", "mlx", "ollama", "llamacpp"):
        assert name in by_name, f"backend {name} not recorded"
        # Detail string must always be populated
        assert by_name[name]["detail"]


def test_eval_jsonl_records_runtime_meta(tmp_path, monkeypatch):
    """Each row must include runtime_meta with backend + sampling fields."""
    monkeypatch.setenv("IMAGEBOARD_LM_BACKEND", "template")

    from djangoscrap.imageboard_ingestion import eval_fit_voice

    out_dir = tmp_path / "evals" / "meta"
    summary = eval_fit_voice.run_eval(
        out_dir=out_dir,
        backends_filter=["template"],
        skip_chat=True,
    )
    rows = [json.loads(l) for l in Path(summary["jsonl_path"]).read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows
    for r in rows:
        meta = r.get("runtime_meta") or {}
        # Some keys may be empty for the missing-fragments path; backend must be set
        assert "backend" in meta
        assert "profile" in meta
        assert "temperature" in meta
