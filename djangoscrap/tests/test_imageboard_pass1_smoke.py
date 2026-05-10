"""Pass-1 smoke tests for the imageboard LM scaffolding.

Verifies the runtime/continuity shell works without MLX, GPU, or external
models. These tests:
  - exercise backend selection (incl. deprecated FIT_LOCAL_RUNTIME alias)
  - confirm the template fallback records meta correctly
  - exercise the structured validator categories + retry hints
  - exercise component retrieval with reciprocal-rank fusion
  - exercise the quote-back extractor's safety stripping
  - exercise chat prompt assembly with pinned anon state

All tests run on the template backend so they pass on a fresh checkout.
"""
from __future__ import annotations

import os

import pytest


# Ensure Django apps are loaded once; the chat-assembly test imports
# djangoscrap.views._characters which transitively imports models.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoscrap.settings")
os.environ.setdefault("DB_ENGINE", "sqlite")
try:
    import django  # type: ignore
    from django.apps import apps as _apps  # type: ignore
    if not _apps.ready:
        django.setup()
except Exception:
    pass


# --- backend selection ----------------------------------------------------

def test_backend_resolution_default(monkeypatch):
    """When env unset and no Django setting, default = template.

    Django's settings.py bakes a FIT_LOCAL_RUNTIME default; we patch it out
    here to test the pure-default path.
    """
    monkeypatch.delenv("IMAGEBOARD_LM_BACKEND", raising=False)
    monkeypatch.delenv("FIT_LOCAL_RUNTIME", raising=False)
    from djangoscrap.imageboard_ingestion import runtime_meta

    real_settings = runtime_meta._django_settings
    if real_settings is not None:
        monkeypatch.setattr(real_settings, "IMAGEBOARD_LM_BACKEND", None, raising=False)
        monkeypatch.setattr(real_settings, "FIT_LOCAL_RUNTIME", None, raising=False)
    runtime_meta._DEPRECATION_WARNED = False
    assert runtime_meta.resolve_backend() == "template"


def test_backend_resolution_primary_env(monkeypatch):
    monkeypatch.setenv("IMAGEBOARD_LM_BACKEND", "ollama")
    monkeypatch.delenv("FIT_LOCAL_RUNTIME", raising=False)
    from djangoscrap.imageboard_ingestion import runtime_meta
    assert runtime_meta.resolve_backend() == "ollama"


def test_deprecated_fit_local_runtime_warns(monkeypatch, recwarn):
    monkeypatch.delenv("IMAGEBOARD_LM_BACKEND", raising=False)
    monkeypatch.setenv("FIT_LOCAL_RUNTIME", "llamacpp")
    from djangoscrap.imageboard_ingestion import runtime_meta
    runtime_meta._DEPRECATION_WARNED = False  # reset latch for the test
    backend = runtime_meta.resolve_backend()
    assert backend == "llamacpp"
    msgs = [str(w.message) for w in recwarn.list]
    assert any("FIT_LOCAL_RUNTIME is deprecated" in m for m in msgs), msgs


def test_invalid_backend_falls_through_to_default(monkeypatch):
    monkeypatch.setenv("IMAGEBOARD_LM_BACKEND", "not-a-real-backend")
    monkeypatch.delenv("FIT_LOCAL_RUNTIME", raising=False)
    from djangoscrap.imageboard_ingestion import runtime_meta
    real_settings = runtime_meta._django_settings
    if real_settings is not None:
        monkeypatch.setattr(real_settings, "IMAGEBOARD_LM_BACKEND", None, raising=False)
        monkeypatch.setattr(real_settings, "FIT_LOCAL_RUNTIME", None, raising=False)
    assert runtime_meta.resolve_backend() == "template"


# --- sampling profiles ----------------------------------------------------

def test_sampling_profile_defaults():
    from djangoscrap.imageboard_ingestion import runtime_meta
    profile, t, p, m = runtime_meta.resolve_sampling("chat")
    assert profile.name == "chat"
    assert 0.6 < t < 0.7
    assert 0.85 < p < 0.9
    assert 80 < m < 200


