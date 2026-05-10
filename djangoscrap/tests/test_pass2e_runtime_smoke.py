"""Pass-2E smoke tests for the runtime cleanup.

Pins the three failures observed live:
  1. Format leakage — model parroted `user:` / `anon:` / OP_ANCHOR labels
     because RECENT_TURNS was stuffed into a single user message.
  2. Within-generation looping — the same sentence repeated 6× in one reply.
  3. Generic assistant / motivational tone leaking from base RLHF.

These tests do NOT require MLX. They exercise the deterministic pieces:
prompt assembly, validator, stop-trim, build_request defaults.
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


# --- 1. Prompt assembly: no labels in the user-side --------------------------

def test_chat_messages_have_no_recent_turns_block_in_user_side():
    """The system prompt may name labels for documentation, but no user
    message should literally contain `user:` / `anon:` lines that the model
    can parrot."""
    from djangoscrap.views._characters import _build_system_prompt, _build_chat_messages
    from djangoscrap.imageboard_ingestion import anon_zoo

    variant = anon_zoo.pick_for_session(seed="t1")
    state = {"op_anchor": "trying to be disciplined", "session_summary": "",
             "recent_fragment_uids": []}
    sys_prompt = _build_system_prompt(
        persona_system="You are /fit/.", variant=variant, state=state, fragments=[],
    )

    class M:
        def __init__(self, role, text):
            self.role = role
            self.text = text

    history = [M("user", "first thing"), M("anon", "first reply")]
    msgs = _build_chat_messages(system_prompt=sys_prompt, history=history,
                                user_msg="like what")

    # No RECENT_TURNS / OP_ANCHOR / BOARD: scaffold inside any user/assistant message
    for m in msgs:
        if m["role"] != "system":
            assert "RECENT_TURNS" not in m["content"]
            assert "OP_ANCHOR:" not in m["content"]
            assert "BOARD: /fit/" not in m["content"]
            assert "MODE:" not in m["content"]


def test_chat_messages_alternate_user_assistant():
    from djangoscrap.views._characters import _build_chat_messages

    class M:
        def __init__(self, role, text):
            self.role = role
            self.text = text

    history = [
        M("user", "msg1"), M("anon", "rep1"),
        M("user", "msg2"), M("anon", "rep2"),
    ]
    msgs = _build_chat_messages(system_prompt="sys", history=history, user_msg="msg3")
    assert msgs[0]["role"] == "system"
    # Strict alternation after system: user, assistant, user, assistant, user
    expected_after_system = ["user", "assistant", "user", "assistant", "user"]
    actual = [m["role"] for m in msgs[1:]]
    assert actual == expected_after_system, f"got {actual}"


def test_chat_messages_history_anon_role_mapped_to_assistant():
    from djangoscrap.views._characters import _build_chat_messages

    class M:
        def __init__(self, role, text):
            self.role = role
            self.text = text

    history = [M("anon", "earlier reply")]
    msgs = _build_chat_messages(system_prompt="sys", history=history, user_msg="hi")
    assert msgs[1]["role"] == "assistant"
    assert "earlier reply" in msgs[1]["content"]


def test_system_prompt_contains_anti_assistant_rules():
    """Pass 2E hardened the system prompt; the rules block must be present."""
    from djangoscrap.views._characters import _build_system_prompt
    from djangoscrap.imageboard_ingestion import anon_zoo

    variant = anon_zoo.pick_for_session(seed="t1")
    sys = _build_system_prompt(
        persona_system="You are /fit/.", variant=variant,
        state={"op_anchor": "x"}, fragments=[],
    )
    # A few canonical phrases from _ANTI_ASSISTANT_RULES
    assert "Reply ONCE" in sys or "1 short post" in sys or "1-3 lines" in sys
    assert "no role label" in sys or "Do NOT write 'user:'" in sys
    # Anti motivational
    assert "you need to" in sys.lower() or "not say 'you need to'" in sys.lower()


# --- 2. build_request defaults --------------------------------------------

def test_build_request_chat_profile_attaches_default_stops(monkeypatch):
    monkeypatch.delenv("IMAGEBOARD_LM_REPETITION_PENALTY", raising=False)
    monkeypatch.delenv("IMAGEBOARD_LM_NO_REPEAT_NGRAM_SIZE", raising=False)
    from djangoscrap.imageboard_ingestion import local_fit_model
    req = local_fit_model.build_request(system="s", user="u", profile="chat")
    assert req.repetition_penalty == pytest.approx(1.10)
    assert req.no_repeat_ngram_size == 4
    # Chat default should include the canonical stop sequences
    stops = set(req.stop_sequences)
    assert "\nuser:" in stops
    assert "\nanon:" in stops
    assert "RECENT_TURNS:" in stops
    assert "OP_ANCHOR:" in stops


def test_build_request_thought_profile_no_default_stops():
    from djangoscrap.imageboard_ingestion import local_fit_model
    req = local_fit_model.build_request(system="s", user="u", profile="thought")
    # Stops are chat-specific so thoughts don't get them by default
    assert req.stop_sequences == ()


def test_env_overrides_repetition_controls(monkeypatch):
    monkeypatch.setenv("IMAGEBOARD_LM_REPETITION_PENALTY", "1.30")
    monkeypatch.setenv("IMAGEBOARD_LM_NO_REPEAT_NGRAM_SIZE", "6")
    from djangoscrap.imageboard_ingestion import local_fit_model
    req = local_fit_model.build_request(system="s", user="u", profile="chat")
    assert req.repetition_penalty == pytest.approx(1.30)
    assert req.no_repeat_ngram_size == 6


# --- 3. Stop-trim helper ---------------------------------------------------

def test_trim_at_stops_cuts_at_first_match():
    from djangoscrap.imageboard_ingestion.local_fit_model import _trim_at_stops
    text = "ok this is the reply\nuser: continued by mistake\nanon: more"
    out, matched = _trim_at_stops(text, ("\nuser:", "\nanon:"))
    assert out == "ok this is the reply"
    assert matched == "\nuser:"


def test_trim_at_stops_no_match():
    from djangoscrap.imageboard_ingestion.local_fit_model import _trim_at_stops
    out, matched = _trim_at_stops("clean reply", ("\nuser:",))
    assert out == "clean reply"
    assert matched == ""


# --- 4. Within-generation repetition validator ----------------------------

def test_within_generation_loop_caught():
    from djangoscrap.imageboard_ingestion import output_validator as ov
    bad = (
        "try eating clean and working out without excuses. "
        "try eating clean and working out without excuses. "
        "try eating clean and working out without excuses."
    )
    r = ov.validate(bad)
    assert r["ok"] is False
    assert "within_generation_repetition" in r["reasons"]
    # Retry instruction is specific
    assert "Do not repeat" in r["suggested_retry_instruction"]


def test_normal_reply_passes_validator():
    from djangoscrap.imageboard_ingestion import output_validator as ov
    r = ov.validate(">missed lifts again\nspreadsheet doesnt lie")
    assert r["ok"] is True


# --- 5. Format-leak + assistant-tone validator -----------------------------

def test_format_leak_user_label():
    from djangoscrap.imageboard_ingestion import output_validator as ov
    r = ov.validate("anon: try eating clean. user: ok ill try")
    assert r["ok"] is False
    assert "format_leak" in r["reasons"]


def test_format_leak_scaffold_token():
    from djangoscrap.imageboard_ingestion import output_validator as ov
    r = ov.validate("BOARD: /fit/\n>missed lifts again")
    assert r["ok"] is False
    assert "format_leak" in r["reasons"]


@pytest.mark.parametrize("text", [
    "you need to make a change anon",
    "it's time to make it happen",
    "you've got this, keep pushing",
    "i hope this helps",
])
def test_assistant_tone_rejected(text):
    from djangoscrap.imageboard_ingestion import output_validator as ov
    r = ov.validate(text)
    assert r["ok"] is False
    assert "assistant_tone" in r["reasons"]


# --- 6. Eval harness Pass 2E regression --------------------------------

def test_eval_includes_pass2e_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGEBOARD_LM_BACKEND", "template")
    from djangoscrap.imageboard_ingestion import eval_fit_voice

    summary = eval_fit_voice.run_eval(
        out_dir=tmp_path / "evals" / "p2e",
        backends_filter=["template"],
        skip_chat=True,
    )
    metrics = summary.get("mode_collapse_metrics") or {}
    assert "pass2e_regression" in metrics
    pb = metrics["pass2e_regression"].get("template") or {}
    for key in ("format_leak_count", "within_generation_loop_count", "assistant_tone_count"):
        assert key in pb


def test_eval_chat_cases_include_regressions():
    from djangoscrap.imageboard_ingestion import eval_fit_voice
    case_ids = {c["id"] for c in eval_fit_voice.CHAT_CASES}
    assert "regress_format_leak" in case_ids
    assert "regress_within_loop" in case_ids
    assert "regress_motivational_assistant" in case_ids
