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
CATEGORY_CURRENT_SALARY = "current_salary"
CATEGORY_PREFERRED_NAME = "preferred_name"
CATEGORY_REFERRAL_SOURCE = "referral_source"
CATEGORY_EMPLOYMENT_TYPE = "employment_type_preference"
CATEGORY_BACKGROUND_CHECK = "willing_background_check"
#: Answered by `answer_engine._language_fluency_answer`, not by a plain
#: attribute formatter: the question names WHICH language ("are you fluent in
#: German?"), so the answer depends on the question text and not on the profile
#: alone. Same shape as the demographic categories in that respect.
CATEGORY_LANGUAGE_FLUENCY = "language_fluency"
CATEGORY_YEARS_OF_EXPERIENCE = "years_of_experience"
CATEGORY_HIGHEST_EDUCATION = "highest_education_level"
CATEGORY_WILLING_TO_RELOCATE = "willing_to_relocate"
#: "Do you require relocation assistance?" — a question about MONEY, and a
#: different one from WILLING_TO_RELOCATE above. Kept apart because a candidate
#: can be happy to move and still need it paid for; answering this one from
#: `willing_to_relocate` is the wrong-answer bug this category prevents.
CATEGORY_RELOCATION_ASSISTANCE = "requires_relocation_assistance"
CATEGORY_WILLING_TO_TRAVEL = "willing_to_travel"
CATEGORY_DRUG_TEST = "willing_drug_test"
CATEGORY_DRIVERS_LICENSE = "has_drivers_license"
CATEGORY_AGE_OVER_18 = "age_over_18"
CATEGORY_SECURITY_CLEARANCE = "security_clearance"
CATEGORY_REFERRER_NAME = "referrer_name"
CATEGORY_TIME_ZONE = "time_zone"
CATEGORY_DEMOGRAPHIC_GENDER = "demographic_gender"
CATEGORY_DEMOGRAPHIC_VETERAN_STATUS = "demographic_veteran_status"
CATEGORY_DEMOGRAPHIC_DISABILITY_STATUS = "demographic_disability_status"
CATEGORY_DEMOGRAPHIC_RACE_ETHNICITY = "demographic_race_ethnicity"
CATEGORY_DEMOGRAPHIC_PRONOUNS = "demographic_pronouns"

#: Every demographic category — `answer_engine.py` checks membership in this
#: set to enforce the "never LLM-guess a demographic answer" hard rule.
#:
#: PRONOUNS belong here, and belong here more than anything else does: the only
#: thing a model could infer pronouns from is the candidate's name, so an LLM
#: answering this question is a coin-flip that misgenders someone on a real job
#: application. Answered only from what the candidate stored, or not at all.
DEMOGRAPHIC_CATEGORIES = frozenset({
    CATEGORY_DEMOGRAPHIC_GENDER,
    CATEGORY_DEMOGRAPHIC_VETERAN_STATUS,
    CATEGORY_DEMOGRAPHIC_DISABILITY_STATUS,
    CATEGORY_DEMOGRAPHIC_RACE_ETHNICITY,
    CATEGORY_DEMOGRAPHIC_PRONOUNS,
})

