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

**Option-bearing questions.** A question can arrive as a bare `str` (free
text — "Why do you want to work here?") or as a `Question` carrying the exact
choices the on-page control offers, read straight off the DOM by
`automation/forms/field_handlers.py::read_field_options` and passed in by
`automation/ats/base.py`. When options are present:

- the LLM is told to answer with one of them, verbatim, matching on meaning
  rather than wording ("requires sponsorship" + `["Yes", "No"]` -> `"Yes"`);
- whatever comes back is independently re-resolved against the real option
  list by `_match_option` and REPLACED with the verbatim option string, so
  what reaches the page is always something the DOM actually has. An answer
  that matches nothing, or matches two options equally, is discarded — the
  question is left for a human rather than filled with a near-miss;
- deterministic answers get the same treatment, except that a profile fact
  which can't be mapped mechanically ("US Citizen" against `["Yes", "No"]`)
  falls through to the LLM, which can read it semantically. Demographic
  answers are the exception that never falls through — see
  `_demographic_answer`.

`AnswerResult.available_options` echoes the list back on every result,
including declined ones, so the review UI can show a human the real choices.

Both paths are backed by a persistent, per-user, exact-match answer cache
(`app/services/answer_cache_repository.py`) keyed by normalized question
text — pass `db`/`user_id` at construction to use it; omitting them (e.g. in
tests, or any caller without a DB session handy) just means every call
re-derives its answer instead of reusing a stored one.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

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


class Question(str):
    """One screening question, carrying the exact set of choices the on-page
    control offers (a `<select>`'s `<option>`s, a radio group's labels, ...).
    `options` is what makes the difference between "write me a sentence" and
    "pick one of exactly these" — see `_OPTION_PROMPT` and `_match_option`.

    Deliberately a `str` SUBCLASS rather than a plain dataclass: a question
    was a bare string for this class's whole existence, and `answer_batch()`
    is called with one by `automation/ats/base.py`, by every ATS adapter test
    double, and by anything else duck-typing an answer engine. Being a real
    string means all of that keeps working untouched — `Question("Why us?")`
    compares equal to, hashes like, indexes a dict like, and JSON-serializes
    as `"Why us?"` — while option-aware code reads `.options` off the same
    object. A separate dataclass would have forced every existing caller and
    double to be rewritten in lockstep for a field most of them don't use.

    `.text` is the plain `str` form, for call sites where relying on the
    subclass being a string would read as a trick rather than a fact."""

    __slots__ = ("options",)

    def __new__(cls, text: str, options: Iterable[str] = ()) -> "Question":
        question = super().__new__(cls, text)
        question.options = tuple(options)
        return question

    @property
    def text(self) -> str:
        return str(self)

    def __repr__(self) -> str:
        return f"Question({str(self)!r}, options={self.options!r})"


@dataclass
class AnswerResult:
    question: str
    answer: str
    source: str  # "deterministic" | "cache" | "llm" | "needs_user_input"
    confidence: float
    #: The choices this question offered, echoed back verbatim. Empty for a
    #: free-text question. Populated even (especially) when the engine
    #: declines to answer — `ApplicationFlowManager`/the review UI can show a
    #: human the real options instead of making them re-read the form.
    available_options: tuple[str, ...] = ()


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
    "application form. Every word you produce is typed VERBATIM into the "
    "employer's form and read by a hiring manager as the candidate's own "
    "words. "
    "Answer strictly and only from the candidate profile and job description "
    "given to you. Never invent specific facts (dates, employer names, "
    "numbers, certifications) that are not present in the provided profile. "
    "Two rules about form of answer, both of which matter as much as "
    "correctness:\n"
    "(1) ANSWER WITH THE VALUE, NOT A SENTENCE ABOUT IT. If the question "
    "asks for a name, employer, location, number, date, or duration, reply "
    "with just that value — 'Navikenz', not 'My most recent employer is "
    "Navikenz'; 'Ada', not 'The candidate's preferred name is Ada'. Only a "
    "question that genuinely asks for prose ('why do you want to work "
    "here?', 'describe a time you...') gets sentences, and then at most 1-3 "
    "of them, professional and in the first person.\n"
    "(2) NEVER WRITE ABOUT THE PROFILE, THE CANDIDATE IN THE THIRD PERSON, "
    "OR YOUR OWN LIMITATIONS. Text like 'the candidate profile does not "
    "specify...', 'this is not provided', 'I cannot determine...', or "
    "'please use the candidate's...' is never an acceptable answer — it is "
    "meta-commentary addressed to the wrong audience, and putting it in an "
    "employer's form actively damages the application. If you don't have "
    "what a question asks for, set `answer` to null and `confidence` to 0.0 "
    "so a human can fill it in. An unanswered field costs the candidate a "
    "few seconds; a field explaining that their profile is incomplete costs "
    "them the role. "
    "Respond with a JSON object of the exact shape "
    '{"answers": [{"answer": "...", "confidence": 0.0}, ...]} with exactly '
    "one entry per question, in the same order the questions were given, and "
    "nothing else. `confidence` is your own honest certainty from 0.0 to 1.0 "
    "that the answer is well-grounded in the profile you were given and is "
    "safe to submit on the candidate's behalf without a human reading it "
    "first. Calibrate honestly and err toward UNDER-confidence: score low "
    "when the profile lacks what the question asks for, when you had to "
    "answer generically, or when the question is subjective. A low score "
    "costs the candidate a few seconds of review; an overconfident wrong "
    "answer can cost them the role."
)

