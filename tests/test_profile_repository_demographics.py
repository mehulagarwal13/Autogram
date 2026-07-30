"""
Phase 8 additions to app/services/profile_repository.py:

- the four new compliance columns on CandidateProfile (work_authorized,
  requires_sponsorship, visa_type, sponsorship_countries) round-trip through
  `_apply_profile_fields`/`profile_to_dict` the same way every other plain
  field already does (see tests/test_profile_repository_helpers.py) — no new
  encryption/decoding logic needed since these aren't PII-sensitive columns.
- `get_demographics`/`upsert_demographics` (the candidate_demographics
  table) — DB-touching calls exercised against a `MagicMock` session, same
  "no live Postgres needed" approach tests/test_answer_cache_repository.py
  and tests/test_application_repository.py use.
"""

from unittest.mock import MagicMock

from app.models.db_models import CandidateProfile
from app.services.profile_repository import (
    _apply_profile_fields,
    get_demographics,
    profile_to_dict,
    upsert_demographics,
)


# ---------- new CandidateProfile compliance columns ----------

def test_new_compliance_fields_round_trip_through_profile_to_dict():
    profile = CandidateProfile(profile_id="p1", user_id="u1")
    _apply_profile_fields(profile, {
        "work_authorized": True,
        "requires_sponsorship": False,
        "visa_type": "H1B",
        "sponsorship_countries": ["USA"],
    })

    result = profile_to_dict(profile)

    assert result["work_authorized"] is True
    assert result["requires_sponsorship"] is False
    assert result["visa_type"] == "H1B"
    assert result["sponsorship_countries"] == ["USA"]


def test_compliance_fields_default_to_none_when_never_set():
    profile = CandidateProfile(profile_id="p1", user_id="u1")
    result = profile_to_dict(profile)

    assert result["work_authorized"] is None
    assert result["requires_sponsorship"] is None
    assert result["visa_type"] is None
    assert result["sponsorship_countries"] is None


# ---------- candidate_demographics (get/upsert) ----------

def test_get_demographics_returns_none_when_never_asked():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    assert get_demographics(db, "profile-1") is None


def test_upsert_demographics_creates_a_new_row_when_none_exists():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    upsert_demographics(db, "profile-1", {"gender": "female"})

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert added.candidate_id == "profile-1"
    assert added.gender == "female"
    db.commit.assert_called_once()


def test_upsert_demographics_updates_an_existing_row_in_place():
    db = MagicMock()
    existing = MagicMock(gender="female", veteran_status=None)
    db.query.return_value.filter.return_value.first.return_value = existing

    upsert_demographics(db, "profile-1", {"veteran_status": "not_veteran"})

    db.add.assert_not_called()  # updated in place, not inserted again
    assert existing.veteran_status == "not_veteran"
    assert existing.gender == "female"  # untouched — partial update semantics
    db.commit.assert_called_once()


def test_upsert_demographics_only_touches_fields_present_in_the_request():
    db = MagicMock()
    existing = MagicMock(gender="female", veteran_status="veteran", disability_status="no_disability", race_ethnicity=None)
    db.query.return_value.filter.return_value.first.return_value = existing

    upsert_demographics(db, "profile-1", {"race_ethnicity": "decline_to_answer"})

    assert existing.race_ethnicity == "decline_to_answer"
    assert existing.gender == "female"
    assert existing.veteran_status == "veteran"
    assert existing.disability_status == "no_disability"
