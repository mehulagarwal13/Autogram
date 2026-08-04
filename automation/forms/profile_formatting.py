"""
The ONE place that decides how a `CandidateProfile` value becomes text typed
into a form.

There used to be two paths and only one of them formatted anything:

- `answer_engine.ApplicationAnswerEngine` had per-field formatters, but keyed
  by QUESTION CATEGORY (`CATEGORY_NOTICE_PERIOD`, ...) from
  `question_classifier.py` — reachable only when a question had already been
  classified.
- `ats/base.py::_resolve_profile_value()` — the path `FieldMapper` uses, which
  answers the majority of fields on a real form — was a bare
  `getattr(profile, attribute)` that returned the raw column and let
  `str(value)` happen downstream.

So a field resolved by label/name went into the employer's form as the raw
Python value: `requires_sponsorship` typed **"True"**, `notice_period_days`
typed **"30"** instead of "30 days", `expected_salary` typed **"120000.0"**
instead of "USD 120,000", `years_of_experience` typed **"5.0"**. All four were
observed on live postings.

Both paths now call in here. `PROFILE_VALUE_FORMATTERS` is keyed by
**profile attribute name** (the thing both callers actually have), and
`answer_engine`'s category-keyed table is a thin mapping onto these same
functions — so a formatting change happens once, not twice.

`format_scalar()` is the type-based safety net underneath the named
formatters: any bool becomes Yes/No and any list becomes a comma-joined
string even with no named formatter registered. That's deliberate — the
failure mode this module exists to prevent is a NEW typed column being added
without anyone remembering this file exists, and a type-based default means
the worst case is "not ideally phrased" rather than "the literal word True in
an employer's form."
"""

from __future__ import annotations

from typing import Any, Callable

from automation.interfaces import CandidateProfile


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def format_requires_sponsorship(profile: CandidateProfile) -> str | None:
    """Prefers the boolean `requires_sponsorship` (a real yes/no fact), and
    appends the visa type when there is one — "Yes (H1B)" tells a recruiter
    more than "Yes" and costs nothing. Falls back to echoing the older
    free-text `work_authorization`/`visa_status` columns (pre-Phase-8
    behaviour, kept for profiles that never set the boolean)."""
    if profile.requires_sponsorship is not None:
        answer = _yes_no(profile.requires_sponsorship)
        if profile.requires_sponsorship and profile.visa_type:
            answer += f" ({profile.visa_type})"
        return answer
    return profile.work_authorization or profile.visa_status or None


def format_work_authorized(profile: CandidateProfile) -> str | None:
    """Same pattern as `format_requires_sponsorship`, minus the visa suffix —
    "are you authorized to work" is a plain yes/no, and appending a visa type
    to it would answer a question that wasn't asked."""
    if profile.work_authorized is not None:
        return _yes_no(profile.work_authorized)
    return profile.work_authorization or profile.visa_status or None


def format_visa_type(profile: CandidateProfile) -> str | None:
    return profile.visa_type or None


def format_notice_period(profile: CandidateProfile) -> str | None:
    if profile.notice_period_days is None:
        return None
    return f"{profile.notice_period_days} days"


def format_expected_salary(profile: CandidateProfile) -> str | None:
    if profile.expected_salary is None:
        return None
    salary = profile.expected_salary
    formatted = f"{int(salary):,}" if float(salary).is_integer() else f"{salary:,}"
    currency = profile.expected_salary_currency or ""
    return f"{currency} {formatted}".strip()


def format_current_salary(profile: CandidateProfile) -> str | None:
    """Same shaping as `format_expected_salary` — "USD 120,000", never the raw
    "120000.0" — reading the CURRENT columns. Falls back to
    `expected_salary_currency` for the unit only: a candidate who set one
    currency and two amounts means the same currency, and a bare number with no
    unit is worse than a number with the right one."""
    if profile.current_salary is None:
        return None
    salary = profile.current_salary
    formatted = f"{int(salary):,}" if float(salary).is_integer() else f"{salary:,}"
    currency = profile.current_salary_currency or profile.expected_salary_currency or ""
    return f"{currency} {formatted}".strip()


def format_preferred_name(profile: CandidateProfile) -> str | None:
    """Falls back to `first_name`, which is not a guess: a form asking what to
    call someone is correctly answered with their first name until they say
    otherwise. Returning `None` instead would leave a trivially fillable field
    blank on most Greenhouse and Lever forms."""
    return (profile.preferred_name or profile.first_name or "").strip() or None


def format_referral_source(profile: CandidateProfile) -> str | None:
    return (profile.referral_source or "").strip() or None