#: Appended to `_SYSTEM_PROMPT` only when at least one question in the batch
#: actually carries options — a free-text-only batch keeps exactly the
#: prompt it had before, with no wasted tokens explaining a rule it can't hit.
#:
#: Note this is instruction, not enforcement: `_match_option` independently
#: rejects any answer that isn't one of the real options, so a model that
#: ignores this paragraph can't invent a choice — same "prompt asks, code
#: enforces" split the rest of this module uses.
_OPTION_PROMPT = (
    " Some questions include an AVAILABLE OPTIONS list — those are the only "
    "choices the form actually offers. For such a question your `answer` MUST "
    "be exactly one of the listed options, copied verbatim, character for "
    "character. Do not reword, abbreviate, expand, or combine options, and "
    "never answer with a choice that is not on the list. Match on MEANING, "
    "not wording: if the profile says the candidate needs visa sponsorship "
    'and the options are ["Yes", "No"], the correct answer to "Do you require '
    'sponsorship?" is "Yes". If no listed option is a defensible fit for this '
    "candidate, or two options fit equally well, set `answer` to null and "
    "`confidence` to 0.0 — a human will choose. Never pick one at random to "
    "fill the field."
)


#: Text that is *about* answering rather than an answer. A model told not to
#: invent facts will otherwise happily write "The candidate profile does not
#: specify CTC expectations." into an employer's salary field — observed on a
#: real Greenhouse posting, at high enough self-reported confidence to clear
#: `ANSWER_REVIEW_CONFIDENCE_THRESHOLD`.
#:
#: `_SYSTEM_PROMPT` forbids this in words; this is the enforcement, for the
#: same reason `_match_option` exists — the prompt asks, the code decides.
#: Deliberately conservative: each pattern is phrasing that could only ever be
#: commentary addressed to the operator, never a real answer a candidate would
#: type. Note "the candidate profile" is listed but plain "the candidate"
#: isn't — "the ideal candidate for this role..." is a legitimate opening for
#: a genuine free-text answer.
_META_COMMENTARY_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bprofile (does not|doesn't|did not) (specify|include|contain|mention|provide|state)\b",
    r"\bdoes not specify\b",
    r"\bnot (specified|provided|available|listed|mentioned|included) in the\b",
    r"\b(cannot|can't|could not|couldn't|unable to) (determine|infer|answer|provide|confirm)\b",
    r"\binsufficient information\b",
    r"\bthe candidate profile\b",
    r"\bplease (use|provide|refer|consult|contact|specify)\b",
    r"\bas an ai\b",
    r"\bi (do not|don't) have (access|enough|any|the)\b",
    r"\bno information (about|on|regarding|is|was)\b",
))


def _looks_like_meta_commentary(text: str) -> bool:
    return any(pattern.search(text) for pattern in _META_COMMENTARY_PATTERNS)


