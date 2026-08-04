"""
The second tier of profile fields — seven questions real ATS forms ask that
nothing in `CandidateProfile` could answer, plus one question it answered WRONG.

The wrong answer first, since it's the only defect here rather than a gap:
`question_classifier` listed "current ctc" among the EXPECTED-salary phrases, so
"What is your current CTC?" resolved to `expected_salary` and the candidate's
expected number was typed into a field asking what they earn today. A blank field
costs a few seconds of review; a number that says the candidate currently earns
their target is a negotiating position they never chose. `current_salary` is now
its own column, category, formatter and synonym set.

The six gaps: preferred name, referral source ("How did you hear about this
job?"), employment type, language fluency, and willingness to undergo a
background check.

Language fluency is the one category that can't be a plain attribute formatter:
the question names WHICH language, so the answer depends on the question text.
A live Lever posting asked it as a required radio group (Yes / No / Limited
Working Proficiency) and it was left blank.
"""

from __future__ import annotations

import json

from app.models.db_models import CandidateProfile
from automation.forms.answer_engine import ApplicationAnswerEngine, Question
from automation.forms.field_mapper import FieldMapper
from automation.forms.profile_formatting import (
    format_current_salary,
    format_employment_type,
    format_languages,
    format_preferred_name,
    format_profile_value,
    normalized_languages,
)
from automation.forms.question_classifier import (
    CATEGORY_BACKGROUND_CHECK,
    CATEGORY_CURRENT_SALARY,
    CATEGORY_EMPLOYMENT_TYPE,
    CATEGORY_EXPECTED_SALARY,
    CATEGORY_LANGUAGE_FLUENCY,
    CATEGORY_PREFERRED_NAME,
    CATEGORY_REFERRAL_SOURCE,
    classify_question,
)

FLUENCY_OPTIONS = ("Yes", "No", "Limited Working Proficiency")


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(profile_id="profile-1", user_id="user-1", skills={})
    defaults.update(overrides)
    return CandidateProfile(**defaults)


def _engine(profile, llm_fn=None) -> ApplicationAnswerEngine:
    def must_not_be_called(*, task, prompt, system=None, **overrides):
        raise AssertionError("this question must be answered from the profile, not the LLM")

    return ApplicationAnswerEngine(profile=profile, llm_fn=llm_fn or must_not_be_called)


# ---------------------------------------------------------------------------
# current vs. expected salary — the wrong-answer regression
# ---------------------------------------------------------------------------

def test_current_ctc_no_longer_resolves_to_the_expected_salary_category():
    assert classify_question("What is your current CTC?") == CATEGORY_CURRENT_SALARY
    assert classify_question("Current salary") == CATEGORY_CURRENT_SALARY
    assert classify_question("Current annual salary (in INR)") == CATEGORY_CURRENT_SALARY


def test_expected_salary_phrasings_still_resolve_to_expected():
    assert classify_question("Expected CTC") == CATEGORY_EXPECTED_SALARY
    assert classify_question("What are your salary expectations?") == CATEGORY_EXPECTED_SALARY
    assert classify_question("Desired compensation") == CATEGORY_EXPECTED_SALARY


def test_the_two_salary_questions_get_the_two_different_numbers():
    profile = _profile(
        current_salary=1_800_000, current_salary_currency="INR",
        expected_salary=2_500_000, expected_salary_currency="INR",
    )
    engine = _engine(profile)

    assert engine.answer("What is your current CTC?").answer == "INR 1,800,000"
    assert engine.answer("What is your expected CTC?").answer == "INR 2,500,000"


def test_current_salary_is_formatted_not_dumped_raw():
    """The failure this whole formatting module exists to prevent: "1800000.0"
    typed into an employer's form."""
    profile = _profile(current_salary=1_800_000.0, current_salary_currency="INR")

    assert format_current_salary(profile) == "INR 1,800,000"
    assert format_profile_value(profile, "current_salary", profile.current_salary) == "INR 1,800,000"


def test_current_salary_borrows_the_expected_currency_when_it_has_none():
    """One currency and two amounts means the same currency — a bare number with
    no unit is worse than the number with the right one."""
    profile = _profile(current_salary=1_800_000, expected_salary_currency="INR")

    assert format_current_salary(profile) == "INR 1,800,000"


