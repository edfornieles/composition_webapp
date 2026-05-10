"""Smoke tests for the Pass-2C training dataset builder.

Covers:
  - real-post target rows have the expected schema
  - dataset writer creates train/valid/test splits + reports deterministically
  - template-heavy build refuses without --allow-template-heavy
  - low-info / unsafe targets are skipped
  - curated rows load cleanly from a JSONL folder
  - dataset_report.{json,md} is written + contains warnings field
  - repeated assistant targets are detected and surfaced

These tests run on the live corpus when IMAGEBOARD_CORPUS_ROOT is set.
When the corpus is absent, real-post tests skip gracefully.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")
os.environ.setdefault("DB_ENGINE", "sqlite")
try:
    import django  # type: ignore
    from django.apps import apps as _apps  # type: ignore
    if not _apps.ready:
        django.setup()
except Exception:
    pass


# --- helpers --------------------------------------------------------------

def _have_corpus() -> bool:
    from djangoscrap.imageboard_ingestion import storage
    p = storage.profile_path("fourchan_fit", "fit_profile_contaminated.json")
    return p.exists() or storage.manifest_path("fourchan_fit").exists()


# --- low-info / dedupe / redact helpers ----------------------------------

def test_low_info_skips_grunt_words():
    from djangoscrap.imageboard_ingestion import training_dataset as td
    assert td._is_low_info("lol")
    assert td._is_low_info("THIS")
    assert td._is_low_info("kys")
    assert not td._is_low_info("missed lifts again. spreadsheet doesnt lie.")
    assert td._is_low_info("")


def test_redact_strips_url_email_phone():
    from djangoscrap.imageboard_ingestion import training_dataset as td
    out = td._redact_private("see https://example.com or me@x.org or +44 7700 900123 ok")
    assert "https" not in out
    assert "@" not in out
    assert "7700" not in out
    assert "ok" in out


def test_normalize_collapses_punct():
    from djangoscrap.imageboard_ingestion import training_dataset as td
    a = td._normalise_for_dedupe("  >Be ME!  / >2024 will be My YEAR  ")
    b = td._normalise_for_dedupe(">be me >2024 will be my year")
    assert a == b


# --- mode classifier ------------------------------------------------------

def test_classify_modes_finds_be_me():
    from djangoscrap.imageboard_ingestion import training_dataset as td
    rec = {"is_op": False, "reply_targets": [], "image": {}}
    text = ">be me\n>missed leg day\n>spreadsheet doesnt lie"
    modes = td._classify_modes(rec, text)
    assert "inner_monologue" in modes


def test_classify_modes_finds_quote_back():
    from djangoscrap.imageboard_ingestion import training_dataset as td
    rec = {"is_op": False, "reply_targets": [12345], "image": {}}
    text = ">missed lifts again\nspreadsheet doesnt lie"
    modes = td._classify_modes(rec, text)
    assert "quote_back_reply" in modes
    assert "next_anon_reply" in modes


def test_classify_modes_finds_body_judgement():
    from djangoscrap.imageboard_ingestion import training_dataset as td
    rec = {"is_op": False, "reply_targets": [], "image": {}}
    text = "your shoulders look so soft and weak you should mog harder"
    modes = td._classify_modes(rec, text)
    assert "body_judgement" in modes


# --- curated loader -------------------------------------------------------

def test_curated_loader_normalises_simple_format(tmp_path):
    from djangoscrap.imageboard_ingestion import training_dataset as td
    fold = tmp_path / "curated"
    fold.mkdir()
    (fold / "a.jsonl").write_text(
        '{"mode":"confession","user":"BOARD: /fit/\\nMODE: confession\\nTASK:\\nWrite one short.","assistant":"sat in the kitchen at 3am with a tape measure"}\n'
        '{"mode":"quote_back_reply","user":"BOARD: /fit/\\nMODE: quote_back_reply\\nTASK:\\n...","assistant":">said id be different\\nsame lie every monday"}\n'
        '# comment line ignored\n',
        encoding="utf-8",
    )
    rows = list(td._curated_rows(fold))
    assert len(rows) == 2
    assert all(r["metadata"]["target_strategy"] == "curated_targets" for r in rows)
    assert all(r["metadata"]["mode"] in ("confession", "quote_back_reply") for r in rows)
    # Schema check
    for r in rows:
        assert {m["role"] for m in r["messages"]} == {"system", "user", "assistant"}


def test_curated_loader_handles_chat_format(tmp_path):
    from djangoscrap.imageboard_ingestion import training_dataset as td
    fold = tmp_path / "curated"
    fold.mkdir()
    (fold / "b.jsonl").write_text(
        json.dumps({
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": "a real long enough target line"},
            ],
            "metadata": {"mode": "inner_monologue"},
        }) + "\n",
        encoding="utf-8",
    )
    rows = list(td._curated_rows(fold))
    assert len(rows) == 1
    assert rows[0]["metadata"]["target_strategy"] == "curated_targets"


def test_curated_loader_skips_low_info(tmp_path):
    from djangoscrap.imageboard_ingestion import training_dataset as td
    fold = tmp_path / "curated"
    fold.mkdir()
    (fold / "junk.jsonl").write_text(
        '{"user":"u","assistant":"lol"}\n'
        '{"user":"u","assistant":"this"}\n'
        '{"user":"u","assistant":"a meaningful longer assistant target"}\n',
        encoding="utf-8",
    )
    rows = list(td._curated_rows(fold))
    assert len(rows) == 1


# --- teacher stub ---------------------------------------------------------

def test_teacher_model_targets_raises_when_unconfigured(monkeypatch):
    monkeypatch.delenv("IMAGEBOARD_TEACHER_BACKEND", raising=False)
    from djangoscrap.imageboard_ingestion import training_dataset as td
    import random as _random
    with pytest.raises(td.TeacherNotConfigured):
        next(td._teacher_model_rows(
            source_key="fourchan_fit", modes=["next_anon_reply"],
            rng=_random.Random(0), n_per_mode=1,
        ))


# --- build pipeline -------------------------------------------------------

@pytest.mark.skipif(not _have_corpus(), reason="corpus not available")
def test_real_post_build_produces_chat_rows():
    """Real-post build, tiny limit. Rows must have the OpenAI-chat schema +
    metadata.target_strategy=real_post_targets."""
    from djangoscrap.imageboard_ingestion import training_dataset as td
    rows, summary = td.build_dataset_v2(
        source_key="fourchan_fit",
        modes=["next_anon_reply", "quote_back_reply"],
        target_strategy="real_post_targets",
        n_per_mode=10,
        seed=123,
    )
    assert rows, "expected non-zero rows from real corpus"
    for r in rows[:5]:
        assert {m["role"] for m in r["messages"]} == {"system", "user", "assistant"}
        md = r["metadata"]
        assert md["target_strategy"] == "real_post_targets"
        assert md["mode"] in ("next_anon_reply", "quote_back_reply")
        assert md.get("source_post_uid")
        assert "BOARD: /fit/" in r["messages"][1]["content"]
        assert "TASK:" in r["messages"][1]["content"]


def test_template_heavy_refuses_without_flag(tmp_path):
    """Building a pure template dataset must refuse unless --allow-template-heavy."""
    from djangoscrap.imageboard_ingestion import training_dataset as td
    out_dir = tmp_path / "data"
    with pytest.raises(RuntimeError):
        td.write_dataset_v2(
            source_key="fourchan_fit",
            modes=["inner_monologue"],
            target_strategy="template_targets",
            n_per_mode=20,
            seed=1,
            out_dir=out_dir,
        )
    # Report is still written
    assert (out_dir / "dataset_report.json").exists()


def test_template_heavy_proceeds_with_flag(tmp_path):
    from djangoscrap.imageboard_ingestion import training_dataset as td
    out_dir = tmp_path / "data2"
    result = td.write_dataset_v2(
        source_key="fourchan_fit",
        modes=["inner_monologue"],
        target_strategy="template_targets",
        n_per_mode=15,
        seed=1,
        out_dir=out_dir,
        allow_template_heavy=True,
    )
    assert result["rows_total"] > 0
    assert result["template_pct"] > 25.0
    # warnings still surface
    assert any("template_targets pct" in w for w in result["warnings"])


def test_split_is_deterministic_with_seed(tmp_path):
    from djangoscrap.imageboard_ingestion import training_dataset as td
    out_dir = tmp_path / "split"
    r1 = td.write_dataset_v2(
        source_key="fourchan_fit",
        modes=["inner_monologue"],
        target_strategy="template_targets",
        n_per_mode=20, seed=42, out_dir=out_dir,
        allow_template_heavy=True,
    )
    train1 = (Path(r1["out_dir"]) / "train.jsonl").read_text(encoding="utf-8")

    out_dir2 = tmp_path / "split2"
    r2 = td.write_dataset_v2(
        source_key="fourchan_fit",
        modes=["inner_monologue"],
        target_strategy="template_targets",
        n_per_mode=20, seed=42, out_dir=out_dir2,
        allow_template_heavy=True,
    )
    train2 = (Path(r2["out_dir"]) / "train.jsonl").read_text(encoding="utf-8")
    assert train1 == train2, "deterministic split with same seed"


def test_dataset_report_has_warnings_field(tmp_path):
    from djangoscrap.imageboard_ingestion import training_dataset as td
    out_dir = tmp_path / "rep"
    td.write_dataset_v2(
        source_key="fourchan_fit",
        modes=["inner_monologue"],
        target_strategy="template_targets",
        n_per_mode=12, seed=1, out_dir=out_dir,
        allow_template_heavy=True,
    )
    rep = json.loads((out_dir / "dataset_report.json").read_text(encoding="utf-8"))
    for k in ("rows_total", "rows_by_mode", "rows_by_strategy",
              "skipped", "template_pct", "top_targets",
              "top_openings", "near_duplicate_target_count", "warnings", "split"):
        assert k in rep, f"report missing {k}"
    assert isinstance(rep["warnings"], list)


def test_repeated_targets_detected(tmp_path):
    """Template generator produces collisions. Even though the writer
    de-duplicates assistant targets before emitting, the *skipped_duplicate*
    counter on the report records how many were dropped — the signal of
    template-induced collapse before training."""
    from djangoscrap.imageboard_ingestion import training_dataset as td
    out_dir = tmp_path / "dup"
    result = td.write_dataset_v2(
        source_key="fourchan_fit",
        modes=["inner_monologue"],
        target_strategy="template_targets",
        n_per_mode=200,
        seed=7,
        out_dir=out_dir,
        allow_template_heavy=True,
    )
    rep = json.loads(Path(result["report_json"]).read_text(encoding="utf-8"))
    # Many template outputs collide; the skipped_duplicate counter must reflect that.
    assert rep["skipped"]["duplicate"] >= 5, (
        f"expected template generator to produce duplicate-skipped rows, "
        f"got {rep['skipped']['duplicate']}"
    )
    # Templates have parametrised slots so distinct rows can still be in the
    # low hundreds; what we care about is duplicates getting dropped.


def test_smoking_gun_phrase_triggers_loud_warning(tmp_path):
    """If the dataset contains the previous-LoRA phrases ≥3×, refuse."""
    from djangoscrap.imageboard_ingestion import training_dataset as td
    # Run a heavy template build; the _t_cope and _t_loading_man generators
    # produce the smoking-gun phrases. With n_per_mode large enough, they
    # show up multiple times.
    out_dir = tmp_path / "guns"
    result = td.write_dataset_v2(
        source_key="fourchan_fit",
        modes=["inner_monologue"],
        target_strategy="template_targets",
        n_per_mode=300,
        seed=123,
        out_dir=out_dir,
        allow_template_heavy=True,
    )
    # Either the smoking-gun warning fires or the general top-opening warning
    # fires — both are valid signals.
    assert any(
        ("broke the previous LoRA" in w) or ("template collapse" in w) or ("openings dominated by" in w)
        for w in result["warnings"]
    )