#: The demographic categories a form asks as "select all that apply" rather than
#: pick-one — `answer_engine` reads a LIST of stored values for these (see
#: `CandidateDemographics.ethnicities`) and `ats/base.py`'s checkbox-group pass
#: may tick more than one member for them. Everything else stays single-answer.
MULTI_VALUE_DEMOGRAPHIC_CATEGORIES = frozenset({
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
    # "Do you hold an active security clearance?" — standard on US
    # defense/government-adjacent postings, and answered ONLY from the stored
    # column: a clearance the candidate doesn't hold is a false statement on a
    # federal application, so an empty column leaves this for a human.
    (CATEGORY_SECURITY_CLEARANCE, [
        "security clearance", "active clearance", "clearance level",
        "level of clearance", "hold a clearance", "government clearance",
    ]),
    # Before YEARS_OF_EXPERIENCE purely as documentation — neither list's
    # phrases appear inside the other's ("18 years of age" doesn't contain
    # "years of experience") — and never answered from a date of birth, which
    # this system deliberately doesn't store.
    (CATEGORY_AGE_OVER_18, [
        "at least 18", "18 years of age", "18 years or older", "18 or older",
        "over the age of 18", "age of 18 or", "are you 18", "legal working age",
    ]),
    (CATEGORY_NOTICE_PERIOD, [
        "notice period", "when can you start", "how soon can you start",
        "start date availability",
        # Observed live on a Lever posting as a required question that matched
        # nothing here and so fell to the LLM, which had no notice-period fact
        # in its prompt and correctly answered null — leaving it blank. It is
        # the same question as "when can you start", worded as availability.
        "available to start", "when are you available", "when would you be able to start",
        "when could you start", "when can you join", "how soon can you join",
        "earliest start date", "earliest you can start", "availability to start",
        # NOTE: a bare "start date" is deliberately NOT here. Greenhouse's
        # Education block has "Start date year" fields (see the education
        # questions in automation/tests/test_resume_context.py), and answering
        # those with a notice period would be confidently wrong.
    ]),
    # "Which time zone are you based in?" — asked by most remote postings and
    # unanswerable from `location`/`country`, which don't imply one zone (the US
    # has six). Answered from `CandidateProfile.time_zone`, in the candidate's
    # own words.
    (CATEGORY_TIME_ZONE, [
        "time zone", "timezone", "which zone are you", "utc offset",
    ]),
    # CURRENT before EXPECTED, and "current ctc" MOVED out of the expected
    # bucket where it used to live. That listing was a wrong-answer bug, not a
    # gap: "What is your current CTC?" resolved to `expected_salary`, so the
    # candidate's expected number was typed into a field asking what they earn
    # today. The two are different facts (see `CandidateProfile.current_salary`),
    # and a form that asks both gets both.
    (CATEGORY_CURRENT_SALARY, [
        "current salary", "current ctc", "current compensation", "present salary",
        "current annual salary", "salary drawn", "current package", "current pay",
        "existing ctc", "current fixed",
    ]),
    (CATEGORY_EXPECTED_SALARY, [
        "expected salary", "salary expectation", "compensation expectation",
        "desired compensation", "expected ctc", "expected pay",
        "desired salary", "salary requirement", "expected compensation",
    ]),
    (CATEGORY_YEARS_OF_EXPERIENCE, [
        "years of experience", "years experience", "how many years have you worked",
    ]),
    # Answered from `CandidateProfile.highest_education_level` when it's set —
    # and, when it isn't, this category still falls through to the LLM+résumé
    # path exactly as before (see `answer_engine._deterministic_answer`), which
    # is what already answers "What is your highest level of education?" from
    # the candidate's stored degrees. So this only makes the common case free
    # and deterministic; it takes nothing away.
    (CATEGORY_HIGHEST_EDUCATION, [
        "highest level of education", "highest education level", "highest education",
        "level of education", "education level", "highest degree",
        "highest qualification", "highest level of study",
    ]),
    # "Are you fluent in <language>?" — a live Lever posting asked this as a
    # required radio group (Yes / No / Limited Working Proficiency) and it was
    # left blank. Which language is named decides the answer, so
    # `answer_engine` handles this category specially rather than through a
    # formatter that only sees the profile.
    (CATEGORY_LANGUAGE_FLUENCY, [
        "fluent in", "fluency in", "proficient in", "proficiency in",
        "do you speak", "language proficiency", "level of english",
        "command of english", "comfortable communicating in",
        # "English proficiency" / "English level" as a bare label — extremely
        # common, and matching none of the phrases above. English is spelled out
        # rather than handled generically because a bare "proficiency" would
        # claim "Proficiency in Python", a skills question.
        #
        # Note "proficient in" DOES claim "Are you proficient in Python?", and
        # that degrades correctly rather than answering wrongly:
        # `_language_fluency_answer` finds no stored language named in the
        # question and falls through to the LLM.
        "english proficiency", "english level", "spoken english",
    ]),
    (CATEGORY_PREFERRED_NAME, [
        "preferred name", "preferred first name", "what should we call you",
        "what do you go by", "name you go by", "nickname",
        # NOTE: no bare "preferred" — "preferred location", "preferred start
        # date" and "preferred pronouns" are all different questions.
    ]),
    (CATEGORY_REFERRAL_SOURCE, [
        "how did you hear", "how did you find", "where did you hear",
        "how you heard about", "where did you find this",
        "how did you learn about", "referral source", "source of application",
    ]),
    # AFTER referral source, deliberately: a combined "How did you hear about
    # this job? (If referred by an employee, give their name)" is asked as a
    # single free-text field, and the leading question is the one being asked, so
    # `referral_source` should win it. A form that asks separately for the
    # referrer's name reaches this bucket, and gets a person's name rather than
    # the "LinkedIn" that would otherwise be typed into it.
    (CATEGORY_REFERRER_NAME, [
        "who referred you", "referred you to", "name of the person who referred",
        "referred by", "referrer name", "name of the employee who referred",
    ]),
    (CATEGORY_EMPLOYMENT_TYPE, [
        "type of employment", "employment type", "type of role are you looking",
        "employment preference", "full-time or part-time", "full time or part time",
        "what type of position",
    ]),
    (CATEGORY_BACKGROUND_CHECK, [
        "background check", "background screening", "background verification",
        "criminal record check",
    ]),
    # Its own category rather than a phrase in the background-check bucket: a
    # candidate can consent to one and not the other, and consenting to a drug
    # test on their behalf because they agreed to a background check is exactly
    # the inference the tri-state columns exist to prevent.
    (CATEGORY_DRUG_TEST, [
        "drug test", "drug screen", "drug screening", "drug and alcohol",
        "substance screening",
    ]),
    (CATEGORY_DRIVERS_LICENSE, [
        "driver's license", "driver’s license", "drivers license", "driver license",
        "driving license", "driving licence", "driver's licence", "valid license to drive",
        # NOTE: no bare "license"/"licence" — "Do you hold a professional
        # license?" (nursing, PE, legal) is a different question, and this
        # column would answer it wrongly.
    ]),
    (CATEGORY_WILLING_TO_RELOCATE, [
        "willing to relocate", "able to relocate", "open to relocation",
        "open to relocating", "would you relocate", "willing to move to",
        "prepared to relocate", "comfortable relocating",
        # NOTE: a bare "relocate"/"relocation" is deliberately NOT here.
        # "Do you require relocation assistance?" is a question about money, not
        # about willingness, and answering it "Yes" from `willing_to_relocate`
        # would be confidently wrong. It now has its own category and column
        # (immediately below) instead of being left blank.
    ]),
    # AFTER willing-to-relocate, and the order is load-bearing in that
    # direction: "Are you willing to relocate? (a relocation package is
    # available)" is a willingness question that happens to mention the package,
    # and the bucket above should keep claiming it. A form asking only "Do you
    # require relocation assistance?" matches nothing above and lands here.
    (CATEGORY_RELOCATION_ASSISTANCE, [
        "relocation assistance", "relocation support", "relocation package",
        "help with relocation", "relocation benefits", "assistance with relocation",
    ]),
    (CATEGORY_WILLING_TO_TRAVEL, [
        "willing to travel", "able to travel", "open to travel", "open to traveling",
        "comfortable traveling", "comfortable with travel", "travel requirement",
        "willing to travel up to", "amount of travel",
        # NOTE: no bare "travel". "Do you require travel reimbursement?" and
        # "have you travelled to the US before?" are different questions.
    ]),
    # Before GENDER, not after: some forms label this "Gender pronouns", and the
    # GENDER bucket's deliberately-bare "gender" phrase would otherwise claim
    # it. Bare "pronoun" here for the same reason that bare "gender" is there —
    # every phrasing of the question contains the word, and a demographic
    # category can only ever be answered from a stored value, so the cost of
    # over-matching is a field left for a human rather than a wrong answer.
    (CATEGORY_DEMOGRAPHIC_PRONOUNS, [
        "pronoun",
    ]),
    (CATEGORY_DEMOGRAPHIC_GENDER, [
        "what is your gender", "gender identity", "you identify as (select gender)",
        "gender:",
        # A bare "gender", deliberately. On a live Lever posting the EEO
        # `<select name="eeo[gender]">` is wrapped in its own `<label>`, so the
        # recovered text is the label PLUS every option:
        # "GenderSelect ...MaleFemaleDecline to self-identify". None of the
        # phrases above appear in that, so the question was classified as novel
        # and sent to the LLM — which this module and answer_engine.py both
        # state must NEVER happen for a demographic question. Its sibling
        # `eeo[race]` and `eeo[veteran]` selects were caught only by accident,
        # because "hispanic or latino" and "veteran status" happen to appear in
        # their option text.
        #
        # Over-matching is the safe direction here: a category listed in
        # DEMOGRAPHIC_CATEGORIES is answered ONLY from what the candidate
        # explicitly stored, so the worst case is an unrelated question
        # mentioning "gender" being left for a human. The alternative is an LLM
        # choosing a gender on the candidate's behalf.
        "gender",
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
        # "I identify my ethnicity as / Select all that apply" — the recovered
        # text of Lever's eight-checkbox ethnicity group, which matched none of
        # the phrases above. Bare "ethnicity"/"ethnic origin" for the same
        # reason a bare "gender" is listed above: a demographic category is
        # answered ONLY from a stored value, so over-matching costs at worst a
        # blank field, while under-matching sends an EEO question to the LLM.
        "identify my ethnicity", "ethnicity", "ethnicities", "ethnic origin",
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
