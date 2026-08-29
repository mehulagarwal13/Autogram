"""
Tests for application_question_repository.py — the per-application
screening-question ledger (HITL platform). DB-touching calls are exercised
against a `MagicMock` session, same convention as
tests/test_application_repository.py and tests/test_answer_cache_repository.py.
"""

from unittest.mock import MagicMock

import pytest

from app.services.application_question_repository import (
    apply_review,
    record_question,
    summarize_for_application,
)


# ---------- record_question / confidence bucketing ----------

def test_record_question_buckets_profile_source_as_high_confidence_and_auto_filled():
    db = MagicMock()
    record_question(db, "app-1", question_text="What is your notice period?", source="profile", confidence=0.9)

    added = db.add.call_args[0][0]
    assert added.confidence_level == "HIGH"
    assert added.review_status == "auto_filled"
    db.commit.assert_called_once()


def test_record_question_buckets_low_llm_confidence_as_low_and_pending_review():
    db = MagicMock()
    record_question(db, "app-1", question_text="Why do you want to work here?", source="llm", confidence=0.3)

    added = db.add.call_args[0][0]
    assert added.confidence_level == "LOW"
    assert added.review_status == "pending_review"


def test_record_question_buckets_needs_user_input_as_low_regardless_of_confidence():
    """A demographic question the candidate never answered — zero confidence,
    empty answer, always surfaced for a human (see answer_engine.py's
    SOURCE_NEEDS_USER_INPUT)."""
    db = MagicMock()
    record_question(
        db, "app-1", question_text="Are you legally authorized to work in the United States?",
        source="needs_user_input", confidence=0.0, available_options=["Yes", "No"],
    )

    added = db.add.call_args[0][0]
    assert added.confidence_level == "LOW"
    assert added.review_status == "pending_review"
    assert added.answer is None
    assert added.available_options == ["Yes", "No"]


def test_record_question_buckets_mid_llm_confidence_as_medium():
    db = MagicMock()
    record_question(db, "app-1", question_text="Describe a challenging project.", source="llm", confidence=0.7)

    added = db.add.call_args[0][0]
    assert added.confidence_level == "MEDIUM"
    assert added.review_status == "auto_filled"


# ---------- apply_review ----------

def test_apply_review_approve_marks_approved_and_stamps_reviewed_at():
    db = MagicMock()
    question = MagicMock(review_status="pending_review", reviewed_at=None)

    result = apply_review(db, question, action="approve")

    assert question.review_status == "approved"
    assert question.reviewed_at is not None
    db.commit.assert_called_once()
    assert result is question


def test_apply_review_edit_requires_a_non_empty_answer():
    db = MagicMock()
    question = MagicMock()

    with pytest.raises(ValueError):
        apply_review(db, question, action="edit", answer="   ")
    db.commit.assert_not_called()


def test_apply_review_edit_stores_the_human_answer_without_touching_the_original():
    db = MagicMock()
    question = MagicMock(answer="Maybe", human_answer=None)

    apply_review(db, question, action="edit", answer="Yes, I am authorized.")

    assert question.human_answer == "Yes, I am authorized."
    assert question.answer == "Maybe"  # untouched — this is what automation actually produced
    assert question.review_status == "edited"


def test_apply_review_reject_marks_rejected():
    db = MagicMock()
    question = MagicMock()

    apply_review(db, question, action="reject")

    assert question.review_status == "rejected"


def test_apply_review_rejects_an_unknown_action():
    db = MagicMock()
    question = MagicMock()

    with pytest.raises(ValueError):
        apply_review(db, question, action="approve_forever")


# ---------- summarize_for_application ----------

def test_summarize_for_application_counts_answered_generated_and_reviewed(monkeypatch):
    import app.services.application_question_repository as repo

    questions = [
        MagicMock(question_text="Q1", answer="30 days", human_answer=None, source="profile",
                   confidence_level="HIGH", review_status="auto_filled"),
        MagicMock(question_text="Q2", answer="Because I love the mission.", human_answer=None, source="llm",
                   confidence_level="MEDIUM", review_status="auto_filled"),
        MagicMock(question_text="Q3", answer=None, human_answer=None, source="needs_user_input",
                   confidence_level="LOW", review_status="pending_review"),
        MagicMock(question_text="Q4", answer=None, human_answer="Yes", source="llm",
                   confidence_level="LOW", review_status="edited"),
    ]
    monkeypatch.setattr(repo, "list_for_application", lambda db, aid: questions)

    summary = summarize_for_application(MagicMock(), "app-1")

    assert summary["questions_total"] == 4
    assert summary["questions_answered"] == 3  # Q1, Q2, Q4 (via human_answer)
    assert summary["questions_generated"] == 2  # Q2, Q4 — source == "llm"
    assert summary["questions_human_reviewed"] == 1  # Q4 — edited
    assert summary["missing_fields"] == ["Q3"]
    assert summary["risky_answers"] == ["Q3"]  # Q4 is LOW but already edited — resolved, not risky