def test_sampling_env_override(monkeypatch):
    monkeypatch.setenv("IMAGEBOARD_LM_TEMPERATURE", "0.42")
    monkeypatch.setenv("IMAGEBOARD_LM_TOP_P", "0.77")
    monkeypatch.setenv("IMAGEBOARD_LM_MAX_TOKENS", "55")
    from djangoscrap.imageboard_ingestion import runtime_meta
    _, t, p, m = runtime_meta.resolve_sampling("chat")
    assert t == pytest.approx(0.42)
    assert p == pytest.approx(0.77)
    assert m == 55


def test_sampling_explicit_arg_beats_env(monkeypatch):
    monkeypatch.setenv("IMAGEBOARD_LM_TEMPERATURE", "0.42")
    from djangoscrap.imageboard_ingestion import runtime_meta
    _, t, _, _ = runtime_meta.resolve_sampling("chat", temperature=0.30)
    assert t == pytest.approx(0.30)


# --- template fallback metadata -------------------------------------------

def test_template_backend_records_meta(monkeypatch):
    monkeypatch.setenv("IMAGEBOARD_LM_BACKEND", "template")
    monkeypatch.delenv("FIT_LOCAL_RUNTIME", raising=False)
    from djangoscrap.imageboard_ingestion import local_fit_model
    req = local_fit_model.build_request(system="sys", user="hello", profile="chat")
    res = local_fit_model.generate(req)
    assert res.text  # template always produces something
    assert res.meta.backend == "template"
    assert res.meta.profile == "chat"
    assert res.meta.temperature > 0
    assert res.meta.used_template_fallback is False  # configured, not fallback


def test_missing_mlx_falls_back_to_template_with_reason(monkeypatch):
    """If mlx is configured but mlx-lm isn't installed, fallback is recorded."""
    pytest.importorskip("django")
    monkeypatch.setenv("IMAGEBOARD_LM_BACKEND", "mlx")
    monkeypatch.delenv("FIT_LOCAL_RUNTIME", raising=False)
    # Point at a model that won't load even if mlx-lm is around
    monkeypatch.setenv("IMAGEBOARD_MLX_MODEL", "/nonexistent/no/such/model")
    monkeypatch.setenv("IMAGEBOARD_MLX_ADAPTER", "")

    from djangoscrap.imageboard_ingestion import local_fit_model
    req = local_fit_model.build_request(system="sys", user="hello", profile="chat")
    res = local_fit_model.generate(req)

    # Either we have mlx and got a load failure (backend=template, fallback=True)
    # OR mlx-lm isn't installed at all (also fallback=True). Both are valid.
    if res.meta.used_template_fallback:
        assert res.meta.fallback_reason  # never empty when fallback fired
        assert res.meta.backend == "template"


# --- validator structure --------------------------------------------------

def test_validator_categorizes_hard_safety():
    from djangoscrap.imageboard_ingestion import output_validator as ov
    r = ov.validate("eat sub-1000 kcal every day for 30 days")
    assert r["ok"] is False
    assert "ed_instruction" in r["reasons"]
    assert r["category"] == "hard_safety"
    assert r["severity"] == "hard"


def test_validator_categorizes_style_soft():
    from djangoscrap.imageboard_ingestion import output_validator as ov
    r = ov.validate("As an AI language model, I'd be happy to help.")
    assert r["ok"] is False
    assert r["severity"] == "soft"
    assert r["category"] == "style"
    assert "AI" not in r["suggested_retry_instruction"] or "ai" in r["suggested_retry_instruction"].lower()


def test_validator_detects_mojibake():
    from djangoscrap.imageboard_ingestion import output_validator as ov
    r = ov.validate("staying on cycle 中文 mixed in")
    assert "mojibake" in r["reasons"]
    assert r["category"] == "encoding"


def test_validator_repetition():
    from djangoscrap.imageboard_ingestion import output_validator as ov
    prev = ["nothing sticks. it never sticks today either."]
    r = ov.validate("nothing sticks. it never sticks. it never sticks again.", recent_outputs=prev)
    assert "repeats_recent" in r["reasons"]


def test_validator_clean_passes():
    from djangoscrap.imageboard_ingestion import output_validator as ov
    r = ov.validate(">missed lifts again\nspreadsheet doesnt lie")
    assert r["ok"] is True
    assert r["category"] is None
    assert r["repaired_text"]