def test_an_unset_current_salary_says_nothing():
    assert format_current_salary(_profile()) is None


def test_current_and_expected_are_separate_field_mapper_attributes():
    assert FieldMapper.map_field(label="Current CTC")[0] == "current_salary"
    assert FieldMapper.map_field(label="Expected CTC")[0] == "expected_salary"


# ---------------------------------------------------------------------------
# preferred name
# ---------------------------------------------------------------------------

def test_preferred_name_falls_back_to_the_first_name():
    """Not a guess: a form asking what to call someone is correctly answered with
    their first name until they say otherwise."""
    assert format_preferred_name(_profile(first_name="Mehul")) == "Mehul"
    assert format_preferred_name(_profile(first_name="Mehul", preferred_name="Mo")) == "Mo"
    assert format_preferred_name(_profile()) is None


def test_a_preferred_name_field_does_not_get_the_legal_first_name_attribute():
    """`preferred_name` sits BEFORE `first_name` in FIELD_SYNONYMS because
    "preferred first name" contains "first name" — the wrong order fills the
    preferred-name box and leaves the real first-name box empty."""
    assert FieldMapper.map_field(label="Preferred first name")[0] == "preferred_name"
    assert FieldMapper.map_field(label="Preferred name")[0] == "preferred_name"
    assert FieldMapper.map_field(label="First name")[0] == "first_name"
    assert FieldMapper.map_field(label="Legal last name")[0] == "last_name"


def test_preferred_name_is_classified_but_a_preferred_location_is_not():
    assert classify_question("What should we call you?") == CATEGORY_PREFERRED_NAME
    assert classify_question("Preferred location") is None
    assert classify_question("Preferred start date") is None


# ---------------------------------------------------------------------------
# referral source, employment type, background check
# ---------------------------------------------------------------------------

def test_how_did_you_hear_is_answered_from_the_profile():
    engine = _engine(_profile(referral_source="LinkedIn"))

    assert classify_question("How did you hear about this job?") == CATEGORY_REFERRAL_SOURCE
    assert engine.answer("How did you hear about this job?").answer == "LinkedIn"


def test_employment_type_is_worded_the_way_forms_word_it():
    """Stored as a token; forms offer "Full-time". The raw token matches neither
    "Full-time" nor "Full Time", so it is reshaped before matching."""
    assert format_employment_type(_profile(employment_type_preference="full_time")) == "Full-Time"
    assert classify_question("What type of employment are you seeking?") == CATEGORY_EMPLOYMENT_TYPE

    engine = _engine(_profile(employment_type_preference="full_time"))
    result = engine.answer("What type of employment are you seeking?", ("Full-time", "Part-time", "Contract"))
    assert result.answer == "Full-time"


def test_no_preference_employment_type_says_nothing_rather_than_inventing_an_option():
    """"No preference" is a real stored answer but not one forms offer, so
    staying quiet lets a human (or the LLM) pick from what's actually listed."""
    assert format_employment_type(_profile(employment_type_preference="no_preference")) is None


def test_background_check_willingness_answers_yes_no_and_nothing():
    assert classify_question("Are you willing to complete a background check?") == CATEGORY_BACKGROUND_CHECK
    assert _engine(_profile(willing_background_check=True)).answer(
        "Are you willing to complete a background check?", ("Yes", "No")).answer == "Yes"
    assert _engine(_profile(willing_background_check=False)).answer(
        "Are you willing to complete a background check?", ("Yes", "No")).answer == "No"


def test_an_unanswered_background_check_falls_through_rather_than_consenting():
    """`None` means never asked. Consenting to a background check because the
    candidate was silent is not something this system does."""
    def llm_fn(*, task, prompt, system=None, **overrides):
        return '{"answers": [{"answer": null, "confidence": 0.0}]}'

    engine = _engine(_profile(willing_background_check=None), llm_fn=llm_fn)
    result = engine.answer("Are you willing to complete a background check?", ("Yes", "No"))

    assert result.answer == ""


# ---------------------------------------------------------------------------
# language fluency — the question-aware category
# ---------------------------------------------------------------------------

