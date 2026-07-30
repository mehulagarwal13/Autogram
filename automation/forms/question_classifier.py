"""
QuestionClassifier — Phase 8 (see ARCHITECTURE.md / PART 3 of the request
that created this module).

Sits inside `automation/forms/answer_engine.py`'s deterministic path, as a
sharper replacement for that module's original flat `_DETERMINISTIC_CATEGORIES`
table. The problem this fixes: a single free-text `work_authorization`
category conflated two genuinely different compliance questions —

    "Are you legally authorized to work in <country>?"   (a present-tense fact)
    "Will you now or in the future require sponsorship?"  (a different fact —
                                                            a candidate can be
                                                            authorized today
                                                            AND still need
                                                            sponsorship later,
                                                            e.g. OPT -> H-1B)

— which meant the old code could only ever echo back whatever free text was
in `CandidateProfile.work_authorization`/`visa_status`, never answer either
question as a direct yes/no from a real boolean. This module classifies a
question's full text into one narrow category; `answer_engine.py` decides
*which profile field* answers each category and how.

Design note — this module never imports `app.*` / `automation.*` beyond
stdlib; it's pure text classification, same "cheapest deterministic path
first" philosophy as `field_mapper.py` and `ats/detector.py`: an ordered list
of phrase sets, first match wins, `None` (never guess) if nothing matches
confidently. This is a DIFFERENT layer from `FieldMapper` on purpose:
`FieldMapper` resolves a field's short DOM label/name to a profile attribute
via substring synonyms (kept exactly as-is — see its own module docstring
for the trade-offs of touching that table); this module classifies a full
screening-QUESTION SENTENCE (the kind of text that reaches
`ApplicationAnswerEngine` because `FieldMapper` couldn't confidently resolve
it, or a longer sentence `FieldMapper`'s substring matching was never
designed to parse). The two layers deliberately overlap in vocabulary
without needing to agree — a short label a real ATS renders as an actual
`<label>` next to a field is `FieldMapper`'s job; the full sentence text of a
screening question (which may or may not also be a `<label>`) is this
module's job.

Order matters below: more specific categories are checked before broader
ones, so "will you require visa sponsorship" (SPONSORSHIP) is never
misclassified as the more generic AUTHORIZATION bucket just because both
mention "visa".
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Categories — plain string constants (matches the rest of this codebase's
# convention of string enums backed by a VALID_* set, e.g. db_models.py,
# rather than a Python Enum class).
# ---------------------------------------------------------------------------

CATEGORY_REQUIRES_SPONSORSHIP = "requires_sponsorship"
CATEGORY_WORK_AUTHORIZED = "work_authorized"
CATEGORY_VISA_TYPE = "visa_type"
CATEGORY_NOTICE_PERIOD = "notice_period_days"
CATEGORY_EXPECTED_SALARY = "expected_salary"
CATEGORY_YEARS_OF_EXPERIENCE = "years_of_experience"
CATEGORY_DEMOGRAPHIC_GENDER = "demographic_gender"
CATEGORY_DEMOGRAPHIC_VETERAN_STATUS = "demographic_veteran_status"
CATEGORY_DEMOGRAPHIC_DISABILITY_STATUS = "demographic_disability_status"
CATEGORY_DEMOGRAPHIC_RACE_ETHNICITY = "demographic_race_ethnicity"

#: Every demographic category — `answer_engine.py` checks membership in this
#: set to enforce the "never LLM-guess a demographic answer" hard rule.
DEMOGRAPHIC_CATEGORIES = frozenset({
    CATEGORY_DEMOGRAPHIC_GENDER,
    CATEGORY_DEMOGRAPHIC_VETERAN_STATUS,
    CATEGORY_DEMOGRAPHIC_DISABILITY_STATUS,
    CATEGORY_DEMOGRAPHIC_RACE_ETHNICITY,
})

# Checked in this exact order — see module docstring on why SPONSORSHIP must
# be tried before AUTHORIZED (both legitimately mention "visa"/"work").
_CATEGORY_PHRASES: list[tuple[str, list[str]]] = [
    (CATEGORY_REQUIRES_SPONSORSHIP, [
        "require sponsorship", "requires sponsorship", "need sponsorship",
        "visa sponsorship", "sponsorship now or in the future",
        "sponsorship in the future", "now or in the future require",
        "require a visa", "need a visa", "require work sponsorship",
        "will you need sponsorship", "will you require sponsorship",
    ]),
    (CATEGORY_VISA_TYPE, [
        "visa type", "type of visa", "which visa", "what visa",
        "current visa classification", "visa classification",
    ]),
    (CATEGORY_WORK_AUTHORIZED, [
        "authorized to work", "legally authorized to work", "authorised to work",
        "legally eligible to work", "eligible to work in", "legally permitted to work",
        "work permit", "right to work in",
    ]),
    (CATEGORY_NOTICE_PERIOD, [
        "notice period", "when can you start", "how soon can you start",
        "start date availability",
    ]),
    (CATEGORY_EXPECTED_SALARY, [
        "expected salary", "salary expectation", "compensation expectation",
        "desired compensation", "expected ctc", "current ctc", "expected pay",
    ]),
    (CATEGORY_YEARS_OF_EXPERIENCE, [
        "years of experience", "years experience", "how many years have you worked",
    ]),
    (CATEGORY_DEMOGRAPHIC_GENDER, [
        "what is your gender", "gender identity", "you identify as (select gender)",
        "gender:",
    ]),
    (CATEGORY_DEMOGRAPHIC_VETERAN_STATUS, [
        "veteran status", "are you a veteran", "protected veteran",
    ]),
    (CATEGORY_DEMOGRAPHIC_DISABILITY_STATUS, [
        "disability status", "do you have a disability", "self-identify as a person with a disability",
        "voluntary self-identification of disability",
    ]),
    (CATEGORY_DEMOGRAPHIC_RACE_ETHNICITY, [
        "race/ethnicity", "race and ethnicity", "what is your race", "what is your ethnicity",
        "race or ethnic", "hispanic or latino",
    ]),
]

_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text.strip().lower())


def classify_question(question: str) -> str | None:
    """Classifies a full screening-question sentence into one of the
    categories above, or `None` if this looks like a genuinely
    subjective/novel question (`ApplicationAnswerEngine`'s LLM path handles
    those) — never guesses at a category it isn't confident about."""
    normalized = _normalize(question)
    if not normalized:
        return None
    for category, phrases in _CATEGORY_PHRASES:
        if any(phrase in normalized for phrase in phrases):
            return category
    return None


def is_demographic(category: str | None) -> bool:
    return category in DEMOGRAPHIC_CATEGORIES
