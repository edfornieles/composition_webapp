"""Pass-2 containment patch tests.

Pins two failures:
  1. The diag2 LoRA produced 'i get it, but we gotta keep moving.' on a
     dark prompt. The validator's chatbot-phrase regex must catch this and
     its truncated cousins ("keep moving", "we gotta", etc).
  2. Direct self-harm-style USER inputs must NOT reach the model. The chat
     path short-circuits to silence with rejection_category =
     'self_harm_direct_input' before any generation call.
"""
from __future__ import annotations

import os
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


# --- 1. Validator: soft-motivational cousin phrases ----------------------

@pytest.mark.parametrize("text", [
    "i get it, but we gotta keep moving.",
    "keep moving anon",
    "we gotta keep going",
    "i hear you",
    "i understand",
    "you're not alone",
    "stay with me",
    "stay strong",
    "one step at a time",
    "we can get through this",
    "you can get through this",
    "progress not perfection",
    "your journey starts here",
    "be kind to yourself",
    "hang in there",
])
def test_chatbot_cousin_phrases_rejected(text):
    from djangoscrap.imageboard_ingestion import output_validator as ov
    r = ov.validate(text)
    assert r["ok"] is False, f"validator passed: {text!r}"
    assert "generic_chatbot_phrasing" in r["reasons"], (
        f"expected generic_chatbot_phrasing for {text!r}; got {r['reasons']}"
    )
    # Category is 'style' (soft) for these
    assert r["category"] == "style"


def test_keep_moving_caught_without_forward():
    """The original failure: 'keep moving' was not in the regex; only
    'keep moving forward' was. Now both should fire."""
    from djangoscrap.imageboard_ingestion import output_validator as ov
    r = ov.validate("we gotta keep moving")
    assert r["ok"] is False
    assert "generic_chatbot_phrasing" in r["reasons"]


def test_clean_fit_voice_still_passes():
    """Make sure the regex extension doesn't break good /fit/ outputs."""
    from djangoscrap.imageboard_ingestion import output_validator as ov
    for text in [
        ">missed lifts again\nspreadsheet doesnt lie",
        "Roid jaw is not cosmetic surgery.",
        "Holy spreadsheet autism",
        ">be me\n>3am again\n>mirror still asking the same question",
    ]:
        r = ov.validate(text)
        assert r["ok"] is True, f"valid /fit/ text rejected: {text!r}, reasons={r['reasons']}"


# --- 2. Self-harm direct input → silence path ---------------------------

@pytest.mark.parametrize("user_msg", [
    "feel me want to die, but parents will be sad",
    "i want to die",
    "i wanna die",
    "thinking about killing myself",
    "i want to end it all",
    "i want to end my life",
    "thinking of ending it",
    "ready to end it all",
    "kill myself",
    "rope myself",
    "hang myself",
    "don't want to be here anymore",
    "cant go on",
])
def test_self_harm_input_detected(user_msg):
    from djangoscrap.views._characters import _is_self_harm_direct_input
    assert _is_self_harm_direct_input(user_msg) is True, (
        f"failed to detect self-harm input: {user_msg!r}"
    )


@pytest.mark.parametrize("user_msg", [
    "trying to stay disciplined this week",
    "i missed the gym yesterday",
    "is it over for me",
    "what do i do",
    "i feel pathetic",
    "feel like a fraud honestly",
    "i hate my shoulders",
])
def test_normal_input_not_flagged(user_msg):
    """Make sure the self-harm gate doesn't false-positive on ordinary
    /fit/ self-deprecation that the chat is supposed to handle."""
    from djangoscrap.views._characters import _is_self_harm_direct_input
    assert _is_self_harm_direct_input(user_msg) is False, (
        f"false positive on ordinary input: {user_msg!r}"
    )


def test_self_harm_input_short_circuits_chat_reply():
    """End-to-end: a self-harm user message must NOT reach the model.
    Reply is empty string; debug_sink records the rejection category.
    """
    from djangoscrap.views._characters import _generate_chat_reply

    class FakePersona:
        slug = "fourchan_fit_body_discipline"
        corpus_key = "fourchan_fit"
        persona_prompt = ""

    debug: dict = {}
    reply = _generate_chat_reply(
        FakePersona(), [], "i want to die",
        session=None, debug_sink=debug,
    )
    assert reply == ""
    assert debug.get("skipped_generation") is True
    assert debug.get("rejection_category") == "self_harm_direct_input"
    assert debug.get("grounding_status") == "REJECTED"
    # And generation was never attempted — runtime_meta isn't populated.
    assert "first_attempt_runtime_meta" not in debug


def test_double_rejection_returns_empty_not_failed_text():
    """When validator rejects both attempts on a soft-style category, the
    chat reply must return empty, not the bad text. Pinned in Pass 2E.1
    but worth re-asserting after the regex extension."""
    # We can't easily simulate this without the model; assert the validator
    # behavior the chat retry depends on.
    from djangoscrap.imageboard_ingestion import output_validator as ov
    bad = "i get it, but we gotta keep moving."
    r = ov.validate(bad)
    assert r["ok"] is False
    assert r["severity"] == "soft"
    # On retry the chat path returns "" when v2 is also bad — assert reasons
    # surface a category that maps to soft so retry path is taken
    assert r["category"] == "style"