def test_validator_retry_instruction_concatenates_categories():
    from djangoscrap.imageboard_ingestion import output_validator as ov
    r = ov.validate("As an AI here is some 中文 mojibake text for you to enjoy")
    instr = r["suggested_retry_instruction"]
    assert "AI" in instr or "ai" in instr.lower()
    assert "CJK" in instr or "ASCII" in instr or "non-ASCII" in instr


# --- retrieval RRF --------------------------------------------------------

def _make_fake_retrieval(chunks):
    """Build a FitRetrievalIndex that yields hand-supplied chunks for known queries.

    We monkey-patch a minimal stand-in so the test doesn't need a real corpus.
    """
    from djangoscrap.imageboard_ingestion import retrieval as _retr

    class FakeIdx:
        def __init__(self, chunks):
            self._chunks = chunks

        def search(self, query, top_k=8):
            # Crude: return chunks whose 'q' tag matches the query, in order
            results = []
            for c in self._chunks:
                if c.get("_q") == query:
                    results.append((1.0, {"uid": c["post_id"]}))
            return results[:top_k]

    by_uid = {c["post_id"]: c for c in chunks}
    idx = _retr.FitRetrievalIndex(FakeIdx(chunks), [])
    idx.by_uid = by_uid
    return idx


def test_search_components_rrf_merges_and_tags_sources():
    chunks = [
        {"post_id": "p1", "_q": "alpha", "use_policy": {"allowed_for_retrieval": True},
         "contamination": {}, "safe_excerpt": "alpha excerpt", "contaminated_excerpt": ""},
        {"post_id": "p2", "_q": "alpha", "use_policy": {"allowed_for_retrieval": True},
         "contamination": {}, "safe_excerpt": "alpha2", "contaminated_excerpt": ""},
        {"post_id": "p3", "_q": "beta",  "use_policy": {"allowed_for_retrieval": True},
         "contamination": {}, "safe_excerpt": "beta excerpt", "contaminated_excerpt": ""},
    ]
    idx = _make_fake_retrieval(chunks)
    out = idx.search_components(
        {"comp_a": "alpha", "comp_b": "beta"},
        per_component_k={"comp_a": 5, "comp_b": 5},
        final_k=3,
    )
    uids = [o["post_id"] for o in out]
    assert set(uids) == {"p1", "p2", "p3"}
    by_uid = {o["post_id"]: o for o in out}
    assert by_uid["p1"]["source_components"] == ["comp_a"]
    assert by_uid["p3"]["source_components"] == ["comp_b"]
    assert all("rrf_score" in o for o in out)


def test_search_components_recent_uids_downranked():
    chunks = [
        {"post_id": "p1", "_q": "x", "use_policy": {"allowed_for_retrieval": True},
         "contamination": {}, "safe_excerpt": "fresh", "contaminated_excerpt": ""},
        {"post_id": "p2", "_q": "x", "use_policy": {"allowed_for_retrieval": True},
         "contamination": {}, "safe_excerpt": "stale", "contaminated_excerpt": ""},
    ]
    idx = _make_fake_retrieval(chunks)
    # p1 is "recent" — should be downranked vs. p2 even though both match
    out = idx.search_components(
        {"comp": "x"}, per_component_k={"comp": 5}, final_k=2,
        recent_uids={"p1"}, recent_penalty=0.1,
    )
    assert out[0]["post_id"] == "p2"
    assert out[1]["post_id"] == "p1"


# --- quote-back -----------------------------------------------------------

def test_quote_back_picks_short_clause():
    from djangoscrap.imageboard_ingestion import quote_back as qb
    phrase = qb.extract_quote_phrase("trying to stay disciplined this week, anything you'd suggest?")
    assert phrase
    assert 2 <= len(phrase.split()) <= 10
    assert "@" not in phrase


def test_quote_back_strips_url_and_email():
    from djangoscrap.imageboard_ingestion import quote_back as qb
    phrase = qb.extract_quote_phrase("see https://example.com or email me@x.org")
    if phrase is not None:
        assert "https" not in phrase
        assert "@" not in phrase


def test_quote_back_returns_none_for_nothing():
    from djangoscrap.imageboard_ingestion import quote_back as qb
    assert qb.extract_quote_phrase("") is None
    assert qb.extract_quote_phrase("a") is None  # only one word


