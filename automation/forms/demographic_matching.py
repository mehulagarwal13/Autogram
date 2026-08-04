"""
Turning a STORED demographic value into the option string a given form offers.

This closes a gap that made the "answer EEO questions only from what the
candidate explicitly stored" rule fail silently in practice. The values in
`app/models/db_models.py`'s `VALID_GENDER_VALUES` / `VALID_VETERAN_STATUS_VALUES`
/ `VALID_DISABILITY_STATUS_VALUES` are storage tokens — `non_binary`,
`decline_to_answer`, `not_veteran`, `no_disability` — and real ATS dropdowns
word the same answers as prose:

    stored "non_binary"        form ["Female", "Male", "Non-binary"]
    stored "decline_to_answer" form [..., "Decline to self-identify"]
    stored "not_veteran"       form ["I am not a protected veteran", ...]
    stored "no_disability"     form ["No, I do not have a disability and have
                                      not had one in the past", ...]

`option_matching.match_option` correctly refuses every one of those: the
underscore form is neither equal to nor contained in the form's wording. So
`answer_engine._demographic_answer` found a stored value, failed to map it, and
surfaced the question for a human anyway — the exact outcome storing the answer
was supposed to prevent. Observed on a live Lever posting, where Gender, Race,
and Veteran status all came out blank.

Two mechanisms, cheapest first, both of which end in `match_option` so the
"resolve to exactly one real option or refuse" rule is unchanged:

1. **Token variants** — mechanical: `non_binary` also tried as "non binary",
   "non-binary", "nonbinary". No vocabulary knowledge needed.
2. **Known phrasings** — a small ordered table of how real forms word each
   canonical answer. Order inside each entry is load-bearing: the most
   specific/longest phrasing is tried FIRST, so a form that spells out "No, I
   do not have a disability and have not had one in the past" matches on that
   rather than on the bare "No", which `match_option`'s containment tier would
   find ambiguous ("no" is also inside "I do not want to answer").

What this deliberately does NOT do: infer, translate between different facts,
or widen what may be filled. Every candidate string still has to resolve to
exactly one option the DOM actually has, and an unresolvable stored value still
surfaces for a human. This module only lets a value the candidate DID give be
recognized in the vocabulary the form happens to use.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from automation.forms.option_matching import match_option

#: Every "I'd rather not say" wording seen across real EEO forms, shared by all
#: four demographic categories. One list rather than per-category ones because
#: candidates are tried in order against THIS form's options and non-matches
#: cost nothing: a gender select resolves on "Decline to self-identify", a
#: disability one on "I do not want to answer", and neither is confused by the
#: other being in the list.
_DECLINE_PHRASINGS: tuple[str, ...] = (
    "Decline to self-identify",
    "I decline to self-identify",
    "I don't wish to answer",
    "I do not wish to answer",
    "I do not want to answer",
    "I don't wish to disclose",
    "I do not wish to disclose",
    "Decline to answer",
    "Prefer not to say",
    "Prefer not to disclose",
    "Prefer not to answer",
    "Choose not to disclose",
    "Do not wish to disclose",
    "Not disclosed",
)

#: Canonical stored token -> real-world form phrasings, longest/most specific
#: first (see module docstring on why that order matters).
CANONICAL_OPTION_PHRASINGS: dict[str, tuple[str, ...]] = {
    # --- gender (VALID_GENDER_VALUES) ---
    "male": ("Male", "Man"),
    "female": ("Female", "Woman"),
    "non_binary": ("Non-binary", "Non binary", "Nonbinary", "Non-Binary/Genderqueer"),
    "self_described": (
        "I prefer to self-describe", "Prefer to self-describe", "Self-describe",
        "Self-identify", "Another gender identity",
    ),

    # --- veteran status (VALID_VETERAN_STATUS_VALUES) ---
    # "Yes"/"No" come last on purpose: a form offering the long OFCCP wording
    # should match that, and a plain Yes/No form has no long option for the
    # earlier candidates to hit.
    "veteran": (
        "I identify as one or more of the classifications of a protected veteran",
        "I identify as one or more of the classifications of protected veteran",
        "I am a protected veteran",
        "I am one or more of the classifications of a protected veteran",
        "Protected veteran",
        "Yes",
    ),
    "not_veteran": (
        "I am not a protected veteran",
        "I am not a veteran",
        "Not a protected veteran",
        "No, I am not a protected veteran",
        "No",
    ),

    # --- disability status (VALID_DISABILITY_STATUS_VALUES) ---
    "has_disability": (
        "Yes, I have a disability, or have had one in the past",
        "Yes, I have a disability",
        "I have a disability",
        "Yes",
    ),
    "no_disability": (
        "No, I do not have a disability and have not had one in the past",
        "No, I do not have a disability",
        "No, I don't have a disability",
        "I do not have a disability",
        "No",
    ),

    # --- shared across every category ---
    "decline_to_answer": _DECLINE_PHRASINGS,

    # --- pronouns (free text, so these are conveniences rather than a schema) ---
    # No bare "he"/"him"/"her": `match_option`'s containment tier would match
    # "he" inside "She/her", and a wrong pronoun on a real application is worse
    # than a blank one.
    # Both spellings of each key: a user storing pronouns the way forms word
    # them ("he/him") and one following the snake_case convention of the gender/
    # veteran columns ("he_him") should both resolve.
    "he/him": ("He/him", "He/Him/His", "He/him/his", "he / him"),
    "he_him": ("He/him", "He/Him/His", "He/him/his"),
    "she/her": ("She/her", "She/Her/Hers", "She/her/hers", "she / her"),
    "she_her": ("She/her", "She/Her/Hers", "She/her/hers"),
    "they/them": ("They/them", "They/Them/Theirs", "They/them/theirs", "they / them"),
    "they_them": ("They/them", "They/Them/Theirs", "They/them/theirs"),
    "use_name_only": ("Use name only", "Just my name", "Name only"),
}


def _token_variants(value: str) -> tuple[str, ...]:
    """Mechanical re-spellings of a storage token — `non_binary` as "non
    binary", "non-binary", "nonbinary". Pure punctuation shuffling, no
    vocabulary knowledge, so it can't introduce a wrong answer: every variant
    still has to resolve against the form's real options."""
    spaced = value.replace("_", " ").replace("-", " ").strip()
    if not spaced:
        return ()
    return tuple(dict.fromkeys((
        spaced,
        spaced.replace(" ", "-"),
        spaced.replace(" ", ""),
    )))


