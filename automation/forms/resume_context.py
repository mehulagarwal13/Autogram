"""
The candidate's résumé, in the shape the answer LLM actually needs.

Why this module exists: `ApplicationAnswerEngine` used to send the model FOUR
facts about the candidate — `current_role`, `current_company`,
`years_of_experience`, `skills`. Everything else the résumé knows was parsed,
stored, and then never shown to the model that has to answer questions about it.

The consequence, observed live on a real Greenhouse posting: "In which year did
you complete your Bachelor's degree?" and a required "Degree" dropdown both came
back `null` and were left blank. Not a bug in the model or the dropdown code —
the answer simply wasn't in the prompt. `_SYSTEM_PROMPT` forbids inventing
facts, so with no education in context, `null` was the CORRECT response. The fix
belongs here, in what gets sent, not in loosening that rule.

Two deliberate choices about what this does and doesn't include:

- **Structured rows, not the résumé PDF.** `EducationEntry` / `ExperienceEntry`
  already hold the parsed résumé (written by `app/services/resume_parser.py`),
  so the facts are available without re-parsing a file on the fill path, and
  they arrive as fields the model can quote exactly — `end_date="2018"` answers
  a graduation-year question far more reliably than the model finding "2018"
  somewhere in three pages of prose.
- **No job descriptions or achievement bullets.** `ExperienceEntry.description`
  is deliberately dropped. It is the largest text on a résumé, it is the least
  useful for answering a screening question, and it is prose the model can
  plausibly paste verbatim into a form field — the exact failure
  `_META_COMMENTARY_PATTERNS` exists to catch. Titles, employers and dates are
  what screening questions actually ask about.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# Caps, so a long career can't crowd the questions out of an 800-token answer
# budget. Ordered most-recent-first before truncating, so what survives is the
# part a screening question is most likely to be about.
MAX_EDUCATION_ENTRIES = 5
MAX_EXPERIENCE_ENTRIES = 8

_EDUCATION_FIELDS = ("degree", "field_of_study", "university", "start_date", "end_date")
_EXPERIENCE_FIELDS = ("job_title", "company_name", "start_date", "end_date")


def _clean(value: Any) -> str | None:
    """A trimmed string, or `None` for anything empty — so a blank column shows
    up as an absent key rather than as `""`, which a model can read as a real
    (empty) answer."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _entry_facts(entry: Any, fields: Sequence[str]) -> dict[str, str]:
    """Only the attributes that are actually populated. Reading via `getattr`
    with a default keeps this working against an ORM row, a dataclass view
    (`automation/interfaces.py`'s `EducationView`), or a test double."""
    facts = {}
    for name in fields:
        value = _clean(getattr(entry, name, None))
        if value is not None:
            facts[name] = value
    return facts


def _recency_key(facts: dict[str, str]) -> tuple[int, str]:
    """Sorts most-recent-first. An entry with no end date is treated as CURRENT
    (an ongoing degree or the present job — both `EducationEntry.end_date` and
    `ExperienceEntry.end_date` use empty/None for that, see their columns), so
    it sorts ahead of every dated one. Dates are free-form strings ("2018",
    "2018-08"), which compare correctly for this purpose precisely because
    ISO-ish prefixes sort lexicographically the same way they sort
    chronologically."""
    end = facts.get("end_date")
    if end is None:
        return (1, "")
    return (0, end)


def _facts_list(entries: Iterable[Any], fields: Sequence[str], limit: int) -> tuple[dict[str, str], ...]:
    facts = [_entry_facts(entry, fields) for entry in entries or ()]
    facts = [f for f in facts if f]  # a row where every column was blank tells the model nothing
    facts.sort(key=_recency_key, reverse=True)
    return tuple(facts[:limit])


def _certifications(profile: Any) -> tuple[str, ...]:
    """`CandidateProfile.skills` is a JSONB dict whose `certifications` key is a
    `list[str]` (see the column's comment in `app/models/db_models.py`).
    Included because "do you hold X certification?" is a common screening
    question, and answering it from the résumé beats leaving it blank."""
    skills = getattr(profile, "skills", None)
    if not isinstance(skills, dict):
        return ()
    raw = skills.get("certifications")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(c for c in (_clean(item) for item in raw) if c)


@dataclass(frozen=True)
class ResumeContext:
    """The résumé facts sent alongside the questions. Frozen and plain-data on
    purpose: it is built once per run, only ever read, and must be trivially
    constructible in a test with no database."""

    education: tuple[dict[str, str], ...] = ()
    experience: tuple[dict[str, str], ...] = ()
    certifications: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.education or self.experience or self.certifications)

    def as_prompt_payload(self) -> dict[str, Any]:
        """Only the sections that have content. An empty section is omitted
        rather than sent as `[]`: a form whose candidate has no stored
        education produces byte-for-byte the prompt it did before this module
        existed, and the model is never shown an empty list it could read as
        "this candidate has no degree" — which is a fact, not a gap, and not
        one we know."""
        payload: dict[str, Any] = {}
        if self.education:
            payload["education"] = [dict(entry) for entry in self.education]
        if self.experience:
            payload["experience"] = [dict(entry) for entry in self.experience]
        if self.certifications:
            payload["certifications"] = list(self.certifications)
        return payload


def build_resume_context(
    profile: Any = None,
    education: Iterable[Any] = (),
    experience: Iterable[Any] = (),
) -> ResumeContext:
    """Pure assembly — no DB, no I/O. `load_resume_context` is the DB-backed
    wrapper; this is what tests and any caller holding rows already use."""
    return ResumeContext(
        education=_facts_list(education, _EDUCATION_FIELDS, MAX_EDUCATION_ENTRIES),
        experience=_facts_list(experience, _EXPERIENCE_FIELDS, MAX_EXPERIENCE_ENTRIES),
        certifications=_certifications(profile),
    )


def load_resume_context(db: Any, profile: Any) -> ResumeContext:
    """Best-effort load from the candidate's stored résumé rows.

    Never raises: a missing `db`, a profile with no `profile_id`, or a failing
    query all yield an EMPTY context, which downstream means "the model gets
    what it always got" — the same fail-open contract `_get_demographics` uses.
    Losing résumé enrichment costs one unfilled field a human can complete;
    letting a query error escape would abort the whole application run."""
    if db is None:
        return build_resume_context(profile)
    profile_id = getattr(profile, "profile_id", None)
    if not profile_id:
        return build_resume_context(profile)
    # Imported here rather than at module scope so this module stays importable
    # (and unit-testable) without the app's DB layer being constructible.
    from automation.interfaces import list_education, list_experience

    try:
        education = list_education(db, profile_id)
        experience = list_experience(db, profile_id)
    except Exception:  # noqa: BLE001 - enrichment is never worth failing a run over
        import logging

        logging.getLogger(__name__).debug(
            "Could not load résumé rows for profile %r — answering from the base profile only.",
            profile_id,
        )
        return build_resume_context(profile)
    return build_resume_context(profile, education=education, experience=experience)
