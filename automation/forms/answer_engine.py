"""
ApplicationAnswerEngine — Phase 6 (see ARCHITECTURE.md).

Answers screening questions that `automation/ats/base.py::ATSAdapter`
couldn't resolve to a known profile field via `FieldMapper` (Phase 5). By
the time a question reaches this class, FieldMapper has already tried (and
failed) to match its label/name/placeholder against a known synonym — so
what's left is either (a) a real fact phrased in a way FieldMapper's
substring synonyms didn't happen to cover (a second, slightly broader
deterministic check below), or (b) a genuinely subjective/novel question
("Why do you want to work here?", "Describe a time you..."), which only an
LLM can answer at all.

Two paths, cheapest first:

1. **Deterministic** (free, instant, never fabricated): `question_classifier.py`
   recognizes a handful of recurring *factual* screening-question shapes —
   work authorization, sponsorship, visa type, notice period, salary
   expectation, years of experience — and answers straight from
   `CandidateProfile`. If the relevant profile field is empty, this path
   declines rather than guess (same philosophy as `FieldMapper` "never
   guess") and falls through to the LLM path instead. Note this is
   deliberately a fallback, not the primary mechanism: FieldMapper (Phase 5)
   already answers most of these directly via label/name matching before a
   question ever reaches this class.

   **Demographic questions (Phase 8) are a hard exception to "fall through
   to the LLM":** gender, veteran status, disability status, and
   race/ethnicity are NEVER answered by the LLM and NEVER inferred — only
   from a value the candidate explicitly stored via `PUT
   /profile/demographics` (`app/models/db_models.py::CandidateDemographics`).
   If nothing is stored yet, the question is surfaced as
   `SOURCE_NEEDS_USER_INPUT` (zero confidence, empty answer) instead of ever
   reaching `_call_llm` — see `_demographic_answer` below.
2. **LLM** (costed, via `automation.interfaces.generate_answer` ->
   `app.ai.llm.router`): every question that isn't a recognized factual
   shape, plus any factual shape whose profile field was empty.
   `answer_batch()` sends every still-unanswered question for one form in a
   SINGLE call (see ARCHITECTURE.md "Fill priority: cheapest path first")
   rather than one call per question. The LLM is instructed never to invent
   concrete personal facts it wasn't given — if it can't produce a usable
   answer, the question is left unanswered (this class never types a
   placeholder/error string into a real application field).

Both paths are backed by a persistent, per-user, exact-match answer cache
(`app/services/answer_cache_repository.py`) keyed by normalized question
text — pass `db`/`user_id` at construction to use it; omitting them (e.g. in
tests, or any caller without a DB session handy) just means every call
re-derives its answer instead of reusing a stored one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.services import answer_cache_repository
from automation.forms.question_classifier import (
    CATEGORY_EXPECTED_SALARY,
    CATEGORY_NOTICE_PERIOD,
    CATEGORY_REQUIRES_SPONSORSHIP,
    CATEGORY_VISA_TYPE,
    CATEGORY_WORK_AUTHORIZED,
    CATEGORY_YEARS_OF_EXPERIENCE,
    classify_question,
    is_demographic,
)
from automation.interfaces import (
    CandidateDemographics,
    CandidateProfile,
    generate_answer,
    get_candidate_demographics,
)

logger = logging.getLogger(__name__)


@dataclass
class AnswerResult:
    question: str
    answer: str
    source: str  # "deterministic" | "cache" | "llm" | "needs_user_input"
    confidence: float


#: A demographic question the candidate has never answered before. NOT sent
#: to the LLM (see module docstring / PART 2 of the request that created
#: this) — surfaces with a zero-confidence, empty answer so
#: `ApplicationFlowManager`'s confidence score correctly reflects "a human
#: needs to answer this once," and the run lands in `needs_review` instead
#: of either guessing or silently leaving a required EEO question blank.
SOURCE_NEEDS_USER_INPUT = "needs_user_input"


# --- deterministic classification -------------------------------------
# Phase 8: classification itself now lives in `question_classifier.py`
# (shared, narrower categories — see that module's docstring for why
# WORK_AUTHORIZED and REQUIRES_SPONSORSHIP are no longer one bucket). This
# module's job is purely "given a category, what's the answer" — reading the
# right `CandidateProfile`/`CandidateDemographics` field and formatting it.

def _format_requires_sponsorship(profile: CandidateProfile) -> str | None:
    """Prefers the new boolean `requires_sponsorship` (a real yes/no fact) —
    only falls back to echoing the old free-text `work_authorization`/
    `visa_status` fields (pre-Phase-8 behavior, kept for any profile that
    hasn't set the new field yet) when the boolean itself was never set."""
    if profile.requires_sponsorship is not None:
        answer = "Yes" if profile.requires_sponsorship else "No"
        if profile.requires_sponsorship and profile.visa_type:
            answer += f" ({profile.visa_type})"
        return answer
    return profile.work_authorization or profile.visa_status or None


def _format_work_authorized(profile: CandidateProfile) -> str | None:
    """Same pattern as `_format_requires_sponsorship`: prefer the real
    boolean; fall back to the old free-text echo if it was never set."""
    if profile.work_authorized is not None:
        return "Yes" if profile.work_authorized else "No"
    return profile.work_authorization or profile.visa_status or None


def _format_visa_type(profile: CandidateProfile) -> str | None:
    return profile.visa_type or None


def _format_notice_period(profile: CandidateProfile) -> str | None:
    if profile.notice_period_days is None:
        return None
    return f"{profile.notice_period_days} days"


def _format_expected_salary(profile: CandidateProfile) -> str | None:
    if profile.expected_salary is None:
        return None
    salary = profile.expected_salary
    formatted = f"{int(salary):,}" if float(salary).is_integer() else f"{salary:,}"
    currency = profile.expected_salary_currency or ""
    return f"{currency} {formatted}".strip()


def _format_years_of_experience(profile: CandidateProfile) -> str | None:
    if profile.years_of_experience is None:
        return None
    years = profile.years_of_experience
    return f"{int(years)} years" if float(years).is_integer() else f"{years} years"


_DETERMINISTIC_FORMATTERS: dict[str, Callable[[CandidateProfile], str | None]] = {
    CATEGORY_REQUIRES_SPONSORSHIP: _format_requires_sponsorship,
    CATEGORY_WORK_AUTHORIZED: _format_work_authorized,
    CATEGORY_VISA_TYPE: _format_visa_type,
    CATEGORY_NOTICE_PERIOD: _format_notice_period,
    CATEGORY_EXPECTED_SALARY: _format_expected_salary,
    CATEGORY_YEARS_OF_EXPERIENCE: _format_years_of_experience,
}

#: CandidateDemographics attribute name for each demographic category —
#: `_demographic_answer` reads straight off the stored row via this mapping.
_DEMOGRAPHIC_PROFILE_FIELDS: dict[str, str] = {
    "demographic_gender": "gender",
    "demographic_veteran_status": "veteran_status",
    "demographic_disability_status": "disability_status",
    "demographic_race_ethnicity": "race_ethnicity",
}

DETERMINISTIC_CONFIDENCE = 0.9
# An LLM-generated answer is never as trustworthy as a fact straight from the
# candidate's own profile — kept below FieldMapper's/ApplicationFlowManager's
# AUTO_SUBMIT bar (0.85) on purpose, so a form that leans on the LLM path
# lands in copilot/needs-review rather than auto-submitting on a guess.
LLM_CONFIDENCE = 0.6

_SYSTEM_PROMPT = (
    "You are helping a job candidate answer screening questions on a job "
    "application form. Answer strictly and only from the candidate profile "
    "and job description given to you. Keep each answer concise (1-3 "
    "sentences), professional, and in the first person. Never invent "
    "specific facts (dates, employer names, numbers, certifications) that "
    "are not present in the provided profile — if a question asks for a "
    "concrete fact you don't have, answer honestly and generically instead "
    "of making one up. Respond with a JSON object of the exact shape "
    '{"answers": ["...", "..."]} with exactly one answer per question, in '
    "the same order the questions were given, and nothing else."
)


class ApplicationAnswerEngine:
    """Answers one form's worth of leftover screening questions. Cheap to
    construct per run — `db`/`user_id` are optional so tests (and any
    caller without a DB session handy) can still use the deterministic + LLM
    paths without a persistent cache."""

    def __init__(
        self,
        profile: CandidateProfile,
        job_description: str | None = None,
        db: Session | None = None,
        user_id: str | None = None,
        llm_fn: Callable[..., str] | None = None,
    ):
        self.profile = profile
        self.job_description = job_description
        self.db = db
        self.user_id = user_id
        # Injectable for tests; defaults to the real router
        # (`automation.interfaces.generate_answer` -> `app.ai.llm.router`).
        self._llm_fn = llm_fn or generate_answer
        # Lazily loaded (see `_get_demographics`) — most runs never touch a
        # demographic question at all, so there's no reason to query for a
        # row that will usually just be unused.
        self._demographics: CandidateDemographics | None = None
        self._demographics_loaded = False

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _cache_lookup(self, question: str) -> AnswerResult | None:
        if self.db is None or self.user_id is None:
            return None
        try:
            cached = answer_cache_repository.get_cached_answer(self.db, self.user_id, question)
        except Exception:  # noqa: BLE001 - a broken cache lookup must never block answering
            logger.debug("Answer cache lookup failed for %r — answering fresh instead.", question)
            return None
        if cached is None:
            return None
        return AnswerResult(question=question, answer=cached.answer, source="cache", confidence=cached.confidence)

    def _cache_save(self, question: str, answer: str, source: str, confidence: float) -> None:
        if self.db is None or self.user_id is None or not answer:
            return
        try:
            answer_cache_repository.save_answer(
                self.db, self.user_id, question, answer=answer, source=source, confidence=confidence,
            )
        except Exception:  # noqa: BLE001 - a failed cache write must never break an otherwise-good answer
            logger.debug("Could not cache the answer for %r — continuing without it.", question)

    # ------------------------------------------------------------------
    # Deterministic path
    # ------------------------------------------------------------------

    def _get_demographics(self) -> CandidateDemographics | None:
        """Loads (once) the candidate's stored EEO/demographic row, if any.
        Requires a real `db` — without one (e.g. most unit tests, or a
        caller that never supplied one) this always returns `None`, which
        `_demographic_answer` correctly treats as "never asked" rather than
        as an error."""
        if not self._demographics_loaded:
            self._demographics_loaded = True
            if self.db is not None:
                profile_id = getattr(self.profile, "profile_id", None)
                if profile_id:
                    try:
                        self._demographics = get_candidate_demographics(self.db, profile_id)
                    except Exception:  # noqa: BLE001 - a broken lookup must never crash the run
                        logger.debug("Could not load candidate_demographics for %r — treating as unanswered.", profile_id)
        return self._demographics

    def _demographic_answer(self, question: str, category: str) -> AnswerResult:
        """HARD RULE (see module docstring / PART 2 of the request that
        created this): a demographic question is answered ONLY from a value
        the candidate has already explicitly stored via `PUT
        /profile/demographics` — NEVER inferred, NEVER sent to the LLM.
        `answer_batch()` guarantees this method's result is never routed
        into `llm_questions` regardless of what it returns here."""
        demographics = self._get_demographics()
        field_name = _DEMOGRAPHIC_PROFILE_FIELDS.get(category)
        value = getattr(demographics, field_name, None) if demographics is not None and field_name else None
        if value:
            return AnswerResult(question=question, answer=value, source="deterministic", confidence=DETERMINISTIC_CONFIDENCE)
        # Never asked yet — surfaces as zero-confidence/empty rather than
        # guessing or falling through to the LLM, so ApplicationFlowManager's
        # confidence score correctly reflects "needs a human to answer this
        # once" (see ARCHITECTURE.md's review/auto-submit decision table).
        return AnswerResult(question=question, answer="", source=SOURCE_NEEDS_USER_INPUT, confidence=0.0)

    def _deterministic_answer(self, question: str) -> AnswerResult | None:
        """Returns `None` only for "not a recognized factual shape at all" —
        genuinely demographic questions are always handled (see
        `_demographic_answer`, which never returns `None`) so they can never
        accidentally fall through to the LLM path in `answer_batch()`."""
        category = classify_question(question)
        if category is None:
            return None
        if is_demographic(category):
            return self._demographic_answer(question, category)
        answer = _DETERMINISTIC_FORMATTERS[category](self.profile)
        if not answer:
            return None  # profile has nothing to say here — fall through to the LLM rather than guess
        return AnswerResult(question=question, answer=answer, source="deterministic", confidence=DETERMINISTIC_CONFIDENCE)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def answer(self, question: str) -> AnswerResult:
        """Single-question path. Prefer `answer_batch()` whenever more than
        one question is outstanding for the same form — it costs one LLM
        call instead of N."""
        return self.answer_batch([question])[0]

    def answer_batch(self, questions: list[str]) -> list[AnswerResult]:
        """Resolves cache/deterministic hits first (free), then answers
        every remaining question in ONE LLM call, preserving the original
        order of `questions`."""
        results: dict[int, AnswerResult] = {}
        llm_indices: list[int] = []
        llm_questions: list[str] = []

        for i, question in enumerate(questions):
            cached = self._cache_lookup(question)
            if cached is not None:
                results[i] = cached
                continue
            deterministic = self._deterministic_answer(question)
            if deterministic is not None:
                self._cache_save(question, deterministic.answer, "deterministic", deterministic.confidence)
                results[i] = deterministic
                continue
            llm_indices.append(i)
            llm_questions.append(question)

        if llm_questions:
            answers = self._call_llm(llm_questions)
            for idx, question, answer_text in zip(llm_indices, llm_questions, answers):
                if answer_text:
                    self._cache_save(question, answer_text, "llm", LLM_CONFIDENCE)
                    results[idx] = AnswerResult(question=question, answer=answer_text, source="llm", confidence=LLM_CONFIDENCE)
                else:
                    # Never type a placeholder/error string into a real
                    # application field — an empty answer here means
                    # `automation/ats/base.py` leaves the field unfilled,
                    # which correctly pulls down the run's confidence score
                    # (see ApplicationFlowManager._aggregate_confidence)
                    # instead of silently pretending the question was handled.
                    results[idx] = AnswerResult(question=question, answer="", source="llm", confidence=0.0)

        return [results[i] for i in range(len(questions))]

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    def _call_llm(self, questions: list[str]) -> list[str | None]:
        """One `generate_answer` call for the whole batch — see module
        docstring. Returns one entry per question, in order; `None` for any
        question the LLM couldn't answer (bad response, parse failure, or
        the call itself failing) rather than raising and losing every other
        answer in the batch."""
        prompt = self._build_prompt(questions)
        try:
            raw = self._llm_fn(
                task="application_answer",
                prompt=prompt,
                system=_SYSTEM_PROMPT,
                json_mode=True,
            )
            parsed = json.loads(raw)
            answers = parsed.get("answers") if isinstance(parsed, dict) else None
            if not isinstance(answers, list) or len(answers) != len(questions):
                raise ValueError(f"Expected {len(questions)} answers, got: {answers!r}")
            return [str(a).strip() or None for a in answers]
        except Exception:  # noqa: BLE001 - never let a bad LLM response crash the whole form
            logger.exception("application_answer LLM call failed for %d question(s)", len(questions))
            return [None] * len(questions)

    def _build_prompt(self, questions: list[str]) -> str:
        profile_summary = {
            "current_role": self.profile.current_role,
            "current_company": self.profile.current_company,
            "years_of_experience": self.profile.years_of_experience,
            "skills": self.profile.skills,
        }
        payload = {
            "candidate_profile": profile_summary,
            "job_description": self.job_description,
            "questions": questions,
        }
        return json.dumps(payload, default=str)
