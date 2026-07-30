"""
FieldMapper — Phase 5 (see ARCHITECTURE.md).

Resolves an on-page form field's raw DOM signals (label text, placeholder,
`name`/`id` attribute, nearby text) to a canonical `CandidateProfile`
attribute path (see `automation/interfaces.py`), e.g. "Given Name" ->
"first_name", `name="job_application[first_name]"` -> "first_name".

Design: a small static synonym table per canonical profile field
(`FIELD_SYNONYMS`), checked in order of decreasing certainty:

    name/id attribute  >  label text  >  placeholder  >  nearby text

`name`/`id` comes first even though it looks like the least "human" signal —
in practice it's the most reliable, because it's the ATS backend's own field
identifier and rarely changes across a redesign, whereas label text is
frequently reworded. Only falls through to the LLM
(`automation/forms/answer_engine.py`, Phase 6) when nothing matches with
sufficient confidence — this class never guesses.

Matching is intentionally a simple, explainable containment check (a
synonym is a substring of the normalized signal), not fuzzy/ML matching —
same "cheapest deterministic path first" philosophy as `ats/detector.py`'s
tiered detection. One real trade-off worth knowing: a short, ambiguous
backend field name like `name="company"` alone will NOT match
`current_company`'s multi-word synonyms ("current employer", "current
company"), because the synonym isn't a substring of the bare word "company"
— on purpose. Guessing that a field called just "company" means the
*current* employer (rather than a previous one, or the job's own posting
company) is exactly the kind of ambiguous call this class is designed to
decline rather than get wrong. Add a shorter synonym to `FIELD_SYNONYMS` if
a specific ATS is known to use one — that's a config change, not an
algorithm change.
"""

from __future__ import annotations

import re

# Canonical CandidateProfile attribute -> known label/placeholder/name
# synonyms across ATS platforms. Keys match real `app.models.db_models
# .CandidateProfile` attribute names (or, for encrypted columns, the
# plaintext-facing key `app/services/profile_repository.py` uses — "phone",
# "address" — never the `_encrypted` column name itself).
FIELD_SYNONYMS: dict[str, list[str]] = {
    "first_name": ["first name", "given name", "legal first name"],
    "last_name": ["last name", "surname", "family name", "legal last name"],
    "full_name": ["full name", "candidate name", "your name"],
    "email": ["email", "email address", "e-mail"],
    "phone": ["phone", "phone number", "mobile", "contact number"],
    "location": ["location", "current location", "city, state"],
    "city": ["city"],
    "state": ["state", "province"],
    "country": ["country"],
    "address": ["address", "street address", "mailing address"],
    "linkedin_url": ["linkedin", "linkedin url", "linkedin profile"],
    "github_url": ["github", "github url"],
    "portfolio_url": ["portfolio", "portfolio url", "personal website"],
    "website_url": ["website", "website url", "personal site"],
    "current_company": ["current employer", "current company"],
    "current_role": ["current title", "current role", "current position"],
    "years_of_experience": ["years of experience", "total experience"],
    "notice_period_days": ["notice period"],
    "expected_salary": ["expected salary", "expected ctc", "salary expectation"],
    "expected_salary_currency": ["salary currency", "currency"],
    "work_authorization": ["work authorization", "authorized to work"],
    "visa_status": ["visa status"],
    # Phase 8 — narrower, boolean-typed compliance fields (see
    # app/models/db_models.py's CandidateProfile docstring and
    # automation/forms/question_classifier.py's module docstring for why
    # these are distinct from work_authorization/visa_status above). Kept
    # AFTER "work_authorization" in this dict on purpose: `_first_matching_attribute`
    # returns the first matching entry it finds while iterating this table
    # in order, and "authorized to work" is deliberately still claimed by
    # the existing free-text `work_authorization` attribute (a real, tested
    # behavior — see automation/tests/test_greenhouse_adapter.py's bracket-
    # notation-label regression tests) rather than being reassigned here.
    # `requires_sponsorship`'s own synonyms don't overlap with either.
    "requires_sponsorship": ["require sponsorship", "visa sponsorship", "need sponsorship"],
    "visa_type": ["visa type", "type of visa"],
}