def _normalize_option(text: str) -> str:
    """Case- and whitespace-insensitive form used only for MATCHING an
    answer back to a real option — never for what gets typed into the page.
    The verbatim option string is always what's filled."""
    return " ".join(text.split()).casefold()


def _match_option(answer: str, options: Sequence[str]) -> str | None:
    """Resolves the model's answer to one of `options`, returning that
    option VERBATIM (so `field_handlers` selects a string the DOM really
    has), or `None` if it can't be resolved to exactly one.

    Three tiers, tightest first:

    1. Exact string equality.
    2. Case/whitespace-normalized equality — covers "yes" vs "Yes" and the
       stray trailing space an ATS puts in its own `<option>` text.
    3. Containment, but ONLY when exactly one option matches: a model
       answering "Yes" against `["Yes, now or in the future", "No"]` is
       clearly right, and rejecting it would send a perfectly answerable
       question to a human. Ambiguity is not resolved by picking the first
       or longest match — two candidates means `None`, which routes the
       question to review. That's the deliberate trade: this module never
       guesses between plausible options (same rule as `FieldMapper`).
    """
    if not options:
        return None
    for option in options:
        if answer == option:
            return option
    normalized_answer = _normalize_option(answer)
    if not normalized_answer:
        return None
    for option in options:
        if _normalize_option(option) == normalized_answer:
            return option
    contained = [
        option for option in options
        if normalized_answer in _normalize_option(option)
        or _normalize_option(option) in normalized_answer
    ]
    return contained[0] if len(contained) == 1 else None


