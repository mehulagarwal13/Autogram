"""
ATSAdapter — the base interface every ATS-specific adapter implements.

Phase 4 (see ARCHITECTURE.md). Defining this contract lets every adapter
(`automation/ats/greenhouse/`, `automation/ats/lever/`, ...) be added
independently without touching this file or any other adapter — the
extensibility goal in the project brief.

Each adapter is handed a Playwright `Page` (already navigated to the job
application URL by `ApplicationFlowManager`/`BrowserManager`) plus the real
`CandidateProfile` and `ProfileDocument` ORM objects (re-exported from
`automation/interfaces.py`) — `automation/` is an internal module of this
application now, so adapters read profile/document fields directly rather
than through a translation layer (see `automation/interfaces.py`).

Beyond the abstract contract, this class also provides generic, non-ATS-
specific helpers every concrete adapter can use:

- `_fill_first_match` — fill the first visible input matching any of a list
  of candidate selectors (platform-specific selectors are supplied by the
  adapter; the "try each in order, skip what's not there" logic is shared).
- `_fill_known_questions` — a generic, sweep of the page that resolves
  fields to a profile attribute via `automation/forms/field_mapper.py`'s
  `FieldMapper` (Phase 5): first every `<label>`'s text, then — for anything
  not already handled — every remaining input/select/textarea's `name` and
  `placeholder` attributes directly, which catches fields an ATS renders
  with no `<label>` at all. A field is only ever filled once:
  `_fill_first_match` and the label pass both mark whatever input they
  touch (`_AUTOMATION_EXAMINED_ATTR`) so the name/placeholder pass never
  redundantly re-fills — and re-count toward confidence — the same field.
  A labeled field that doesn't match a synonym is genuinely novel/subjective
  territory — Phase 6's `ApplicationAnswerEngine` handles it instead, IF one
  was injected via `answer_engine=` at construction time (see `__init__`);
  with none (the default, and every adapter built before Phase 6), the field
  is simply left unfilled, exactly as before.
- `upload_resume` — concrete (not abstract): every adapter's implementation
  was identical, so it lives here once, built on the same field-handler
  pipeline below.

Every actual DOM interaction — `.fill()`, `.select_option()`, `.check()`,
`.set_input_files()`, and the open/search/scroll dance a react-select or
other custom dropdown needs — is delegated to
`automation/forms/field_handlers.py::fill_field()`. This class's job is
purely "what field is this, and what value belongs in it"; `field_handlers`
owns "how do I interact with and verify this specific kind of widget."
Every fill path below (`_fill_first_match`, the label pass, the
answer-engine pass, the name/placeholder pass, and `upload_resume`) routes
through it, so a new widget-interaction strategy (or a fix to an existing
one) benefits every adapter and every fill path at once — never duplicated
per-adapter or per-pass.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Error as PlaywrightError, Locator

from app.core.crypto import decrypt_field
from automation.browser.selectors import find_file_upload_input, find_upload_trigger_button
from automation.forms.answer_engine import ApplicationAnswerEngine
from automation.forms.field_handlers import FieldFailure, describe_field, fill_field
from automation.forms.field_mapper import FieldMapper
from automation.interfaces import CandidateProfile, ProfileDocument

logger = logging.getLogger(__name__)

# Marks any input this adapter has already examined/filled (via any of the
# three fill paths) so later passes never double-process — and double-count
# toward `ApplicationFlowManager`'s confidence score — the same field.
_AUTOMATION_EXAMINED_ATTR = "data-automation-examined"

# Fields the name/placeholder pass can meaningfully fill by calling
# `.fill()`/`.select_option()` on. Deliberately excludes checkbox/radio
# (need `.check()`, a different interaction) and hidden/submit/button/file
# (not "fields" in the profile-mapping sense at all). `{marker}` is filled in
# with `_AUTOMATION_EXAMINED_ATTR` at call time — a comma-separated CSS
# selector list needs the exclusion repeated on EVERY branch (appending it
# once at the end would only apply to the last branch, `textarea`).
_MAPPABLE_FIELD_SELECTOR_TEMPLATE = (
    "input:not([type='hidden']):not([type='submit']):not([type='button'])"
    ":not([type='file']):not([type='checkbox']):not([type='radio']):not([{marker}]), "
    "select:not([{marker}]), "
    "textarea:not([{marker}])"
)

# `phone`/`address` are the two `CandidateProfile` columns stored encrypted
# at rest (`phone_encrypted`/`address_encrypted` — see
# `app/models/db_models.py`); there is no plain `profile.phone` or
# `profile.address` attribute at all. `FieldMapper.FIELD_SYNONYMS` correctly
# resolves a label like "Current Residence Address" to the canonical
# attribute name `"address"` (see `automation/forms/field_mapper.py`'s module
# docstring — it deliberately keys on the plaintext-facing name, never the
# `_encrypted` column), but `getattr(self.profile, "address", None)` then
# silently returns `None` — not because the value is missing, but because
# that attribute doesn't exist on the ORM model under that name. The result:
# a real, filled-in address is resolved correctly and then thrown away,
# and the field is reported as "nothing to fill" rather than a failure.
# `fill_personal_information()` already avoided this by using `self.phone`
# (the adapter's own decrypting property) directly; the generic label/name
# sweep below did not. `_resolve_profile_value()` is the single place that
# now knows about this gap.
_ENCRYPTED_PROFILE_PROPERTIES = frozenset({"phone", "address"})

# A required "I agree to the Privacy Policy"-style checkbox has no
# CandidateProfile attribute at all — it isn't applicant data, it's a
# standing consent that submitting the application already implies. This is
# why `FieldMapper` correctly never claims it (no synonym could ever be
# right) and why Phase 6's answer-engine handoff explicitly excludes
# checkboxes (`_NON_FILLABLE_INPUT_TYPES`, below) — but the result, before
# `_fill_consent_checkboxes()` was added, was that NOTHING ever reached it:
# a required consent checkbox was silently left unchecked and blocked
# submission. Deliberately a narrow keyword list, not "check every
# checkbox" — gated by required-ness too (see `_fill_consent_checkboxes`) so
# an optional, non-required opt-in ("Send me job alerts") is never touched.
_CONSENT_CHECKBOX_KEYWORDS = (
    "i agree", "agree to", "i consent", "consent to", "i acknowledge",
    "acknowledge that", "i accept", "accept the", "terms and conditions",
    "terms of service", "privacy policy", "privacy notice",
)


def _looks_like_consent_checkbox(label_text: str) -> bool:
    normalized = label_text.strip().lower()
    return any(keyword in normalized for keyword in _CONSENT_CHECKBOX_KEYWORDS)


@dataclass
class FieldFillResult:
    """One field's fill outcome — used for confidence scoring and the
    review/needs-review/auto-submit decision in ARCHITECTURE.md.

    `failure` is optional/backward-compatible (defaults to `None`): a
    structured `automation.forms.field_handlers.FieldFailure` — field type,
    expected vs. actual value, why it failed, how many attempts were made —
    populated whenever `filled` is `False` because the field-handler
    pipeline actually tried and couldn't confirm the value stuck (as
    opposed to "there was nothing to fill" or "this was never this pass's
    job to answer", which stay `None`)."""

    field_key: str
    profile_path: str | None
    value_used: Any
    confidence: float
    filled: bool
    failure: FieldFailure | None = None


