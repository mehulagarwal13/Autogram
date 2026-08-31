"""
Tests for `profile_repository.write_back_stable_answer` (spec §13) — a human
answer to a stable contact/professional-link question also lands in the
candidate's profile, but a sensitive/compliance-adjacent one never does, even
though `FieldMapper.map_field` can technically resolve to it too.

DB-touching calls are exercised against a `MagicMock` session — same
"no live Postgres needed" approach `tests/test_answer_cache_repository.py`
uses. `CandidateProfile` itself is a plain in-memory instance (a SQLAlchemy
model is just a Python object until added to a session), per
`tests/test_profile_repository_helpers.py`.
"""

from unittest.mock import MagicMock

import pytest

from app.models.db_models import CandidateProfile
from app.services.profile_repository import write_back_stable_answer


def _blank_profile() -> CandidateProfile:
    return CandidateProfile(profile_id="p1", user_id="u1")


def _db_with_profile(profile: CandidateProfile) -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = profile
    return db


@pytest.mark.parametrize("question,value,attribute", [
    ("What is your phone number?", "+1-555-0100", "phone"),
    ("What city do you live in?", "Austin", "city"),
    ("LinkedIn profile", "https://linkedin.com/in/ada", "linkedin_url"),
    ("Portfolio URL", "https://ada.dev", "portfolio_url"),
    ("What is your postal code?", "78701", "postal_code"),
])
def test_writes_back_a_stable_contact_fact(question, value, attribute):
    profile = _blank_profile()
    db = _db_with_profile(profile)

    result = write_back_stable_answer(db, "u1", question, value)

    assert result is True
    stored = profile.phone_encrypted if attribute == "phone" else getattr(profile, attribute)
    if attribute == "phone":
        from app.core.crypto import decrypt_field
        assert decrypt_field(stored) == value
    else:
        assert stored == value


@pytest.mark.parametrize("question", [
    "What is your visa status?",
    "Do you require sponsorship?",
    "Are you willing to complete a background check?",
    "What is your expected salary?",
    "Are you willing to relocate?",
    "Do you have an active security clearance?",
])
def test_never_writes_back_a_compliance_or_screening_fact(question):
    """These resolve through the same `FieldMapper.map_field` classifier, but
    must never silently become a permanent profile value from one answered
    question — this codebase treats them as per-application, human-reviewed
    facts elsewhere (tri-state, "never asked" by default)."""
    profile = _blank_profile()
    db = _db_with_profile(profile)

    result = write_back_stable_answer(db, "u1", question, "Yes")

    assert result is False
    db.commit.assert_not_called()


def test_returns_false_when_nothing_maps_to_a_profile_attribute():
    profile = _blank_profile()
    db = _db_with_profile(profile)

    result = write_back_stable_answer(db, "u1", "Why do you want to work here?", "Because I love the mission.")

    assert result is False
    db.commit.assert_not_called()


def test_returns_false_when_the_user_has_no_profile_yet():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    result = write_back_stable_answer(db, "u1", "What city do you live in?", "Austin")

    assert result is False
    db.commit.assert_not_called()


def test_returns_false_for_a_blank_value():
    profile = _blank_profile()
    db = _db_with_profile(profile)

    result = write_back_stable_answer(db, "u1", "What city do you live in?", "   ")

    assert result is False
    db.commit.assert_not_called()


def test_a_failure_writing_back_is_swallowed_not_raised():
    """A missed write-back is a convenience lost, not a correctness problem —
    it must never break the caller's own HITL response."""
    profile = _blank_profile()
    db = _db_with_profile(profile)
    db.commit.side_effect = RuntimeError("db unavailable")

    result = write_back_stable_answer(db, "u1", "What city do you live in?", "Austin")

    assert result is False
