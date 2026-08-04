"""
The third tier of profile fields — twelve more questions real ATS forms ask that
nothing in `CandidateProfile` could answer.

One of them is a documented wrong-answer risk rather than a plain gap:
"Do you require relocation assistance?" is a question about MONEY, not about
willingness, so `question_classifier` deliberately refused to answer it from
`willing_to_relocate` (see the NOTE on its WILLING_TO_RELOCATE phrases) and the
field was left blank on every form that asked. It now has its own column,
category, formatter and synonym set — and the two questions stay independent in
both directions, which is what these tests pin down.

The rest are gaps: legal middle name, postal code, time zone, professional
summary, earliest start date, security clearance, referrer name, and four more
tri-state booleans (age 18+, travel, drug screening, driver's licence).

The tri-state rule is the same one `marketing_opt_in` established and is tested
here per field: `None` means "never asked", so an unanswered consent or
attestation falls through to the LLM/human path instead of being answered "No"
(or, worse, "Yes") on the candidate's behalf.
"""

from __future__ import annotations

import pytest

from app.models.db_models import CandidateProfile
from automation.forms.answer_engine import ApplicationAnswerEngine
from automation.forms.field_mapper import FieldMapper
from automation.forms.profile_formatting import (
    format_earliest_start_date,
    format_notice_period_or_start_date,
    format_profile_value,
    format_referrer_name,
    format_requires_relocation_assistance,
    format_security_clearance,
)
from automation.forms.question_classifier import (
    CATEGORY_AGE_OVER_18,
    CATEGORY_DRIVERS_LICENSE,
    CATEGORY_DRUG_TEST,
    CATEGORY_NOTICE_PERIOD,
    CATEGORY_REFERRAL_SOURCE,
    CATEGORY_REFERRER_NAME,
    CATEGORY_RELOCATION_ASSISTANCE,
    CATEGORY_SECURITY_CLEARANCE,
    CATEGORY_TIME_ZONE,
    CATEGORY_WILLING_TO_RELOCATE,
    CATEGORY_WILLING_TO_TRAVEL,
    classify_question,
)

YES_NO = ("Yes", "No")


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(profile_id="profile-1", user_id="user-1", skills={})
    defaults.update(overrides)
    return CandidateProfile(**defaults)


def _engine(profile, llm_fn=None) -> ApplicationAnswerEngine:
    def must_not_be_called(*, task, prompt, system=None, **overrides):
        raise AssertionError("this question must be answered from the profile, not the LLM")

    return ApplicationAnswerEngine(profile=profile, llm_fn=llm_fn or must_not_be_called)


def _declining_llm(*, task, prompt, system=None, **overrides):
    return '{"answers": [{"answer": null, "confidence": 0.0}]}'


# ---------------------------------------------------------------------------
# relocation assistance vs. willingness to relocate — the wrong-answer risk
# ---------------------------------------------------------------------------

def test_relocation_assistance_is_its_own_category_not_the_willingness_one():
    assert classify_question("Do you require relocation assistance?") == CATEGORY_RELOCATION_ASSISTANCE
    assert classify_question("Will you need relocation support?") == CATEGORY_RELOCATION_ASSISTANCE
    assert classify_question("Are you willing to relocate?") == CATEGORY_WILLING_TO_RELOCATE


def test_a_willingness_question_that_mentions_the_package_stays_a_willingness_question():
    """Order is load-bearing: WILLING_TO_RELOCATE is checked first, so a question
    that asks whether the candidate will move — and merely mentions that a
    package exists — is not read as a question about money."""
    question = "Are you willing to relocate? A relocation package is available."

    assert classify_question(question) == CATEGORY_WILLING_TO_RELOCATE


def test_the_two_relocation_questions_get_the_two_different_answers():
    engine = _engine(_profile(willing_to_relocate=True, requires_relocation_assistance=False))

    assert engine.answer("Are you willing to relocate?", YES_NO).answer == "Yes"
    assert engine.answer("Do you require relocation assistance?", YES_NO).answer == "No"


