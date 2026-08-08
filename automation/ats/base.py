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
from dataclasses import dataclass, field as _dc_field
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError, Locator

from app.core.crypto import decrypt_field
from automation.browser.selectors import (
    dismiss_overlays,
    find_file_upload_input,
    find_next_button,
    find_page_heading,
    find_submit_button,
    find_unfilled_required_field_locators,
    find_upload_trigger_button,
    looks_like_review_page,
)
from automation.forms.answer_engine import (
    DETERMINISTIC_CONFIDENCE,
    ApplicationAnswerEngine,
    Question,
)
from automation.forms.field_handlers import (
    Field,
    FieldFailure,
    describe_field,
    fill_field,
    read_field_options,
)
from automation.forms.field_mapper import FieldMapper, looks_like_opt_in_label
from automation.forms.profile_formatting import format_profile_value
from automation.forms.vision_fallback import VisionField, VisionFormAnswerer, save_debug_crops
from automation.interfaces import CandidateProfile, ProfileDocument

logger = logging.getLogger(__name__)

# Marks any input this adapter has already examined/filled (via any of the
# three fill paths) so later passes never double-process — and double-count
# toward `ApplicationFlowManager`'s confidence score — the same field.
_AUTOMATION_EXAMINED_ATTR = "data-automation-examined"

# A per-pass stable identity for each candidate field, written once up front by
# `_stamp_candidates()`.
#
# This exists because `_AUTOMATION_EXAMINED_ATTR` appears in the very selector
# the sweep iterates (`_MAPPABLE_FIELD_SELECTOR_TEMPLATE`), and `Locator.all()`
# does NOT snapshot elements — see `_stamp_candidates`. Addressing candidates
# by a unique attribute of their own instead means writing the examined marker
# can never change what an in-flight locator points at, so marking is free to
# happen at the correct time (after the fill) rather than early.
_AUTOMATION_CANDIDATE_ATTR = "data-automation-candidate"

# The required-field marker, which is part of the RENDERED text of a question
# and therefore rides along in any text recovered from the DOM. Lever uses
# U+2731 HEAVY ASTERISK, not an ASCII "*" — observed live as
# 'When are you available to start working?✱'. Left in place it travels into
# the LLM prompt, into the persistent answer cache's key, and into synonym
# matching, none of which want it. `_normalize_text` in field_mapper.py strips
# a trailing ASCII asterisk only, so these need handling here too.
_REQUIRED_MARKER_CHARS = "*✱＊∗﹡·•"


def _strip_required_marker(text: str) -> str:
    """Question text without its trailing required marker or the whitespace
    around it. Only ever strips from the END: a leading bullet could be
    meaningful content, and no ATS puts the marker first."""
    return " ".join(text.split()).rstrip(_REQUIRED_MARKER_CHARS).strip()

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
# A GENERATED answer (as opposed to one read straight out of the candidate's
# profile) is only auto-filled when the answer engine reports at least this
# much confidence in it; below this, the field is left blank for a human.
#
# 0.80 is not arbitrary — it is the threshold both specs independently settled
# on for "take this action yourself vs. hand it to a human." Note this gates
# only `_fill_questions_via_answer_engine`: deterministic answers come from
# real profile columns at `DETERMINISTIC_CONFIDENCE` and are unaffected, as
# are cache hits, which carry forward whatever confidence they were stored
# with.
ANSWER_REVIEW_CONFIDENCE_THRESHOLD = 0.80

# Short on purpose: this only ever runs over fields the required-field scan
# already confirmed VISIBLE, so a scroll that can't complete quickly means
# something about the element is unusual, and the crop attempt that follows
# either works from wherever the page currently sits or is skipped. Waiting
# Playwright's 30s default per field would turn one odd control into a
# minutes-long stall at the very end of a run.
_VISION_SCROLL_TIMEOUT_MS = 3_000

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