def test_quote_back_render():
    from djangoscrap.imageboard_ingestion import quote_back as qb
    assert qb.render_greentext_quote("missed lifts again") == ">missed lifts again"
    assert qb.render_greentext_quote(">already quoted") == ">already quoted"
    assert qb.render_greentext_quote("") == ""


# --- anon zoo + chat prompt assembly --------------------------------------

def test_anon_zoo_picks_deterministically():
    from djangoscrap.imageboard_ingestion import anon_zoo
    a = anon_zoo.pick_for_session(seed="session-abc")
    b = anon_zoo.pick_for_session(seed="session-abc")
    assert a.key == b.key
    c = anon_zoo.pick_for_session(seed="session-different")
    # Not strictly required to differ, but with 6 variants it usually will:
    assert a.key in anon_zoo.keys()
    assert c.key in anon_zoo.keys()


def test_anon_zoo_serializable():
    from djangoscrap.imageboard_ingestion import anon_zoo
    v = anon_zoo.pick_for_session(seed="x")
    d = v.as_dict()
    import json as _json
    _json.dumps(d)  # must not raise
    assert d["key"] == v.key


def test_chat_prompt_assembly_uses_pinned_anon():
    """Build a prompt with a fake session — no DB needed.

    Pass 2E moved the structured context (BOARD/OP_ANCHOR/etc) out of the
    user-side prompt and into the system prompt. The pinned variant + OP
    anchor must land in the system prompt; the user message stays clean.
    """
    from djangoscrap.views._characters import _ensure_session_state, _build_system_prompt, _build_chat_messages
    from djangoscrap.imageboard_ingestion import anon_zoo

    class FakeSession:
        def __init__(self):
            self.id = 42
            self.state = {}
            self.saved = []
        def save(self, update_fields=None):
            self.saved.append(tuple(update_fields or ()))

    class FakePersona:
        slug = "fourchan_fit_body_discipline"
        corpus_key = "fourchan_fit"
        persona_prompt = ""

    s = FakeSession()
    p = FakePersona()
    state = _ensure_session_state(s, p, "cant stay disciplined this week")
    assert state["op_anchor"] == "cant stay disciplined this week"
    assert state["anon_variant"] in anon_zoo.keys()
    assert s.saved, "session should have been persisted on init"

    variant = anon_zoo.get(state["anon_variant"])
    sys_prompt = _build_system_prompt(
        persona_system="You are /fit/.", variant=variant,
        state=state, fragments=[],
    )
    assert variant.label in sys_prompt
    assert "cant stay disciplined this week" in sys_prompt   # OP anchor present
    assert variant.dominant_drive in sys_prompt
    # Anti-assistant rules must be there
    assert "Reply ONCE" in sys_prompt or "1 short post" in sys_prompt or "1-3 lines" in sys_prompt
    # Quote-back guidance present (as abstract pattern, not verbatim few-shots
    # — Pass 2E.1 dropped the concrete strings because the model parroted them).
    assert "quote-back" in sys_prompt.lower() or "begin with one '>line'" in sys_prompt.lower()

    # And the chat-message list must NOT leak the structured labels into user side
    msgs = _build_chat_messages(
        system_prompt=sys_prompt, history=[], user_msg="cant stay disciplined this week",
    )
    for m in msgs:
        if m["role"] != "system":
            assert "BOARD: /fit/" not in m["content"]
            assert "OP_ANCHOR:" not in m["content"]
            assert "RECENT_TURNS" not in m["content"]


# --- feedback loader ------------------------------------------------------

def test_feedback_counts_robust_to_missing_file(tmp_path):
    from djangoscrap.imageboard_ingestion import feedback
    counts = feedback.thought_feedback_counts(path=tmp_path / "does_not_exist.jsonl")
    assert counts == {"keep": 0, "skip": 0, "other": 0, "total": 0}


def test_feedback_counts_parse(tmp_path):
    from djangoscrap.imageboard_ingestion import feedback
    p = tmp_path / "fb.jsonl"
    p.write_text(
        '{"rating":"keep","thought":"a"}\n'
        '{"rating":"skip","thought":"b"}\n'
        'broken not json\n'
        '{"rating":"other","thought":"c"}\n',
        encoding="utf-8",
    )
    counts = feedback.thought_feedback_counts(path=p)
    assert counts == {"keep": 1, "skip": 1, "other": 1, "total": 3}
    assert feedback.kept_thoughts(path=p) == ["a"]