# Confidence per signal tier, decreasing certainty top to bottom — mirrors
# `automation/ats/detector.py`'s URL/DOM/meta confidence constants.
NAME_MATCH_CONFIDENCE = 0.97
LABEL_MATCH_CONFIDENCE = 0.9
PLACEHOLDER_MATCH_CONFIDENCE = 0.75
NEARBY_TEXT_MATCH_CONFIDENCE = 0.55

_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_BRACKET_OR_DOT_SEGMENT = re.compile(r"[\[\].]")
_WHITESPACE = re.compile(r"\s+")


def _normalize_text(value: str) -> str:
    """For human-readable signals (label, placeholder, nearby text): trim,
    drop a trailing required-field marker ("LinkedIn Profile*" -> "linkedin
    profile" — very common in real ATS forms), lowercase, and collapse
    internal whitespace. Order matters: the asterisk is usually the very
    last character before trailing whitespace, not necessarily the last
    character of the raw string."""
    return _WHITESPACE.sub(" ", value.strip().rstrip("*").strip().lower())


def _normalize_name_or_id_variants(value: str) -> list[str]:
    """For machine identifiers (`name`/`id` attributes), which aren't prose:

    - Bracket/dot notation (`"job_application[first_name]"`,
      `"candidate.first_name"`) — keep only the last, most specific segment,
      since that's the part that actually names the field.
    - snake_case / kebab-case (`"first_name"`, `"first-name"`) — underscores
      and hyphens become spaces, always.
    - camelCase is ambiguous, so this returns TWO variants and lets the
      caller check both: a "split" one (`"firstName"` -> `"first name"`,
      treating the capital as a joined-word boundary) and a "contiguous" one
      (`"firstName"` -> `"firstname"`, no split at all). Splitting is right
      for two-joined-words identifiers like `firstName`, but wrong for brand
      names with an internal capital that AREN'T two words, like `LinkedIn`
      or `GitHub` (splitting would produce "linked in" / "git hub", which no
      longer contains "linkedin" / "github"). Trying both variants against
      `FIELD_SYNONYMS` lets either style resolve correctly without having to
      special-case every brand name that happens to use internal capitals.
    """
    segments = [segment for segment in _BRACKET_OR_DOT_SEGMENT.split(value) if segment]
    last_segment = segments[-1] if segments else value
    contiguous = _normalize_text(last_segment.replace("_", " ").replace("-", " "))
    split = _normalize_text(_CAMEL_CASE_BOUNDARY.sub(" ", last_segment).replace("_", " ").replace("-", " "))
    return [contiguous] if contiguous == split else [contiguous, split]


def _first_matching_attribute(normalized: str) -> str | None:
    if not normalized:
        return None
    for attribute, synonyms in FIELD_SYNONYMS.items():
        if any(synonym in normalized for synonym in synonyms):
            return attribute
    return None


def _first_matching_attribute_any_variant(normalized_variants: list[str]) -> str | None:
    for attribute, synonyms in FIELD_SYNONYMS.items():
        for normalized in normalized_variants:
            if normalized and any(synonym in normalized for synonym in synonyms):
                return attribute
    return None


class FieldMapper:
    """Resolves one on-page form field's raw DOM signals to a
    `(profile_attribute, confidence)` pair, or `None` if nothing matches
    confidently — see module docstring for the tiering and its trade-offs."""

    @staticmethod
    def map_field(
        label: str | None = None,
        placeholder: str | None = None,
        name: str | None = None,
        nearby_text: str | None = None,
    ) -> tuple[str, float] | None:
        """Checks each supplied signal in order of decreasing certainty and
        returns the first match. Callers don't need to supply every signal —
        pass whatever the page actually has for this field (e.g. a bare
        `<input name="...">` with no `<label>` at all is a completely normal
        call with only `name` set)."""
        if name:
            attribute = _first_matching_attribute_any_variant(_normalize_name_or_id_variants(name))
            if attribute:
                return attribute, NAME_MATCH_CONFIDENCE

        if label:
            attribute = _first_matching_attribute(_normalize_text(label))
            if attribute:
                return attribute, LABEL_MATCH_CONFIDENCE

        if placeholder:
            attribute = _first_matching_attribute(_normalize_text(placeholder))
            if attribute:
                return attribute, PLACEHOLDER_MATCH_CONFIDENCE

        if nearby_text:
            attribute = _first_matching_attribute(_normalize_text(nearby_text))
            if attribute:
                return attribute, NEARBY_TEXT_MATCH_CONFIDENCE

        return None