class ATSAdapter(ABC):
    """Base interface for every ATS-specific automation adapter.

    Concrete adapters live one per platform under `automation/ats/<name>/`
    and are selected via `automation/ats/detector.py::ATSDetector`. Only the
    five methods below are abstract; everything else on this class is a
    shared, concrete helper subclasses may call from their implementations.
    """

    #: Machine-readable platform identifier, e.g. "greenhouse", "lever".
    name: str

    def __init__(
        self,
        page: Any,
        profile: CandidateProfile,
        resume_document: ProfileDocument,
        answer_engine: ApplicationAnswerEngine | None = None,
    ):
        self.page = page
        self.profile = profile
        self.resume_document = resume_document
        # Phase 6: optional. `None` (the default) preserves the exact
        # pre-Phase-6 behavior — a labeled question FieldMapper can't match
        # is simply left unfilled. Only adapters constructed with a real
        # engine (see `ApplicationFlowManager`/`app/api/applications.py`)
        # attempt to answer these via `automation/forms/answer_engine.py`.
        self.answer_engine = answer_engine
        self._pending_answer_engine_questions: list[tuple[str, Locator]] = []

    # ------------------------------------------------------------------
    # Abstract contract
    # ------------------------------------------------------------------

    @abstractmethod
    def detect(self) -> float:
        """Return a confidence score (0..1) that `self.page` is this ATS.
        `ATSDetector` calls this as a secondary check after URL/domain matching
        when the URL pattern alone is ambiguous."""
        raise NotImplementedError

    @abstractmethod
    def fill_personal_information(self) -> list[FieldFillResult]:
        """Fill name/email/phone/location/links from `self.profile`."""
        raise NotImplementedError

    @abstractmethod
    def answer_questions(self) -> list[FieldFillResult]:
        """Answer screening questions — deterministic matches via
        `_fill_known_questions` today; genuinely subjective/novel questions
        route to Phase 6's `ApplicationAnswerEngine`."""
        raise NotImplementedError

    @abstractmethod
    def submit_application(self) -> bool:
        """Click submit — only ever called after the decision logic in
        ARCHITECTURE.md ("Compliance & Risk") authorizes it."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Shared helpers — concrete, available to every subclass
    # ------------------------------------------------------------------

    @property
    def phone(self) -> str | None:
        """Decrypted phone number (`CandidateProfile.phone_encrypted` is
        Fernet-encrypted at rest — see `app/core/crypto.py`)."""
        return decrypt_field(self.profile.phone_encrypted)

    @property
    def address(self) -> str | None:
        return decrypt_field(self.profile.address_encrypted)

    def _fill_context(self) -> dict:
        """Phase 8, PART 13 — extra debugging context attached to every
        `fill_field()` call this adapter makes, so a `FieldFailure`'s
        eventual `format_failure_report()` includes which ATS platform and
        which URL it happened on without `field_handlers.py` itself needing
        to know anything about either (it deliberately stays ATS-agnostic —
        see that module's docstring). Best-effort: a page that's already
        navigated away/closed just means `url` is omitted, never a crash."""
        context = {"ats_type": self.name}
        try:
            context["url"] = self.page.url
        except Exception:  # noqa: BLE001 - debugging context must never break a fill
            pass
        return context

    def upload_resume(self) -> bool:
        """Locates the resume upload `<input type=file>` (see
        `automation.browser.selectors.find_file_upload_input` — prefers a
        resume/CV-hinted input when more than one file input exists on the
        page) and uploads `self.resume_document` through the shared
        `FileUploadHandler` (fill + verify: the filename must actually show
        up on the input's `.files`, or visibly somewhere nearby for ATS UIs
        that keep the real input off-screen). Some ATS "Attach / Dropbox /
        Google Drive / Enter manually" UIs only reveal the real input after
        a visible trigger is clicked — tried once as a fallback before
        giving up. Concrete rather than abstract: every adapter's
        implementation was byte-identical, so it lives here once."""
        upload_input = find_file_upload_input(self.page)
        if upload_input is None:
            trigger = find_upload_trigger_button(self.page)
            if trigger is not None:
                try:
                    trigger.click()
                    upload_input = find_file_upload_input(self.page)
                except PlaywrightError as e:
                    logger.debug("%s: clicking the upload trigger failed (%s).", self.name, e)

        if upload_input is None:
            logger.warning("%s: no resume upload input found on page.", self.name)
            return False

        field = describe_field(upload_input, label="resume_upload", page=self.page)
        outcome = fill_field(field, self.resume_document.stored_path, context=self._fill_context())
        if outcome.filled:
            logger.info("%s: resume uploaded (%s).", self.name, outcome.actual_value or self.resume_document.original_filename)
        else:
            reason = outcome.failure.failure_reason if outcome.failure else "unknown"
            logger.warning("%s: resume upload could not be verified (%s).", self.name, reason)
        return outcome.filled

    def _fill_first_match(self, selectors: list[str], value: Any) -> tuple[bool, FieldFailure | None]:
        """Fills the first visible input matching any selector in `selectors`
        (tried in order) with `value` via the shared field-handler pipeline
        (fill + verify + retry). Returns `(False, None)` immediately if
        `value` is empty/None; otherwise `(True, None)` on the first
        candidate that verifies filled, or `(False, <last attempted
        candidate's failure>)` if none did."""
        if value in (None, "", []):
            return False, None

        last_failure: FieldFailure | None = None
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                if locator.count() == 0:
                    continue
                target = locator.first
                if not target.is_visible():
                    continue
            except PlaywrightError as e:
                logger.debug("Selector %r not usable (%s) — trying next candidate.", selector, e)
                continue

            field = describe_field(target, label=selector, page=self.page)
            outcome = fill_field(field, value, context=self._fill_context())
            if outcome.filled:
                self._mark_examined(target)
                return True, None
            last_failure = outcome.failure

        return False, last_failure

    def _mark_examined(self, locator: Locator) -> None:
        """Flags `locator`'s element as already handled so `_fill_known_questions`'s
        name/placeholder pass skips it — best-effort; a failure here just
        means that one field might be looked at twice, never a crash."""
        try:
            locator.evaluate(f"el => el.setAttribute('{_AUTOMATION_EXAMINED_ATTR}', '1')")
        except PlaywrightError as e:
            logger.debug("Could not mark an element as examined (%s) — continuing anyway.", e)

    def _resolve_profile_value(self, attribute: str) -> Any:
        """Reads `attribute` for the generic label/name sweep — routes
        `phone`/`address` through this adapter's own decrypting properties
        (`self.phone`/`self.address`) rather than `self.profile` directly,
        since those two are Fernet-encrypted columns with no plaintext
        attribute of the same name on the ORM model (see
        `_ENCRYPTED_PROFILE_PROPERTIES`). Every other attribute is read off
        `self.profile` exactly as before."""
        if attribute in _ENCRYPTED_PROFILE_PROPERTIES:
            return getattr(self, attribute, None)
        return getattr(self.profile, attribute, None)

    def _match_label_to_profile_attribute(self, label_text: str) -> str | None:
        """Matches a question's label text against `FieldMapper` (Phase 5).
        Returns the matching `CandidateProfile` attribute name, or `None` if
        this looks like a subjective/novel question (Phase 6 territory)."""
        match = FieldMapper.map_field(label=label_text)
        return match[0] if match else None

    def _input_for_label(self, label: Locator) -> Locator | None:
        """Finds the form control associated with a `<label>`: via its `for`
        attribute, or an input/select/textarea nested inside the label
        itself (both are valid, common HTML patterns).

        Real ATS markup regularly uses array-style ids for checkbox groups —
        e.g. `for="question_11371103007[]"` (Rails/PHP multi-value field
        convention) — which is NOT a valid bare CSS identifier. Building the
        selector as `f"#{for_id}"` breaks on exactly this (a `SyntaxError`
        from the browser's own `querySelectorAll`, seen in production against
        a real Greenhouse posting: `'#question_11371103007[]' is not a valid
        selector`), and — because that error wasn't caught here — it aborted
        the entire application run instead of just this one field. Using an
        attribute-equals selector instead treats `for_id` as a literal string
        value rather than parsed selector syntax, so brackets/spaces/etc.
        inside it can't break the selector; the `.count()` calls are also
        wrapped so any other unexpected DOM error degrades to "no match"
        instead of propagating."""
        try:
            for_id = label.get_attribute("for")
        except PlaywrightError:
            for_id = None

        if for_id:
            escaped_id = for_id.replace("\\", "\\\\").replace('"', '\\"')
            try:
                target = self.page.locator(f'[id="{escaped_id}"]')
                if target.count() > 0:
                    return target.first
            except PlaywrightError as e:
                logger.debug("Could not resolve label target for id %r (%s) — falling back.", for_id, e)

        try:
            nested = label.locator("input, select, textarea")
            if nested.count() > 0:
                return nested.first
        except PlaywrightError as e:
            logger.debug("Could not resolve a nested input for a label (%s).", e)
        return None

    def _fill_known_questions(self) -> list[FieldFillResult]:
        """Generic, cross-ATS sweep of the page: every `<label>`'s text
        first (Phase 5 — `FieldMapper`), then — if an `ApplicationAnswerEngine`
        was injected (Phase 6) — every labeled-but-unmatched question that
        pass collected, batched into one call, then — for anything still not
        examined — every remaining input/select/textarea's `name`/
        `placeholder` attributes directly, which catches fields an ATS
        renders with no `<label>` at all, then finally any required
        "I agree"/consent checkbox nothing above could ever reach (see
        `_fill_consent_checkboxes`). Fields nothing could answer are
        left unfilled — not reported as failures, just not this run's to
        answer (or, with no engine injected, exactly Phase 5's behavior)."""
        results = self._fill_questions_by_label()
        results.extend(self._fill_questions_via_answer_engine())
        results.extend(self._fill_questions_by_name_or_placeholder())
        results.extend(self._fill_consent_checkboxes())
        return results

    def _fill_questions_by_label(self) -> list[FieldFillResult]:
        """Pass 1: matches each `<label>`'s text against `FieldMapper` and,
        for matches with a non-empty profile value, fills the associated
        input/select. Marks whatever input it examines (matched or not) so
        pass 2 never reconsiders the same field."""
        results: list[FieldFillResult] = []
        self._pending_answer_engine_questions = []
        labels = self.page.locator("label")

        try:
            label_count = labels.count()
        except PlaywrightError:
            return results

        for i in range(label_count):
            label = labels.nth(i)
            try:
                text = (label.text_content() or "").strip()
            except PlaywrightError:
                continue
            if not text:
                continue

            attribute = self._match_label_to_profile_attribute(text)
            if attribute is None:
                self._collect_for_answer_engine(text, label)
                continue  # not a deterministic match — Phase 6's ApplicationAnswerEngine, if any, handles it next

            value = self._resolve_profile_value(attribute)
            if value in (None, "", []):
                continue

            try:
                input_locator = self._input_for_label(label)
            except Exception as e:  # noqa: BLE001 - a broken selector for ONE field must never abort the whole sweep
                logger.debug("Could not resolve the control for label %r (%s) — skipping this field.", text, e)
                results.append(FieldFillResult(field_key=text, profile_path=attribute, value_used=value, confidence=0.0, filled=False))
                continue
            if input_locator is None:
                results.append(FieldFillResult(field_key=text, profile_path=attribute, value_used=value, confidence=0.0, filled=False))
                continue

            try:
                already_examined = input_locator.get_attribute(_AUTOMATION_EXAMINED_ATTR)
            except PlaywrightError:
                already_examined = None
            if already_examined:
                # A real ATS page can legitimately have more than one <label>
                # resolving to the SAME input — e.g. a resume-autofill preview
                # panel that echoes "First Name" back with its own label
                # pointing at the very field fill_personal_information() (or
                # an earlier label in this same pass) already filled. Without
                # this check the field gets redundantly re-filled (harmless
                # to the value, but it inflates the result count and
                # therefore ApplicationFlowManager's confidence score).
                continue

            try:
                is_visible = input_locator.is_visible()
            except PlaywrightError as e:
                logger.debug("Could not check visibility for matched question %r (%s): %s", text, attribute, e)
                is_visible = False

            if not is_visible:
                results.append(FieldFillResult(field_key=text, profile_path=attribute, value_used=value, confidence=0.0, filled=False))
                continue

            field = describe_field(input_locator, label=text, page=self.page, profile_attribute=attribute)
            outcome = fill_field(field, value, context=self._fill_context())
            if outcome.filled:
                self._mark_examined(input_locator)
            results.append(FieldFillResult(
                field_key=text, profile_path=attribute, value_used=value,
                confidence=0.9 if outcome.filled else 0.0, filled=outcome.filled, failure=outcome.failure,
            ))

        return results

    # ------------------------------------------------------------------
    # Phase 6 — ApplicationAnswerEngine handoff
    # ------------------------------------------------------------------

    #: `<input type=...>` values the answer engine must never try to `.fill()`
    #: — same exclusion list as `_MAPPABLE_FIELD_SELECTOR_TEMPLATE`'s pass 2.
    _NON_FILLABLE_INPUT_TYPES = frozenset({"checkbox", "radio", "hidden", "submit", "button", "file"})

    def _collect_for_answer_engine(self, text: str, label: Locator) -> None:
        """Records a labeled-but-FieldMapper-unmatched question as a
        candidate for Phase 6's `ApplicationAnswerEngine` — a no-op unless
        one was actually injected (`self.answer_engine`), which keeps every
        adapter built before Phase 6 (and every call site that doesn't pass
        `answer_engine=`) behaving exactly as it did under Phase 5 alone."""
        if self.answer_engine is None:
            return

        try:
            input_locator = self._input_for_label(label)
        except Exception as e:  # noqa: BLE001 - a broken selector for ONE field must never abort the whole sweep
            logger.debug("Could not resolve the control for question %r (%s) — skipping.", text, e)
            return
        if input_locator is None:
            return

        try:
            already_examined = input_locator.get_attribute(_AUTOMATION_EXAMINED_ATTR)
        except PlaywrightError:
            already_examined = None
        if already_examined:
            return

        try:
            if not input_locator.is_visible():
                return
            tag_name = input_locator.evaluate("el => el.tagName.toLowerCase()")
            if tag_name not in ("input", "textarea", "select"):
                return
            if tag_name == "input":
                input_type = (input_locator.get_attribute("type") or "text").lower()
                if input_type in self._NON_FILLABLE_INPUT_TYPES:
                    return
        except PlaywrightError as e:
            logger.debug("Could not inspect the control for question %r (%s) — skipping.", text, e)
            return

        # Marked immediately, not just once actually filled below — this is
        # what stops another <label> resolving to the same input (see the
        # duplicate-label fix, above) or pass 2's name/placeholder sweep from
        # ever picking this control up a second time.
        self._mark_examined(input_locator)
        self._pending_answer_engine_questions.append((text, input_locator))

    def _fill_questions_via_answer_engine(self) -> list[FieldFillResult]:
        """Phase 6: batches every labeled-but-unmatched question collected
        during the label pass into ONE `ApplicationAnswerEngine.answer_batch()`
        call, then fills whatever it could answer. Returns `[]` immediately
        (no-op) if no engine was injected, or nothing was collected."""
        pending = self._pending_answer_engine_questions
        self._pending_answer_engine_questions = []
        if not self.answer_engine or not pending:
            return []

        questions = [text for text, _ in pending]
        try:
            answers = self.answer_engine.answer_batch(questions)
        except Exception as e:  # noqa: BLE001 - a broken AnswerEngine call must never abort the whole sweep
            logger.debug(
                "ApplicationAnswerEngine batch call failed (%s) — leaving %d question(s) unanswered.",
                e, len(pending),
            )
            return [
                FieldFillResult(field_key=text, profile_path=None, value_used=None, confidence=0.0, filled=False)
                for text, _ in pending
            ]

        results: list[FieldFillResult] = []
        for (text, input_locator), answer_result in zip(pending, answers):
            profile_path = f"answer_engine:{answer_result.source}"
            if not answer_result.answer:
                results.append(FieldFillResult(field_key=text, profile_path=profile_path, value_used=None, confidence=0.0, filled=False))
                continue

            field = describe_field(input_locator, label=text, page=self.page, profile_attribute=profile_path)
            outcome = fill_field(field, answer_result.answer, context=self._fill_context())
            results.append(FieldFillResult(
                field_key=text, profile_path=profile_path, value_used=answer_result.answer,
                confidence=answer_result.confidence if outcome.filled else 0.0, filled=outcome.filled, failure=outcome.failure,
            ))

        return results

    def _fill_questions_by_name_or_placeholder(self) -> list[FieldFillResult]:
        """Pass 2 (Phase 5): every input/select/textarea NOT already marked
        `_AUTOMATION_EXAMINED_ATTR` — by `_fill_first_match` or pass 1 above
        — resolved via its own `name`/`placeholder` attribute instead of a
        `<label>`. This is what actually lets a bare
        `<input name="linkedin_url" placeholder="LinkedIn URL">` with no
        `<label>` at all get filled, which pass 1 alone could never do."""
        results: list[FieldFillResult] = []
        selector = _MAPPABLE_FIELD_SELECTOR_TEMPLATE.format(marker=_AUTOMATION_EXAMINED_ATTR)

        try:
            # `.all()` snapshots the matching elements once, up front. This
            # loop marks each element it examines with the exclusion
            # attribute as it goes — using a live `.nth(i)`/`.count()` query
            # here instead (like the label pass above does, safely, since it
            # never touches the elements `page.locator("label")` matches)
            # would re-run the selector on every call and shift every later
            # index as earlier elements drop out of the `:not([marker])` set.
            candidates = self.page.locator(selector).all()
        except PlaywrightError:
            return results

        for input_locator in candidates:
            try:
                name_attr = input_locator.get_attribute("name") or ""
                placeholder_attr = input_locator.get_attribute("placeholder") or ""
            except PlaywrightError:
                continue
            if not name_attr and not placeholder_attr:
                continue

            match = FieldMapper.map_field(name=name_attr, placeholder=placeholder_attr)
            if match is None:
                continue  # not a deterministic match — leave for Phase 6
            attribute, confidence = match

            value = self._resolve_profile_value(attribute)
            if value in (None, "", []):
                continue

            field_key = name_attr or placeholder_attr
            try:
                is_visible = input_locator.is_visible()
            except PlaywrightError as e:
                logger.debug("Could not check visibility for matched field %r (%s): %s", field_key, attribute, e)
                is_visible = False

            self._mark_examined(input_locator)  # unconditional — this pass's own snapshot never revisits it either way

            if not is_visible:
                results.append(FieldFillResult(field_key=field_key, profile_path=attribute, value_used=value, confidence=0.0, filled=False))
                continue

            field = describe_field(input_locator, label=field_key, page=self.page, profile_attribute=attribute)
            outcome = fill_field(field, value, context=self._fill_context())
            results.append(FieldFillResult(
                field_key=field_key, profile_path=attribute, value_used=value,
                confidence=confidence if outcome.filled else 0.0, filled=outcome.filled, failure=outcome.failure,
            ))

        return results

    def _fill_consent_checkboxes(self) -> list[FieldFillResult]:
        """Pass 4: a required "I agree to the Privacy Policy"-style checkbox
        has no `CandidateProfile` attribute at all — it isn't applicant
        data, it's a standing consent that submitting this application
        already implies. Without this pass it's structurally unreachable:
        `_fill_questions_by_label` only proceeds once `FieldMapper` returns a
        profile attribute (it correctly never does for consent text), and
        `_collect_for_answer_engine` explicitly excludes checkbox inputs
        (`_NON_FILLABLE_INPUT_TYPES`) even when an answer engine is
        injected. The result, before this pass existed: a required consent
        checkbox was silently left unchecked, which blocks submission
        outright on any ATS that enforces it client-side.

        Deliberately narrow — this is NOT "check every checkbox on the
        page." A checkbox is only auto-checked when BOTH:

        - its own `<label>` text matches a legal/consent keyword
          (`_looks_like_consent_checkbox` — "I agree", "I consent",
          "privacy policy", "terms of service", ...), AND
        - it looks required (a native `required` attribute,
          `aria-required="true"`, or the same trailing `*` convention
          `FieldMapper` already strips off other required-field labels).

        An optional, non-required opt-in checkbox ("Send me job alerts")
        fails the second condition and is left exactly as the candidate
        would have left it — unchecked."""
        results: list[FieldFillResult] = []
        labels = self.page.locator("label")

        try:
            label_count = labels.count()
        except PlaywrightError:
            return results

        for i in range(label_count):
            label = labels.nth(i)
            try:
                text = (label.text_content() or "").strip()
            except PlaywrightError:
                continue
            if not text or not _looks_like_consent_checkbox(text):
                continue

            try:
                input_locator = self._input_for_label(label)
            except Exception as e:  # noqa: BLE001 - a broken selector for ONE field must never abort the whole sweep
                logger.debug("Could not resolve the control for consent label %r (%s) — skipping.", text, e)
                continue
            if input_locator is None:
                continue

            try:
                already_examined = input_locator.get_attribute(_AUTOMATION_EXAMINED_ATTR)
                tag_name = input_locator.evaluate("el => el.tagName.toLowerCase()")
                input_type = (input_locator.get_attribute("type") or "").lower() if tag_name == "input" else ""
                looks_required = (
                    "*" in text
                    or input_locator.get_attribute("required") is not None
                    or (input_locator.get_attribute("aria-required") or "").lower() == "true"
                )
                is_visible = input_locator.is_visible()
            except PlaywrightError as e:
                logger.debug("Could not inspect consent checkbox candidate %r (%s) — skipping.", text, e)
                continue

            if already_examined or tag_name != "input" or input_type != "checkbox":
                continue
            if not looks_required or not is_visible:
                continue

            self._mark_examined(input_locator)
            field = describe_field(input_locator, label=text, page=self.page, profile_attribute="consent_checkbox")
            outcome = fill_field(field, True, context=self._fill_context())
            results.append(FieldFillResult(
                field_key=text, profile_path="consent_checkbox", value_used=True,
                confidence=0.9 if outcome.filled else 0.0, filled=outcome.filled, failure=outcome.failure,
            ))

        return results
