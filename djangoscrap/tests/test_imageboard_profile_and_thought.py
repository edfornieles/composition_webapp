"""Profile + grounding tests using a small synthetic manifest written to a temp dir."""
import json
from pathlib import Path

import pytest

from djangoscrap.imageboard_ingestion import (
    adapters,
    embeddings,
    profile_builder,
    retrieval,
    storage,
    thought_grounding,
)


@pytest.fixture
def temp_corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "CORPUS_ROOT", tmp_path / "imageboards")
    return "test__fit"


def _fake_post(no: int, com: str) -> dict:
    return {"no": no, "com": com, "time": 1700000000 + no, "name": "Anon"}


def _seed_manifest(source_key: str, posts: list[tuple[int, str]]):
    storage.ensure_layout(source_key)
    for i, (no, com) in enumerate(posts):
        rec = adapters.build_record(
            source_key=source_key, board="fit", thread_no=42,
            post=_fake_post(no, com), is_op=(i == 0),
        )
        storage.append_manifest(source_key, rec)


def test_build_record_tags_and_safety(temp_corpus):
    rec = adapters.build_record(
        source_key=temp_corpus, board="fit", thread_no=1,
        post=_fake_post(101, "Did my push pull legs routine. Felt disgusted in the mirror."),
        is_op=True,
    )
    assert rec["post_uid"].endswith(":fit:101")
    assert "discipline_routine" in rec["fit_tags"]
    assert "self_loathing" in rec["fit_tags"]
    assert rec["model_safe_text"]  # safe content survives


def test_build_record_high_risk_blanks_model_safe_text(temp_corpus):
    rec = adapters.build_record(
        source_key=temp_corpus, board="fit", thread_no=1,
        post=_fake_post(102, "Anyone got a source for tren in EU"),
        is_op=False,
    )
    assert rec["safety"]["likely_steroid_sourcing"] is True
    assert rec["model_safe_text"] == ""


def test_profile_counts_drives_and_excludes_unsafe(temp_corpus):
    _seed_manifest(temp_corpus, [
        (1, "discipline every day, the streak is everything"),
        (2, "I am disgusted in the mirror but I will be reborn"),
        (3, "where to buy gear cheap"),  # excluded from safe corpus
        (4, "PR today, finally proud, mirror check made me happy"),
    ])
    profile = profile_builder.build_profile(temp_corpus)
    assert profile["stats"]["total_posts"] == 4
    assert profile["stats"]["model_safe_posts"] == 3
    drive_labels = {d["label"] for d in profile["core_drives"]}
    assert "discipline" in drive_labels or "transformation" in drive_labels
    obj_labels = {o["label"] for o in profile["recurring_objects"]}
    assert "mirror" in obj_labels


def test_grounded_thought_requires_min_fragments(temp_corpus):
    _seed_manifest(temp_corpus, [
        (1, "the mirror does not lie about the work I have skipped"),
        (2, "discipline is the only voice I trust at five in the morning"),
    ])
    profile_builder.write_profile(temp_corpus, mode="contaminated_art")
    ret = retrieval.build_retrieval(temp_corpus, mode="contaminated_art")
    retrieval.save(ret, temp_corpus)
    text, debug = thought_grounding.generate_fit_grounded_thought(
        source_key=temp_corpus, seed_query="mirror discipline"
    )
    assert text is None
    assert debug["error"] in ("insufficient_fragments",)


def test_grounded_thought_succeeds_with_enough_corpus(temp_corpus):
    _seed_manifest(temp_corpus, [
        (1, "the mirror does not lie about the work I have skipped today"),
        (2, "discipline is the only voice I trust at five in the morning when the gym is empty"),
        (3, "I look at the mirror and I am disgusted, then I do another set"),
        (4, "every day the same routine, every day the streak grows"),
        (5, "transformation is slow, the mirror notices before anyone else"),
        (6, "the kitchen at midnight is a courtroom and the fridge is the judge"),
        (7, "every craving has my voice but worse and louder"),
        (8, "be me / mirror / the same verdict every time"),
    ])
    profile_builder.write_profile(temp_corpus, mode="contaminated_art")
    ret = retrieval.build_retrieval(temp_corpus, mode="contaminated_art")
    retrieval.save(ret, temp_corpus)
    out = thought_grounding.generate_fit_be_me_thought(
        current_context={"object": "mirror", "place": "bathroom"},
        source_key=temp_corpus,
        contamination_intensity=0.8,
        require_grounding=True,
    )
    assert out["thought"] is not None, out
    assert out["grounding_status"] == "OK"
    assert len(out["internal_grounding"]) >= thought_grounding.MIN_FRAGMENTS
    # Output should look like greentext be-me
    assert out["thought"].lstrip().startswith(">")
