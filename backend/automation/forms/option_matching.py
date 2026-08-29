"""
Resolving an answer to one of a form control's REAL options.

Lifted out of `automation/forms/answer_engine.py` (where it lived as
`_match_option`) once a second caller appeared: `automation/ats/base.py`'s
checkbox-group pass has to match a stored profile value against a group's
member labels, and it must do it by exactly the same rules — including the
refuse-on-ambiguity rule, which is the whole point. A second, subtly looser
copy of this logic in another module is precisely how a near-miss ends up
typed into an employer's form.

`answer_engine` keeps calling it as `_match_option`; nothing about its
behaviour changed in the move.
"""

from __future__ import annotations

from typing import Sequence


def normalize_option(text: str) -> str:
    """Case- and whitespace-insensitive form used only for MATCHING an
    answer back to a real option — never for what gets typed into the page.
    The verbatim option string is always what's filled."""
    return " ".join(text.split()).casefold()


def match_option(answer: str, options: Sequence[str]) -> str | None:
    """Resolves an answer to one of `options`, returning that option VERBATIM
    (so `field_handlers` selects a string the DOM really has), or `None` if it
    can't be resolved to exactly one.

    Three tiers, tightest first:

    1. Exact string equality.
    2. Case/whitespace-normalized equality — covers "yes" vs "Yes" and the
       stray trailing space an ATS puts in its own `<option>` text.
    3. Containment, but ONLY when exactly one option matches: a model
       answering "Yes" against `["Yes, now or in the future", "No"]` is
       clearly right, and rejecting it would send a perfectly answerable
       question to a human. Ambiguity is not resolved by picking the first
       or longest match — two candidates means `None`, which routes the
       question to review. That's the deliberate trade: this never guesses
       between plausible options (same rule as `FieldMapper`).
    """
    if not options:
        return None
    for option in options:
        if answer == option:
            return option
    normalized_answer = normalize_option(answer)
    if not normalized_answer:
        return None
    for option in options:
        if normalize_option(option) == normalized_answer:
            return option
    contained = [
        option for option in options
        if normalized_answer in normalize_option(option)
        or normalize_option(option) in normalized_answer
    ]
    return contained[0] if len(contained) == 1 else None
