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
from functools import lru_cache

# Canonical CandidateProfile attribute -> known label/placeholder/name
# synonyms across ATS platforms. Keys match real `app.models.db_models
# .CandidateProfile` attribute names (or, for encrypted columns, the
# plaintext-facing key `app/services/profile_repository.py` uses — "phone",
# "address" — never the `_encrypted` column name itself).
FIELD_SYNONYMS: dict[str, list[str]] = {
    # BEFORE "first_name", and the order is load-bearing:
    # `_first_matching_attribute` returns the first entry that matches while
    # iterating this table, and "preferred first name" contains "first name". Put
    # this after `first_name` and a form's preferred-name field gets the
    # candidate's LEGAL first name typed into it while the real first-name field
    # goes unfilled. `format_preferred_name` already falls back to `first_name`
    # when nothing is stored, so nothing is lost by claiming it here.
    "preferred_name": ["preferred name", "preferred first name", "nickname", "name you go by"],
    "first_name": ["first name", "given name", "legal first name"],
    # No "middle initial": that field wants one letter, and typing a whole
    # middle name into it is a wrong answer where a blank is a harmless one.
    "middle_name": ["middle name", "legal middle name"],
    "last_name": ["last name", "surname", "family name", "legal last name"],
    "full_name": ["full name", "candidate name", "your name"],
    "email": ["email", "email address", "e-mail"],
    "phone": ["phone", "phone number", "mobile", "contact number"],
    "location": ["location", "current location", "city, state"],
    "city": ["city"],
    "state": ["state", "province"],
    "postal_code": ["zip", "zip code", "zipcode", "postal code", "postcode", "pin code"],
    "country": ["country"],
    "address": ["address", "street address", "mailing address"],
    "time_zone": ["time zone", "timezone"],
    "linkedin_url": ["linkedin", "linkedin url", "linkedin profile"],
    "github_url": ["github", "github url"],
    "portfolio_url": ["portfolio", "portfolio url", "personal website"],
    "website_url": ["website", "website url", "personal site"],
    "current_company": ["current employer", "current company"],
    "current_role": ["current title", "current role", "current position"],
    "years_of_experience": ["years of experience", "total experience"],
    "notice_period_days": ["notice period"],
    # Current before expected: these are different facts (see
    # `CandidateProfile.current_salary`), and neither synonym list contains the
    # other's phrasing, so the ordering here is documentation rather than a
    # dependency.
    "current_salary": ["current salary", "current ctc", "current compensation", "current package"],
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
    # Two more profile-backed facts forms ask for directly. Both also have a
    # `question_classifier` category for when they arrive as a full sentence
    # rather than a short label — this table is the short-label half (a
    # `<select name="education_level">`, a "Willing to relocate?" checkbox), and
    # the two layers overlapping in vocabulary is by design (see that module's
    # docstring). No "relocation"/"relocate" on its own here either: "relocation
    # assistance" is a question about money, not willingness.
    "highest_education_level": [
        "highest level of education", "highest education level", "highest education",
        "level of education", "education level", "highest degree",
    ],
    "willing_to_relocate": ["willing to relocate", "open to relocation", "able to relocate"],
    # AFTER willing_to_relocate, same load-bearing order as the matching
    # `question_classifier` categories: a "Willing to relocate?" checkbox whose
    # label mentions the package stays a willingness question, and only a field
    # that asks about assistance alone reaches this entry.
    "requires_relocation_assistance": ["relocation assistance", "relocation support", "relocation package"],
    "willing_to_travel": ["willing to travel", "able to travel", "open to travel"],
    "referral_source": ["how did you hear", "how did you find", "referral source"],
    # AFTER referral_source, for the reason spelled out on the matching
    # `question_classifier` category: a combined "how did you hear (if referred,
    # by whom)" field is the source question, and only a dedicated referrer field
    # gets a person's name.
    "referrer_name": ["referred by", "referrer", "referrer name", "who referred you"],
    "employment_type_preference": ["employment type", "type of employment", "employment preference"],
    "willing_background_check": ["background check", "background screening"],
    "willing_drug_test": ["drug test", "drug screen", "drug screening"],
    # No bare "license"/"licence" — a professional license (nursing, PE, legal)
    # is a different question this column would answer wrongly.
    "has_drivers_license": [
        "driver's license", "driver’s license", "drivers license", "driver license",
        "driving license", "driving licence",
    ],
    "age_over_18": ["at least 18", "18 years or older", "18 or older", "over the age of 18"],
    "security_clearance": ["security clearance", "clearance level", "active clearance"],
    # "Earliest start date", never a bare "start date": Greenhouse's Education
    # block renders "Start date year" fields, and a notice-period-shaped answer
    # in a degree's start-year box is confidently wrong (the same note appears on
    # `question_classifier`'s NOTICE_PERIOD phrases).
    "earliest_start_date": [
        "earliest start date", "earliest available start", "available start date",
        "availability date", "date available to start",
    ],
    "professional_summary": [
        "professional summary", "profile summary", "candidate summary",
        # No "about you"/"tell us about yourself": those are prose questions
        # tailored per posting, and `answer_engine`'s LLM path (which can now see
        # this column — see `_PROMPT_PROFILE_ATTRIBUTES`) writes a better answer
        # for them than a verbatim paste of the stored summary.
    ],
    # "languages spoken", never a bare "languages": a "Programming languages"
    # field is extremely common on engineering applications, and filling it with
    # the candidate's SPOKEN languages would be confidently wrong. Their
    # programming languages live in `skills`, which this table doesn't map.
    "languages": ["languages spoken", "spoken languages", "languages you speak"],
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


@lru_cache(maxsize=512)
def _synonym_pattern(synonym: str) -> re.Pattern[str]:
    """A synonym must match on WORD BOUNDARIES, not as a bare substring.

    Plain containment produced a genuinely dangerous false positive on a live
    Lever form: the label "Are you legally authorized to work in the United
    States?" matched the `state` synonym — "state" sits inside "States" — and
    resolved to `('state', 0.9)`. That's above the 0.85 auto-submit bar, so the
    candidate's home state would have been typed into a work-authorization
    question and submitted. "province" inside "provincial", "city" inside
    "capacity", and "currency" inside "concurrency" are the same failure
    waiting to happen.

    This does NOT loosen the "simple and explainable" contract in the module
    docstring, and it does not change the deliberate `name="company"` behaviour
    described there — a synonym still has to appear verbatim in the signal,
    just as whole words rather than as any character run."""
    return re.compile(rf"\b{re.escape(synonym)}\b")


def _matches_synonym(normalized: str, synonyms: list[str]) -> bool:
    return any(_synonym_pattern(synonym).search(normalized) for synonym in synonyms)


#: Prose that marks a label as a marketing/consent OPT-IN — a boolean choice
#: the candidate makes — rather than a request for a profile value.
#:
#: The bug this fixes: "Send me job alerts by email" matched the `email`
#: synonym (correctly, on a word boundary — "by email"), resolved to
#: `('email', 0.9)`, and the candidate's email address was then handed to
#: `CheckboxHandler`, which ticked the marketing opt-in. Confidence 0.9 is
#: above the auto-submit bar, so a candidate could be silently subscribed to
#: marketing email on a real application.
#:
#: These labels belong to the checkbox-intent path, never the text-value path,
#: so a match here suppresses the mapping regardless of which synonym hit.
#: Written as patterns, not one label: any future "Notify me about...",
#: "Subscribe to...", "Sign up for..." phrasing is covered too.
#:
#: Deliberately applied ONLY to prose signals (label, nearby text) and NOT to
#: `name`/`id`. A machine identifier like `name="email"` is never marketing
#: prose, and suppressing on it would break ordinary email fields.
_OPT_IN_LABEL_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bsend me\b",
    r"\bnotify me\b",
    r"\bemail me\b",
    r"\btext me\b",
    r"\bcontact me (about|with|regarding)\b",
    r"\bsubscribe\b",
    r"\bopt[\s-]?in\b",
    r"\bsign me up\b",
    r"\bsign up for\b",
    r"\bkeep me (posted|informed|updated|in the loop)\b",
    r"\b(job|email|marketing) alerts\b",
    r"\bnewsletter\b",
    r"\bi (would like|want) to receive\b",
    r"\breceive (updates|news|emails|marketing|communications|notifications)\b",
))


