"""
ApplicationFlowManager — Phase 4 (see ARCHITECTURE.md).

Drives a single application end-to-end through an `ATSAdapter`: launches the
browser (`BrowserManager`, Phase 2), checks for a CAPTCHA before ever filling
anything (§9 — never bypass one), fills personal info/resume/questions,
detects and clicks "Next"/"Continue" across multi-step forms (capped at
`MAX_STEPS` so a broken selector can't loop forever), applies the
auto-submit/needs-review/copilot-review decision from ARCHITECTURE.md, and
captures a screenshot/trace/error-log for every run (§14).

Returns an `automation.interfaces.ApplicationRunResult` — `app/` decides how
to persist it (Phase 4's `applications`/`automation_runs` tables don't exist
yet; see `automation/interfaces.py`). This class never writes to the
database itself.

**Phase 6.** An optional `answer_engine` (see
`automation/forms/answer_engine.py::ApplicationAnswerEngine`) is threaded
straight through to whatever adapter this run constructs — this class never
calls it directly, it's purely a pass-through. `None` (the default)
preserves pre-Phase-6 behavior exactly: a screening question `FieldMapper`
can't resolve is simply left unfilled.

**Manual review handoff.** The whole point of `NEEDS_REVIEW`/`COPILOT_REVIEW`
is that a human looks at (and, for copilot mode, submits) the actual page —
but the browser used to auto-close the instant a run finished, regardless of
outcome, so there was never anything left to look at. Now: if the browser
was launched visibly (`headless=False` — see `__init__`'s `headless` param)
AND the run landed on one of `REVIEW_STATUSES`, the browser is deliberately
left open instead of closed (see `should_keep_browser_open`). Callers that
want this (`app/api/applications.py`, whenever `autopilot_enabled=False`)
must pass `headless=False` explicitly; the default stays whatever
`AUTOMATION_HEADLESS` says, so every existing (headless) test and caller is
unaffected. There's no auto-cleanup for a left-open browser — each run that
leaves one open spawns its own separate Chromium window that stays up until
someone closes it by hand.
"""

from __future__ import annotations

import logging

from playwright.sync_api import BrowserContext, Locator, Page

from automation.ats.base import ATSAdapter, FieldFillResult
from automation.browser.browser_manager import BrowserAutomationError, BrowserManager
from automation.browser.selectors import (
    find_human_gate,
    find_next_button,
    find_unfilled_required_fields,
    find_validation_errors,
    page_has_captcha,
    wait_for_form_ready,
    wait_for_submission_confirmation,
)
from automation.forms.answer_engine import ApplicationAnswerEngine
from automation.forms.vision_fallback import VisionFormAnswerer
from automation.interfaces import ApplicationRunResult, CandidateProfile, ProfileDocument

logger = logging.getLogger(__name__)

# ATS platforms that are public (no login) and therefore eligible for
# autopilot at all — mirrors the decision table in ARCHITECTURE.md exactly.
PUBLIC_ATS_PLATFORMS = frozenset({"greenhouse", "lever", "smartrecruiters", "ashby"})

AUTO_SUBMIT_CONFIDENCE_THRESHOLD = 0.85
NEEDS_REVIEW_CONFIDENCE_THRESHOLD = 0.6

# Outcomes where a human is expected to look at — and possibly act on — the
# actual page: solve a CAPTCHA, double-check a low-confidence fill, or click
# submit themselves in copilot mode.
REVIEW_STATUSES = frozenset({"manual_required", "needs_review", "copilot_review"})


def should_keep_browser_open(headless: bool, status: str) -> bool:
    """Whether `run()` should skip auto-closing the browser once it's done.

    Only makes sense when the browser is actually visible — leaving a
    HEADLESS browser process running just leaks it with nothing for anyone
    to look at, so this is always `False` when `headless` is `True`,
    regardless of status. That's deliberate: it's what keeps every existing
    headless run/test behaving exactly as it did before this existed."""
    return (not headless) and status in REVIEW_STATUSES


# Browsers deliberately left open for manual review, keyed by application_id.
# Holding a strong reference here is REQUIRED, not just a nicety: `run()`
# executes inside a FastAPI `BackgroundTasks` call, and once it returns,
# nothing else in the process references this run's `ApplicationFlowManager`,
# its `BrowserManager`, or the underlying Playwright driver connection at
# all — every local variable goes out of scope. Without something keeping a
# real Python reference alive, that connection is eligible for garbage
# collection the instant the background task function returns, which is
# exactly the kind of silent, hard-to-diagnose way a "kept open" browser
# could end up closing (or its controlling connection dying) anyway. This
# dict is that reference. See `close_review_session()` to explicitly close
# one later (e.g. from a future "I'm done reviewing" API action).
_OPEN_REVIEW_SESSIONS: dict[str, BrowserManager] = {}


