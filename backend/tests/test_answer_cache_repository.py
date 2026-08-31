"""
Tests for answer_cache_repository.py: `normalize_question`/`compute_question_hash`'s
insensitivity (this is what makes trivially-reworded repeat questions hit the
same cache entry) and the get/save upsert logic. DB-touching calls are
exercised against a `MagicMock` session — same "no live Postgres needed"
approach `tests/test_application_repository.py` uses.
"""

from unittest.mock import MagicMock

import pytest

from app.services.answer_cache_repository import (
    SEMANTIC_SIMILARITY_THRESHOLD,
    compute_question_hash,
    find_similar_answer,
    get_cached_answer,
    normalize_question,
    save_answer,
)


# ---------- normalize_question / compute_question_hash ----------

def test_normalize_question_is_case_insensitive():
    assert normalize_question("What is your Notice Period?") == normalize_question("what is your notice period?")


def test_normalize_question_ignores_punctuation():
    assert normalize_question("What's your notice period?") == normalize_question("whats your notice period")


def test_normalize_question_collapses_whitespace():
    assert normalize_question("  What   is\tyour notice period?  ") == normalize_question("What is your notice period?")


def test_compute_question_hash_is_deterministic():
    q = "Do you require visa sponsorship?"
    assert compute_question_hash(q) == compute_question_hash(q)


def test_compute_question_hash_matches_across_trivial_rewording():
    a = compute_question_hash("Do you require visa sponsorship?")
    b = compute_question_hash("do you require visa sponsorship")
    assert a == b


def test_compute_question_hash_differs_for_different_questions():
    a = compute_question_hash("What is your notice period?")
    b = compute_question_hash("What is your expected salary?")
    assert a != b


# ---------- get_cached_answer ----------

def test_get_cached_answer_returns_none_on_miss():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    result = get_cached_answer(db, "user-1", "What is your notice period?")

    assert result is None


def test_get_cached_answer_returns_the_matching_row():
    db = MagicMock()
    fake_entry = MagicMock(answer="30 days", confidence=0.9)
    db.query.return_value.filter.return_value.first.return_value = fake_entry

    result = get_cached_answer(db, "user-1", "What is your notice period?")

    assert result is fake_entry


# ---------- save_answer ----------

def test_save_answer_inserts_a_new_row_when_nothing_cached_yet():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    save_answer(db, "user-1", "What is your notice period?", answer="30 days", source="deterministic", confidence=0.9)

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert added.user_id == "user-1"
    assert added.answer == "30 days"
    assert added.source == "deterministic"
    assert added.confidence == 0.9
    assert added.question_hash == compute_question_hash("What is your notice period?")
    db.commit.assert_called_once()


def test_save_answer_overwrites_an_existing_cached_row_instead_of_duplicating():
    db = MagicMock()
    existing = MagicMock(answer="old answer", source="llm", confidence=0.6)
    db.query.return_value.filter.return_value.first.return_value = existing

    save_answer(db, "user-1", "What is your notice period?", answer="45 days", source="deterministic", confidence=0.9)

    db.add.assert_not_called()  # updated in place, not inserted again
    assert existing.answer == "45 days"
    assert existing.source == "deterministic"
    assert existing.confidence == 0.9
    db.commit.assert_called_once()


def test_save_answer_computes_and_stores_an_embedding():
    """HITL platform: every save also embeds the question text, so a later,
    differently-worded question can find it via `find_similar_answer` — the
    real (local, free) embedding model, not mocked, same as the rest of the
    codebase's embedding calls."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    save_answer(db, "user-1", "What is your notice period?", answer="30 days", source="deterministic", confidence=0.9)

    added = db.add.call_args[0][0]
    assert added.embedding_vector is not None
    assert len(added.embedding_vector) == 384


def test_save_answer_degrades_to_no_embedding_when_embedding_generation_fails(monkeypatch):
    import app.services.answer_cache_repository as repo

    monkeypatch.setattr(repo, "generate_embedding", lambda text: (_ for _ in ()).throw(RuntimeError("model unavailable")))
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    # Must not raise — a broken embedding call degrades to exact-match-only
    # for this row rather than blocking the (otherwise good) answer save.
    save_answer(db, "user-1", "What is your notice period?", answer="30 days", source="deterministic", confidence=0.9)

    added = db.add.call_args[0][0]
    assert added.embedding_vector is None
    db.commit.assert_called_once()


# ---------- save_answer: secret-prompt guard (spec §12 / non-negotiable #9) ----------

@pytest.mark.parametrize("question", [
    "Enter the verification code we sent you",
    "What is your one-time password?",
    "Please enter your OTP",
    "Enter the 6-digit security code",
    "Two-factor authentication code",
    "Complete the CAPTCHA below",
    "I'm not a robot",
    "Enter your password to continue",
])
def test_save_answer_refuses_to_cache_a_secret_shaped_question(question):
    """Defense-in-depth: the real OTP protection lives entirely in
    `TaskHandle.pending_secret` and never reaches this cache — this is the
    backstop for a misclassified secret prompt reaching `save_answer` at all."""
    db = MagicMock()

    result = save_answer(db, "user-1", question, answer="123456", source="llm", confidence=0.9)

    assert result is None
    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.parametrize("question", [
    "What is your postal code?",
    "What's your employee code?",
    "Reference code for your application",
    "What is your notice period?",
])
def test_save_answer_still_caches_an_ordinary_question_that_merely_says_code(question):
    """The guard must not over-match on the bare word 'code' — a zip/employee/
    reference code is an ordinary screening question, not a secret prompt."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    save_answer(db, "user-1", question, answer="12345", source="deterministic", confidence=0.9)

    db.add.assert_called_once()
    db.commit.assert_called_once()


# ---------- find_similar_answer ----------

def test_find_similar_answer_returns_none_when_nothing_clears_the_threshold():
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
        MagicMock(answer="30 days"), SEMANTIC_SIMILARITY_THRESHOLD - 0.1,
    )

    result = find_similar_answer(db, "user-1", "How soon could you start a new role?")

    assert result is None


def test_find_similar_answer_returns_the_match_above_the_threshold():
    db = MagicMock()
    fake_entry = MagicMock(answer="30 days")
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
        fake_entry, SEMANTIC_SIMILARITY_THRESHOLD + 0.05,
    )

    result = find_similar_answer(db, "user-1", "How soon could you start a new role?")

    assert result is fake_entry


def test_find_similar_answer_returns_none_when_the_user_has_nothing_cached():
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

    result = find_similar_answer(db, "user-1", "How soon could you start a new role?")

    assert result is None


def test_find_similar_answer_degrades_to_none_on_a_broken_query(monkeypatch):
    db = MagicMock()
    db.query.side_effect = RuntimeError("vector index unavailable")

    # A broken semantic lookup must never block answering — it just means
    # this particular attempt falls through to the LLM path, same as any
    # other cache miss.
    result = find_similar_answer(db, "user-1", "How soon could you start a new role?")

    assert result is None