def looks_like_opt_in_label(text: str) -> bool:
    """True when `text` reads as a marketing/consent opt-in rather than a
    request for a value. Public because `automation/ats/base.py` and its tests
    reason about the same distinction."""
    return any(pattern.search(text) for pattern in _OPT_IN_LABEL_PATTERNS)


def _first_matching_attribute(normalized: str) -> str | None:
    if not normalized:
        return None
    for attribute, synonyms in FIELD_SYNONYMS.items():
        if _matches_synonym(normalized, synonyms):
            return attribute
    return None


def _first_matching_attribute_any_variant(normalized_variants: list[str]) -> str | None:
    for attribute, synonyms in FIELD_SYNONYMS.items():
        for normalized in normalized_variants:
            if normalized and _matches_synonym(normalized, synonyms):
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

        # Prose signals only: an opt-in label is a boolean choice, not a value
        # request, so it must not resolve to a profile attribute even when a
        # synonym legitimately matches inside it. See `_OPT_IN_LABEL_PATTERNS`.
        if label and not looks_like_opt_in_label(label):
            attribute = _first_matching_attribute(_normalize_text(label))
            if attribute:
                return attribute, LABEL_MATCH_CONFIDENCE

        if placeholder:
            attribute = _first_matching_attribute(_normalize_text(placeholder))
            if attribute:
                return attribute, PLACEHOLDER_MATCH_CONFIDENCE

        if nearby_text and not looks_like_opt_in_label(nearby_text):
            attribute = _first_matching_attribute(_normalize_text(nearby_text))
            if attribute:
                return attribute, NEARBY_TEXT_MATCH_CONFIDENCE

        return None