def test_an_unanswered_assistance_question_is_never_answered_from_willingness():
    """Happy to move at their own expense and needing it paid for are different
    facts. With only the willingness on file, the assistance question falls
    through rather than borrowing the other column's answer."""
    engine = _engine(_profile(willing_to_relocate=True), llm_fn=_declining_llm)

    assert format_requires_relocation_assistance(_profile(willing_to_relocate=True)) is None
    assert engine.answer("Do you require relocation assistance?", YES_NO).answer == ""


# ---------------------------------------------------------------------------
# the four new tri-state booleans
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question, field, expected_category", [
    ("Are you willing to travel for this role?", "willing_to_travel", CATEGORY_WILLING_TO_TRAVEL),
    ("Are you willing to complete a pre-employment drug screening?", "willing_drug_test", CATEGORY_DRUG_TEST),
    ("Do you hold a valid driver's license?", "has_drivers_license", CATEGORY_DRIVERS_LICENSE),
    ("Are you at least 18 years of age?", "age_over_18", CATEGORY_AGE_OVER_18),
])
def test_each_new_boolean_answers_its_own_question_yes_and_no(question, field, expected_category):
    assert classify_question(question) == expected_category
    assert _engine(_profile(**{field: True})).answer(question, YES_NO).answer == "Yes"
    assert _engine(_profile(**{field: False})).answer(question, YES_NO).answer == "No"


@pytest.mark.parametrize("question", [
    "Are you willing to travel for this role?",
    "Are you willing to complete a pre-employment drug screening?",
    "Do you hold a valid driver's license?",
    "Are you at least 18 years of age?",
])
def test_an_unanswered_boolean_falls_through_rather_than_answering_for_the_candidate(question):
    """`None` means never asked. Consenting to a drug test, or attesting to an
    age or a licence, because the candidate was silent is not something this
    system does."""
    engine = _engine(_profile(), llm_fn=_declining_llm)

    assert engine.answer(question, YES_NO).answer == ""


def test_a_drug_screening_question_is_not_answered_from_the_background_check_consent():
    """Two separate consents. A candidate can agree to one and not the other, so
    the drug-test category reads its own column and nothing else."""
    engine = _engine(_profile(willing_background_check=True), llm_fn=_declining_llm)

    assert engine.answer("Are you willing to complete a drug test?", YES_NO).answer == ""


def test_a_professional_license_question_is_not_answered_from_the_driving_licence():
    """No bare "license" phrase in either table — a nursing or PE licence is a
    different claim entirely."""
    assert classify_question("Do you hold a professional license in this state?") is None
    assert FieldMapper.map_field(label="Professional license number") is None


# ---------------------------------------------------------------------------
# earliest start date — the date-shaped half of the notice-period category
# ---------------------------------------------------------------------------

def test_a_stored_notice_period_still_wins_the_notice_period_question():
    profile = _profile(notice_period_days=30, earliest_start_date="2026-09-01")

    assert format_notice_period_or_start_date(profile) == "30 days"


def test_when_can_you_start_is_answered_from_the_start_date_when_there_is_no_notice_period():
    """This category covers "when can you start?" as well as "what is your notice
    period?" — a candidate who recorded a date and no duration used to get a
    blank on a required field."""
    profile = _profile(earliest_start_date="September 2026")

    assert classify_question("When are you available to start?") == CATEGORY_NOTICE_PERIOD
    assert format_notice_period_or_start_date(profile) == "September 2026"
    assert _engine(profile).answer("When are you available to start?").answer == "September 2026"


def test_the_start_date_is_stored_in_the_candidates_own_words():
    """"Immediately" is a real answer to "earliest start date" and a `Date`
    column would have rejected it."""
    assert format_earliest_start_date(_profile(earliest_start_date="Immediately")) == "Immediately"
    assert format_earliest_start_date(_profile(earliest_start_date="   ")) is None
    assert format_earliest_start_date(_profile()) is None