@dataclass
class VisionPassOutcome:
    """What the vision fallback pass (`fill_unfilled_fields_with_vision`) did.

    Two separate answers, deliberately not flattened into one list:

    - `results` — fields it actually tried to fill, scored like any other fill.
    - `confirmed_already_filled` — fields whose SCREENSHOT showed a value the
      required-field scan couldn't see (a react-select combobox, a country
      picker: the visible selection lives outside the input whose value the
      scan reads). Nothing was filled and nothing failed, so there is no
      honest `FieldFillResult` to emit — but the flow manager needs to know,
      because otherwise these fields keep every such form permanently in
      `manual_required` over values that are demonstrably already there.
      Names match `find_unfilled_required_fields`' naming exactly, so the flow
      manager can reconcile the two lists."""

    results: list[FieldFillResult] = _dc_field(default_factory=list)
    confirmed_already_filled: list[str] = _dc_field(default_factory=list)


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
        # Radio group `name`s already queued this sweep. A group asks ONE
        # question but has N members, each of which reaches
        # `_collect_for_answer_engine` through its own option label.
        self._seen_radio_groups: set[str] = set()

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
    # Multi-page navigation — concrete defaults, overridable per platform
    # ------------------------------------------------------------------
    # These are NOT abstract, and that is the design: every method below has a
    # working generic implementation built on `automation/browser/selectors.py`,
    # so a long application works on any ATS without per-platform code, and
    # every adapter written before multi-page support existed keeps behaving
    # exactly as it did. A platform whose markup the generic heuristics can't
    # read (Workday, whose buttons are identified by `data-automation-id`
    # rather than by their text) overrides just the one method it needs.
    #
    # `ApplicationFlowManager` calls only these — never `find_next_button` and
    # friends directly — which is what keeps the page loop free of any
    # knowledge of a specific ATS.

    def find_next_control(self) -> Locator | None:
        """The control that advances to the next page, or `None` on the last
        one. Generic default: a "Next"/"Continue"/"Save and Continue" button
        that isn't inside a cookie banner (see `find_next_button`)."""
        return find_next_button(self.page)

    def find_submit_control(self) -> Locator | None:
        """The control that submits the completed application, or `None`."""
        return find_submit_button(self.page)

    def is_final_page(self) -> bool:
        """Whether the application has reached its last page — the review or
        submit step, where the run stops and hands over.

        Generic default: no Next control means there is nowhere left to go. That
        is exactly the rule the flow manager applied inline before multi-page
        support existed, so single-page platforms (Greenhouse, Lever) are
        unaffected. A platform that keeps a Next-shaped button on its review
        page overrides this."""
        return self.find_next_control() is None

    def is_review_page(self) -> bool:
        """Whether this page is showing the application back for a final check
        before submission. Informational — it sharpens the run log and the
        handoff message; `is_final_page` is what actually stops the loop."""
        return looks_like_review_page(self.page)

    def page_label(self) -> str:
        """What a human would call the current page ("My Experience"), for the
        run log. `""` when the page has no heading of its own."""
        return find_page_heading(self.page)

    def dismiss_distractions(self) -> list[str]:
        """Closes cookie/consent/chat overlays that would otherwise intercept
        clicks on this page's own controls. Returns what was dismissed."""
        return dismiss_overlays(self.page)

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
        implementation was byte-identical, so it lives here once.

        A `True` here means "the file was accepted at this moment," which on a
        client-rendered form is NOT the same as "the application will carry a
        résumé" — see `ensure_resume_attached`, which is what actually
        guarantees that and is called at the end of the run."""
        upload_input = self._find_resume_input()
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

    def _find_resume_input(self) -> Locator | None:
        """The page's résumé file input, revealing it first if this ATS keeps
        it behind an "Attach"/"Upload" trigger. `None` when this page has no
        upload field at all (a later step of a multi-step form, a posting that
        doesn't ask for a résumé)."""
        upload_input = find_file_upload_input(self.page)
        if upload_input is not None:
            return upload_input

        trigger = find_upload_trigger_button(self.page)
        if trigger is None:
            return None
        try:
            trigger.click()
        except PlaywrightError as e:
            logger.debug("%s: clicking the upload trigger failed (%s).", self.name, e)
            return None
        return find_file_upload_input(self.page)

    #: Is a résumé visibly attached, as the FORM sees it? Checks the input's
    #: own `files` first, then — for ATS UIs that upload to S3 and clear the
    #: input, keeping the filename only in their own rendered state — whether
    #: the uploaded file's name appears in the upload widget's text. Without
    #: that second half, every such UI would look "empty" and get re-uploaded
    #: on every check.
    _RESUME_ATTACHED_JS = """
    (el, filename) => {
      if (el.files && el.files.length > 0) return true;
      const group = el.closest('[class*="file-upload" i], [class*="upload" i], [class*="resume" i]')
                 || el.parentElement;
      const text = ((group && group.textContent) || '').toLowerCase();
      return !!filename && text.includes(filename.toLowerCase());
    }
    """

    def resume_attachment_state(self) -> str:
        """`"attached"` | `"missing"` | `"no_field"` — what the LIVE page says
        about the résumé right now, as opposed to what `upload_resume()`
        verified when it ran.

        `"no_field"` is a distinct answer from `"missing"` on purpose: a page
        with no upload input isn't a page that lost the résumé, and treating
        the two the same would make every later step of a multi-step form
        re-attempt an upload it has no field for."""
        upload_input = find_file_upload_input(self.page)
        if upload_input is None:
            return "no_field"
        filename = Path(str(self.resume_document.stored_path)).name
        try:
            attached = bool(upload_input.evaluate(self._RESUME_ATTACHED_JS, filename))
        except PlaywrightError as e:
            logger.debug("%s: could not read the résumé input's state (%s).", self.name, e)
            return "missing"
        return "attached" if attached else "missing"

    def ensure_resume_attached(self, *, max_attempts: int = 2) -> bool | None:
        """Re-checks — at the END of the run, after every fill pass and after
        the form has had time to hydrate — that the résumé is still attached,
        and re-uploads it if it isn't. Returns `True`/`False` for
        attached/not, or `None` when this page has no upload field to check
        (so the caller leaves its earlier upload result alone rather than
        recording a failure for a field that doesn't exist here).

        This exists because a successful `upload_resume()` is not durable on a
        client-rendered form. Observed on a live Greenhouse posting: the file
        was set and verified (`input.files.length == 1`), React hydration
        recovered from an error ~600ms later and re-created the upload widget,
        and the application went on to be filled and handed over with the
        résumé silently gone — while the run log said "resume upload
        succeeded." A verification is only as good as the moment it was taken;
        this is the one taken at the moment that matters."""
        state = self.resume_attachment_state()
        if state == "no_field":
            logger.debug("%s: no résumé field on the current page — nothing to re-check.", self.name)
            return None
        if state == "attached":
            logger.info("%s: résumé still attached at the end of the run.", self.name)
            return True

        for attempt in range(1, max_attempts + 1):
            logger.warning(
                "%s: the résumé is NO LONGER attached (the form dropped it after it was uploaded) "
                "— re-uploading, attempt %d/%d.",
                self.name, attempt, max_attempts,
            )
            self.upload_resume()
            if self.resume_attachment_state() == "attached":
                logger.info("%s: résumé re-attached successfully.", self.name)
                return True

        logger.error(
            "%s: could not keep the résumé attached after %d attempt(s) — the application would be "
            "submitted without it, so this run must go to a human.",
            self.name, max_attempts,
        )
        return False

    # ------------------------------------------------------------------
    # Vision fallback — see automation/forms/vision_fallback.py
    # ------------------------------------------------------------------

    #: How much of the form around a field to include in its crop. The top
    #: margin is by far the largest and is the whole point: the commonest field
    #: this pass exists for is a conditional follow-up ("If yes to the above,
    #: what role?") whose answer depends entirely on the question and answer
    #: ABOVE it. A tight crop of the field itself would show the model exactly
    #: what the DOM already showed the text engine — i.e. nothing new.
    _VISION_CROP_PADDING = {"top": 240, "bottom": 80, "left": 48, "right": 48}

    #: The clip rectangle for one field's crop, in VIEWPORT coordinates (what
    #: `page.screenshot(clip=...)` takes when `full_page` is false), already
    #: padded and clamped to the viewport so the caller can pass it straight
    #: through. Climbs to the nearest ancestor with a real box when the control
    #: itself has none — a custom widget's actual input is often a zero-size or
    #: visually-hidden element whose rect would be unusable.
    _VISION_CROP_RECT_JS = """
    (el, pad) => {
      let rect = null, cur = el;
      for (let i = 0; i < 4 && cur; i++) {
        const r = cur.getBoundingClientRect();
        if (r.width > 1 && r.height > 1) { rect = r; break; }
        cur = cur.parentElement;
      }
      if (!rect) return null;
      const x = Math.max(0, rect.x - pad.left);
      const y = Math.max(0, rect.y - pad.top);
      const right = Math.min(window.innerWidth, rect.x + rect.width + pad.right);
      const bottom = Math.min(window.innerHeight, rect.y + rect.height + pad.bottom);
      if (right - x < 2 || bottom - y < 2) return null;
      return {x: x, y: y, width: right - x, height: bottom - y};
    }
    """

    #: The best question text the page exposes for a control: its ARIA name, a
    #: `<label for>`, or a wrapping `<label>`. Returns `""` when there is none,
    #: which is a real answer — a field whose question the DOM never connected
    #: to it is precisely the kind this pass reads off a screenshot instead.
    _FIELD_QUESTION_TEXT_JS = """
    el => {
      const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
      const aria = clean(el.getAttribute('aria-label'));
      if (aria) return aria;
      const labelledBy = el.getAttribute('aria-labelledby');
      if (labelledBy) {
        const parts = labelledBy.split(/\\s+/)
          .map(id => { const n = document.getElementById(id); return n ? clean(n.textContent) : ''; })
          .filter(Boolean);
        if (parts.length) return parts.join(' ');
      }
      if (el.id) {
        const forLabel = document.querySelector('label[for="' + el.id.replace(/"/g, '\\\\"') + '"]');
        if (forLabel) { const t = clean(forLabel.textContent); if (t) return t; }
      }
      const wrapping = el.closest('label');
      if (wrapping) { const t = clean(wrapping.textContent); if (t) return t; }
      return '';
    }
    """

    def _question_text_for(self, locator: Locator) -> str:
        """The question a control is asking, as text — its own label/ARIA name
        if it has one, else the nearby-text recovery the unlabeled-field pass
        uses. `""` when neither finds anything."""
        try:
            labelled = _strip_required_marker(locator.evaluate(self._FIELD_QUESTION_TEXT_JS) or "")
        except PlaywrightError:
            labelled = ""
        return labelled or self._nearby_question_text(locator)

    def _vision_crop(self, locator: Locator) -> bytes | None:
        """A PNG of `locator` in context, or `None` if it can't be captured.

        Scrolls the field into view first — the crop is taken in viewport
        coordinates, so a field below the fold would otherwise be clipped to
        nothing (or to whatever unrelated part of the form happens to be on
        screen, which is worse: the model would be reading the wrong
        question)."""
        try:
            locator.scroll_into_view_if_needed(timeout=_VISION_SCROLL_TIMEOUT_MS)
        except PlaywrightError as e:
            logger.debug("%s: could not scroll a field into view for its crop (%s).", self.name, e)
        try:
            clip = locator.evaluate(self._VISION_CROP_RECT_JS, self._VISION_CROP_PADDING)
            if not clip:
                return None
            return self.page.screenshot(clip=clip)
        except PlaywrightError as e:
            logger.debug("%s: could not capture a field crop (%s).", self.name, e)
            return None

    def collect_unfilled_fields_for_vision(self) -> list[tuple[VisionField, Field]]:
        """Every still-unfilled required field, paired with the introspected
        `Field` the eventual fill will reuse. Read-only apart from the option
        probe (`read_field_options` opens a custom dropdown and closes it
        again — see its docstring); the crop is taken BEFORE that probe so a
        menu that fails to close can't end up in the next field's screenshot."""
        collected: list[tuple[VisionField, Field]] = []
        for name, locator in find_unfilled_required_field_locators(self.page):
            question = self._question_text_for(locator)
            screenshot = self._vision_crop(locator)
            if screenshot is None:
                logger.info(
                    "%s: skipping %r in the vision pass — no screenshot could be taken of it.",
                    self.name, question or name,
                )
                continue
            described = describe_field(locator, label=question or name, page=self.page, profile_attribute="vision")
            collected.append((
                VisionField(
                    name=name,
                    question=question,
                    screenshot=screenshot,
                    options=read_field_options(described),
                    widget=described.tag_name or described.input_type or "",
                ),
                described,
            ))
        return collected

    def fill_unfilled_fields_with_vision(
        self,
        answerer: VisionFormAnswerer,
        *,
        debug_dir: Path | None = None,
    ) -> VisionPassOutcome:
        """LAST pass over the form: screenshots every required field still
        empty after every other pass, asks `answerer` to read them, and fills
        what comes back through the ordinary `fill_field()` pipeline (so a
        vision answer is verified against the live DOM like any other).

        Gated by the same `ANSWER_REVIEW_CONFIDENCE_THRESHOLD` as the text
        answer engine — an answer the model isn't confident in is left for a
        human rather than typed into an employer's form. See
        `automation/forms/vision_fallback.py` for the rest of the guardrails
        (demographics never asked, options re-resolved against the DOM,
        meta-commentary discarded)."""
        collected = self.collect_unfilled_fields_for_vision()
        if not collected:
            logger.info("%s: no unfilled required fields left — vision pass has nothing to do.", self.name)
            return VisionPassOutcome()

        vision_fields = [item for item, _described in collected]
        logger.info(
            "%s: vision pass looking at %d unfilled field(s): %s",
            self.name, len(vision_fields),
            ", ".join(item.question or item.name for item in vision_fields),
        )
        if debug_dir is not None:
            save_debug_crops(vision_fields, debug_dir)

        try:
            answers = answerer.answer(vision_fields)
        except Exception as e:  # noqa: BLE001 - a best-effort last pass must never abort the run
            logger.warning("%s: vision pass failed (%s) — leaving its fields for a human.", self.name, e)
            return VisionPassOutcome()

        outcome = VisionPassOutcome()
        for (item, described), answer in zip(collected, answers):
            if answer.already_filled:
                logger.info(
                    "%s: %r is already answered on the form (%s) — leaving it alone.",
                    self.name, item.question or item.name, answer.reason or "read off the screenshot",
                )
                outcome.confirmed_already_filled.append(item.name)
                continue

            profile_path = "vision"
            if not answer.answered:
                logger.info(
                    "%s: vision pass could not answer %r (%s).",
                    self.name, item.question or item.name, answer.reason or "declined",
                )
                outcome.results.append(FieldFillResult(
                    field_key=item.question or item.name, profile_path=profile_path,
                    value_used=None, confidence=0.0, filled=False,
                ))
                continue

            if answer.confidence < ANSWER_REVIEW_CONFIDENCE_THRESHOLD:
                logger.info(
                    "%s: leaving %r for a human — vision answer confidence %.2f is below %.2f (%s).",
                    self.name, item.question or item.name, answer.confidence,
                    ANSWER_REVIEW_CONFIDENCE_THRESHOLD, answer.reason or "no reason given",
                )
                outcome.results.append(FieldFillResult(
                    field_key=item.question or item.name, profile_path=profile_path,
                    value_used=None, confidence=0.0, filled=False,
                ))
                continue

            logger.info(
                "%s: vision pass answering %r (%s).",
                self.name, item.question or item.name, answer.reason or "no reason given",
            )
            fill_outcome = fill_field(described, answer.answer, context=self._fill_context())
            self._mark_examined(described.locator)
            outcome.results.append(FieldFillResult(
                field_key=item.question or item.name, profile_path=profile_path,
                value_used=answer.answer,
                confidence=answer.confidence if fill_outcome.filled else 0.0,
                filled=fill_outcome.filled, failure=fill_outcome.failure,
            ))

        return outcome

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
            raw = getattr(self, attribute, None)
        else:
            raw = getattr(self.profile, attribute, None)
        # Raw column values are NOT form text. This used to be a bare getattr,
        # so `requires_sponsorship` was typed into employers' forms as "True",
        # `notice_period_days` as "30" rather than "30 days", `expected_salary`
        # as "120000.0" and `years_of_experience` as "5.0" — all four observed
        # on live postings. `format_profile_value` is the single shared
        # formatter that `answer_engine` also routes through, so phrasing is
        # decided in one place and a newly added typed column can't silently
        # regress this (see `automation/forms/profile_formatting.py`).
        return format_profile_value(self.profile, attribute, raw)

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
        first (Phase 5 — `FieldMapper`), then every UNLABELED control whose
        question text can be recovered from the surrounding markup
        (`_fill_questions_by_nearby_text` — this is the only pass that reaches
        Lever's screening questions), then — if an `ApplicationAnswerEngine`
        was injected (Phase 6) — every unmatched question those two passes
        collected, batched into one call, then every CHECKBOX GROUP ("Pronouns",
        "I identify my ethnicity as — select all that apply") the passes above
        structurally cannot see, plus the "may we contact you about future
        roles" opt-in when — and only when — the candidate has opted in, then —
        for anything still not
        examined — every remaining input/select/textarea's `name`/
        `placeholder` attributes directly, which catches fields an ATS
        renders with no `<label>` at all, then finally any required
        "I agree"/consent checkbox nothing above could ever reach (see
        `_fill_consent_checkboxes`). Fields nothing could answer are
        left unfilled — not reported as failures, just not this run's to
        answer (or, with no engine injected, exactly Phase 5's behavior)."""
        results = self._fill_questions_by_label()
        results.extend(self._fill_questions_by_nearby_text())
        results.extend(self._fill_questions_via_answer_engine())
        results.extend(self._fill_checkbox_groups())
        results.extend(self._fill_opt_in_checkboxes())
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
        self._seen_radio_groups = set()
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
                # Not on the page the user is looking at — so neither scored nor
                # marked. Both halves matter on a multi-page application.
                #
                # Not scored: a wizard that keeps every step in the DOM and
                # toggles `display` (Workday's accordions, and most custom
                # multi-step forms) exposes ALL of its labels on every page. This
                # sweep runs once per page, so counting the other pages' fields
                # as failures buries the run's confidence under fields that were
                # never on screen — and `find_unfilled_required_fields` already
                # ignores hidden fields, so scoring them here made the two halves
                # of the same judgement disagree.
                #
                # Not marked: the examined marker persists for the whole
                # document, and this field may well be the one this form shows
                # three pages from now. Marking it would make it permanently
                # unfillable at the exact moment it becomes relevant.
                logger.debug("Skipping %r — its control is not visible on this page.", text)
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
    # `radio` is deliberately NOT here. It was, and that alone left a required
    # question blank on a live Lever posting ("Are you fluent in English?" with
    # Yes / No / Limited Working Proficiency): every member of the group hit
    # this guard and the group was never asked at all. The exclusion made sense
    # when the engine could only produce prose — typing a sentence at a radio
    # is meaningless — but it is option-aware now, so a fixed-choice group is
    # among the SAFEST things to send it: `read_field_options` supplies the real
    # choices and `_match_option` discards anything that isn't one of them.
    # Checkboxes stay excluded: they have their own narrow, consent-gated path
    # (`_fill_consent_checkboxes`) and a checkbox is never a multi-choice
    # question the engine should be picking an answer for.
    _NON_FILLABLE_INPUT_TYPES = frozenset({"checkbox", "hidden", "submit", "button", "file"})

    #: The question a RADIO GROUP asks, which is never any one member's own
    #: label. On the live Lever form each radio is `<label><input>Yes</label>`,
    #: so pass 1 sees the OPTION text ("Yes") and the real question sits four
    #: ancestors up, in a container holding all three radios:
    #:
    #:     <li class="application-question">
    #:       <div>Are you fluent in English?✱
    #:         <div class="application-field">
    #:           <ul><li><label><input type=radio value=Yes>Yes</label></li> ...
    #:
    #: `_NEARBY_QUESTION_TEXT_JS` cannot reach it: that helper stops as soon as
    #: an ancestor holds more than one control, which is correct for a lone text
    #: input (shared text must not be attributed to one of several fields) and
    #: exactly wrong here, where several controls sharing one question IS the
    #: shape. So this walks up while every control in the container belongs to
    #: THIS radio group, and strips the option labels — a `<label>` that wraps a
    #: member or points at one via `for` — leaving only the group's own prose.
    _RADIO_GROUP_QUESTION_JS = """
    el => {
      const groupName = el.name;
      const isMember = n => n.tagName === 'INPUT' && n.type === 'radio' && n.name === groupName;
      let cur = el;
      for (let i = 0; i < 6 && cur.parentElement; i++) {
        cur = cur.parentElement;
        const controls = Array.from(cur.querySelectorAll('input, textarea, select'));
        if (!controls.length || !controls.every(isMember)) break;
        const clone = cur.cloneNode(true);
        clone.querySelectorAll('label').forEach(l => {
          const wraps = !!l.querySelector('input[type="radio"]');
          let pointsAt = null;
          const forAttr = l.getAttribute('for');
          if (forAttr) {
            try { pointsAt = clone.querySelector('#' + CSS.escape(forAttr)); } catch (e) { pointsAt = null; }
          }
          if (wraps || (pointsAt && isMember(pointsAt))) l.remove();
        });
        clone.querySelectorAll('input, textarea, select, button').forEach(n => n.remove());
        const text = (clone.textContent || '').replace(/\\s+/g, ' ').trim();
        if (text.length >= 10 && text.length <= 400) return text;
      }
      return '';
    }
    """

    def _radio_group_question(self, radio: Locator) -> str:
        """The question a radio group asks, or "" if it can't be recovered."""
        try:
            return _strip_required_marker(radio.evaluate(self._RADIO_GROUP_QUESTION_JS) or "")
        except PlaywrightError:
            return ""

    def _radio_group_name(self, radio: Locator) -> str:
        try:
            return radio.get_attribute("name") or ""
        except PlaywrightError:
            return ""

    def _mark_radio_group_examined(self, radio: Locator) -> None:
        """Marks every OTHER member of the group. The member being queued is
        left unmarked for `_fill_questions_via_answer_engine` to mark after it
        describes — the siblings are never described, and leaving them unmarked
        would let pass 2 pick them up as separate fields."""
        group_name = self._radio_group_name(radio)
        if not group_name:
            return
        try:
            radio.evaluate(
                """(el, attr) => {
                    document.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.name)}"]`)
                        .forEach(n => { if (n !== el) n.setAttribute(attr, '1'); });
                }""",
                _AUTOMATION_EXAMINED_ATTR,
            )
        except PlaywrightError as e:
            logger.debug("Could not mark the rest of radio group %r (%s).", group_name, e)

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
            input_type = ""
            if tag_name == "input":
                input_type = (input_locator.get_attribute("type") or "text").lower()
                if input_type in self._NON_FILLABLE_INPUT_TYPES:
                    return
        except PlaywrightError as e:
            logger.debug("Could not inspect the control for question %r (%s) — skipping.", text, e)
            return

        if input_type == "radio":
            # `text` here is this member's OWN label — "Yes" — which is an
            # option, not a question. Asking the engine "Yes" would be asking
            # it nothing; three members would ask it three times.
            group_name = self._radio_group_name(input_locator)
            if group_name and group_name in self._seen_radio_groups:
                return  # already queued once, via an earlier member's label
            group_question = self._radio_group_question(input_locator)
            if not group_question:
                logger.debug(
                    "Could not recover the question for radio group %r — leaving it for a human "
                    "rather than asking the engine about the option label %r.",
                    group_name, text,
                )
                return
            if group_name:
                self._seen_radio_groups.add(group_name)
            self._mark_radio_group_examined(input_locator)
            # The group's own question, and the member locator `RadioHandler`
            # resolves the whole group from. Not marked here — see below.
            self._pending_answer_engine_questions.append((group_question, input_locator))
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
        (no-op) if no engine was injected, or nothing was collected.

        Each question carries the control's real choices when it has any (a
        `<select>`, a radio group), so the engine picks among strings the DOM
        actually offers instead of writing prose at a dropdown — see
        `automation/README.md`'s "Option-aware answering"."""
        pending = self._pending_answer_engine_questions
        self._pending_answer_engine_questions = []
        if not self.answer_engine or not pending:
            return []

        # Introspect each control ONCE, up front, for two reasons: the
        # resulting `Field` is reused verbatim for the fill below (no second
        # round-trip), and a fixed-choice control's real options go INTO the
        # question. Without that the engine is answering blind — it can only
        # write prose, which is useless for a <select>, and nothing stops it
        # proposing a choice the DOM doesn't have. `read_field_options` is
        # read-only and returns `()` for anything that isn't a fixed-choice
        # widget, which is exactly the old free-text behavior.
        # Describe FIRST, then mark. The collecting passes deliberately leave
        # these unmarked precisely so this describe still resolves — marking a
        # field whose locator is predicated on the examined attribute makes it
        # resolve to zero elements and stalls `describe_field` for the full
        # Playwright timeout. See `_stamp_candidates`.
        fields = [
            describe_field(input_locator, label=text, page=self.page, profile_attribute="answer_engine")
            for text, input_locator in pending
        ]
        for _text, input_locator in pending:
            self._mark_examined(input_locator)
        questions = [
            Question(text=text, options=read_field_options(field))
            for (text, _), field in zip(pending, fields)
        ]
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
        for (text, _input_locator), field, answer_result in zip(pending, fields, answers):
            profile_path = f"answer_engine:{answer_result.source}"
            field.profile_attribute = profile_path
            if not answer_result.answer:
                results.append(FieldFillResult(field_key=text, profile_path=profile_path, value_used=None, confidence=0.0, filled=False))
                continue

            # A generated answer the engine itself isn't confident in must not
            # be typed into a real application on the candidate's behalf —
            # leave the field empty so a human answers it. Reported as
            # unfilled, which correctly drags the run's aggregate confidence
            # down and routes it to review rather than auto-submit.
            if answer_result.confidence < ANSWER_REVIEW_CONFIDENCE_THRESHOLD:
                logger.info(
                    "%s: leaving %r for a human — generated answer confidence %.2f is below %.2f.",
                    self.name, text, answer_result.confidence, ANSWER_REVIEW_CONFIDENCE_THRESHOLD,
                )
                results.append(FieldFillResult(
                    field_key=text, profile_path=profile_path, value_used=None,
                    confidence=0.0, filled=False,
                ))
                continue

            outcome = fill_field(field, answer_result.answer, context=self._fill_context())
            results.append(FieldFillResult(
                field_key=text, profile_path=profile_path, value_used=answer_result.answer,
                confidence=answer_result.confidence if outcome.filled else 0.0, filled=outcome.filled, failure=outcome.failure,
            ))

        return results

    #: JS that recovers the question text for a control with no `<label>`.
    #: Walks up a few ancestors and returns the first one whose text (minus
    #: the form controls themselves) reads like a question — but ONLY while
    #: that ancestor contains exactly one control, which is what keeps it from
    #: climbing out of the question and scooping up the whole form's text.
    _NEARBY_QUESTION_TEXT_JS = """
    el => {
      let cur = el;
      for (let i = 0; i < 5 && cur.parentElement; i++) {
        cur = cur.parentElement;
        if (cur.querySelectorAll('input, textarea, select').length !== 1) break;
        const clone = cur.cloneNode(true);
        clone.querySelectorAll('input, textarea, select, button').forEach(n => n.remove());
        const text = (clone.textContent || '').replace(/\\s+/g, ' ').trim();
        if (text.length >= 10 && text.length <= 400) return text;
      }
      return '';
    }
    """

    def _nearby_question_text(self, input_locator) -> str:
        """The question text sitting next to an unlabeled control, or "".

        The trailing required marker is stripped — it is rendered text, so it
        arrives as part of the question ('When are you available to start
        working?✱' on the live Lever form) and would otherwise end up in the
        LLM prompt and in the answer cache's key. See `_strip_required_marker`."""
        try:
            return _strip_required_marker(input_locator.evaluate(self._NEARBY_QUESTION_TEXT_JS) or "")
        except PlaywrightError:
            return ""

    def _has_own_label_or_aria(self, input_locator) -> bool:
        """True if pass 1 (or an ARIA name) already had a shot at this control
        — those are not this pass's business."""
        try:
            return bool(input_locator.evaluate(
                "el => !!(el.getAttribute('aria-label') || el.getAttribute('aria-labelledby')"
                " || el.closest('label') || (el.id && document.querySelector(`label[for=\"${el.id}\"]`)))"
            ))
        except PlaywrightError:
            return False

    def _stamp_candidates(self, selector: str) -> list[Locator]:
        """Resolve `selector` ONCE and return locators that don't depend on it.

        `Locator.all()` cannot be used for this. It returns a list of
        `Locator`s, each of which re-resolves its own selector against the live
        DOM on every call — `repr()` shows them as `"<selector> >> nth=0"`.
        Nothing is snapshotted. When the selector excludes
        `[data-automation-examined]` (as the sweep's does) and the loop writes
        that attribute, two things break at once:

        1. The locator for the element just marked resolves to ZERO elements,
           so the next `describe_field()`/`fill_field()` call on it waits out
           Playwright's full 30s default timeout and the field is reported as
           unfillable — with the element sitting in the DOM the whole time.
        2. Every LATER index shifts, because the match set shrank. With
           candidates [e0, e1, e2], marking e0 makes `nth=1` resolve to e2, so
           e1 is silently never examined at all.

        Stamping each match with its own unique `_AUTOMATION_CANDIDATE_ATTR`
        and addressing it by that gives locators whose resolution is
        independent of the examined marker, which is what makes it safe to mark
        at the right time (after the fill) instead of before.

        Old stamps are cleared first so a second sweep of the same page (a
        multi-step form) can't inherit stale ids.
        """
        try:
            count = self.page.evaluate(
                """([selector, stampAttr]) => {
                    document.querySelectorAll('[' + stampAttr + ']')
                        .forEach(el => el.removeAttribute(stampAttr));
                    const matches = Array.from(document.querySelectorAll(selector));
                    matches.forEach((el, i) => el.setAttribute(stampAttr, String(i)));
                    return matches.length;
                }""",
                [selector, _AUTOMATION_CANDIDATE_ATTR],
            )
        except PlaywrightError as e:
            logger.debug("Could not stamp candidate fields (%s) — skipping this pass.", e)
            return []
        return [
            self.page.locator(f'[{_AUTOMATION_CANDIDATE_ATTR}="{index}"]')
            for index in range(int(count or 0))
        ]

    def _fill_questions_by_nearby_text(self) -> list[FieldFillResult]:
        """Pass 1b: screening questions whose control has NO `<label>`, no
        `aria-label`, no `aria-labelledby` and no `id` a label could point at
        — so pass 1 is structurally blind to them and pass 2 has nothing but a
        machine-generated `name` to go on.

        This is not a hypothetical shape. On a live Lever posting all four
        required screening questions render as:

            <div class="text">Are you legally authorized to work ...?</div>
            <input type="text" name="cards[<uuid>][field2]"
                   placeholder="Type your response" required>

        Ten `<label>` elements on that page, every one of them a standard
        field (resume, name, email, phone, location, company, URLs), and not
        one of them attached to a screening question. `FieldMapper` sees
        `name="cards[<uuid>][field2]"` / `placeholder="Type your response"`,
        matches nothing, and all four required fields are left empty — which
        is exactly what the form looked like.

        Recovering the text makes them answerable: a `FieldMapper` hit fills
        from the profile, and anything else joins the SAME batched answer-engine
        call as the labeled questions (this pass runs before
        `_fill_questions_via_answer_engine`, so it costs no extra LLM call).

        Confidence comes from `NEARBY_TEXT_MATCH_CONFIDENCE` (0.55), not the
        0.9 a real `<label>` earns — recovered-by-proximity text is a weaker
        signal, and 0.55 sits below the auto-submit bar on purpose."""
        results: list[FieldFillResult] = []
        selector = _MAPPABLE_FIELD_SELECTOR_TEMPLATE.format(marker=_AUTOMATION_EXAMINED_ATTR)
        candidates = self._stamp_candidates(selector)

        for input_locator in candidates:
            if self._has_own_label_or_aria(input_locator):
                continue
            # Same rule as the other two sweeps: a control on a step that isn't
            # on screen is not this page's field. Recovering its question text
            # works perfectly well on a hidden element, so without this check
            # the whole rest of a wizard's form gets answered from page 1 —
            # into controls the user cannot see, which the ATS may well
            # discard, and at the cost of an LLM call per question.
            try:
                if not input_locator.is_visible():
                    continue
            except PlaywrightError:
                continue
            question = self._nearby_question_text(input_locator)
            if not question:
                continue

            # PRECEDENCE GUARD. `FieldMapper`'s tiers are name/id (0.97) >
            # label (0.9) > placeholder (0.75) > nearby text (0.55), but this
            # pass runs BEFORE `_fill_questions_by_name_or_placeholder` so that
            # unlabeled questions can join the single batched answer-engine
            # call. Without this check that ordering silently outranks a
            # stronger signal: a bare `<input name="linkedin_url">` with no
            # label would be claimed here on proximity text, marked examined,
            # queued for the engine — and if no engine was injected, the
            # pending list is discarded and the field is left EMPTY, even
            # though pass 2 would have filled it deterministically from its
            # own `name`. Leave those untouched entirely (no marking, no
            # queueing) and let pass 2 have them.
            try:
                name_attr = input_locator.get_attribute("name") or ""
                placeholder_attr = input_locator.get_attribute("placeholder") or ""
            except PlaywrightError:
                name_attr = placeholder_attr = ""
            if FieldMapper.map_field(name=name_attr, placeholder=placeholder_attr) is not None:
                continue

            match = FieldMapper.map_field(nearby_text=question)
            if match is None:
                # Genuinely novel — hand to the answer engine, which is the
                # only thing that can answer "are you able to work in person
                # from our Mountain View office?" at all. Deliberately NOT
                # marked here: `_fill_questions_via_answer_engine` marks it
                # once it has actually described the field. Marking now would
                # be marking before a describe that happens in a later step.
                self._pending_answer_engine_questions.append((question, input_locator))
                continue

            attribute, confidence = match
            value = self._resolve_profile_value(attribute)
            if value in (None, "", []):
                # Nothing on file for a field we DID recognize — let the answer
                # engine try rather than leaving a required question blank.
                self._pending_answer_engine_questions.append((question, input_locator))
                continue

            field = describe_field(input_locator, label=question, page=self.page, profile_attribute=attribute)
            outcome = fill_field(field, value, context=self._fill_context())
            self._mark_examined(input_locator)  # after the fill, never before
            results.append(FieldFillResult(
                field_key=question, profile_path=attribute, value_used=value,
                confidence=confidence if outcome.filled else 0.0, filled=outcome.filled, failure=outcome.failure,
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
        # `Locator.all()` must NOT be used here. It does not snapshot anything:
        # it returns a list of `Locator`s, each of which re-resolves its own
        # selector against the live DOM on every call (`repr()` shows them as
        # `"<selector> >> nth=0"`). Since this selector excludes
        # `[data-automation-examined]` and this loop writes that attribute,
        # `.all()` meant (a) the locator for a just-marked element resolved to
        # zero, stalling the next `describe_field()` for Playwright's full 30s
        # timeout and reporting a present, fillable field as unfillable, and
        # (b) every later index shifted as the match set shrank, so with
        # candidates [e0, e1, e2] marking e0 made `nth=1` resolve to e2 and e1
        # was silently never examined. `_stamp_candidates()` gives each match
        # its own stable id instead, so marking can't perturb the iteration.
        candidates = self._stamp_candidates(selector)

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

            if not is_visible:
                # Left completely alone — not scored, and deliberately NOT
                # marked examined, for the reasons spelled out in the matching
                # branch of `_fill_questions_by_label`. This branch used to mark
                # it, which on a multi-page form meant a field belonging to a
                # later step was retired on page 1 and could never be filled
                # once the form actually reached it.
                continue

            field = describe_field(input_locator, label=field_key, page=self.page, profile_attribute=attribute)
            outcome = fill_field(field, value, context=self._fill_context())
            # AFTER the fill, never before — see `_stamp_candidates`. Still
            # unconditional on `outcome.filled`: a field this pass attempted
            # and failed must not be retried (and re-counted) by a later pass.
            self._mark_examined(input_locator)
            results.append(FieldFillResult(
                field_key=field_key, profile_path=attribute, value_used=value,
                confidence=confidence if outcome.filled else 0.0, filled=outcome.filled, failure=outcome.failure,
            ))

        return results

    # ------------------------------------------------------------------
    # Checkbox groups ("select all that apply") and the marketing opt-in
    # ------------------------------------------------------------------

    #: Recovers, for ONE checkbox: its own option label, the question its GROUP
    #: asks, and how many members that group has.
    #:
    #: A checkbox group is invisible to every other pass by construction, which
    #: is why "Pronouns" and "I identify my ethnicity as" came back untouched on
    #: a live Lever form where everything else filled: pass 1 sees each member's
    #: own `<label>` ("He/him"), which is an OPTION rather than a question and
    #: matches no `FieldMapper` synonym; `_collect_for_answer_engine` then drops
    #: it because `_NON_FILLABLE_INPUT_TYPES` excludes checkboxes; pass 3's
    #: selector excludes them too; and `_fill_consent_checkboxes` only ever
    #: looks at required legal-consent text.
    #:
    #: Same ancestor-walk shape as `_RADIO_GROUP_QUESTION_JS`, with two
    #: deliberate differences:
    #:
    #: - Membership is "any checkbox", not "same `name`". Real forms build these
    #:   groups both ways — one shared `name="...[]"` (Rails/PHP multi-value
    #:   convention) or a distinct `name` per box — and the container is the
    #:   thing that actually delimits the question either way.
    #: - It returns the FIRST (innermost) ancestor that both holds 2+ checkboxes
    #:   and has prose of its own, then stops. Climbing to the outermost
    #:   all-checkbox ancestor would happily merge two adjacent groups (an EEO
    #:   section holding both a pronoun group and an ethnicity group) into one
    #:   question belonging to neither.
    _CHECKBOX_GROUP_JS = """
    el => {
      const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
      const ownLabel = (() => {
        const wrapping = el.closest('label');
        if (wrapping) return wrapping.textContent;
        const forAttr = el.id;
        if (forAttr) {
          try {
            const l = document.querySelector(`label[for="${CSS.escape(forAttr)}"]`);
            if (l) return l.textContent;
          } catch (e) {}
        }
        return el.getAttribute('aria-label') || el.value || '';
      })();
      const isMember = n => n.tagName === 'INPUT' && n.type === 'checkbox';
      let cur = el;
      for (let i = 0; i < 6 && cur.parentElement; i++) {
        cur = cur.parentElement;
        const controls = Array.from(cur.querySelectorAll('input, textarea, select'));
        if (!controls.length || !controls.every(isMember)) break;
        if (controls.length < 2) continue;
        const clone = cur.cloneNode(true);
        clone.querySelectorAll('label').forEach(l => {
          const wraps = !!l.querySelector('input[type="checkbox"]');
          let pointsAt = null;
          const forAttr = l.getAttribute('for');
          if (forAttr) {
            try { pointsAt = clone.querySelector('#' + CSS.escape(forAttr)); } catch (e) { pointsAt = null; }
          }
          if (wraps || (pointsAt && isMember(pointsAt))) l.remove();
        });
        clone.querySelectorAll('input, textarea, select, button').forEach(n => n.remove());
        const text = clean(clone.textContent);
        if (text.length >= 3 && text.length <= 400) {
          return {ownLabel: clean(ownLabel), question: text, size: controls.length};
        }
      }
      return {ownLabel: clean(ownLabel), question: '', size: 0};
    }
    """

    def _fill_checkbox_groups(self) -> list[FieldFillResult]:
        """Ticks the members of a "select all that apply" checkbox group that
        the candidate's own STORED answers name — pronouns, ethnicity.

        Answered exclusively through `ApplicationAnswerEngine.stored_choices()`,
        which never calls the LLM for any question. That is stricter than the
        answer-engine pass above, on purpose: a wrongly-ticked box is the least
        visible mistake this system can make (on a review screen it looks
        identical to a deliberately-ticked one), and the two groups this exists
        for are pronouns and ethnicity, where a guess is worse than a blank in
        every direction.

        No engine injected means no pass at all — the engine is what holds the
        DB session the demographics row is read through. That matches how every
        other Phase 6+ pass degrades.

        Members of a group nothing could answer are deliberately left UNMARKED
        rather than stamped as examined: two adjacent required consent
        checkboxes are technically a two-member "group" with a section heading
        for a question, and marking them here would make
        `_fill_consent_checkboxes` skip them and silently block submission."""
        if self.answer_engine is None:
            return []

        checkboxes = self._stamp_candidates(
            f"input[type='checkbox']:not([{_AUTOMATION_EXAMINED_ATTR}])"
        )
        if not checkboxes:
            return []

        # question -> [(member_label, locator)], in DOM order. Grouping on the
        # recovered question text is what stitches N members back into the one
        # question they collectively ask.
        groups: dict[str, list[tuple[str, Locator]]] = {}
        for checkbox in checkboxes:
            try:
                if not checkbox.is_visible():
                    continue
                info = checkbox.evaluate(self._CHECKBOX_GROUP_JS)
            except PlaywrightError as e:
                logger.debug("Could not inspect a checkbox for group detection (%s) — skipping it.", e)
                continue
            if not isinstance(info, dict):
                continue
            question = _strip_required_marker(str(info.get("question") or ""))
            member_label = _strip_required_marker(str(info.get("ownLabel") or ""))
            if not question or not member_label or int(info.get("size") or 0) < 2:
                continue
            groups.setdefault(question, []).append((member_label, checkbox))

        results: list[FieldFillResult] = []
        for question, members in groups.items():
            if len(members) < 2:
                continue  # only one member actually resolved — not a group we can read
            options = list(dict.fromkeys(label for label, _ in members))
            try:
                chosen = self.answer_engine.stored_choices(Question(text=question, options=options))
            except Exception as e:  # noqa: BLE001 - a broken engine call must never abort the sweep
                logger.debug("stored_choices failed for checkbox group %r (%s) — leaving it blank.", question, e)
                continue
            if not chosen:
                logger.debug(
                    "Nothing stored answers the checkbox group %r (%d options) — leaving it for a human.",
                    question, len(options),
                )
                continue

            for member_label, locator in members:
                if member_label not in chosen:
                    continue
                self._mark_examined(locator)
                field = describe_field(
                    locator, label=f"{question} — {member_label}",
                    page=self.page, profile_attribute="checkbox_group",
                )
                outcome = fill_field(field, True, context=self._fill_context())
                results.append(FieldFillResult(
                    field_key=f"{question} — {member_label}", profile_path="checkbox_group",
                    value_used=member_label,
                    confidence=DETERMINISTIC_CONFIDENCE if outcome.filled else 0.0,
                    filled=outcome.filled, failure=outcome.failure,
                ))

        return results

    def _fill_opt_in_checkboxes(self) -> list[FieldFillResult]:
        """Ticks a "Yes, <company> can contact me about future roles" marketing
        opt-in — and ONLY when `profile.marketing_opt_in` is exactly `True`.

        `None` (never asked) and `False` are both left as the page rendered
        them, unticked, and this pass then does nothing at all. Consent is the
        one thing in this codebase that is never inferred from silence, so the
        tri-state is the point rather than an accident: the alternative reading
        of `None` — "probably fine" — is how an automation subscribes someone to
        a talent-pool mailing list they never agreed to.

        Reuses `field_mapper.looks_like_opt_in_label`, the same predicate that
        stops opt-in prose from resolving to a profile VALUE (see
        `automation/tests/test_checkbox_intent.py`) — one definition of "this
        label is a marketing opt-in", used by both the code that refuses to
        touch it and the code that acts on an explicit yes."""
        if getattr(self.profile, "marketing_opt_in", None) is not True:
            return []

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
            if not text or not looks_like_opt_in_label(text):
                continue

            try:
                input_locator = self._input_for_label(label)
            except Exception as e:  # noqa: BLE001 - one broken selector must never abort the sweep
                logger.debug("Could not resolve the control for opt-in label %r (%s) — skipping.", text, e)
                continue
            if input_locator is None:
                continue

            try:
                already_examined = input_locator.get_attribute(_AUTOMATION_EXAMINED_ATTR)
                tag_name = input_locator.evaluate("el => el.tagName.toLowerCase()")
                input_type = (input_locator.get_attribute("type") or "").lower() if tag_name == "input" else ""
                is_visible = input_locator.is_visible()
            except PlaywrightError as e:
                logger.debug("Could not inspect opt-in checkbox candidate %r (%s) — skipping.", text, e)
                continue
            if already_examined or input_type != "checkbox" or not is_visible:
                continue

            self._mark_examined(input_locator)
            field = describe_field(input_locator, label=text, page=self.page, profile_attribute="marketing_opt_in")
            outcome = fill_field(field, True, context=self._fill_context())
            results.append(FieldFillResult(
                field_key=text, profile_path="marketing_opt_in", value_used=True,
                confidence=DETERMINISTIC_CONFIDENCE if outcome.filled else 0.0,
                filled=outcome.filled, failure=outcome.failure,
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
