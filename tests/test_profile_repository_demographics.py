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

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.profile import create_demographics, put_demographics
from app.models.db_models import CandidateProfile
from app.models.profile import DemographicsRequest
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


def test_the_third_tier_of_form_answer_fields_round_trips():
    profile = CandidateProfile(profile_id="p1", user_id="u1")
    _apply_profile_fields(profile, {
        "middle_name": "Kumar",
        "postal_code": "560001",
        "time_zone": "IST",
        "professional_summary": "Backend engineer, 6 years on Python services.",
        "earliest_start_date": "2026-09-01",
        "security_clearance": "None",
        "referrer_name": "Priya Rao",
        "age_over_18": True,
        "willing_to_travel": True,
        "requires_relocation_assistance": False,
        "willing_drug_test": True,
        "has_drivers_license": False,
    })

    result = profile_to_dict(profile)

    assert result["middle_name"] == "Kumar"
    assert result["postal_code"] == "560001"
    assert result["time_zone"] == "IST"
    assert result["professional_summary"] == "Backend engineer, 6 years on Python services."
    assert result["earliest_start_date"] == "2026-09-01"
    assert result["security_clearance"] == "None"
    assert result["referrer_name"] == "Priya Rao"
    assert result["age_over_18"] is True
    assert result["willing_to_travel"] is True
    assert result["requires_relocation_assistance"] is False
    assert result["willing_drug_test"] is True
    assert result["has_drivers_license"] is False


@pytest.mark.parametrize("field", [
    "age_over_18", "willing_to_travel", "requires_relocation_assistance",
    "willing_drug_test", "has_drivers_license",
])
def test_the_new_booleans_default_to_none_not_false(field):
    """Tri-state, same as `marketing_opt_in`: `None` means never asked. `False`
    would be an answer the user never gave — and for the consent-shaped ones
    (drug screening) and the attestation-shaped ones (age, licence) that
    distinction is the whole reason the columns are nullable."""
    result = profile_to_dict(CandidateProfile(profile_id="p1", user_id="u1"))

    assert result[field] is None


def test_relocation_willingness_and_assistance_are_stored_independently():
    """The point of the second column: a candidate can be happy to move AND need
    the employer to pay for it, which is why one question was never answerable
    from the other (see `question_classifier`'s WILLING_TO_RELOCATE note)."""
    profile = CandidateProfile(profile_id="p1", user_id="u1")
    _apply_profile_fields(profile, {
        "willing_to_relocate": True,
        "requires_relocation_assistance": True,
    })

    result = profile_to_dict(profile)

    assert result["willing_to_relocate"] is True
    assert result["requires_relocation_assistance"] is True


# ---------- POST /profile/demographics ----------
# Same create/update split as POST vs PATCH /profile: POST is the first-answer
# path and 409s rather than overwriting what an earlier answer session stored.

def _fake_profile():
    return MagicMock(profile_id="profile-1")


def test_post_demographics_creates_the_row_when_there_is_none():
    db = MagicMock()
    created = MagicMock(candidate_id="profile-1", gender="female")
    with patch("app.api.profile.repo.get_by_user_id", return_value=_fake_profile()), \
         patch("app.api.profile.repo.get_demographics", return_value=None), \
         patch("app.api.profile.repo.upsert_demographics", return_value=created) as upsert:
        result = create_demographics(
            body=DemographicsRequest(gender="female"), user=MagicMock(user_id="u1"), db=db,
        )

    assert result is created
    assert upsert.call_args[0][2] == {"gender": "female"}  # only what was sent


def test_post_demographics_rejects_a_second_create_with_409():
    with patch("app.api.profile.repo.get_by_user_id", return_value=_fake_profile()), \
         patch("app.api.profile.repo.get_demographics", return_value=MagicMock()), \
         patch("app.api.profile.repo.upsert_demographics") as upsert:
        with pytest.raises(HTTPException) as exc_info:
            create_demographics(
                body=DemographicsRequest(gender="male"), user=MagicMock(user_id="u1"), db=MagicMock(),
            )

    assert exc_info.value.status_code == 409
    upsert.assert_not_called()  # nothing already on file was overwritten


def test_post_demographics_writes_only_the_fields_the_user_actually_answered():
    """An omitted field stays `None` — "never asked" — and is never defaulted to
    `decline_to_answer` on the user's behalf."""
    with patch("app.api.profile.repo.get_by_user_id", return_value=_fake_profile()), \
         patch("app.api.profile.repo.get_demographics", return_value=None), \
         patch("app.api.profile.repo.upsert_demographics", return_value=MagicMock()) as upsert:
        create_demographics(
            body=DemographicsRequest(pronouns="they/them", ethnicities=["Asian"]),
            user=MagicMock(user_id="u1"), db=MagicMock(),
        )

    assert upsert.call_args[0][2] == {"pronouns": "they/them", "ethnicities": ["Asian"]}


@pytest.mark.parametrize("payload, field", [
    ({"gender": "attack helicopter"}, "gender"),
    ({"veteran_status": "maybe"}, "veteran_status"),
    ({"disability_status": "unsure"}, "disability_status"),
])
def test_post_demographics_validates_the_closed_vocabulary_fields(payload, field):
    with patch("app.api.profile.repo.get_by_user_id", return_value=_fake_profile()), \
         patch("app.api.profile.repo.get_demographics", return_value=None), \
         patch("app.api.profile.repo.upsert_demographics") as upsert:
        with pytest.raises(HTTPException) as exc_info:
            create_demographics(
                body=DemographicsRequest(**payload), user=MagicMock(user_id="u1"), db=MagicMock(),
            )

    assert exc_info.value.status_code == 400
    assert field in exc_info.value.detail
    upsert.assert_not_called()


def test_post_and_put_demographics_accept_exactly_the_same_values():
    """The two endpoints share `_validate_demographic_choices` so they can't
    drift on what they accept — an open-vocabulary pronoun set has to pass both,
    and a bogus gender token has to fail both."""
    with patch("app.api.profile.repo.get_by_user_id", return_value=_fake_profile()), \
         patch("app.api.profile.repo.get_demographics", return_value=None), \
         patch("app.api.profile.repo.upsert_demographics", return_value=MagicMock()):
        for endpoint in (create_demographics, put_demographics):
            endpoint(body=DemographicsRequest(pronouns="ze/hir"), user=MagicMock(user_id="u1"), db=MagicMock())

            with pytest.raises(HTTPException) as exc_info:
                endpoint(body=DemographicsRequest(gender="nope"), user=MagicMock(user_id="u1"), db=MagicMock())
            assert exc_info.value.status_code == 400


def test_post_demographics_404s_when_there_is_no_profile_yet():
    with patch("app.api.profile.repo.get_by_user_id", return_value=None):
        with pytest.raises(HTTPException) as exc_info:
            create_demographics(
                body=DemographicsRequest(gender="female"), user=MagicMock(user_id="u1"), db=MagicMock(),
            )

    assert exc_info.value.status_code == 404


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
