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


# ---------- the fields real ATS forms left blank ----------
# Five columns for five questions a live Lever posting couldn't answer because
# the profile had nowhere to store the answer. See
# automation/tests/test_demographic_option_matching.py for the matching side and
# automation/tests/test_checkbox_group_fill.py for the fill side.

def test_form_answer_fields_round_trip_through_profile_to_dict():
    profile = CandidateProfile(profile_id="p1", user_id="u1")
    _apply_profile_fields(profile, {
        "highest_education_level": "Bachelor's Degree",
        "willing_to_relocate": True,
        "marketing_opt_in": False,
    })

    result = profile_to_dict(profile)

    assert result["highest_education_level"] == "Bachelor's Degree"
    assert result["willing_to_relocate"] is True
    assert result["marketing_opt_in"] is False


def test_marketing_opt_in_defaults_to_none_not_false():
    """The tri-state is the point: `None` means never asked, and only an
    explicit `True` ever ticks an opt-in box (see
    `automation/ats/base.py::_fill_opt_in_checkboxes`). Defaulting it to `False`
    would lose the distinction; defaulting it to `True` would invent consent."""
    result = profile_to_dict(CandidateProfile(profile_id="p1", user_id="u1"))

    assert result["marketing_opt_in"] is None
    assert result["willing_to_relocate"] is None
    assert result["highest_education_level"] is None


def test_the_second_tier_of_form_answer_fields_round_trips():
    profile = CandidateProfile(profile_id="p1", user_id="u1")
    _apply_profile_fields(profile, {
        "preferred_name": "Mo",
        "current_salary": 1_800_000.0,
        "current_salary_currency": "INR",
        "referral_source": "LinkedIn",
        "employment_type_preference": "full_time",
        "languages": [{"language": "English", "proficiency": "fluent"}],
        "willing_background_check": True,
    })

    result = profile_to_dict(profile)

    assert result["preferred_name"] == "Mo"
    assert result["current_salary"] == 1_800_000.0
    assert result["current_salary_currency"] == "INR"
    assert result["referral_source"] == "LinkedIn"
    assert result["employment_type_preference"] == "full_time"
    assert result["languages"] == [{"language": "English", "proficiency": "fluent"}]
    assert result["willing_background_check"] is True


def test_current_and_expected_salary_are_stored_independently():
    """The whole point of the new column: a form asking both gets both, and
    neither question is ever answered with the other's number."""
    profile = CandidateProfile(profile_id="p1", user_id="u1")
    _apply_profile_fields(profile, {"current_salary": 1_800_000.0, "expected_salary": 2_500_000.0})

    result = profile_to_dict(profile)

    assert result["current_salary"] == 1_800_000.0
    assert result["expected_salary"] == 2_500_000.0


def test_upsert_demographics_stores_pronouns_and_the_ethnicity_list():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    upsert_demographics(db, "profile-1", {
        "pronouns": "they/them",
        "ethnicities": ["Asian", "White / Caucasian"],
    })

    added = db.add.call_args[0][0]
    assert added.pronouns == "they/them"
    assert added.ethnicities == ["Asian", "White / Caucasian"]