def test_a_degree_start_year_field_does_not_get_the_availability_date():
    """The same trap `question_classifier`'s NOTICE_PERIOD note describes:
    Greenhouse's Education block renders "Start date year" inputs."""
    assert FieldMapper.map_field(label="Start date year") is None
    assert FieldMapper.map_field(label="Earliest start date")[0] == "earliest_start_date"


# ---------------------------------------------------------------------------
# security clearance, referrer name, time zone
# ---------------------------------------------------------------------------

def test_a_clearance_is_answered_only_from_what_the_candidate_stored():
    """Claiming a clearance the candidate doesn't hold is a false statement on a
    federal application, so an empty column answers nothing at all."""
    assert classify_question("Do you hold an active security clearance?") == CATEGORY_SECURITY_CLEARANCE
    assert format_security_clearance(_profile(security_clearance="Active Secret")) == "Active Secret"
    assert format_security_clearance(_profile()) is None

    engine = _engine(_profile(security_clearance="Active Secret"))
    assert engine.answer("Do you hold an active security clearance?").answer == "Active Secret"


def test_who_referred_you_gets_a_person_not_the_referral_source():
    """The bug this column prevents: "LinkedIn" typed into a field asking which
    employee referred the candidate."""
    profile = _profile(referral_source="LinkedIn", referrer_name="Priya Rao")

    assert classify_question("Who referred you to this role?") == CATEGORY_REFERRER_NAME
    assert _engine(profile).answer("Who referred you to this role?").answer == "Priya Rao"
    assert _engine(profile).answer("How did you hear about this job?").answer == "LinkedIn"


def test_a_referrer_name_is_never_borrowed_from_the_referral_source():
    engine = _engine(_profile(referral_source="LinkedIn"), llm_fn=_declining_llm)

    assert format_referrer_name(_profile(referral_source="LinkedIn")) is None
    assert engine.answer("Who referred you to this role?").answer == ""


def test_a_combined_how_did_you_hear_field_stays_with_the_referral_source():
    """Asked as one free-text field on Greenhouse; the leading question is the
    one being asked, so REFERRER_NAME sits after REFERRAL_SOURCE in the table."""
    question = "How did you hear about this job? If referred by an employee, please give their name."

    assert classify_question(question) == CATEGORY_REFERRAL_SOURCE


def test_a_time_zone_question_is_answered_from_the_stored_zone():
    """`location`/`country` don't imply a zone — the US has six — so this was
    unanswerable before the column existed."""
    assert classify_question("Which time zone are you based in?") == CATEGORY_TIME_ZONE
    assert _engine(_profile(time_zone="IST", country="India")).answer(
        "Which time zone are you based in?").answer == "IST"


# ---------------------------------------------------------------------------
# the label-resolved half: FieldMapper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label, attribute", [
    ("Middle name", "middle_name"),
    ("Legal middle name", "middle_name"),
    ("Zip code", "postal_code"),
    ("Postal code", "postal_code"),
    ("Time zone", "time_zone"),
    ("Professional summary", "professional_summary"),
    ("Security clearance", "security_clearance"),
    ("Referred by", "referrer_name"),
    ("Willing to travel", "willing_to_travel"),
    ("Relocation assistance", "requires_relocation_assistance"),
    ("Drug screening", "willing_drug_test"),
    ("Driver's license", "has_drivers_license"),
])
def test_the_new_columns_resolve_from_a_short_label(label, attribute):
    assert FieldMapper.map_field(label=label)[0] == attribute


def test_a_middle_initial_field_is_left_alone():
    """It wants one letter; a whole middle name in it is a wrong answer where a
    blank is a harmless one."""
    assert FieldMapper.map_field(label="Middle initial") is None


def test_the_new_columns_are_formatted_not_dumped_raw():
    """The failure `profile_formatting` exists to prevent — the literal word
    "True" in an employer's form."""
    profile = _profile(willing_to_travel=True, has_drivers_license=False, postal_code="560001")

    assert format_profile_value(profile, "willing_to_travel", profile.willing_to_travel) == "Yes"
    assert format_profile_value(profile, "has_drivers_license", profile.has_drivers_license) == "No"
    assert format_profile_value(profile, "postal_code", profile.postal_code) == "560001"