def close_review_session(application_id: str) -> bool:
    """Closes and forgets a browser that was left open for manual review.
    Returns `False` if there wasn't one (already closed, or this
    application never resulted in one being left open)."""
    browser_manager = _OPEN_REVIEW_SESSIONS.pop(application_id, None)
    if browser_manager is None:
        return False
    try:
        browser_manager.close()
    except Exception:  # noqa: BLE001 - best-effort cleanup, never raise from here
        logger.debug("application %s: error closing a manually-reviewed browser (ignored).", application_id)
    return True


def list_open_review_sessions() -> list[str]:
    """application_ids with a browser currently left open for review —
    useful for a debug endpoint or just introspecting from a shell."""
    return list(_OPEN_REVIEW_SESSIONS.keys())


def decide_action(confidence: float, ats_platform: str, autopilot_enabled: bool) -> str:
    """The exact decision table from ARCHITECTURE.md, as a standalone,
    independently testable function:

    - `AUTO_SUBMIT` only when the user opted in AND the platform is public
      (no login) AND confidence clears the high bar.
    - `NEEDS_REVIEW` when confidence is too low to trust at all.
    - `COPILOT_REVIEW` otherwise — form is filled, a human clicks submit.
    """
    high_confidence = confidence >= AUTO_SUBMIT_CONFIDENCE_THRESHOLD
    is_public_ats = ats_platform in PUBLIC_ATS_PLATFORMS
    if autopilot_enabled and is_public_ats and high_confidence:
        return "AUTO_SUBMIT"
    if confidence < NEEDS_REVIEW_CONFIDENCE_THRESHOLD:
        return "NEEDS_REVIEW"
    return "COPILOT_REVIEW"


