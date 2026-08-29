"""
automation/forms/question_classifier.py (Phase 8) — pure-logic tests, no
browser/DB needed. See the module docstring for why SPONSORSHIP is checked
before the broader WORK_AUTHORIZED bucket, and why demographic categories
exist at all (never LLM-answered — enforced in answer_engine.py, tested in
test_answer_engine_phase8.py).
"""

from automation.forms.question_classifier import (
    CATEGORY_DEMOGRAPHIC_DISABILITY_STATUS,
    CATEGORY_DEMOGRAPHIC_GENDER,
    CATEGORY_DEMOGRAPHIC_RACE_ETHNICITY,
    CATEGORY_DEMOGRAPHIC_VETERAN_STATUS,
    CATEGORY_EXPECTED_SALARY,
    CATEGORY_NOTICE_PERIOD,
    CATEGORY_REQUIRES_SPONSORSHIP,
    CATEGORY_VISA_TYPE,
    CATEGORY_WORK_AUTHORIZED,
    CATEGORY_YEARS_OF_EXPERIENCE,
    classify_question,
    is_demographic,
)


# ---------- the PART 3 example questions, verbatim ----------

def test_are_you_legally_authorized_to_work_classifies_as_work_authorized():
    assert classify_question("Are you legally authorized to work in the United States?") == CATEGORY_WORK_AUTHORIZED


def test_will_you_now_or_in_future_require_sponsorship_classifies_as_sponsorship():
    assert classify_question("Will you now or in the future require visa sponsorship?") == CATEGORY_REQUIRES_SPONSORSHIP


def test_will_you_require_sponsorship_short_form_classifies_as_sponsorship():
    assert classify_question("Will you require sponsorship?") == CATEGORY_REQUIRES_SPONSORSHIP


# ---------- sponsorship is checked before the broader authorization bucket ----------

def test_sponsorship_phrasing_is_not_misclassified_as_generic_authorization():
    # Mentions "work" and "visa" — exactly the overlap that could confuse a
    # naive single-bucket classifier.
    category = classify_question("Do you require visa sponsorship to work in this country?")
    assert category == CATEGORY_REQUIRES_SPONSORSHIP


# ---------- other factual categories ----------

def test_visa_type_question():
    assert classify_question("What type of visa do you currently hold?") == CATEGORY_VISA_TYPE


def test_notice_period_question():
    assert classify_question("What is your notice period?") == CATEGORY_NOTICE_PERIOD


def test_expected_salary_question():
    assert classify_question("What is your expected CTC?") == CATEGORY_EXPECTED_SALARY


def test_years_of_experience_question():
    assert classify_question("How many years of experience do you have?") == CATEGORY_YEARS_OF_EXPERIENCE


# ---------- demographic categories (PART 2) ----------

def test_gender_question_classifies_as_demographic():
    category = classify_question("What is your gender?")
    assert category == CATEGORY_DEMOGRAPHIC_GENDER
    assert is_demographic(category) is True


def test_veteran_status_question_classifies_as_demographic():
    category = classify_question("Are you a protected veteran?")
    assert category == CATEGORY_DEMOGRAPHIC_VETERAN_STATUS
    assert is_demographic(category) is True


def test_disability_status_question_classifies_as_demographic():
    category = classify_question("Do you have a disability?")
    assert category == CATEGORY_DEMOGRAPHIC_DISABILITY_STATUS
    assert is_demographic(category) is True


def test_race_ethnicity_question_classifies_as_demographic():
    category = classify_question("What is your race/ethnicity?")
    assert category == CATEGORY_DEMOGRAPHIC_RACE_ETHNICITY
    assert is_demographic(category) is True


def test_non_demographic_category_is_not_flagged_as_demographic():
    assert is_demographic(CATEGORY_NOTICE_PERIOD) is False
    assert is_demographic(None) is False


# ---------- never guesses ----------

def test_subjective_question_returns_none_rather_than_a_wrong_category():
    assert classify_question("Why do you want to work here?") is None
    assert classify_question("") is None