def _as_question(item: str | Question) -> Question:
    """Normalizes `answer_batch`'s heterogeneous input. A bare string is a
    free-text question — what every caller predating options passes."""
    if isinstance(item, Question):
        return item
    return Question(str(item))


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

    def _cache_lookup(self, question: Question) -> AnswerResult | None:
        if self.db is None or self.user_id is None:
            return None
        try:
            cached = answer_cache_repository.get_cached_answer(self.db, self.user_id, question.text)
        except Exception:  # noqa: BLE001 - a broken cache lookup must never block answering
            logger.debug("Answer cache lookup failed for %r — answering fresh instead.", question.text)
            return None
        if cached is None:
            return None
        answer = cached.answer
        if question.options:
            # The cache is keyed by question TEXT alone, so a stored answer
            # can arrive at a form that asks the same thing with a different
            # option set ("Yes"/"No" one week, "Yes, I require sponsorship"
            # the next). Re-resolve against THIS form's options and treat an
            # unresolvable hit as a miss — better to spend one LLM call than
            # to hand `field_handlers` a value the DOM has no option for.
            matched = _match_option(answer, question.options)
            if matched is None:
                logger.debug(
                    "Cached answer %r for %r isn't among this form's options — answering fresh instead.",
                    answer, question.text,
                )
                return None
            answer = matched
        return AnswerResult(
            question=question.text, answer=answer, source="cache",
            confidence=cached.confidence, available_options=question.options,
        )

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

    def _demographic_answer(self, question: Question, category: str) -> AnswerResult:
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
            # Snap to the form's own wording when it offers a fixed list
            # ("Prefer not to say" vs "I don't wish to answer"). An
            # unresolvable stored value does NOT fall through to the LLM the
            # way a factual one does — see `_deterministic_answer` — it
            # surfaces for a human, because inferring a demographic answer is
            # exactly what this path exists to prevent.
            if question.options:
                matched = _match_option(value, question.options)
                if matched is None:
                    logger.info(
                        "Stored demographic value %r isn't among this form's options for %r — leaving it for a human.",
                        value, question.text,
                    )
                    return AnswerResult(
                        question=question.text, answer="", source=SOURCE_NEEDS_USER_INPUT,
                        confidence=0.0, available_options=question.options,
                    )
                value = matched
            return AnswerResult(
                question=question.text, answer=value, source="deterministic",
                confidence=DETERMINISTIC_CONFIDENCE, available_options=question.options,
            )
        # Never asked yet — surfaces as zero-confidence/empty rather than
        # guessing or falling through to the LLM, so ApplicationFlowManager's
        # confidence score correctly reflects "needs a human to answer this
        # once" (see ARCHITECTURE.md's review/auto-submit decision table).
        return AnswerResult(
            question=question.text, answer="", source=SOURCE_NEEDS_USER_INPUT,
            confidence=0.0, available_options=question.options,
        )

    def _deterministic_answer(self, question: Question) -> AnswerResult | None:
        """Returns `None` only for "not a recognized factual shape at all" —
        genuinely demographic questions are always handled (see
        `_demographic_answer`, which never returns `None`) so they can never
        accidentally fall through to the LLM path in `answer_batch()`."""
        category = classify_question(question.text)
        if category is None:
            return None
        if is_demographic(category):
            return self._demographic_answer(question, category)
        answer = _DETERMINISTIC_FORMATTERS[category](self.profile)
        if not answer:
            return None  # profile has nothing to say here — fall through to the LLM rather than guess
        if question.options:
            # The profile stores facts in the candidate's own words ("US
            # Citizen", "Requires H1B sponsorship"); the form wants one of
            # its own strings. Where that mapping is mechanical, take it.
            # Where it isn't, fall through to the LLM — which CAN read "US
            # Citizen" as the answer "Yes" to "are you authorized to work?"
            # — rather than declining outright. The LLM's answer is then
            # option-validated on the way back out, so this widens what gets
            # answered without widening what can be typed into the page.
            matched = _match_option(answer, question.options)
            if matched is None:
                return None
            answer = matched
        return AnswerResult(
            question=question.text, answer=answer, source="deterministic",
            confidence=DETERMINISTIC_CONFIDENCE, available_options=question.options,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def answer(self, question: str | Question, options: Iterable[str] | None = None) -> AnswerResult:
        """Single-question path. Prefer `answer_batch()` whenever more than
        one question is outstanding for the same form — it costs one LLM
        call instead of N.

        `options` is a convenience for the common single-question call:
        `answer("Do you require sponsorship?", ["Yes", "No"])` is the same as
        passing `Question(text=..., options=("Yes", "No"))`."""
        if options is not None:
            question = Question(str(question), options)
        return self.answer_batch([question])[0]

    def answer_batch(self, questions: Sequence[str | Question]) -> list[AnswerResult]:
        """Resolves cache/deterministic hits first (free), then answers
        every remaining question in ONE LLM call, preserving the original
        order of `questions`.

        Accepts bare strings (free-text questions — the original contract)
        and `Question`s carrying the form's real option list, mixed freely in
        one batch."""
        normalized = [_as_question(item) for item in questions]
        results: dict[int, AnswerResult] = {}
        llm_indices: list[int] = []
        llm_questions: list[Question] = []

        for i, question in enumerate(normalized):
            cached = self._cache_lookup(question)
            if cached is not None:
                results[i] = cached
                continue
            deterministic = self._deterministic_answer(question)
            if deterministic is not None:
                self._cache_save(question.text, deterministic.answer, "deterministic", deterministic.confidence)
                results[i] = deterministic
                continue
            llm_indices.append(i)
            llm_questions.append(question)

        if llm_questions:
            answers = self._call_llm(llm_questions)
            for idx, question, (answer_text, confidence) in zip(llm_indices, llm_questions, answers):
                if answer_text:
                    self._cache_save(question.text, answer_text, "llm", confidence)
                    results[idx] = AnswerResult(
                        question=question.text, answer=answer_text, source="llm",
                        confidence=confidence, available_options=question.options,
                    )
                else:
                    # Never type a placeholder/error string into a real
                    # application field — an empty answer here means
                    # `automation/ats/base.py` leaves the field unfilled,
                    # which correctly pulls down the run's confidence score
                    # (see ApplicationFlowManager._aggregate_confidence)
                    # instead of silently pretending the question was handled.
                    results[idx] = AnswerResult(
                        question=question.text, answer="", source="llm",
                        confidence=0.0, available_options=question.options,
                    )

        return [results[i] for i in range(len(normalized))]

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_llm_answer(entry, options: Sequence[str] = ()) -> tuple[str | None, float]:
        """Normalizes one entry of the LLM's `answers` list to
        `(answer_text_or_None, confidence)`.

        Accepts BOTH response shapes on purpose:

        - `{"answer": "...", "confidence": 0.7}` — what `_SYSTEM_PROMPT` now
          asks for, so the 0.80 review gate has a real per-answer number to
          act on instead of one flat constant for every answer alike.
        - a bare `"..."` string — the original shape. A model that ignores
          the schema (or an older cached/stubbed provider, or any test
          double written against the previous contract) still produces usable
          answers rather than failing the whole batch. Those fall back to
          `LLM_CONFIDENCE`, exactly the pre-existing behavior.

        A malformed/out-of-range confidence is clamped rather than trusted:
        a model claiming 1.0 on everything (or returning `"high"`) must not
        be able to talk its way past the review gate.

        An answer that is meta-commentary rather than an answer ("the
        candidate profile does not specify...") is discarded outright — see
        `_META_COMMENTARY_PATTERNS`.

        When `options` is non-empty the answer is additionally resolved
        against it via `_match_option` and REPLACED by the verbatim option
        string. An answer that resolves to nothing — the model invented a
        choice, or hedged across two — is discarded as `(None, 0.0)`, which
        `answer_batch` turns into "leave it for a human." This is the
        enforcement half of `_OPTION_PROMPT`: the DOM's option list, not the
        model's word, decides what may be typed into the page.
        """
        if entry is None:
            return None, 0.0
        if isinstance(entry, dict):
            text = str(entry.get("answer") or "").strip() or None
            raw_confidence = entry.get("confidence")
            try:
                confidence = float(raw_confidence)
            except (TypeError, ValueError):
                confidence = LLM_CONFIDENCE
            confidence = min(1.0, max(0.0, confidence))
        else:
            text, confidence = str(entry).strip() or None, LLM_CONFIDENCE
        if text is not None and _looks_like_meta_commentary(text):
            logger.info("Discarding LLM answer %r — meta-commentary, not an answer.", text)
            return None, 0.0
        if text is None or not options:
            return text, confidence
        matched = _match_option(text, options)
        if matched is None:
            logger.info(
                "Discarding LLM answer %r — not resolvable to exactly one of the form's options %r.",
                text, list(options),
            )
            return None, 0.0
        return matched, confidence

    def _call_llm(self, questions: list[Question]) -> list[tuple[str | None, float]]:
        """One `generate_answer` call for the whole batch — see module
        docstring. Returns one `(answer, confidence)` per question, in order;
        `(None, 0.0)` for any question the LLM couldn't answer (bad response,
        parse failure, the call itself failing, or — for an option-bearing
        question — an answer that isn't one of the real options) rather than
        raising and losing every other answer in the batch."""
        prompt = self._build_prompt(questions)
        system = _SYSTEM_PROMPT
        if any(question.options for question in questions):
            system += _OPTION_PROMPT
        try:
            raw = self._llm_fn(
                task="application_answer",
                prompt=prompt,
                system=system,
                json_mode=True,
            )
            parsed = json.loads(raw)
            answers = parsed.get("answers") if isinstance(parsed, dict) else None
            if not isinstance(answers, list) or len(answers) != len(questions):
                raise ValueError(f"Expected {len(questions)} answers, got: {answers!r}")
            return [
                self._parse_llm_answer(entry, question.options)
                for entry, question in zip(answers, questions)
            ]
        except Exception:  # noqa: BLE001 - never let a bad LLM response crash the whole form
            logger.exception("application_answer LLM call failed for %d question(s)", len(questions))
            return [(None, 0.0)] * len(questions)

    def _build_prompt(self, questions: list[Question]) -> str:
        """`questions` stays a flat list of strings — the shape this prompt
        has always had. Options ride alongside in an index-parallel `options`
        list (`null` for a free-text question), and the key is omitted
        entirely when nothing in the batch has options, so a free-text-only
        form produces byte-for-byte the prompt it did before."""
        profile_summary = {
            "current_role": self.profile.current_role,
            "current_company": self.profile.current_company,
            "years_of_experience": self.profile.years_of_experience,
            "skills": self.profile.skills,
        }
        payload = {
            "candidate_profile": profile_summary,
            "job_description": self.job_description,
            "questions": [question.text for question in questions],
        }
        if any(question.options for question in questions):
            payload["options"] = [
                list(question.options) if question.options else None
                for question in questions
            ]
        return json.dumps(payload, default=str)