class ApplicationFlowManager:
    """Orchestrates one application run. One instance = one run."""

    #: Safety cap on multi-step "Next" loops — a broken/looping selector
    #: fails loudly after this many steps rather than hanging forever.
    MAX_STEPS = 10

    def __init__(
        self,
        *,
        application_id: str,
        user_id: str,
        job_url: str,
        ats_platform: str,
        adapter_cls: type[ATSAdapter],
        profile: CandidateProfile,
        resume_document: ProfileDocument,
        autopilot_enabled: bool = False,
        browser_manager: BrowserManager | None = None,
        headless: bool | None = None,
        answer_engine: ApplicationAnswerEngine | None = None,
        vision_answerer: VisionFormAnswerer | None = None,
    ) -> None:
        self.application_id = application_id
        self.user_id = user_id
        self.job_url = job_url
        self.ats_platform = ats_platform
        self.adapter_cls = adapter_cls
        self.profile = profile
        self.resume_document = resume_document
        self.autopilot_enabled = autopilot_enabled
        # Phase 6: optional. `None` (the default) means every labeled
        # question FieldMapper can't match is simply left unfilled — exactly
        # Phase 5 behavior. Passed straight through to the adapter; this
        # class never calls it directly.
        self.answer_engine = answer_engine
        # The vision fallback (`automation/forms/vision_fallback.py`), run once
        # at the very end over whatever required fields are STILL empty. Also
        # optional, and for the same reason: `None` keeps the run's behavior
        # exactly as it was before this existed — the leftovers stay empty and
        # the run goes to a human. Unlike `answer_engine` this one IS called
        # from here rather than passed to the adapter, because it can only run
        # after every fill pass on every step has finished.
        self.vision_answerer = vision_answerer
        # `headless` only applies when this instance builds its own
        # BrowserManager — an injected one (tests; a caller managing its own
        # lifecycle) is used exactly as given. Left `None` (the default),
        # BrowserManager falls back to the `AUTOMATION_HEADLESS` config, same
        # as always.
        self.browser_manager = browser_manager or BrowserManager(
            user_id=user_id, ats_platform=ats_platform, headless=headless,
        )
        self._steps_completed: list[str] = []

    @property
    def steps_completed(self) -> list[str]:
        return list(self._steps_completed)

    def checkpoint(self, step_name: str) -> None:
        """Records progress. Until the Phase 4 `applications`/`automation_runs`
        tables + a repository exist (see `automation/interfaces.py`), this is
        in-memory + logged only — `app/` persists the final
        `ApplicationRunResult` by hand for now. Once that repository exists,
        this becomes a real per-step DB write, same pattern as everything
        else in `automation/interfaces.py`."""
        self._steps_completed.append(step_name)
        logger.info("application %s: step '%s' complete", self.application_id, step_name)

    def detect_next_step(self, page: Page) -> Locator | None:
        """The page's Next/Continue control, or `None` if this is the final
        step of the form (see `automation.browser.selectors.find_next_button`)."""
        return find_next_button(page)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> ApplicationRunResult:
        try:
            context = self.browser_manager.launch_context()
            page = context.new_page()
            self.browser_manager.start_trace(context)
        except Exception as e:  # noqa: BLE001 - launching/preparing the browser itself failed
            logger.exception("application %s: could not launch or prepare the browser", self.application_id)
            try:
                self.browser_manager.close()  # best-effort — don't leak a half-launched browser
            except Exception:  # noqa: BLE001
                pass
            return ApplicationRunResult(
                application_id=self.application_id,
                status="failed",
                ats_platform=self.ats_platform,
                confidence=0.0,
                error_log=self._safe_write_error_log(repr(e)),
            )

        try:
            result = self._run_on_page(page)
        except Exception as e:  # noqa: BLE001 - any failure becomes a reportable result, never a crash
            logger.exception("application %s: run failed", self.application_id)
            result = self._build_failure_result(page, e)
        result.trace_path = self._safe_stop_trace(context)

        self._finish_browser_session(context, result.status)
        return result

    def _finish_browser_session(self, context: BrowserContext, status: str) -> None:
        """Saves the session either way (so a login carries over to the next
        run regardless of outcome), then closes the browser — UNLESS this
        run needs a human to look at it AND the browser is actually visible,
        in which case it's left open on purpose. See the module docstring's
        "Manual review handoff" note."""
        try:
            self.browser_manager.save_session(context)
        except Exception:  # noqa: BLE001 - a failed session save must never block cleanup or crash the run
            logger.debug("application %s: could not save the browser session", self.application_id)

        if should_keep_browser_open(self.browser_manager.headless, status):
            logger.info(
                "application %s: keeping browser open for copilot review (status=%s) — not closing it.",
                self.application_id, status,
            )
            # See _OPEN_REVIEW_SESSIONS's docstring: this reference is load-
            # bearing, not decorative — without it, nothing else in the
            # process keeps this BrowserManager (or its Playwright driver
            # connection) alive once `run()` returns.
            _OPEN_REVIEW_SESSIONS[self.application_id] = self.browser_manager
            return

        logger.info("application %s: closing browser (status=%s).", self.application_id, status)
        self.browser_manager.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_on_page(self, page: Page) -> ApplicationRunResult:
        self.browser_manager.run_with_retries(lambda: page.goto(self.job_url, wait_until="domcontentloaded"))
        self.checkpoint("navigated")
        # `domcontentloaded` above is "the HTML parsed", which on a React/Remix
        # ATS is before the form is hydrated — and filling a form mid-hydration
        # can be silently undone by the framework as it takes over the DOM. See
        # `wait_for_form_ready`'s comment block for the live Greenhouse run
        # where exactly that dropped an already-verified résumé.
        wait_for_form_ready(page)
        logger.info(
            "application %s: reached the job application page (%s, headless=%s)",
            self.application_id, self.job_url, self.browser_manager.headless,
        )

        if page_has_captcha(page):
            self.checkpoint("captcha_detected")
            logger.info("application %s: CAPTCHA detected — stopping before filling anything.", self.application_id)
            return ApplicationRunResult(
                application_id=self.application_id,
                status="manual_required",
                ats_platform=self.ats_platform,
                confidence=0.0,
                screenshot_paths=self._safe_screenshot(page),
                error_log=self._safe_write_error_log("CAPTCHA present — a human must solve it; automation never will."),
            )

        # Everything else that only a human may transact with: an account
        # wall, OTP/MFA, email/SMS verification, identity documents, payment.
        # Same hard stop as CAPTCHA, and for the same reason — these are
        # never worked around, only handed over.
        human_gate = find_human_gate(page)
        if human_gate is not None:
            reason = f"Human intervention required before this form can be filled: {human_gate}."
            self.checkpoint("human_gate_detected")
            logger.info("application %s: %s", self.application_id, reason)
            return ApplicationRunResult(
                application_id=self.application_id,
                status="manual_required",
                ats_platform=self.ats_platform,
                confidence=0.0,
                screenshot_paths=self._safe_screenshot(page),
                error_log=self._safe_write_error_log(reason),
            )

        adapter = self.adapter_cls(
            page=page, profile=self.profile, resume_document=self.resume_document, answer_engine=self.answer_engine,
        )

        all_results: list[FieldFillResult] = []
        resume_result = self._upload_resume(adapter)
        all_results.append(resume_result)

        for step_index in range(self.MAX_STEPS):
            personal_info_results = adapter.fill_personal_information()
            question_results = adapter.answer_questions()
            all_results.extend(personal_info_results)
            all_results.extend(question_results)
            for r in personal_info_results + question_results:
                # Field name + outcome only — not `r.value_used`, which can
                # be PII (phone, address, answers to personal questions).
                logger.info(
                    "application %s: step %d — %s '%s'",
                    self.application_id, step_index, "filled" if r.filled else "NOT filled",
                    r.profile_path or r.field_key,
                )
            self.checkpoint(f"step_{step_index}_filled")

            next_control = self.detect_next_step(page)
            if next_control is None:
                break

            try:
                self.browser_manager.run_with_retries(lambda: next_control.click())
            except BrowserAutomationError:
                logger.warning(
                    "application %s: could not click Next at step %d — stopping here.",
                    self.application_id, step_index,
                )
                break
            self.checkpoint(f"step_{step_index}_advanced")
        else:
            logger.warning(
                "application %s: hit the %d-step safety cap — treating the current page as final.",
                self.application_id, self.MAX_STEPS,
            )

        # Last chance for the fields nothing above could fill: read them off
        # screenshots. Runs BEFORE the confidence/missing-required checks so
        # anything it fills counts, and after every step's fill passes because
        # a field is only genuinely "left over" once they've all had a turn.
        vision_confirmed_filled = self._run_vision_pass(adapter, all_results)

        # Re-verify the résumé LAST, for the reason spelled out in
        # `ATSAdapter.ensure_resume_attached`: a client-rendered form can drop
        # an already-verified upload while it hydrates, so the only
        # verification worth acting on is one taken after the form has settled
        # and everything else has been filled.
        self._recheck_resume(adapter, resume_result)

        confidence = self._aggregate_confidence(all_results)
        logger.info(
            "application %s: reached the final step — %d/%d tracked fields filled (confidence=%.4f)",
            self.application_id, sum(1 for r in all_results if r.filled), len(all_results), confidence,
        )

        # A required field with no value anywhere in the candidate's profile
        # isn't "low confidence" — it's "automation cannot proceed without a
        # human," regardless of how well everything else filled. Checked
        # (and reported by name) before ever considering AUTO_SUBMIT.
        missing_required = self._still_missing_required(page, vision_confirmed_filled)
        if missing_required:
            reason = f"Required field(s) could not be filled — no value available in the candidate's profile: {', '.join(missing_required)}."
            logger.info("application %s: %s", self.application_id, reason)
            self.checkpoint("manual_required_missing_fields")
            return ApplicationRunResult(
                application_id=self.application_id,
                status="manual_required",
                ats_platform=self.ats_platform,
                confidence=confidence,
                screenshot_paths=self._safe_screenshot(page),
                error_log=self._safe_write_error_log(reason),
            )

        # Both specs gate auto-submit on "zero visible validation errors".
        # Unlike a missing required field, these are the ATS telling us
        # something we DID fill is unacceptable (bad phone format, over a
        # character limit, rejected file type) — never something to submit
        # over, and not something automation can resolve on its own, since
        # the value came from the candidate's own profile.
        validation_errors = find_validation_errors(page)
        if validation_errors:
            reason = f"Validation errors remain on the form: {'; '.join(validation_errors)}."
            logger.info("application %s: %s", self.application_id, reason)
            self.checkpoint("needs_review_validation_errors")
            return ApplicationRunResult(
                application_id=self.application_id,
                status="needs_review",
                ats_platform=self.ats_platform,
                confidence=confidence,
                screenshot_paths=self._safe_screenshot(page),
                error_log=self._safe_write_error_log(reason),
            )

        action = decide_action(confidence, self.ats_platform, self.autopilot_enabled)
        self.checkpoint(f"decision_{action.lower()}")
        logger.info(
            "application %s: decision=%s (confidence=%.4f, autopilot_enabled=%s, ats_platform=%s)",
            self.application_id, action, confidence, self.autopilot_enabled, self.ats_platform,
        )

        error_log: str | None = None

        if action == "AUTO_SUBMIT":
            logger.info("application %s: clicking submit (autopilot).", self.application_id)
            if not adapter.submit_application():
                status = "failed"
            else:
                # A landed click is NOT an accepted application — see
                # `find_submission_confirmation`. Only positive confirmation
                # is allowed to produce "applied".
                self.checkpoint("submit_clicked")
                confirmation = wait_for_submission_confirmation(page)
                if confirmation:
                    status = "applied"
                    self.checkpoint("submission_confirmed")
                    logger.info(
                        "application %s: submission CONFIRMED via %s.", self.application_id, confirmation,
                    )
                else:
                    # Deliberately not "applied" (we cannot claim the
                    # candidate applied) and not "failed" (the submission may
                    # well have gone through — we just can't prove it). A
                    # human has to confirm on the ATS side, and must do that
                    # BEFORE any retry, since a retry of a submission that
                    # actually succeeded would double-apply.
                    status = "needs_review"
                    rejection = find_validation_errors(page)
                    reason = (
                        "Submit was clicked but no confirmation could be detected "
                        "(no confirmation page, success message, or application reference). "
                        "The application may or may not have been received — verify on the ATS "
                        "before retrying, since retrying a submission that did succeed would double-apply."
                    )
                    if rejection:
                        reason += f" The page reported: {'; '.join(rejection)}."
                    logger.warning("application %s: %s", self.application_id, reason)
                    self.checkpoint("submission_unconfirmed")
                    error_log = self._safe_write_error_log(reason)
        elif action == "NEEDS_REVIEW":
            status = "needs_review"
        else:  # COPILOT_REVIEW — form is filled; a human clicks submit themselves
            status = "copilot_review"
            logger.info("application %s: stopping before submission — copilot review.", self.application_id)

        # A snapshot of exactly what's being handed off — worth keeping in
        # `automation_runs` even for copilot_review, since the browser might
        # get left open (see `should_keep_browser_open`) and later closed by
        # the user without ever submitting; this is the record of what it
        # looked like at handoff time either way.
        screenshot_paths = self._safe_screenshot(page) if status in ("failed", "needs_review", "copilot_review") else []

        return ApplicationRunResult(
            application_id=self.application_id,
            status=status,
            ats_platform=self.ats_platform,
            confidence=confidence,
            screenshot_paths=screenshot_paths,
            error_log=error_log,
        )

    def _run_vision_pass(self, adapter: ATSAdapter, all_results: list[FieldFillResult]) -> set[str]:
        """Runs the vision fallback over whatever required fields are still
        empty, merging its outcomes into `all_results` IN PLACE. Returns the
        names of fields it confirmed were already answered on the form (see
        `VisionPassOutcome`), for `_still_missing_required`.

        Merging rather than appending matters for the confidence score: a field
        an earlier pass already reported as unfilled must not be counted twice
        — once as a failure and once as a success — which would leave a
        perfectly filled form scoring 50% on that field. Results are matched by
        `field_key`, which is the question text on both sides.

        A no-op (empty set, `all_results` untouched) when no answerer was
        injected.

        Runs once, on the FINAL step. On a multi-step form the earlier steps
        are behind a Next click by the time this runs, which is the same
        constraint the missing-required check has always had: whatever a step
        left empty went with it when the form advanced."""
        if self.vision_answerer is None:
            return set()

        try:
            outcome = adapter.fill_unfilled_fields_with_vision(
                self.vision_answerer, debug_dir=self.browser_manager.run_directory(self.application_id),
            )
        except Exception:  # noqa: BLE001 - a best-effort last pass must never fail the run
            logger.exception("application %s: the vision fallback pass failed — continuing without it.", self.application_id)
            return set()

        for result in outcome.results:
            existing = next((r for r in all_results if r.field_key == result.field_key), None)
            if existing is None:
                all_results.append(result)
                continue
            existing.profile_path = result.profile_path
            existing.value_used = result.value_used
            existing.confidence = result.confidence
            existing.filled = result.filled
            existing.failure = result.failure

        filled_count = sum(1 for r in outcome.results if r.filled)
        if outcome.results or outcome.confirmed_already_filled:
            self.checkpoint("vision_fallback_pass")
            logger.info(
                "application %s: vision fallback filled %d/%d field(s); %d already answered on the form.",
                self.application_id, filled_count, len(outcome.results),
                len(outcome.confirmed_already_filled),
            )
        return set(outcome.confirmed_already_filled)

    def _still_missing_required(self, page: Page, vision_confirmed_filled: set[str]) -> list[str]:
        """The required fields that are still empty — excluding any the vision
        pass read as already answered.

        That exclusion is narrow and evidence-based, not a loosening of the
        check: `find_unfilled_required_fields` reads a control's OWN value, so
        a widget whose visible selection lives elsewhere (react-select,
        country pickers — the standard shape on a modern Greenhouse form)
        reports empty while the page plainly shows a value, and every such form
        would otherwise be permanently `manual_required` over answers that are
        demonstrably there. Each exclusion is logged by name so a run's log
        always shows what was waived and on what basis."""
        missing = find_unfilled_required_fields(page)
        if not vision_confirmed_filled:
            return missing

        waived = [name for name in missing if name in vision_confirmed_filled]
        if waived:
            logger.info(
                "application %s: %d required field(s) reported empty but shown as already answered in "
                "their screenshots — not treating these as missing: %s",
                self.application_id, len(waived), ", ".join(waived),
            )
        return [name for name in missing if name not in vision_confirmed_filled]

    def _recheck_resume(self, adapter: ATSAdapter, resume_result: FieldFillResult) -> None:
        """Updates `resume_result` in place from a final, post-fill check of
        whether the résumé is actually still attached — see
        `ATSAdapter.ensure_resume_attached`. `None` from there means this page
        has no upload field to check, in which case the original upload
        result stands untouched."""
        try:
            attached = adapter.ensure_resume_attached()
        except Exception:  # noqa: BLE001 - never let the re-check itself fail a run
            logger.exception("application %s: the résumé re-check failed — leaving the original result.", self.application_id)
            return

        if attached is None or attached == resume_result.filled:
            return

        logger.warning(
            "application %s: résumé attachment changed after filling — was %s, now %s.",
            self.application_id, "attached" if resume_result.filled else "not attached",
            "attached" if attached else "NOT attached",
        )
        resume_result.filled = attached
        resume_result.confidence = 1.0 if attached else 0.0
        self.checkpoint("resume_reattached" if attached else "resume_lost_after_upload")

    def _upload_resume(self, adapter: ATSAdapter) -> FieldFillResult:
        uploaded = adapter.upload_resume()
        logger.info(
            "application %s: resume upload %s", self.application_id, "succeeded" if uploaded else "FAILED",
        )
        self.checkpoint("resume_uploaded" if uploaded else "resume_upload_failed")
        return FieldFillResult(
            field_key="resume_upload",
            profile_path="resume_document",
            value_used=getattr(self.resume_document, "original_filename", None),
            confidence=1.0 if uploaded else 0.0,
            filled=uploaded,
        )

    @staticmethod
    def _aggregate_confidence(results: list[FieldFillResult]) -> float:
        """Fraction of tracked fields (personal info + resume + matched
        questions) that were actually filled. Simple and explainable for
        Phase 4; Phase 6 can weight this by field importance or blend in the
        LLM's own confidence for subjective answers."""
        if not results:
            return 0.0
        filled = sum(1 for r in results if r.filled)
        return round(filled / len(results), 4)

    def _safe_screenshot(self, page: Page) -> list[str]:
        try:
            return [self.browser_manager.screenshot_on_failure(page, self.application_id)]
        except Exception:  # noqa: BLE001 - a failed screenshot must never mask the real result
            logger.debug("application %s: could not capture a screenshot", self.application_id)
            return []

    def _safe_stop_trace(self, context) -> str | None:
        try:
            return self.browser_manager.stop_trace(self.application_id, context)
        except Exception:  # noqa: BLE001
            logger.debug("application %s: could not save the Playwright trace", self.application_id)
            return None

    def _safe_write_error_log(self, message: str) -> str | None:
        try:
            return self.browser_manager.write_error_log(self.application_id, message)
        except Exception:  # noqa: BLE001
            return None

    def _build_failure_result(self, page: Page, error: Exception) -> ApplicationRunResult:
        return ApplicationRunResult(
            application_id=self.application_id,
            status="failed",
            ats_platform=self.ats_platform,
            confidence=0.0,
            screenshot_paths=self._safe_screenshot(page),
            error_log=self._safe_write_error_log(repr(error)),
        )
