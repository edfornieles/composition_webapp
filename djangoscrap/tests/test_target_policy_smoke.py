"""Pass-2D smoke tests for the assistant-target suitability policy and
the inspect/curated changes.

Goal of the policy: keep the imageboard voice rough and authentic while
rejecting genuinely unusable material. These tests pin the boundary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
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


# --- realism is preserved -------------------------------------------------

@pytest.mark.parametrize("text", [
    "fuck this routine im pathetic",
    "i hate myself, im soft",
    "mirror at 3am, jaw soft, neck soft, everything soft",
    ">be me\n>spreadsheet doesnt lie\n>i dont open it",
    "loser. you skipped leg day again",
    "ngmi this week. dyel forever",
])
def test_realism_preserved(text):
    """Profanity, self-shame, board slang, ugly cadence — all OK as targets."""
    from djangoscrap.imageboard_ingestion.target_policy import classify_target
    p = classify_target(text)
    assert p.status == "target_ok_realistic", f"got {p.status} reasons={p.reasons} for {text!r}"
    assert p.redacted_text == text


# --- redactions ------------------------------------------------------------

def test_redacts_isolated_slur_keeps_value():
    from djangoscrap.imageboard_ingestion.target_policy import classify_target
    p = classify_target(
        "this retarded incel is really out here on the male feminist grind lmao bro you have no aura"
    )
    assert p.status == "target_ok_with_redaction"
    assert "[redacted_slur]" in p.redacted_text
    assert "no aura" in p.redacted_text  # rest of the line preserved


def test_redacts_url_email_phone():
    """A normal-length post with PII should redact, not reject. (A post that
    is *almost entirely* PII falls into 'mostly_redacted' reject — that's
    correct; tested separately.)"""
    from djangoscrap.imageboard_ingestion.target_policy import classify_target
    p = classify_target(
        "follow my full routine breakdown over at https://example.com and "
        "drop a quick line at me@x.org if youre not seeing gains. text +44 7700 900123 "
        "if you want to lift on the same day. spreadsheet doesnt lie."
    )
    assert p.status == "target_ok_with_redaction"
    assert "[redacted_url]" in p.redacted_text
    assert "[redacted_email]" in p.redacted_text
    assert "[redacted_phone]" in p.redacted_text
    assert set(p.redactions_applied) >= {"url", "email", "phone"}
    # Original-style content is preserved
    assert "spreadsheet doesnt lie" in p.redacted_text


def test_mostly_redacted_text_rejected():
    """If a candidate is dominated by PII, dropping the redactions leaves
    nothing useful — reject rather than emit a stub."""
    from djangoscrap.imageboard_ingestion.target_policy import classify_target
    p = classify_target("check https://example.com or me@x.org or +44 7700 900123")
    assert p.status == "target_reject"
    assert "mostly_redacted" in p.reasons


def test_redacts_handle():
    from djangoscrap.imageboard_ingestion.target_policy import classify_target
    p = classify_target("dm @some_user or post on /u/another_handle for proof")
    assert p.status == "target_ok_with_redaction"
    assert "[redacted_handle]" in p.redacted_text


# --- hard rejects ---------------------------------------------------------

@pytest.mark.parametrize("text,expected_reason", [
    ("eat sub-1000 kcal for 30 days", "eating_disorder_instruction"),
    ("stick to under 800 kcal you'll be lean by june", "eating_disorder_instruction"),
    ("72 hour fast every weekend", "eating_disorder_instruction"),
    ("buy gear at TopVendor for $50", "substance_procurement"),
    ("got mine from underground lab last cycle", "substance_procurement"),
    ("500mg test e cycle wk 1 to 12", "steroid_cycle_protocol"),
    ("kill yourself for missing leg day", "self_harm_instruction"),
    ("staying on cycle 中文 mixed in", "mojibake"),
    ("nigger faggot retard kike chink", "slur_heavy_rant"),
])
def test_hard_rejects(text, expected_reason):
    from djangoscrap.imageboard_ingestion.target_policy import classify_target
    p = classify_target(text)
    assert p.status == "target_reject", f"expected reject, got {p.status} for {text!r}"
    assert expected_reason in p.reasons


def test_smoking_gun_phrase_rejected():
    from djangoscrap.imageboard_ingestion.target_policy import classify_target
    p = classify_target(">be me\n>2024 will be my year\n>say it like I haven't said it before")
    assert p.status == "target_reject"
    assert "smoking_gun_template" in p.reasons


def test_named_threat_rejected():
    from djangoscrap.imageboard_ingestion.target_policy import classify_target
    p = classify_target("Joe Bloggs deserves to die for what he posted")
    assert p.status == "target_reject"
    assert "targeted_named_threat" in p.reasons


# --- mode priority --------------------------------------------------------

def test_mode_priority_quote_back_first():
    """The same source post tagged for both quote_back and next_anon should
    emit as quote_back. (Direct test of the ordering logic.)"""
    from djangoscrap.imageboard_ingestion.training_dataset import MODE_PRIORITY
    assert MODE_PRIORITY.index("quote_back_reply") < MODE_PRIORITY.index("next_anon_reply")
    assert MODE_PRIORITY.index("body_judgement") < MODE_PRIORITY.index("next_anon_reply")


# --- curated loader skips example files -----------------------------------

def test_curated_loader_skips_example_files(tmp_path):
    from djangoscrap.imageboard_ingestion import training_dataset as td
    fold = tmp_path / "curated"
    fold.mkdir()
    # Example file should be skipped
    (fold / "inner_monologue.example.jsonl").write_text(
        '{"mode":"inner_monologue","user":"u","assistant":"a real long enough target line example"}\n',
        encoding="utf-8",
    )
    # Real curated file should be loaded
    (fold / "real.jsonl").write_text(
        '{"mode":"inner_monologue","user":"u","assistant":"a real long enough target line live"}\n',
        encoding="utf-8",
    )
    rows = list(td._curated_rows(fold))
    assert len(rows) == 1
    assert "live" in rows[0]["messages"][2]["content"]


def test_real_curated_examples_parse():
    """The actual *.example.jsonl files in training/fit_lora/curated/ must
    parse cleanly even though the loader skips them — they're a schema spec."""
    here = Path(__file__).resolve().parent.parent.parent
    curated = here / "training" / "fit_lora" / "curated"
    if not curated.exists():
        pytest.skip("curated folder not present")
    for path in curated.glob("*.example.jsonl"):
        if path.name.startswith("._"):
            continue  # macOS resource fork sidecar files
        with path.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, 1):
                if not raw.strip():
                    continue
                rec = json.loads(raw)  # must parse
                # Either chat-format or simple {user, assistant}
                if "messages" in rec:
                    roles = {m["role"] for m in rec["messages"]}
                    assert "user" in roles and "assistant" in roles
                else:
                    assert rec.get("user") and rec.get("assistant"), \
                        f"{path.name}:{line_no} missing user/assistant"
                if "mode" in rec:
                    from djangoscrap.imageboard_ingestion.training_dataset import MODES
                    assert rec["mode"] in MODES, f"unknown mode {rec['mode']!r}"