def option_candidates(stored_value: str) -> tuple[str, ...]:
    """Every string worth trying against a form's option list for one stored
    demographic value, in order: the value exactly as stored (so a candidate who
    typed the form's own wording matches first and this table is bypassed
    entirely), then its mechanical variants, then known real-world phrasings."""
    if not stored_value:
        return ()
    candidates = [stored_value, *_token_variants(stored_value)]
    candidates.extend(CANONICAL_OPTION_PHRASINGS.get(stored_value.strip().casefold(), ()))
    return tuple(dict.fromkeys(c for c in candidates if c))


def match_demographic_value(stored_value: str, options: Sequence[str]) -> str | None:
    """The stored value as one of `options`, VERBATIM, or `None` if no candidate
    phrasing resolves to exactly one of them — in which case the caller must
    leave the question for a human (never guess: see
    `answer_engine._demographic_answer`)."""
    if not stored_value or not options:
        return None
    for candidate in option_candidates(stored_value):
        matched = match_option(candidate, options)
        if matched is not None:
            return matched
    return None


def match_demographic_values(stored_values: Iterable[str], options: Sequence[str]) -> list[str]:
    """Multi-select form of the above, for "select all that apply" groups
    (ethnicity checkbox groups). Returns the matched options in the order the
    stored values were given, de-duplicated, silently dropping any stored value
    this form has no option for — one unmatched entry in a list of five must not
    throw away the four that did match, and a checkbox group has no way to
    express "some of this answer" other than ticking what it can."""
    matched: list[str] = []
    for value in stored_values:
        option = match_demographic_value(value, options)
        if option is not None and option not in matched:
            matched.append(option)
    return matched