def format_employment_type(profile: CandidateProfile) -> str | None:
    """"full_time" -> "Full-time". Stored as a token (it has a VALID_* set), and
    a form's own option list says "Full-time" or "Full Time" — the hyphenated
    Title Case form is what `match_option` resolves against either of those,
    where the raw token resolves against neither."""
    value = (profile.employment_type_preference or "").strip()
    if not value or value == "no_preference":
        # "No preference" is a real stored answer but not an answer any form
        # offers as an option, so saying nothing lets the LLM (or a human) pick
        # from what the form actually lists.
        return None
    return value.replace("_", "-").title()


def format_willing_background_check(profile: CandidateProfile) -> str | None:
    if profile.willing_background_check is None:
        return None
    return _yes_no(profile.willing_background_check)


def format_languages(profile: CandidateProfile) -> str | None:
    """Every language the candidate listed, with its level — "English (Native),
    Hindi (Fluent)". For the "are you fluent in X?" yes/no question see
    `answer_engine._language_fluency_answer`; this is the free-text form, for a
    field that just asks which languages someone speaks."""
    entries = normalized_languages(profile)
    if not entries:
        return None
    parts = [
        f"{name} ({proficiency.title()})" if proficiency else name
        for name, proficiency in entries
    ]
    return ", ".join(parts) or None


def normalized_languages(profile: CandidateProfile) -> list[tuple[str, str]]:
    """`profile.languages` as `[(language, proficiency), ...]`, lowercased
    proficiencies, skipping anything unusable.

    Tolerates a bare list of strings (`["English"]`) as well as the documented
    list-of-objects shape: the API validates the objects, but a row written
    before that (or by hand) must not crash a fill — it just arrives with an
    empty proficiency, which makes a fluency question fall through to the LLM
    rather than being answered from an assumption about degree."""
    raw = profile.languages
    if not isinstance(raw, (list, tuple)):
        return []
    entries: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("language") or "").strip()
            proficiency = str(item.get("proficiency") or "").strip().lower()
        else:
            name, proficiency = str(item or "").strip(), ""
        if name:
            entries.append((name, proficiency))
    return entries


def format_years_of_experience(profile: CandidateProfile) -> str | None:
    if profile.years_of_experience is None:
        return None
    years = profile.years_of_experience
    return f"{int(years)} years" if float(years).is_integer() else f"{years} years"


def format_highest_education_level(profile: CandidateProfile) -> str | None:
    """Stored in the form's own vocabulary already ("Bachelor's Degree"), so
    there is nothing to reformat — this exists so the attribute has an explicit
    entry in `PROFILE_VALUE_FORMATTERS` rather than silently relying on
    `format_scalar`'s pass-through, and so `None`/"" both come back as `None`
    (which is what makes `answer_engine` fall through to the LLM+résumé path
    instead of filling a blank)."""
    return (profile.highest_education_level or "").strip() or None


def format_willing_to_relocate(profile: CandidateProfile) -> str | None:
    if profile.willing_to_relocate is None:
        return None
    return _yes_no(profile.willing_to_relocate)


def format_sponsorship_countries(profile: CandidateProfile) -> str | None:
    countries = profile.sponsorship_countries
    if not countries:
        return None
    return format_scalar(countries)


#: Profile attribute -> its formatter. Keyed by ATTRIBUTE (not question
#: category) because that is what both call sites have in hand:
#: `_resolve_profile_value(attribute)` and `FieldMapper`'s resolved match.
PROFILE_VALUE_FORMATTERS: dict[str, Callable[[CandidateProfile], str | None]] = {
    "requires_sponsorship": format_requires_sponsorship,
    "work_authorized": format_work_authorized,
    "visa_type": format_visa_type,
    "notice_period_days": format_notice_period,
    "expected_salary": format_expected_salary,
    "years_of_experience": format_years_of_experience,
    "sponsorship_countries": format_sponsorship_countries,
    "highest_education_level": format_highest_education_level,
    "willing_to_relocate": format_willing_to_relocate,
    "current_salary": format_current_salary,
    "preferred_name": format_preferred_name,
    "referral_source": format_referral_source,
    "employment_type_preference": format_employment_type,
    "willing_background_check": format_willing_background_check,
    "languages": format_languages,
}


def format_scalar(value: Any) -> Any:
    """Type-based fallback for any attribute with no named formatter.

    `bool` is checked before anything else on purpose: in Python `bool` is a
    subclass of `int`, so an `isinstance(value, int)` branch placed first would
    swallow `True`/`False` and re-introduce exactly the bug this module fixes.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return _yes_no(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, (list, tuple, set)):
        joined = ", ".join(str(item).strip() for item in value if str(item).strip())
        return joined or None
    return value  # str / int pass through; `str()` downstream is already correct


def format_profile_value(profile: CandidateProfile, attribute: str, raw: Any) -> Any:
    """The single entry point. A named formatter wins (it can consult other
    columns — `requires_sponsorship` reads `visa_type`); otherwise the raw
    value goes through the type-based net."""
    formatter = PROFILE_VALUE_FORMATTERS.get(attribute)
    if formatter is not None:
        return formatter(profile)
    return format_scalar(raw)