def _multilingual(**overrides) -> CandidateProfile:
    return _profile(languages=[
        {"language": "English", "proficiency": "fluent"},
        {"language": "Hindi", "proficiency": "native"},
        {"language": "French", "proficiency": "conversational"},
        {"language": "German", "proficiency": "basic"},
    ], **overrides)


def test_the_live_lever_question_is_answered_yes():
    engine = _engine(_multilingual())

    result = engine.answer("Are you fluent in English?", FLUENCY_OPTIONS)

    assert result.source == "deterministic"
    assert result.answer == "Yes"


def test_a_conversational_language_gets_the_band_not_a_yes():
    """The form offers "Limited Working Proficiency" — a better answer than "No"
    and an honest one, since it's the band the candidate themselves chose."""
    engine = _engine(_multilingual())

    assert engine.answer("Are you fluent in French?", FLUENCY_OPTIONS).answer == "Limited Working Proficiency"


def test_a_conversational_language_answers_no_on_a_yes_no_form():
    """No band on offer, so it falls back to the yes/no — and the answer is "No",
    because overstating language ability is a claim to defend in an interview."""
    engine = _engine(_multilingual())

    assert engine.answer("Are you fluent in German?", ("Yes", "No")).answer == "No"


def test_a_language_the_candidate_never_listed_falls_through_to_the_llm():
    """An absent entry means "they didn't mention it", not "they don't speak it" —
    a stored list is rarely exhaustive, so this is the LLM's call, not an
    inference from silence."""
    calls = []

    def llm_fn(*, task, prompt, system=None, **overrides):
        calls.append(json.loads(prompt)["questions"])
        return '{"answers": [{"answer": null, "confidence": 0.0}]}'

    engine = _engine(_multilingual(), llm_fn=llm_fn)
    engine.answer("Are you fluent in Japanese?", FLUENCY_OPTIONS)

    assert calls == [["Are you fluent in Japanese?"]]


def test_a_question_naming_two_of_the_candidates_languages_is_not_guessed():
    def llm_fn(*, task, prompt, system=None, **overrides):
        return '{"answers": [{"answer": null, "confidence": 0.0}]}'

    engine = _engine(_multilingual(), llm_fn=llm_fn)

    assert engine.answer("Are you fluent in English or French?", FLUENCY_OPTIONS).answer == ""


def test_a_language_with_no_recorded_level_does_not_claim_fluency():
    """"Speaks English" does not establish fluency."""
    def llm_fn(*, task, prompt, system=None, **overrides):
        return '{"answers": [{"answer": null, "confidence": 0.0}]}'

    engine = _engine(_profile(languages=[{"language": "English"}]), llm_fn=llm_fn)

    assert engine.answer("Are you fluent in English?", FLUENCY_OPTIONS).answer == ""


def test_which_languages_do_you_speak_gets_the_whole_list():
    """The other shape of the same category — a free-text field, answered by the
    formatter rather than the yes/no path."""
    engine = _engine(_multilingual())

    result = engine.answer("Language proficiency")

    assert result.answer == "English (Fluent), Hindi (Native), French (Conversational), German (Basic)"


def test_the_languages_column_tolerates_a_bare_list_of_strings():
    """A row written by hand or before the object shape existed must not crash a
    fill — it just arrives with no proficiency."""
    profile = _profile(languages=["English", "Hindi"])

    assert normalized_languages(profile) == [("English", ""), ("Hindi", "")]
    assert format_languages(profile) == "English, Hindi"


def test_garbage_in_the_languages_column_is_ignored_not_fatal():
    assert normalized_languages(_profile(languages="English")) == []
    assert normalized_languages(_profile(languages=None)) == []
    assert normalized_languages(_profile(languages=[{}, {"language": "  "}])) == []


def test_a_programming_languages_field_never_gets_spoken_languages():
    """Extremely common on engineering applications, and the reason this table
    has no bare "languages" synonym."""
    assert FieldMapper.map_field(label="Programming languages") is None
    assert FieldMapper.map_field(label="Languages spoken")[0] == "languages"


def test_fluency_phrasings_are_classified():
    for question in (
        "Are you fluent in English?",
        "What is your level of English?",
        "Do you speak Spanish?",
        "English proficiency",
    ):
        assert classify_question(question) == CATEGORY_LANGUAGE_FLUENCY, question