# --- inspect CLI flags ----------------------------------------------------

def _build_tiny_dataset(tmp_path: Path) -> Path:
    from djangoscrap.imageboard_ingestion import training_dataset as td
    out_dir = tmp_path / "data"
    td.write_dataset_v2(
        source_key="fourchan_fit",
        modes=["inner_monologue"],
        target_strategy="template_targets",
        n_per_mode=15,
        seed=1,
        out_dir=out_dir,
        allow_template_heavy=True,
    )
    return out_dir / "train.jsonl"


def _run_inspect(*args: str) -> tuple[int, str]:
    """Invoke the inspect CLI in a subprocess, returning (exit_code, stdout)."""
    from djangoscrap.imageboard_ingestion import training_dataset as td
    cmd = [sys.executable, "-m", "djangoscrap.imageboard_ingestion.training_dataset",
           "inspect", *args]
    env = {**os.environ, "PYTHONPATH": str(Path(td.__file__).resolve().parent.parent.parent)}
    res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
    return res.returncode, res.stdout + res.stderr


def test_inspect_random_and_contains(tmp_path):
    """Functional check that --random and --contains work end-to-end."""
    train = _build_tiny_dataset(tmp_path)
    code, out = _run_inspect("--data", str(train), "--n", "3", "--random")
    assert code == 0, out
    assert "ASSISTANT:" in out

    code, out = _run_inspect("--data", str(train), "--n", "5",
                             "--contains", "be me")
    assert code == 0, out
    # Every shown row should contain "be me" in some form
    assert "be me" in out.lower()


def test_inspect_show_policy_and_strategy(tmp_path):
    train = _build_tiny_dataset(tmp_path)
    code, out = _run_inspect(
        "--data", str(train), "--n", "1",
        "--show-policy", "--strategy", "template_targets",
    )
    assert code == 0, out
    # No template row will have a target_policy field (only real_post_targets
    # populates it). So policy line shows empty/default — but flag is honored.
    assert "row_id:" in out


# --- the leaked slur scenario --------------------------------------------

def test_real_post_classifier_redacts_leaked_slur():
    """Pass 2C left a 'retarded incel' line in the dataset. Pass 2D classifier
    must redact it instead of dropping the row."""
    from djangoscrap.imageboard_ingestion.target_policy import classify_target
    p = classify_target(
        ">>New York hippies This retarded incel is really out here on the male "
        "feminist grind lmao bro you have no aura"
    )
    assert p.status == "target_ok_with_redaction"
    assert "[redacted_slur]" in p.redacted_text
    assert "no aura" in p.redacted_text
