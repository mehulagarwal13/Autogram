"""
Vision fallback — the last pass over a form, for the fields nothing else could fill.

Every other pass reads the DOM: `FieldMapper` matches a label to a profile
field, `ApplicationAnswerEngine` answers a screening question from the
profile or from one batched text LLM call. Both work off the text an ATS
*exposes*. What's left after them is the residue where that text wasn't
enough:

- a conditional follow-up whose meaning lives in the question ABOVE it ("If
  yes to the above question, what role and what governmental organization?").
  Read alone it is unanswerable, so the text engine correctly declines and the
  required field stays empty — even though a person looking at the form sees
  instantly that the previous answer was "No" and types "N/A";
- a control whose visible selection isn't in its own value (a react-select
  combobox, a country picker), which the required-field scan reports as empty
  while the page plainly shows a value. Answering it again would overwrite a
  correct answer;
- a field whose real label is rendered somewhere the DOM doesn't connect to
  the input at all (no `<label for>`, no `aria-label`, no single-control
  ancestor), so no pass ever knew what it was asking.

All three are visible. So this pass sends what a person would look at: a
cropped SCREENSHOT of each remaining field, in one batched vision call,
alongside the same candidate profile the text engine uses
(`ApplicationAnswerEngine.profile_payload()` — deliberately shared, so the two
passes can never disagree about what the profile says). Whatever comes back is
filled through the ordinary `fill_field()` pipeline, so a vision answer is
verified against the live DOM exactly like every other answer.

**This pass is a fallback, never a shortcut.** It runs after everything else,
only over fields still unfilled, and it is the most expensive path in the
system (one image per field). Anything it answers repeatedly is a signal that
the cheap deterministic path is missing a case, not that this pass is working
well.

Guardrails, all of them the same ones the text path has, because the output
goes to the same place — an employer's form, in the candidate's name:

- **Demographic/EEO questions are dropped before the call, not answered.**
  Gender, veteran status, disability, race/ethnicity, and pronouns come only
  from what the candidate explicitly stored (see `answer_engine`'s module
  docstring). A screenshot doesn't change that, and a vision model asked to
  "read the form and answer" would happily pick one.
- **Options are re-resolved against the DOM's real option list**
  (`option_matching.match_option`) and an answer that isn't exactly one of
  them is discarded, so the model cannot invent a choice.
- **Meta-commentary is discarded** (`answer_engine._looks_like_meta_commentary`)
  — "the candidate profile does not specify..." must never be typed into a
  form.
- **A field the screenshot shows as already answered is left alone.** The
  model reports `already_filled` and the caller skips it; see
  `_SYSTEM_PROMPT`.
- **Confidence gates the fill**, at the same threshold as the text path
  (`automation/ats/base.py::ANSWER_REVIEW_CONFIDENCE_THRESHOLD`) — the caller
  applies it, so both paths hand a human the same class of uncertain answer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field as _dc_field
from pathlib import Path
from typing import Callable, Sequence

from automation.forms.answer_engine import (
    ApplicationAnswerEngine,
    _looks_like_meta_commentary,
)
from automation.forms.option_matching import match_option
from automation.forms.question_classifier import classify_question, is_demographic
from automation.interfaces import generate_answer

logger = logging.getLogger(__name__)

#: LLM task route (`app/ai/llm/registry.py`) — must stay on a vision-capable model.
VISION_TASK = "form_vision_answer"

#: Hard cap on fields per call. Each field costs a high-detail image, and a
#: form with 30 unfilled fields is a form that needs a human, not a bigger
#: prompt. Anything beyond this is reported (never silently dropped) — see
#: `answer()`.
MAX_FIELDS_PER_CALL = 10

#: What the answer of an inapplicable conditional follow-up should be. Spelled
#: out as a constant because it is a real answer typed into a real form, not an
#: internal sentinel: a required "If yes, explain..." box under a "No" answer
#: has to be filled with something, and "N/A" is what a person writes.
NOT_APPLICABLE = "N/A"


@dataclass(frozen=True)
class VisionField:
    """One still-unfilled control, ready to be asked about: what the DOM says
    it's asking (may be `""` — that's part of why this pass exists), the real
    choices it offers, and a cropped screenshot of it in context."""

    name: str                       #: DOM-ish identity, for logs (`id`/`name`/`aria-label`)
    question: str                   #: label/nearby text as read off the DOM, `""` if none
    screenshot: bytes               #: PNG crop of the field and the text around it
    options: tuple[str, ...] = ()
    widget: str = ""                #: "input"/"select"/"textarea"/... — helps the model pick a form of answer


@dataclass(frozen=True)
class VisionAnswer:
    """One answer, post-validation. `answer == ""` means "not answered" —
    either the model declined, or validation rejected what it said. The caller
    leaves such a field for a human; it never types a placeholder."""

    name: str
    question: str
    answer: str
    confidence: float
    reason: str = ""
    #: The model reports the screenshot already shows a value. Distinct from a
    #: plain decline: nothing is wrong, the field just isn't empty, and the
    #: caller must not "fix" it by typing over what's there.
    already_filled: bool = False
    available_options: tuple[str, ...] = _dc_field(default_factory=tuple)

    @property
    def answered(self) -> bool:
        return bool(self.answer)


_SYSTEM_PROMPT = (
    "You are looking at screenshots of the LAST FEW FIELDS of a job "
    "application form that an automated form-filler could not complete. "
    "Everything else on the form is already filled from the candidate's "
    "profile. Your answers are typed VERBATIM into the employer's form, in "
    "the candidate's name, and read by a hiring manager as the candidate's "
    "own words.\n"
    "You are given, for each field: the text the page's markup exposes (often "
    "incomplete — that is why the field reached you), the exact options the "
    "control offers if it has any, and ONE SCREENSHOT of that field with the "
    "surrounding form visible. The screenshots are attached in order: the "
    "first image is FIELD 1, the second is FIELD 2, and so on.\n"
    "READ THE SCREENSHOT, not just the field text. The screenshot is the "
    "whole reason you were asked. It shows the questions above and below the "
    "field, the answers already given to them, and the field's own visible "
    "state — none of which the field text carries.\n"
    "Rules, in priority order:\n"
    "(1) IF THE SCREENSHOT SHOWS THE FIELD ALREADY HAS A VALUE, do not answer "
    "it. Set `already_filled` to true, `answer` to null. Some controls hold "
    "their visible selection outside the value the automation can read, so a "
    "field can reach you looking empty while the form plainly shows an answer. "
    "Overwriting a correct answer is worse than leaving the field alone.\n"
    "(2) A CONDITIONAL FOLLOW-UP WHOSE CONDITION IS NOT MET is answered "
    f'"{NOT_APPLICABLE}", not left blank and not invented. If the field reads '
    "'If yes to the above, describe...' or 'If you answered yes, which "
    "one?' and the screenshot shows the question above it was answered 'No', "
    f'then "{NOT_APPLICABLE}" is the correct, complete, honest answer — that '
    "is what the employer expects and what a person would type. Read the "
    "actual answer to the preceding question off the screenshot; never assume "
    "which way it went.\n"
    "(3) ANSWER WITH THE VALUE, NOT A SENTENCE ABOUT IT. A name, employer, "
    "location, number, date, or duration is answered with just that value. "
    "Only a field genuinely asking for prose gets sentences, and then at most "
    "1-3, professional and in the first person.\n"
    "(4) WHEN A FIELD LISTS OPTIONS, your answer must be exactly one of them, "
    "copied character for character. Match on meaning, not wording. If no "
    "option defensibly fits this candidate, or two fit equally, answer null.\n"
    "(5) NEVER INVENT A FACT ABOUT THIS CANDIDATE — no dates, employers, "
    "numbers, salaries, or certifications that are not in the profile you were "
    "given or visible in the screenshot.\n"
    "(6) NEVER ANSWER A QUESTION ABOUT GENDER, RACE, ETHNICITY, DISABILITY, "
    "VETERAN STATUS, PRONOUNS, OR AGE, even if the form requires it and even "
    "if the profile appears to contain it. Answer null with reason "
    "'demographic'. Only the candidate may answer those about themselves.\n"
    "(7) NEVER WRITE ABOUT THE PROFILE OR YOUR OWN LIMITATIONS. 'The profile "
    "does not specify...', 'I cannot determine...' — that text is addressed to "
    "the wrong audience and damages the application. If you don't have what a "
    "field asks for, answer null and a human will fill it in.\n"
    "Respond with a JSON object of exactly this shape and nothing else:\n"
    '{"answers": [{"field": 1, "answer": "..." or null, "confidence": 0.0, '
    '"already_filled": false, "reason": "..."}, ...]}\n'
    "with one entry per field, `field` being that field's number. `reason` is "
    "at most 12 words, for the run log, explaining what you read off the "
    "screenshot — e.g. 'above question answered No, so not applicable'. "
    "`confidence` is your honest certainty from 0.0 to 1.0 that the answer is "
    "correct and safe to submit without a human reading it first. Calibrate "
    "honestly: an answer you read directly off the screenshot or copied from "
    "the profile is well-grounded and should score high; a subjective or "
    "composed answer should score low. A low score costs the candidate a few "
    "seconds of review — an overconfident wrong answer can cost them the role."
)

#: Appended when the candidate has stored résumé facts, same as the text
#: path's `_RESUME_PROMPT` — a field asking for a graduation year is
#: answerable from `education`, and declining it leaves a required box empty
#: for no reason.
_RESUME_PROMPT = (
    " The profile includes the candidate's own résumé facts — `education` and "
    "`experience`, each ordered most-recent-first, plus `certifications` — "
    "parsed from the résumé being submitted with this application, so treat "
    "them as authoritative. When a field asks for one value and several "
    "entries could supply it, answer with the single entry the field means "
    "(the one it names, or the most recent when it doesn't name one) — never a "
    "list or a range. A value copied straight from these facts is well-"
    "grounded: score it 0.9 or higher."
)


def _declined(field: VisionField, reason: str) -> VisionAnswer:
    return VisionAnswer(
        name=field.name, question=field.question, answer="", confidence=0.0,
        reason=reason, available_options=field.options,
    )


class VisionFormAnswerer:
    """Answers a batch of still-unfilled fields from their screenshots.

    Built per run and handed to `ApplicationFlowManager`, which passes it to
    the adapter's vision pass. Holds no page state of its own: the caller owns
    the browser and does the screenshotting and the filling, this class owns
    only "screenshots + profile -> validated answers." That split is what
    keeps the prompt/validation logic testable without a browser, exactly like
    `ApplicationAnswerEngine`."""

    def __init__(
        self,
        answer_engine: ApplicationAnswerEngine,
        *,
        llm_fn: Callable[..., str] | None = None,
        max_fields: int = MAX_FIELDS_PER_CALL,
    ) -> None:
        # The answer engine is the source of profile/job/résumé context —
        # reused rather than re-derived, so both LLM paths see one candidate.
        self._engine = answer_engine
        self._llm_fn = llm_fn or generate_answer
        self._max_fields = max_fields

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def answer(self, fields: Sequence[VisionField]) -> list[VisionAnswer]:
        """One vision call for the whole batch. Returns one `VisionAnswer` per
        input field, in the same order — a declined/rejected field comes back
        with `answer=""` rather than being omitted, so the caller can report
        every field it looked at."""
        if not fields:
            return []

        results: dict[int, VisionAnswer] = {}
        askable: list[tuple[int, VisionField]] = []

        for index, item in enumerate(fields):
            category = classify_question(item.question) if item.question else None
            if is_demographic(category):
                # Never sent. See the module docstring — this is the same hard
                # rule the text path applies, enforced before the call so a
                # screenshot of an EEO question never leaves the machine.
                logger.info(
                    "Vision pass: not answering %r — demographic question, only the candidate may answer it.",
                    item.question or item.name,
                )
                results[index] = _declined(item, "demographic — needs the candidate's own answer")
                continue
            askable.append((index, item))

        if len(askable) > self._max_fields:
            dropped = askable[self._max_fields:]
            logger.warning(
                "Vision pass: %d unfilled field(s) exceed the %d-per-call cap — asking about the first %d "
                "and leaving these for a human: %s",
                len(askable), self._max_fields, self._max_fields,
                ", ".join(item.question or item.name for _i, item in dropped),
            )
            for index, item in dropped:
                results[index] = _declined(item, f"not asked — over the {self._max_fields}-field cap")
            askable = askable[: self._max_fields]

        if askable:
            asked = [item for _index, item in askable]
            for (index, item), answer in zip(askable, self._call_llm(asked)):
                results[index] = answer

        return [results[i] for i in range(len(fields))]

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    def _call_llm(self, fields: list[VisionField]) -> list[VisionAnswer]:
        """One `generate_answer` call carrying every field's screenshot.
        Returns one `VisionAnswer` per field; a failed call, an unparseable
        response, or a missing entry becomes a decline for that field rather
        than an exception — this is a best-effort last pass, and losing the
        whole batch because of one malformed entry would waste every other
        answer in it."""
        system = _SYSTEM_PROMPT
        if self._engine.has_resume_context():
            system += _RESUME_PROMPT

        try:
            raw = self._llm_fn(
                task=VISION_TASK,
                prompt=self._build_prompt(fields),
                system=system,
                images=[item.screenshot for item in fields],
                json_mode=True,
            )
        except Exception as e:  # noqa: BLE001 - a failed fallback must never fail the run
            logger.warning("Vision pass: LLM call failed for %d field(s) (%s).", len(fields), e)
            return [_declined(item, "vision call failed") for item in fields]

        try:
            parsed = json.loads(raw)
            entries = parsed.get("answers") if isinstance(parsed, dict) else None
            if not isinstance(entries, list):
                raise ValueError(f"Expected an 'answers' list, got: {entries!r}")
        except Exception as e:  # noqa: BLE001
            logger.warning("Vision pass: could not parse the model's response (%s).", e)
            return [_declined(item, "unparseable vision response") for item in fields]

        # Keyed by the model's own `field` number rather than by position: an
        # entry-per-field response that reorders or omits one must not shift
        # every other answer onto the wrong field, which is the one failure
        # mode here that types a real answer into the wrong employer question.
        by_number: dict[int, dict] = {}
        for position, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                continue
            try:
                number = int(entry.get("field", position))
            except (TypeError, ValueError):
                number = position
            by_number.setdefault(number, entry)

        answers: list[VisionAnswer] = []
        for number, item in enumerate(fields, start=1):
            entry = by_number.get(number)
            if entry is None:
                logger.info("Vision pass: no answer returned for field %d (%r).", number, item.question or item.name)
                answers.append(_declined(item, "no answer returned"))
                continue
            answers.append(self._validate(item, entry))
        return answers

    def _validate(self, item: VisionField, entry: dict) -> VisionAnswer:
        """Turns one raw response entry into a `VisionAnswer`, applying every
        guardrail in the module docstring. The prompt asks; this decides."""
        reason = " ".join(str(entry.get("reason") or "").split())[:120]

        if entry.get("already_filled") is True:
            return VisionAnswer(
                name=item.name, question=item.question, answer="", confidence=0.0,
                reason=reason or "already filled on the form", already_filled=True,
                available_options=item.options,
            )

        text = str(entry.get("answer") or "").strip()
        if not text:
            return _declined(item, reason or "model declined")

        try:
            confidence = min(1.0, max(0.0, float(entry.get("confidence"))))
        except (TypeError, ValueError):
            # An unusable confidence is not a reason to trust the answer: this
            # pass reads a screenshot rather than a profile field, so "we don't
            # know how sure it is" has to mean "a human looks at it."
            logger.info(
                "Vision pass: field %r came back with no usable confidence (%r) — treating it as unanswered.",
                item.question or item.name, entry.get("confidence"),
            )
            return _declined(item, "no usable confidence score")

        if _looks_like_meta_commentary(text):
            logger.info("Vision pass: discarding %r — meta-commentary, not an answer.", text)
            return _declined(item, "meta-commentary, not an answer")

        if item.options:
            matched = match_option(text, item.options)
            if matched is None:
                logger.info(
                    "Vision pass: discarding %r — not resolvable to exactly one of the field's options %r.",
                    text, list(item.options),
                )
                return _declined(item, "answer is not one of the field's options")
            text = matched

        return VisionAnswer(
            name=item.name, question=item.question, answer=text,
            confidence=confidence, reason=reason, available_options=item.options,
        )

    def _build_prompt(self, fields: list[VisionField]) -> str:
        """The text half of the call. Fields are numbered from 1 to match the
        image order the system prompt promises, and an unknown label is sent as
        an explicit note rather than an empty string — "the page gave us no
        label, read it off the screenshot" is exactly the instruction that
        field needs."""
        payload = {
            "candidate_profile": self._engine.profile_payload(),
            "job_description": self._engine.job_description,
            "fields": [
                {
                    "field": number,
                    "question_text_from_page": item.question
                    or "(the page exposes no label for this field — read the question off the screenshot)",
                    "control": item.widget or "unknown",
                    "options": list(item.options) or None,
                }
                for number, item in enumerate(fields, start=1)
            ],
        }
        return json.dumps(payload, default=str)


def save_debug_crops(fields: Sequence[VisionField], directory: Path) -> list[str]:
    """Writes each field's screenshot next to the run's other artifacts
    (`logs/<application_id>/`), so what the model was shown is reviewable
    afterwards — the single most useful thing to have when a vision answer
    looks wrong. Best-effort: a write failure is logged and skipped, never
    raised, since this is a debugging aid and the run's real work is done."""
    written: list[str] = []
    for number, item in enumerate(fields, start=1):
        path = directory / f"vision-field-{number}.png"
        try:
            path.write_bytes(item.screenshot)
            written.append(str(path))
        except OSError as e:
            logger.debug("Could not write vision crop %s (%s).", path, e)
    return written
