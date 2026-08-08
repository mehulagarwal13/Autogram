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

**Long, multi-page applications.** The step loop used to be "fill the page,
click Next, immediately fill again", which works for a one-page Greenhouse form
and fails on every 3-to-5-page Workday-style application for three separate
reasons: it never waited for the next page to render (so page 2 was filled
against page 1's DOM), it never checked that the click actually advanced
anything (so a validation-blocked click looked identical to a successful one,
and the loop refilled page 1 until its cap ran out and then treated page 1 as
the final step), and the résumé was uploaded exactly once, before the loop —
useless on a form whose upload field lives on page 2.

The loop is now built around a page cycle instead: settle and dismiss overlays,
upload the résumé if THIS page asks for one, fill in rounds until the page stops
revealing conditional follow-up fields, run the vision fallback over whatever is
still empty HERE (not only on the last page), then ask the adapter whether this
is the final page and, if not, navigate with proof — see
`automation/applications/page_navigator.py`, which compares a structural
signature of the page before and after the click. Navigation that can't be
proven stops the run and hands over with the form's own reason attached, rather
than silently continuing against a page the application never left.

Nothing in the loop knows which ATS it is driving: which control advances the
form, whether this is the last page, and what this page is called are all
`ATSAdapter` methods with working generic defaults (see that class's
"Multi-page navigation" section).

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
from dataclasses import dataclass, field as _dc_field

from playwright.sync_api import BrowserContext, Locator, Page

from automation.applications.page_navigator import (
    NavigationOutcome,
    PageSignature,
    advance_to_next_page,
    capture_page_signature,
    short_url,
    wait_for_page_settled,
)
from automation.ats.base import ATSAdapter, FieldFillResult
from automation.browser.browser_manager import BrowserManager
from automation.browser.selectors import (
    find_human_gate,
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


@dataclass
class _PageProgress:
    """What processing one page of the application achieved — the loop's own
    bookkeeping, not part of any public result."""

    #: The page's structural signature AFTER every fill pass, which is what
    #: navigation is verified against (see `page_navigator.advance_to_next_page`).
    signature: PageSignature
    #: Set when a human-only gate (CAPTCHA, login wall, OTP) was found here; the
    #: run stops immediately and hands over with this as the reason.
    blocked_reason: str | None = None
    #: Whether the adapter reports this as the last page of the application.
    is_final: bool = False
    #: Field names the vision pass read as already answered on THIS page.
    vision_confirmed: set[str] = _dc_field(default_factory=set)


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

    #: Backstop on how many PAGES one application may span. Not an assumption
    #: about form length — nothing in the loop is derived from it, and a form
    #: ends when the adapter says it has (`is_final_page`), not when a counter
    #: runs out. It exists solely so a mis-detected navigation control can't
    #: loop forever. 20 leaves generous headroom over the longest real
    #: applications (Workday's are typically 4-6 pages).
    #:
    #: This is a page cap, not the old attempt cap: because navigation is now
    #: verified, a page that refuses to advance ends the run immediately with
    #: the form's own reason instead of silently consuming iterations.
    MAX_PAGES = 20
    #: Deprecated alias — `MAX_STEPS` predates multi-page support, when one
    #: iteration meant one fill attempt rather than one page.
    MAX_STEPS = MAX_PAGES

    #: Fill passes per page. More than one because answering a question can
    #: REVEAL others ("Do you require sponsorship?" -> "Which visa do you
    #: hold?"), and those follow-ups are as required as anything else on the
    #: page. Rounds stop as soon as a pass reveals nothing new, so a static
    #: page costs exactly one.
    MAX_FILL_ROUNDS = 4

    #: Attempts at advancing off one page. The second attempt only happens
    #: after something actually changed (an overlay dismissed, a field filled)
    #: — see `_recover_before_retry`. Retrying an identical click against an
    #: identical page is the endless-retry loop this cap exists to prevent.
    NAVIGATION_ATTEMPTS = 2

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

    # `detect_next_step()` used to live here and is deliberately gone: which
    # control advances the form is now `ATSAdapter.find_next_control()`, so a
    # platform can override it (Workday's navigation buttons are identified by
    # `data-automation-id`, not by their text). Leaving a flow-manager method
    # the loop no longer consults would be a trap — an override of it would
    # silently do nothing.

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> ApplicationRunResult:
        try:
            context = self.browser_manager.launch_context()
            # `browser_manager.new_page()`, not `context.new_page()`: under the
            # default `cdp` mode this context is the user's own Chrome, and the
            # manager has to know which tabs are ours so cleanup closes those
            # and leaves their Gmail/LinkedIn tabs alone.
            page = self.browser_manager.new_page()
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
        run regardless of outcome), then releases the browser — UNLESS this
        run needs a human to look at it AND the browser is actually visible,
        in which case the tab is left open on purpose. See the module
        docstring's "Manual review handoff" note.

        "Releases" is `BrowserManager.close()`, which under the default `cdp`
        browser mode closes only the tab this run opened; the user's Chrome and
        its other tabs are never ours to close. Note that an attached or
        persistent browser reports `headless=False`, so a review status always
        takes the keep-open branch below — which is the whole point: there is
        now genuinely something on screen to review."""
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

        logger.info(
            "application %s: releasing browser (status=%s, mode=%s).",
            self.application_id, status, self.browser_manager.active_mode,
        )
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

        adapter = self.adapter_cls(
            page=page, profile=self.profile, resume_document=self.resume_document, answer_engine=self.answer_engine,
        )

        all_results: list[FieldFillResult] = []
        vision_confirmed_filled: set[str] = set()
        pages_processed = 0

        # The page cycle. Every iteration is one PAGE of the application, and
        # the loop leaves only for a reason it can name: the adapter reported
        # the final page, a human-only gate appeared, navigation could not be
        # proven, or the safety backstop tripped.
        for page_index in range(self.MAX_PAGES):
            pages_processed = page_index + 1
            progress = self._process_page(adapter, page, page_index, all_results)
            vision_confirmed_filled |= progress.vision_confirmed

            if progress.blocked_reason is not None:
                return ApplicationRunResult(
                    application_id=self.application_id,
                    status="manual_required",
                    ats_platform=self.ats_platform,
                    confidence=0.0,
                    screenshot_paths=self._safe_screenshot(page),
                    error_log=self._safe_write_error_log(progress.blocked_reason),
                )

            if progress.is_final:
                logger.info(
                    "application %s: page %d (%s) is the final page%s — no further navigation.",
                    self.application_id, page_index + 1, progress.signature.describe(),
                    " (review step)" if self._safe_is_review_page(adapter) else "",
                )
                break

            navigation = self._advance_to_next_page(adapter, page, page_index, progress.signature, all_results)
            if not navigation.advanced:
                return self._navigation_blocked_result(page, page_index, navigation, all_results)
            self.checkpoint(f"step_{page_index}_advanced")
        else:
            logger.warning(
                "application %s: hit the %d-page safety backstop — treating the current page as final.",
                self.application_id, self.MAX_PAGES,
            )

        logger.info(
            "application %s: completed %d page(s) of this application.", self.application_id, pages_processed,
        )

        # Re-verify the résumé LAST, for the reason spelled out in
        # `ATSAdapter.ensure_resume_attached`: a client-rendered form can drop
        # an already-verified upload while it hydrates, so the only
        # verification worth acting on is one taken after the form has settled
        # and everything else has been filled. Located by key rather than held
        # in a local: on a multi-page form the upload may have happened on a
        # later page than the first (see `_upload_resume_if_offered`).
        resume_result = next((r for r in all_results if r.field_key == "resume_upload"), None)
        if resume_result is not None:
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

    # ------------------------------------------------------------------
    # One page of a multi-page application
    # ------------------------------------------------------------------

    def _process_page(
        self,
        adapter: ATSAdapter,
        page: Page,
        page_index: int,
        all_results: list[FieldFillResult],
    ) -> _PageProgress:
        """Takes one page from "just arrived" to "everything answerable here is
        answered", and reports whether it's the last one.

        Order matters throughout: settle before reading anything (a
        half-rendered page looks like a short one), check human-only gates
        before filling anything, upload before filling (a résumé upload on a
        modern ATS often auto-populates the fields below it), fill in rounds
        so conditional follow-ups get their turn, and run vision LAST because a
        field is only genuinely "left over" once every cheaper pass has had a
        go at it."""
        wait_for_page_settled(page)

        dismissed = adapter.dismiss_distractions()
        if dismissed:
            logger.info(
                "application %s: page %d — dismissed %d overlay(s): %s",
                self.application_id, page_index + 1, len(dismissed), ", ".join(dismissed),
            )

        # Checked on EVERY page, not just the first. A login wall or an OTP
        # challenge appearing at step 3 of a Workday application is exactly as
        # much of a hard stop as one on the landing page, and before multi-page
        # support these checks could only ever see page 1.
        if page_has_captcha(page):
            self.checkpoint("captcha_detected")
            logger.info(
                "application %s: CAPTCHA detected on page %d — stopping before filling anything.",
                self.application_id, page_index + 1,
            )
            return _PageProgress(
                signature=capture_page_signature(page),
                blocked_reason="CAPTCHA present — a human must solve it; automation never will.",
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
            return _PageProgress(signature=capture_page_signature(page), blocked_reason=reason)

        arrival = capture_page_signature(page)
        logger.info(
            "application %s: page %d — %s%s",
            self.application_id, page_index + 1, arrival.describe(),
            f" @ {short_url(arrival.url)}" if arrival.url else "",
        )

        if page_index == 0:
            # Unchanged from the single-page behavior: the first page is always
            # offered the résumé, whether or not a file input is detectable
            # (some ATS UIs only reveal one after a trigger is clicked — see
            # `ATSAdapter._find_resume_input`).
            all_results.append(self._upload_resume(adapter))
        else:
            self._upload_resume_if_offered(adapter, page_index, all_results)

        signature = self._fill_page(adapter, page, page_index, all_results)

        # The vision fallback now runs per page rather than once at the end.
        # On a multi-page form the old placement could only ever see the LAST
        # page: everything pages 1..n-1 left blank was already behind a Next
        # click by the time it ran, so the single most expensive rescue pass in
        # the system was structurally unable to rescue most of the form.
        vision_confirmed = self._run_vision_pass(adapter, all_results)
        if vision_confirmed:
            signature = capture_page_signature(page)

        is_final = self._safe_is_final_page(adapter)
        return _PageProgress(signature=signature, is_final=is_final, vision_confirmed=vision_confirmed)

    def _fill_page(
        self,
        adapter: ATSAdapter,
        page: Page,
        page_index: int,
        all_results: list[FieldFillResult],
    ) -> PageSignature:
        """Fills the current page, repeating while it keeps revealing new
        fields. Returns the page's signature once it has stopped changing —
        which is exactly the state navigation must be verified against.

        Conditional sections are the reason this is a loop. Answering "Do you
        require sponsorship?" renders a follow-up that did not exist when the
        page was first scanned, and a single pass would leave it blank — where
        it then blocks navigation as a missing required field, with no
        indication of why. Rounds compare control IDENTITIES (never values), so
        an ordinary fill can't be mistaken for new content, and the loop exits
        the moment a pass reveals nothing.

        Only round 0 fills personal information: the later rounds exist for
        newly-revealed fields, and the generic label/name sweep in
        `answer_questions()` already covers any profile-mapped field among
        them. Re-running the personal-info pass would re-fill (and re-count)
        fields that are already done."""
        signature = capture_page_signature(page)

        for round_index in range(self.MAX_FILL_ROUNDS):
            results: list[FieldFillResult] = []
            if round_index == 0:
                results.extend(adapter.fill_personal_information())
            results.extend(adapter.answer_questions())
            all_results.extend(results)

            for r in results:
                # Field name + outcome only — not `r.value_used`, which can
                # be PII (phone, address, answers to personal questions).
                logger.info(
                    "application %s: page %d round %d — %s '%s'",
                    self.application_id, page_index + 1, round_index,
                    "filled" if r.filled else "NOT filled", r.profile_path or r.field_key,
                )

            after = capture_page_signature(page)
            revealed = after.newly_visible_controls(signature)
            signature = after
            if not revealed:
                break

            self.checkpoint(f"step_{page_index}_revealed_fields")
            logger.info(
                "application %s: page %d — answering revealed %d conditional field(s) (%s); filling those too.",
                self.application_id, page_index + 1, len(revealed), ", ".join(revealed[:5]),
            )
        else:
            logger.warning(
                "application %s: page %d kept revealing new fields after %d rounds — continuing with what is filled.",
                self.application_id, page_index + 1, self.MAX_FILL_ROUNDS,
            )

        self.checkpoint(f"step_{page_index}_filled")
        return signature

    def _upload_resume_if_offered(
        self, adapter: ATSAdapter, page_index: int, all_results: list[FieldFillResult],
    ) -> None:
        """Uploads the résumé on a page AFTER the first, when that page is the
        one asking for it.

        This is the fix for the single most consequential multi-page bug: the
        upload used to happen exactly once, before the loop, so on any ATS whose
        upload field lives on a later page (Workday's is on "My Experience",
        page 2 of 5) the application was submitted with no résumé attached —
        and the run log said the upload had merely "failed", on page 1, where
        there had never been a field to fill.

        Updates the existing `resume_upload` result in place rather than
        appending a second one, so a first-page miss followed by a later-page
        success reads as one field that ended up filled — not as one failure
        plus one success dragging the confidence score down by half a field."""
        try:
            state = adapter.resume_attachment_state()
        except Exception:  # noqa: BLE001 - a probe must never fail the run
            logger.debug("application %s: could not read the résumé state on page %d.", self.application_id, page_index + 1)
            return

        if state != "missing":
            return  # "attached" — nothing to do; "no_field" — not this page's job

        logger.info(
            "application %s: page %d asks for a résumé and none is attached — uploading here.",
            self.application_id, page_index + 1,
        )
        uploaded = self._upload_resume(adapter)
        existing = next((r for r in all_results if r.field_key == "resume_upload"), None)
        if existing is None:
            all_results.append(uploaded)
            return
        existing.value_used = uploaded.value_used
        existing.confidence = uploaded.confidence
        existing.filled = uploaded.filled
        existing.failure = uploaded.failure

    # ------------------------------------------------------------------
    # Navigation between pages
    # ------------------------------------------------------------------

    def _advance_to_next_page(
        self,
        adapter: ATSAdapter,
        page: Page,
        page_index: int,
        signature: PageSignature,
        all_results: list[FieldFillResult],
    ) -> NavigationOutcome:
        """Moves off `page_index` and PROVES it happened, retrying only when
        the retry has a reason to behave differently.

        A second identical click on an identical page produces an identical
        rejection, so `_recover_before_retry` must change something first
        (dismiss an overlay, fill what the validation errors point at). When it
        can't, this stops immediately rather than spending the attempt — that
        is the difference between bounded recovery and an endless retry loop."""
        outcome = NavigationOutcome(
            advanced=False, reason="no navigation control was found", before=signature, after=signature,
        )

        for attempt in range(1, self.NAVIGATION_ATTEMPTS + 1):
            control = self._safe_find_next_control(adapter)
            if control is None:
                logger.warning(
                    "application %s: page %d reported more pages ahead but exposes no navigation control.",
                    self.application_id, page_index + 1,
                )
                return outcome

            outcome = advance_to_next_page(page, control, before=signature)
            if outcome.advanced:
                logger.info(
                    "application %s: advanced from page %d — %s",
                    self.application_id, page_index + 1, outcome.reason,
                )
                return outcome

            logger.warning(
                "application %s: page %d did not advance on attempt %d/%d — %s",
                self.application_id, page_index + 1, attempt, self.NAVIGATION_ATTEMPTS, outcome.reason,
            )
            for message in outcome.validation_errors:
                logger.warning("application %s: page %d validation — %s", self.application_id, page_index + 1, message)

            if attempt >= self.NAVIGATION_ATTEMPTS:
                break
            if not self._recover_before_retry(adapter, page, page_index, outcome, all_results):
                logger.info(
                    "application %s: nothing changed on page %d that would make a second attempt behave "
                    "differently — not retrying.", self.application_id, page_index + 1,
                )
                break
            signature = capture_page_signature(page)

        return outcome

    def _recover_before_retry(
        self,
        adapter: ATSAdapter,
        page: Page,
        page_index: int,
        outcome: NavigationOutcome,
        all_results: list[FieldFillResult],
    ) -> bool:
        """Tries to make the page navigable, returning whether anything about
        it actually changed. Only two things are worth attempting: an overlay
        that swallowed the click, and fields the form is complaining about —
        which a fresh fill round plus the vision pass may now be able to
        answer, since validation usually marks up the offending field."""
        changed = False

        if adapter.dismiss_distractions():
            changed = True

        blocking = outcome.validation_errors or find_unfilled_required_fields(page)
        if blocking:
            before = capture_page_signature(page)
            round_results = adapter.answer_questions()
            all_results.extend(round_results)
            self._run_vision_pass(adapter, all_results)
            newly_filled = sum(1 for r in round_results if r.filled)
            if newly_filled or capture_page_signature(page).differs_from(before):
                logger.info(
                    "application %s: page %d — filled %d more field(s) after the form rejected navigation; retrying.",
                    self.application_id, page_index + 1, newly_filled,
                )
                changed = True

        return changed

    def _navigation_blocked_result(
        self,
        page: Page,
        page_index: int,
        navigation: NavigationOutcome,
        all_results: list[FieldFillResult],
    ) -> ApplicationRunResult:
        """The run stops here: the application has more pages, and we cannot
        get to them. Handed to a human with the form's own reason attached —
        never reported as a completed application, and never retried blindly.

        `manual_required` rather than `failed` because nothing malfunctioned:
        the form is asking for something this run could not supply, which is a
        person's job to resolve. It is also the status `app/api/applications.py`
        treats as retryable, so the same application can be picked back up once
        the missing information exists."""
        self.checkpoint("navigation_blocked")
        still_missing = find_unfilled_required_fields(page)
        parts = [
            f"Could not advance past page {page_index + 1} of this application: {navigation.reason}."
        ]
        if navigation.validation_errors:
            parts.append("The form reported: " + "; ".join(navigation.validation_errors) + ".")
        if still_missing:
            parts.append("Required field(s) still empty here: " + ", ".join(still_missing) + ".")
        if navigation.click_failed:
            parts.append("The navigation control could not be clicked at all.")
        parts.append("A human needs to complete this page; the application has NOT been submitted.")
        reason = " ".join(parts)

        logger.warning("application %s: %s", self.application_id, reason)
        return ApplicationRunResult(
            application_id=self.application_id,
            status="manual_required",
            ats_platform=self.ats_platform,
            confidence=self._aggregate_confidence(all_results),
            screenshot_paths=self._safe_screenshot(page),
            error_log=self._safe_write_error_log(reason),
        )

    # ------------------------------------------------------------------
    # Adapter hooks, guarded
    # ------------------------------------------------------------------
    # Every one of these is overridable per ATS (see `ATSAdapter`'s "Multi-page
    # navigation" section), so a platform-specific override raising must degrade
    # to the generic answer rather than crash a run that is otherwise fine.

    def _safe_find_next_control(self, adapter: ATSAdapter) -> Locator | None:
        try:
            return adapter.find_next_control()
        except Exception:  # noqa: BLE001
            logger.exception("application %s: the adapter's next-control lookup failed.", self.application_id)
            return None

    def _safe_is_final_page(self, adapter: ATSAdapter) -> bool:
        try:
            return adapter.is_final_page()
        except Exception:  # noqa: BLE001
            logger.exception(
                "application %s: the adapter's final-page check failed — treating this as the final page.",
                self.application_id,
            )
            return True

    def _safe_is_review_page(self, adapter: ATSAdapter) -> bool:
        try:
            return adapter.is_review_page()
        except Exception:  # noqa: BLE001
            return False

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

        Runs once PER PAGE, at the end of that page's fill rounds — not once at
        the end of the run, which is where it used to sit. On a multi-page form
        that placement meant it could only ever see the final page: everything
        the earlier pages left blank was already behind a Next click by the
        time it ran. It stays cheap despite running more often because
        `collect_unfilled_fields_for_vision` returns nothing on a fully-filled
        page, and no model call is made for an empty list."""
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
