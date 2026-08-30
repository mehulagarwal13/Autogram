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
import re
import time
from dataclasses import dataclass, field as _dc_field
from datetime import datetime, timezone
from typing import Callable

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Locator, Page

from app.core.config import AUTOMATION_HUMAN_WAIT_TIMEOUT_S
from automation.applications.page_navigator import (
    NavigationOutcome,
    PageSignature,
    advance_to_next_page,
    capture_page_signature,
    short_url,
    wait_for_page_settled,
)
from automation.ats.base import ATSAdapter, FieldFillResult
from automation.ats.detector import ATSDetector, FALLBACK_ATS
from automation.ats.generic.generic_adapter import GenericAdapter
from automation.ats.registry import get_adapter_class
from automation.browser.browser_manager import BrowserManager
from automation.applications import verification_channel
from automation.browser.selectors import (
    VERIFICATION_CODE_INPUT_SELECTOR,
    find_apply_entry_button,
    find_human_gate,
    find_job_posting_title_and_company,
    find_unfilled_required_fields,
    find_validation_errors,
    page_has_captcha,
    wait_for_form_ready,
    wait_for_submission_confirmation,
)
from automation.forms.answer_engine import ApplicationAnswerEngine
from automation.forms.vision_fallback import VisionFormAnswerer
from automation.interfaces import ApplicationRunResult, CandidateProfile, ProfileDocument, VALID_TRUST_LEVELS

logger = logging.getLogger(__name__)

# How often the human-verification wait re-checks whether a CAPTCHA/human
# gate has cleared. Cheap (one more selector read), so a short interval costs
# nothing and keeps the reported wait responsive.
HUMAN_WAIT_POLL_INTERVAL_S = 5.0

# HITL platform (§10 Live Automation View) — in-memory "what is this run doing
# RIGHT NOW", keyed by application_id, read by `GET /applications/{id}/live`.
# Same "module-level dict, strong-referenced, cleared on completion" pattern as
# `_OPEN_REVIEW_SESSIONS` below — this is a single-process, best-effort view
# (matches today's single-process `BackgroundTasks` deployment; see
# PHASE2_ARCHITECTURE.md Initiative 4 for the real-queue follow-up), not a
# durable record. Durable, replay-after-restart progress lives in
# `automation_runs`/`application_questions` instead.
LIVE_RUN_STATE: dict[str, dict] = {}


def clear_live_state(application_id: str) -> None:
    LIVE_RUN_STATE.pop(application_id, None)


def get_live_state(application_id: str) -> dict | None:
    return LIVE_RUN_STATE.get(application_id)


class _RunLogCapture(logging.Handler):
    """Captures this run's own log lines as structured `{timestamp, message}`
    entries for `AutomationRun.log_lines` (§18 Observability) — a
    human-readable activity trail distinct from the Playwright trace/
    screenshots. Filters by a simple substring match on `application_id`,
    which every log line in this module already includes by convention
    ("application %s: ...") — good enough for a debugging/activity view
    without threading a logging `extra=` field through every call site."""

    def __init__(self, application_id: str):
        super().__init__(level=logging.INFO)
        self.application_id = application_id
        self.lines: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken log record must never break the run
            return
        if self.application_id not in message:
            return
        self.lines.append({"timestamp": datetime.now(timezone.utc).isoformat(), "message": message})

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


# Browsers deliberately left open for manual review, keyed by application_id
# -> (BrowserManager, ATSAdapter, Page) — the adapter/page are what let
# `POST /applications/{id}/approve` replay a submit click against the SAME
# already-filled page a copilot_review run left open (see
# `submit_open_review_session` below), rather than only ever supporting
# "a human clicks submit themselves in the visible window".
#
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
_OPEN_REVIEW_SESSIONS: dict[str, tuple[BrowserManager, ATSAdapter | None, Page | None]] = {}


def close_review_session(application_id: str) -> bool:
    """Closes and forgets a browser that was left open for manual review.
    Returns `False` if there wasn't one (already closed, or this
    application never resulted in one being left open)."""
    session = _OPEN_REVIEW_SESSIONS.pop(application_id, None)
    if session is None:
        return False
    browser_manager, _adapter, _page = session
    try:
        browser_manager.close()
    except Exception:  # noqa: BLE001 - best-effort cleanup, never raise from here
        logger.debug("application %s: error closing a manually-reviewed browser (ignored).", application_id)
    return True


def list_open_review_sessions() -> list[str]:
    """application_ids with a browser currently left open for review —
    useful for a debug endpoint or just introspecting from a shell."""
    return list(_OPEN_REVIEW_SESSIONS.keys())


def submit_open_review_session(application_id: str) -> tuple[str, str | None] | None:
    """`POST /applications/{id}/approve`'s mechanism: replays a submit click
    against the exact browser/page a `copilot_review` run left open, via the
    SAME `submit_and_confirm` the `AUTO_SUBMIT` decision path uses, so the two
    can never disagree about what counts as a confirmed submission.

    Returns `(status, error_message_or_None)` — `status` is `"applied"`,
    `"needs_review"`, or `"failed"`, matching `ApplicationRunResult.status`.
    Returns `None` if there is no open review session for this application at
    all (already closed, timed out and cleaned up, or the run never left one
    open) — callers should treat that as "nothing to approve here"."""
    session = _OPEN_REVIEW_SESSIONS.get(application_id)
    if session is None:
        return None
    _browser_manager, adapter, page = session
    if adapter is None or page is None:
        # A session was left open (e.g. the run crashed before an adapter was
        # ever constructed) but there's nothing to click submit on.
        close_review_session(application_id)
        return "failed", "No fillable page is open for this application anymore."
    result = submit_and_confirm(adapter, page, application_id)
    close_review_session(application_id)
    return result


def submit_and_confirm(adapter: ATSAdapter, page: Page, application_id: str) -> tuple[str, str | None]:
    """Clicks submit and verifies confirmation — the ONE place this happens,
    shared by `_run_on_page`'s `AUTO_SUBMIT` branch and
    `submit_open_review_session` above, so autopilot and a human-approved
    copilot submission can never disagree about what counts as "confirmed".
    Never claims `"applied"` without positive confirmation (see
    `wait_for_submission_confirmation`) — retrying a submission that actually
    succeeded would double-apply, so an unconfirmed click is reported as
    `needs_review`, never silently retried.

    Returns `(status, error_message_or_None)`."""
    logger.info("application %s: clicking submit.", application_id)
    if not adapter.submit_application():
        return "failed", "The submit control could not be clicked."

    confirmation = wait_for_submission_confirmation(page)
    if confirmation:
        logger.info("application %s: submission CONFIRMED via %s.", application_id, confirmation)
        return "applied", None

    rejection = find_validation_errors(page)
    reason = (
        "Submit was clicked but no confirmation could be detected "
        "(no confirmation page, success message, or application reference). "
        "The application may or may not have been received — verify on the ATS "
        "before retrying, since retrying a submission that did succeed would double-apply."
    )
    if rejection:
        reason += f" The page reported: {'; '.join(rejection)}."
    logger.warning("application %s: %s", application_id, reason)
    return "needs_review", reason


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


def decide_action(
    confidence: float, ats_platform: str, autopilot_enabled: bool,
    trust_level: str = "FULL_MANUAL_REVIEW",
) -> str:
    """The exact decision table from ARCHITECTURE.md, as a standalone,
    independently testable function:

    - `AUTO_SUBMIT` only when the user opted in AND the platform is public
      (no login) AND confidence clears the high bar AND — §6.4 — this job
      posting's site is trusted for auto-submit (`trust_level ==
      "TRUSTED_AUTO_SUBMIT"`). Trust is an ADDITIONAL required condition,
      never a bypass of the other three: a trusted site with a low-confidence
      fill still lands in review, exactly as an untrusted one would.
    - `NEEDS_REVIEW` when confidence is too low to trust at all.
    - `COPILOT_REVIEW` otherwise — form is filled, a human clicks submit.

    `trust_level` defaults to the safe value on purpose: any caller that
    doesn't explicitly resolve one (existing tests, a bare/legacy call site)
    gets today's always-review behavior, never a silent opt-in to auto-submit.
    `FULL_MANUAL_REVIEW` and `DRAFT_ONLY` currently produce identical output
    here — see `VALID_TRUST_LEVELS`'s docstring for why that's not an
    oversight. An unrecognized value is treated the same as
    `FULL_MANUAL_REVIEW` (fail closed), never as `TRUSTED_AUTO_SUBMIT`.
    """
    high_confidence = confidence >= AUTO_SUBMIT_CONFIDENCE_THRESHOLD
    is_public_ats = ats_platform in PUBLIC_ATS_PLATFORMS
    is_trusted = trust_level == "TRUSTED_AUTO_SUBMIT"
    if autopilot_enabled and is_public_ats and high_confidence and is_trusted:
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
        adapter_cls: type[ATSAdapter] | None,
        profile: CandidateProfile,
        resume_document: ProfileDocument,
        autopilot_enabled: bool = False,
        browser_manager: BrowserManager | None = None,
        headless: bool | None = None,
        answer_engine: ApplicationAnswerEngine | None = None,
        vision_answerer: VisionFormAnswerer | None = None,
        on_waiting_for_human: Callable[[str], None] | None = None,
        is_kill_switch_engaged: Callable[[], bool] | None = None,
        resolve_trust_level: Callable[[], str] | None = None,
        human_wait_timeout_s: float = AUTOMATION_HUMAN_WAIT_TIMEOUT_S,
    ) -> None:
        self.application_id = application_id
        self.user_id = user_id
        self.job_url = job_url
        self.ats_platform = ats_platform
        # Immutable snapshot of the pre-flight `ATSDetector` guess (e.g.
        # "smartrecruiters"), kept SEPARATE from `self.ats_platform` above,
        # which is mutated as the run progresses to reflect whichever adapter
        # actually ends up doing the work (see `_detect_supported_ats` and
        # `_resolve_adapter_from_listing_page`'s GenericAdapter fallback).
        # `self.ats_platform` is the one `decide_action` reads — it must
        # always name the adapter that ran, never a platform guess nobody
        # actually automated — while this one is purely for observability:
        # a dashboard/audit log can show "Detected: smartrecruiters / Resolved:
        # custom" instead of silently attributing a generic-adapter fill to a
        # dedicated adapter that never ran.
        self.detected_ats_platform = ats_platform
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
        # HITL platform: `on_waiting_for_human` fires (with a human-readable
        # reason) the moment a CAPTCHA/human-gate is detected, so `app/` can
        # reflect WAITING_FOR_HUMAN immediately rather than only after this
        # run eventually returns — see `_wait_for_human`.
        # `is_kill_switch_engaged` is checked fresh at the top of every page;
        # see `_kill_switch_engaged` for the fail-closed contract.
        self.on_waiting_for_human = on_waiting_for_human
        self.is_kill_switch_engaged = is_kill_switch_engaged
        # §6.4 trust levels: resolved fresh at the decision point (not cached
        # here), since `resolve_trust_level` may read per-site config; see
        # `_resolve_trust_level` for the fail-safe contract.
        self.resolve_trust_level = resolve_trust_level
        # Overridable per instance (defaults to the configured production
        # value) so tests exercising a gate that never clears — the common
        # case — don't have to burn real wall-clock time; pass a small value
        # (e.g. 0) to get the same code path with an effectively immediate
        # timeout.
        self.human_wait_timeout_s = human_wait_timeout_s
        self._steps_completed: list[str] = []
        # Set once `_run_on_page` constructs an adapter for this run — see
        # `_finish_browser_session`, which reads these to populate
        # `_OPEN_REVIEW_SESSIONS` so `POST /applications/{id}/approve` can
        # replay a submit click later.
        self._last_adapter: ATSAdapter | None = None
        self._last_page: Page | None = None
        # "Apply from Job Link": best-effort (title, company) read off the
        # job posting page — see `_safe_detect_job_posting_metadata`. Set
        # once, early in `_run_on_page`, and attached to the final
        # `ApplicationRunResult` in `_run_captured` regardless of how the
        # run ends.
        self._detected_company: str | None = None
        self._detected_position: str | None = None

    @property
    def steps_completed(self) -> list[str]:
        return list(self._steps_completed)

    def checkpoint(self, step_name: str, *, page_number: int | None = None) -> None:
        """Records progress. Until the Phase 4 `applications`/`automation_runs`
        tables + a repository exist (see `automation/interfaces.py`), this is
        in-memory + logged only — `app/` persists the final
        `ApplicationRunResult` by hand for now. Once that repository exists,
        this becomes a real per-step DB write, same pattern as everything
        else in `automation/interfaces.py`.

        Also updates `LIVE_RUN_STATE` (§10 Live Automation View) — every
        checkpoint is a point-in-time snapshot of "what is this run doing
        right now", which is exactly what `GET /applications/{id}/live`
        polls."""
        self._steps_completed.append(step_name)
        logger.info("application %s: step '%s' complete", self.application_id, step_name)
        state = LIVE_RUN_STATE.setdefault(self.application_id, {})
        state["last_step"] = step_name
        if page_number is not None:
            state["page_number"] = page_number
        state["updated_at"] = datetime.now(timezone.utc).isoformat()

    # `detect_next_step()` used to live here and is deliberately gone: which
    # control advances the form is now `ATSAdapter.find_next_control()`, so a
    # platform can override it (Workday's navigation buttons are identified by
    # `data-automation-id`, not by their text). Leaving a flow-manager method
    # the loop no longer consults would be a trap — an override of it would
    # silently do nothing.

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self, *, resume_page: Page | None = None) -> ApplicationRunResult:
        """Entry point. Wraps the actual run in a per-run log capture (§18 —
        see `_RunLogCapture`) and guarantees `LIVE_RUN_STATE` is cleared when
        this run is done, one way or another — a run that ends in a review
        status is no longer "in progress" for the live view even though its
        browser may stay open (see `_finish_browser_session`).

        `resume_page`: continue on an ALREADY-OPEN page instead of opening a
        fresh tab and navigating to `self.job_url` — for a `manual_required`/
        `needs_review` run whose browser was left open (§ manual review
        handoff) and where a human has since fixed whatever blocked it (e.g.
        ticked a required consent checkbox) directly on that tab. Re-navigating
        would reload the page and throw away exactly the correction the human
        just made, which is why this is a distinct path from a normal `run()`
        rather than something `_resolve_adapter_from_listing_page` or a retry
        could paper over. `_OPEN_REVIEW_SESSIONS` (this same module) is the
        in-process registry a long-lived server can use to find that page
        again; it does not survive a process restart, so a caller reconnecting
        from a fresh process (e.g. over CDP, matching the tab by URL) is
        equally valid — this method doesn't care how `resume_page` was found,
        only that it's the SAME live page, never a freshly-navigated one."""
        log_capture = _RunLogCapture(self.application_id)
        automation_logger = logging.getLogger("automation")
        automation_logger.addHandler(log_capture)
        try:
            result = self._run_captured(resume_page=resume_page)
            result.log_lines = log_capture.lines
            return result
        finally:
            automation_logger.removeHandler(log_capture)
            clear_live_state(self.application_id)

    def _run_captured(self, *, resume_page: Page | None = None) -> ApplicationRunResult:
        try:
            context = self.browser_manager.launch_context()
            if resume_page is not None:
                # Adopt rather than open a new tab — this the whole point of
                # a resume: continue on the SAME page a human just acted on.
                self.browser_manager.adopt_page(resume_page)
                page = resume_page
            else:
                # `browser_manager.new_page()`, not `context.new_page()`: under
                # the default `cdp` mode this context is the user's own Chrome,
                # and the manager has to know which tabs are ours so cleanup
                # closes those and leaves their Gmail/LinkedIn tabs alone.
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
                detected_ats_platform=self.detected_ats_platform,
                confidence=0.0,
                error_log=self._safe_write_error_log(repr(e)),
            )

        try:
            result = self._run_on_page(page, skip_navigation=resume_page is not None)
        except Exception as e:  # noqa: BLE001 - any failure becomes a reportable result, never a crash
            logger.exception("application %s: run failed", self.application_id)
            result = self._build_failure_result(page, e)
        result.trace_path = self._safe_stop_trace(context)
        # "Apply from Job Link": attached here, once, regardless of how the
        # run ended — every ApplicationRunResult return point inside
        # _run_on_page would otherwise need this repeated on it individually.
        result.detected_company = self._detected_company
        result.detected_position = self._detected_position

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
            # connection) alive once `run()` returns. The adapter/page are
            # included so `POST /applications/{id}/approve` can later replay a
            # submit click against this exact page (`submit_open_review_session`).
            _OPEN_REVIEW_SESSIONS[self.application_id] = (self.browser_manager, self._last_adapter, self._last_page)
            return

        logger.info(
            "application %s: releasing browser (status=%s, mode=%s).",
            self.application_id, status, self.browser_manager.active_mode,
        )
        self.browser_manager.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_on_page(self, page: Page, *, skip_navigation: bool = False) -> ApplicationRunResult:
        if skip_navigation:
            # Resuming on a page a human already has open (see `run()`'s
            # `resume_page` docstring) — (re-)navigating here would reload it
            # and throw away whatever they just fixed on it.
            self.checkpoint("resumed_existing_page")
        else:
            self.browser_manager.run_with_retries(lambda: page.goto(self.job_url, wait_until="domcontentloaded"))
            self.checkpoint("navigated")
        # `domcontentloaded` above is "the HTML parsed", which on a React/Remix
        # ATS is before the form is hydrated — and filling a form mid-hydration
        # can be silently undone by the framework as it takes over the DOM. See
        # `wait_for_form_ready`'s comment block for the live Greenhouse run
        # where exactly that dropped an already-verified résumé. Calling this
        # even when resuming is still correct (and cheap) — it's a readiness
        # check, not a navigation, and the page may still be settling from
        # whatever the human just did on it.
        wait_for_form_ready(page)
        logger.info(
            "application %s: reached the job application page (%s, headless=%s)",
            self.application_id, self.job_url, self.browser_manager.headless,
        )
        # "Apply from Job Link": best-effort, never fabricated — see
        # `_safe_detect_job_posting_metadata`.
        self._detected_company, self._detected_position = self._safe_detect_job_posting_metadata(page)

        adapter_cls = self.adapter_cls
        if adapter_cls is None:
            # Pre-flight detection (`app/api/applications.py::_run_application`)
            # came back as `FALLBACK_ATS` — this page might be a job LISTING
            # page sitting in front of a supported ATS, not itself
            # unsupported. See `_resolve_adapter_from_listing_page`.
            resolution = self._resolve_adapter_from_listing_page(page)
            if resolution is None:
                reason = (
                    "Could not detect a supported application form on this page, and no "
                    "Apply/Apply Now/Start Application control was found to reach one."
                )
                logger.info("application %s: %s", self.application_id, reason)
                self.checkpoint("unsupported_ats")
                return ApplicationRunResult(
                    application_id=self.application_id,
                    status="needs_review",
                    ats_platform=self.ats_platform,
                    detected_ats_platform=self.detected_ats_platform,
                    confidence=0.0,
                    screenshot_paths=self._safe_screenshot(page),
                    error_log=self._safe_write_error_log(reason),
                    pages_completed=0,
                )
            adapter_cls, page = resolution

        adapter = adapter_cls(
            page=page, profile=self.profile, resume_document=self.resume_document, answer_engine=self.answer_engine,
        )
        self._last_adapter = adapter
        self._last_page = page

        all_results: list[FieldFillResult] = []
        vision_confirmed_filled: set[str] = set()
        pages_processed = 0

        # The page cycle. Every iteration is one PAGE of the application, and
        # the loop leaves only for a reason it can name: the adapter reported
        # the final page, a human-only gate appeared, navigation could not be
        # proven, the safety backstop tripped, or the kill switch engaged.
        for page_index in range(self.MAX_PAGES):
            if self._kill_switch_engaged():
                reason = (
                    "The account-level autopilot kill switch is engaged — stopping before "
                    f"page {page_index + 1}. Re-enable it in Settings, then retry this application."
                )
                logger.warning("application %s: %s", self.application_id, reason)
                self.checkpoint("kill_switch_engaged", page_number=page_index + 1)
                return ApplicationRunResult(
                    application_id=self.application_id,
                    status="manual_required",
                    ats_platform=self.ats_platform,
                    detected_ats_platform=self.detected_ats_platform,
                    confidence=self._aggregate_confidence(all_results),
                    screenshot_paths=self._safe_screenshot(page),
                    error_log=self._safe_write_error_log(reason),
                    pages_completed=page_index,
                )

            pages_processed = page_index + 1
            progress = self._process_page(adapter, page, page_index, all_results)
            vision_confirmed_filled |= progress.vision_confirmed

            if progress.blocked_reason is not None:
                return ApplicationRunResult(
                    application_id=self.application_id,
                    status="manual_required",
                    ats_platform=self.ats_platform,
                    detected_ats_platform=self.detected_ats_platform,
                    confidence=0.0,
                    screenshot_paths=self._safe_screenshot(page),
                    error_log=self._safe_write_error_log(progress.blocked_reason),
                    pages_completed=pages_processed,
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
                # ESCALATE BEFORE GIVING UP. The page is genuinely stuck — a
                # required field this run could not fill, a widget it does not
                # know how to operate (Amex's "Add Experience"/"Add Skill"
                # repeating sections are the motivating case), or a validation
                # error it cannot clear.
                #
                # Until now this ended the run immediately with
                # `manual_required`, which is honest but wasteful: the browser
                # is still open on exactly the page that needs one thing done,
                # and the human is right there. Asking first turns a dead run
                # into a pause the person can resolve in seconds, and the
                # existing wait already resumes ON THE SAME PAGE and re-checks
                # rather than assuming anything changed.
                #
                # Falls through to the original behaviour on timeout, so the
                # worst case is exactly what happened before.
                if self._wait_for_human_to_unblock_page(page, page_index, navigation):
                    self.checkpoint(f"step_{page_index}_unblocked_by_human", page_number=page_index + 1)
                    navigation = self._advance_to_next_page(
                        adapter, page, page_index, capture_page_signature(page), all_results,
                    )
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
                detected_ats_platform=self.detected_ats_platform,
                confidence=confidence,
                screenshot_paths=self._safe_screenshot(page),
                error_log=self._safe_write_error_log(reason),
                pages_completed=pages_processed,
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
                detected_ats_platform=self.detected_ats_platform,
                confidence=confidence,
                screenshot_paths=self._safe_screenshot(page),
                error_log=self._safe_write_error_log(reason),
                pages_completed=pages_processed,
            )

        trust_level = self._resolve_trust_level()
        action = decide_action(confidence, self.ats_platform, self.autopilot_enabled, trust_level)
        self.checkpoint(f"decision_{action.lower()}")
        logger.info(
            "application %s: decision=%s (confidence=%.4f, autopilot_enabled=%s, ats_platform=%s, trust_level=%s)",
            self.application_id, action, confidence, self.autopilot_enabled, self.ats_platform, trust_level,
        )

        error_log: str | None = None

        if action == "AUTO_SUBMIT":
            # Deliberately not "applied" until POSITIVELY confirmed (see
            # `submit_and_confirm`) and not "failed" just because a click
            # landed — those two are NOT the same claim about the world, and
            # ApplicationRunResult must never conflate them.
            status, submit_error = submit_and_confirm(adapter, page, self.application_id)
            if status != "failed":
                self.checkpoint("submit_clicked")
            if status == "applied":
                self.checkpoint("submission_confirmed")
            elif status == "needs_review":
                # A human has to confirm on the ATS side before any retry,
                # since retrying a submission that actually succeeded would
                # double-apply.
                self.checkpoint("submission_unconfirmed")
            error_log = self._safe_write_error_log(submit_error) if submit_error else None
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
            detected_ats_platform=self.detected_ats_platform,
            confidence=confidence,
            screenshot_paths=screenshot_paths,
            error_log=error_log,
            pages_completed=pages_processed,
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
        half-rendered page looks like a short one), check for a hard human
        gate before filling anything (a login wall or OTP challenge means
        there is no real FORM behind it yet — nothing to fill), upload before
        filling (a résumé upload on a modern ATS often auto-populates the
        fields below it), fill in rounds so conditional follow-ups get their
        turn, run vision near the end because a field is only genuinely
        "left over" once every cheaper pass has had a go at it, and check for
        a CAPTCHA LAST, after everything fillable has been attempted — a
        CAPTCHA widget on a real ATS form (Greenhouse included) typically
        gates SUBMISSION, not the fields themselves, so a human is asked to
        solve it only once there is nothing left for automation to usefully
        do on this page, not before a single field has been typed."""
        wait_for_page_settled(page)

        dismissed = adapter.dismiss_distractions()
        if dismissed:
            logger.info(
                "application %s: page %d — dismissed %d overlay(s): %s",
                self.application_id, page_index + 1, len(dismissed), ", ".join(dismissed),
            )

        # Checked on EVERY page, not just the first. A login wall, OTP
        # challenge, identity/payment gate means there is no application FORM
        # behind it at all yet — genuinely nothing fillable — so this alone
        # stays an early hard stop, unlike CAPTCHA below.
        #
        # NEVER bypassed/solved/circumvented — see `_wait_for_human`. The run
        # pauses, waits for a HUMAN to clear it in this same visible browser,
        # and resumes automatically if they do within the timeout; only a
        # timeout falls back to today's hard stop (`manual_required`).
        human_gate = find_human_gate(page)
        if human_gate is not None:
            reason = f"Human intervention required before this form can be filled: {human_gate}."
            self.checkpoint("human_gate_detected", page_number=page_index + 1)
            logger.info("application %s: %s", self.application_id, reason)
            if not self._wait_for_human(page_index, reason, lambda: find_human_gate(page) is None, page=page):
                return _PageProgress(signature=capture_page_signature(page), blocked_reason=reason)
            wait_for_page_settled(page)

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

        # CAPTCHA is checked LAST, after everything fillable on this page has
        # been attempted — see this method's docstring for why. NEVER
        # bypassed/solved/circumvented: the run pauses, waits for a HUMAN to
        # clear it in this same visible browser (`_wait_for_human`), and
        # resumes automatically if they do within the timeout; only a
        # timeout falls back to `manual_required`.
        if page_has_captcha(page):
            self.checkpoint("captcha_detected", page_number=page_index + 1)
            reason = "CAPTCHA present — a human must solve it; automation never will."
            logger.info("application %s: CAPTCHA detected on page %d.", self.application_id, page_index + 1)
            if not self._wait_for_human(page_index, reason, lambda: not page_has_captcha(page)):
                return _PageProgress(signature=signature, blocked_reason=reason)
            wait_for_page_settled(page)
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
        # HITL platform: tells the (possibly reused across pages)
        # answer_engine which page it's on right now, so every question it
        # persists to the per-application ledger carries the right
        # `page_number` — see `ApplicationAnswerEngine.current_page_number`.
        if self.answer_engine is not None:
            self.answer_engine.current_page_number = page_index + 1
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

    def _wait_for_human_to_unblock_page(
        self, page: Page, page_index: int, navigation: NavigationOutcome,
    ) -> bool:
        """Pause and ask a human to finish what this run could not, then report
        whether the page became advanceable.

        This is the capability-gap counterpart to the security gates. Those
        pause for a CAPTCHA or an OTP; this pauses for "the form wants
        something I could not supply" — a required field that exhausted its
        retries, a validation error that would not clear, or a repeating
        section (Add Experience / Add Skill / Add License) whose add-then-fill
        pattern this adapter cannot drive.

        Deliberately reuses `_wait_for_human`, so the pause behaves exactly
        like every other one: the same `manual_required` status, the same live
        reason in the UI, the same poll, and a resume on the SAME page with a
        fresh re-read rather than an assumption about what changed.

        The clear condition is the honest one — "are the required fields now
        filled?" — not "did anything on the page change". A human clicking
        around must not be mistaken for the blocker being resolved.
        """
        missing = find_unfilled_required_fields(page)
        details = []
        if navigation.validation_errors:
            details.append("the form reported: " + "; ".join(navigation.validation_errors))
        if missing:
            details.append("still empty: " + ", ".join(missing[:6]))
        elif navigation.click_failed:
            # No empty required field AND the control would not click: usually a
            # widget the adapter cannot operate rather than a missing value.
            details.append(
                "this page has a section Autogram could not complete on its own "
                "(repeating sections like Add Experience or Add Skill need to be added by hand)"
            )
        reason = (
            f"Autogram could not get past page {page_index + 1} on its own"
            + (f" — {'; '.join(details)}" if details else "")
            + ". Please complete what's missing in the automation's browser window; "
              "it will carry on by itself once the page can continue."
        )

        def cleared() -> bool:
            # Only "the required fields are filled" counts. If none were
            # detectable in the first place there is nothing to poll for, so
            # fall back to the navigation control becoming clickable.
            if missing:
                return not find_unfilled_required_fields(page)
            return not find_validation_errors(page)

        return self._wait_for_human(page_index, reason, cleared, page=page)

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
            detected_ats_platform=self.detected_ats_platform,
            confidence=self._aggregate_confidence(all_results),
            screenshot_paths=self._safe_screenshot(page),
            error_log=self._safe_write_error_log(reason),
            pages_completed=page_index + 1,
        )

    # ------------------------------------------------------------------
    # Adapter hooks, guarded
    # ------------------------------------------------------------------
    # Every one of these is overridable per ATS (see `ATSAdapter`'s "Multi-page
    # navigation" section), so a platform-specific override raising must degrade
    # to the generic answer rather than crash a run that is otherwise fine.

    def _wait_for_human(
        self, page_index: int, reason: str, cleared: Callable[[], bool], page: Page | None = None,
    ) -> bool:
        """Pauses this run for a human to resolve `reason` (a CAPTCHA or
        other human-only gate) in this run's own visible browser, polling
        `cleared()` every `HUMAN_WAIT_POLL_INTERVAL_S` up to
        `AUTOMATION_HUMAN_WAIT_TIMEOUT_S`. Returns `True` the moment `cleared()`
        reports the gate is gone — the caller then continues filling THIS SAME
        run, no restart, no new browser. Returns `False` on timeout, at which
        point the caller falls back to exactly today's behavior
        (`manual_required`, browser left open per `should_keep_browser_open`).

        Never attempts to solve, bypass, or circumvent anything itself — the
        only actions here are reporting the wait and re-checking `cleared()`."""
        self.checkpoint("waiting_for_human", page_number=page_index + 1)
        state = LIVE_RUN_STATE.setdefault(self.application_id, {})
        state["status"] = "WAITING_FOR_HUMAN"
        state["reason"] = reason
        if self.on_waiting_for_human is not None:
            try:
                self.on_waiting_for_human(reason)
            except Exception:  # noqa: BLE001 - a broken status callback must never break the wait
                logger.exception("application %s: on_waiting_for_human callback failed — continuing to wait.", self.application_id)

        logger.info(
            "application %s: waiting up to %.0fs for a human to resolve this on page %d: %s",
            self.application_id, self.human_wait_timeout_s, page_index + 1, reason,
        )
        deadline = time.monotonic() + self.human_wait_timeout_s
        try:
            while time.monotonic() < deadline:
                time.sleep(HUMAN_WAIT_POLL_INTERVAL_S)
                # A code the human typed into Autogram's own UI, rather than
                # into this browser window. Tried BEFORE `cleared()` so a code
                # that clears the gate is noticed on this same iteration.
                if page is not None:
                    self._try_deliver_verification_code(page, page_index)
                try:
                    if cleared():
                        logger.info(
                            "application %s: human verification completed on page %d — resuming automatically.",
                            self.application_id, page_index + 1,
                        )
                        self.checkpoint("human_verification_completed", page_number=page_index + 1)
                        state["status"] = "IN_PROGRESS"
                        state.pop("reason", None)
                        return True
                except Exception:  # noqa: BLE001 - a broken poll check must never crash the wait loop
                    logger.exception(
                        "application %s: error while checking whether the human gate cleared — still waiting.",
                        self.application_id,
                    )

            logger.warning(
                "application %s: human verification wait timed out after %.0fs on page %d.",
                self.application_id, self.human_wait_timeout_s, page_index + 1,
            )
            return False
        finally:
            # However this wait ends — resumed, timed out, or raised — never
            # leave an uncollected code sitting in memory.
            verification_channel.discard(self.application_id)

    def _try_deliver_verification_code(self, page: Page, page_index: int) -> None:
        """Type a code the human entered in Autogram into this run's live page.

        Only ever called from inside the human-gate wait, so a code can never be
        typed into a page that is not actually asking for one.

        SECRET HANDLING, identical in spirit to the autonomous path's
        `_try_consume_pending_secret`:

        * the value is popped (consumed once) and held in a local only;
        * it is never logged, never checkpointed, never written to the database,
          and never attached to `LIVE_RUN_STATE` — which `GET /applications/{id}/live`
          returns to the browser;
        * failure is reported as a fact ("could not be entered"), never with the
          value.

        Best-effort by design: if the field cannot be found or filled, the wait
        simply continues, and the human can still clear the gate in the browser
        window exactly as before. Nothing about the previous behaviour is
        removed — this only adds a second way in.
        """
        code = verification_channel.take(self.application_id)
        if code is None:
            return
        try:
            if not self._fill_verification_code(page, code):
                logger.warning(
                    "application %s: a verification code was supplied but no code field is on the page.",
                    self.application_id,
                )
                return
            self.checkpoint("verification_code_entered", page_number=page_index + 1)
            logger.info("application %s: entered a human-supplied verification code.", self.application_id)
            # A new attempt supersedes any previous rejection notice, so the UI
            # does not keep showing "that code was not accepted" while a fresh
            # one is being checked.
            state = LIVE_RUN_STATE.setdefault(self.application_id, {})
            state.pop("verification_rejected", None)
            state["verification_submitted_at"] = time.time()
            # Submitting is best-effort and deliberately separate: many forms
            # auto-advance on the last digit, and a wrong guess at the submit
            # control must not undo a correctly-filled code.
            self._try_submit_verification(page, page_index)
            self._note_verification_outcome(page)
        except Exception:  # noqa: BLE001 - never let this break the wait
            logger.exception(
                "application %s: the supplied verification code could not be entered.", self.application_id,
            )
        finally:
            # Drop the local reference immediately; the module-level slot was
            # already emptied by `take`.
            code = None


    def _fill_verification_code(self, page: Page, code: str) -> bool:
        """Type `code` into the page's verification input(s). Returns False if
        there is nothing to type into.

        Handles BOTH shapes real sites use, because the common one is not the
        simple one:

        * **one input** — `fill()` the whole code.
        * **one box per digit** — the "six circles" layout (American Express
          uses exactly this). Each box is `maxlength=1`, so filling the first
          with the whole code silently stores a single character and the form
          rejects it. Observed on a real Amex application: the code has to be
          distributed one character per box.

        Split fields are typed with `press_sequentially` rather than `fill`,
        because these components are almost always JS-driven — they listen for
        key events to auto-advance focus and to enable the Verify button, and a
        programmatic `fill` that skips those events leaves the button disabled
        with the boxes looking correctly populated.
        """
        fields = [f for f in page.locator(VERIFICATION_CODE_INPUT_SELECTOR).all() if f.is_visible()]
        if not fields:
            return False

        if len(fields) == 1:
            single = fields[0]
            # A lone box that only accepts one character is still a per-digit
            # layout whose siblings this selector did not match; typing the
            # whole code would be silently truncated.
            if (single.get_attribute("maxlength") or "") == "1" and len(code) > 1:
                single.press_sequentially(code, delay=60)
            else:
                single.fill(code)
            return True

        # Per-digit layout: one character each, in document order.
        for box, character in zip(fields, code):
            box.click()
            box.press_sequentially(character, delay=60)
        logger.info(
            "application %s: entered the verification code across %d input boxes.",
            self.application_id, min(len(fields), len(code)),
        )
        return True

    def _note_verification_outcome(self, page: Page) -> None:
        """Tell the user whether the code they just sent was accepted.

        Without this the UI is silent after a rejected code: the run stays
        paused and looks identical to "still waiting for you to type one", so a
        user who mistyped has no idea anything happened. They then sit waiting
        for automation that is itself waiting for them.

        Checked AFTER a short settle, because a real form navigates or re-renders
        on submit and reading immediately would report the pre-submit page. This
        is an observation only — it never changes control flow. Whether the run
        actually resumes is decided solely by the wait loop's own `cleared()`
        check, which re-reads the live page; a wrong reading here can therefore
        mislabel a message but can never resume a run that is still blocked.

        Records a BOOLEAN, never anything derived from the code itself.
        """
        try:
            wait_for_page_settled(page)
            still_blocked = find_human_gate(page) is not None
        except Exception:  # noqa: BLE001 - an observation must never break the wait
            return
        state = LIVE_RUN_STATE.setdefault(self.application_id, {})
        if still_blocked:
            state["verification_rejected"] = True
            self.checkpoint("verification_code_rejected")
            logger.info(
                "application %s: the verification code was not accepted — still waiting.",
                self.application_id,
            )
        else:
            state.pop("verification_rejected", None)

    def _try_submit_verification(self, page: Page, page_index: int) -> None:
        """Click the verify/continue control next to a just-filled code field.

        Separate from filling so that a form which auto-submits — or one whose
        button this cannot find — still benefits from the code having been
        entered. The human can press the button themselves in that case.
        """
        for name in ("verify", "submit", "continue", "confirm", "next"):
            try:
                button = page.get_by_role("button", name=re.compile(name, re.I)).first
                if button.count() and button.is_visible():
                    button.click()
                    self.checkpoint("verification_code_submitted", page_number=page_index + 1)
                    return
            except Exception:  # noqa: BLE001 - try the next candidate
                continue
        logger.info(
            "application %s: code entered but no verify button was found — the form may auto-submit.",
            self.application_id,
        )

    def _safe_detect_job_posting_metadata(self, page: Page) -> tuple[str | None, str | None]:
        try:
            return find_job_posting_title_and_company(page)
        except Exception:  # noqa: BLE001 - best-effort metadata read must never break a run
            logger.debug("application %s: could not read job posting metadata from the page.", self.application_id)
            return None, None

    def _resolve_adapter_from_listing_page(self, page: Page) -> tuple[type[ATSAdapter], Page] | None:
        """"Apply from Job Link": called whenever there's no REGISTERED
        adapter to hand this run to outright — either pre-flight detection
        came back as `FALLBACK_ATS` (nothing recognizable at all), or it
        confidently named a real platform this deployment has no adapter
        for yet (e.g. `oracle_hcm`, `taleo` — see `ats/registry.py`). Either
        way this page might be a job LISTING/description page sitting in
        front of a supported ATS, reached by clicking its own Apply/Apply
        Now/Start Application control, rather than itself being the form.

        If, after that, still no REGISTERED adapter resolves, this falls
        back to `GenericAdapter` rather than giving up — that is what makes
        "apply from any job link" actually mean any link: an unrecognized
        platform still gets the same label/name/placeholder-driven fill
        every real adapter's `answer_questions()` also just delegates to
        (see `GenericAdapter`'s module docstring). It is never mistaken for
        a fully-automated run: every fallback goes through
        `_fall_back_to_generic_adapter`, which forces `self.ats_platform` to
        `"custom"` — `GenericAdapter.detect()`'s low, constant confidence and
        `"custom"`'s absence from `PUBLIC_ATS_PLATFORMS` then mean a run
        through it can only end at `NEEDS_REVIEW` or `COPILOT_REVIEW`, never
        `AUTO_SUBMIT` — a human always reviews it. (Without that
        reassignment, a page originally detected as a confident-but-
        unregistered `PUBLIC_ATS_PLATFORMS` member — "smartrecruiters" or
        "ashby" today — would still carry that platform name into
        `decide_action` and could reach AUTO_SUBMIT on a well-filled simple
        form despite GenericAdapter, not a vetted adapter, having done the
        filling; see `_fall_back_to_generic_adapter`'s own docstring.)

        Only returns `None` (never bypassing anything) when a human-only
        gate's wait times out — there is a real, unresolved block on this
        page in that case, not merely an unrecognized one, and GenericAdapter
        would have nothing fillable to do about it anyway (see
        `_process_page`'s own human-gate/CAPTCHA checks, which run again once
        an adapter is constructed).

        CAPTCHA/human-gate checks happen here FIRST, before ever looking for
        an Apply button — a listing page can be gated exactly like a form can
        (§9/§5: never bypassed, only waited out)."""
        if page_has_captcha(page):
            self.checkpoint("captcha_detected", page_number=1)
            reason = "CAPTCHA present on the job listing page — a human must solve it; automation never will."
            if not self._wait_for_human(0, reason, lambda: not page_has_captcha(page)):
                return None

        human_gate = find_human_gate(page)
        if human_gate is not None:
            reason = f"Human intervention required before this job page can be opened: {human_gate}."
            self.checkpoint("human_gate_detected", page_number=1)
            if not self._wait_for_human(0, reason, lambda: find_human_gate(page) is None, page=page):
                return None

        resolved = self._detect_supported_ats(page)
        if resolved is not None:
            self.checkpoint("ats_detected_on_live_page")
            return resolved[0], page

        apply_button = find_apply_entry_button(page)
        if apply_button is None:
            logger.info(
                "application %s: no supported ATS detected and no Apply/Apply Now/Start Application "
                "control found on %s — falling back to the generic adapter on this page.",
                self.application_id, page.url,
            )
            return self._fall_back_to_generic_adapter(page)

        self.checkpoint("apply_entry_button_clicked")
        target_page = self._click_apply_and_follow(page, apply_button)
        if target_page is None:
            logger.info(
                "application %s: Apply control could not be followed to a new page — "
                "falling back to the generic adapter on the original page.", self.application_id,
            )
            return self._fall_back_to_generic_adapter(page)

        wait_for_form_ready(target_page)
        resolved = self._detect_supported_ats(target_page)
        if resolved is None:
            logger.info(
                "application %s: clicked Apply but the resulting page (%s) still isn't a "
                "recognized/supported ATS — falling back to the generic adapter there.",
                self.application_id, target_page.url,
            )
            return self._fall_back_to_generic_adapter(target_page)
        self.checkpoint("ats_detected_after_apply_click")
        return resolved[0], target_page

    def _fall_back_to_generic_adapter(self, page: Page) -> tuple[type[ATSAdapter], Page]:
        """The single place `_resolve_adapter_from_listing_page` hands a run
        to `GenericAdapter`. This is a SAFETY-CRITICAL assignment, not
        bookkeeping: `decide_action` (module-level, above) reads
        `self.ats_platform` to decide whether AUTO_SUBMIT is even on the
        table, via `ats_platform in PUBLIC_ATS_PLATFORMS`. Two of that set's
        members — "smartrecruiters" and "ashby" — have no adapter registered
        in `ats/registry.py` yet, so a run that started life confidently
        detected as one of THOSE would, without this reassignment, still
        carry that platform name all the way to `decide_action` even though
        GenericAdapter — not a vetted, platform-specific adapter — is what
        actually filled the form. Combined with autopilot enabled and a
        simple form the generic label/name sweep fills completely (trivially
        >= AUTO_SUBMIT_CONFIDENCE_THRESHOLD), that would silently AUTO_SUBMIT
        an application nobody ever reviewed — exactly the outcome this
        module's docstrings claim can't happen. Forcing `self.ats_platform`
        to `GenericAdapter.name` ("custom", which is never a member of
        `PUBLIC_ATS_PLATFORMS`) here closes that gap for every caller, rather
        than relying on each of the three call sites to remember it.

        `self.detected_ats_platform` — the original pre-flight guess — is
        deliberately left untouched, so observability/audit can still show
        what was actually detected alongside what actually ran (see
        `ApplicationRunResult.detected_ats_platform`)."""
        self.ats_platform = GenericAdapter.name
        self.checkpoint("generic_adapter_fallback")
        return GenericAdapter, page

    def _detect_supported_ats(self, page: Page) -> tuple[type[ATSAdapter], str] | None:
        """Re-runs full (tier-1 + tier-2) `ATSDetector` detection against the
        CURRENT live page — tier 2's DOM-fingerprint check needs a real page
        anyway (see `ATSDetector.detect_from_page`), so this costs nothing
        extra over the pre-flight, page-less check the caller already ran.
        Updates `self.ats_platform` and returns `(adapter_cls, ats_platform)`
        on a confident, supported match; `None` otherwise (still `FALLBACK_ATS`,
        or a real platform this deployment has no adapter for yet)."""
        detection = ATSDetector.detect(page.url, page)
        if detection["ats"] == FALLBACK_ATS:
            return None
        adapter_cls = get_adapter_class(detection["ats"])
        if adapter_cls is None:
            return None
        self.ats_platform = detection["ats"]
        return adapter_cls, detection["ats"]

    def _click_apply_and_follow(self, page: Page, apply_button: Locator) -> Page | None:
        """Clicks the Apply control and returns whichever page ends up hosting
        the real form — a NEW TAB if the click opened one (common: many job
        postings' Apply links use `target="_blank"`), or the SAME page if it
        navigated/re-rendered in place. `None` if the click didn't land or
        nothing changed at all.

        Reuses `advance_to_next_page` (the same click-and-PROVE-it-moved
        logic multi-page navigation uses) for the same-page case, rather than
        a bare `.click()` — an Apply button is exactly as likely to be
        covered by a cookie banner or momentarily unclickable as a form's own
        Next button."""
        before = capture_page_signature(page)
        context = page.context
        new_page: Page | None = None
        outcome: NavigationOutcome | None = None
        try:
            with context.expect_page(timeout=6_000) as new_page_info:
                outcome = advance_to_next_page(page, apply_button, before=before)
            new_page = new_page_info.value
        except PlaywrightError:
            pass  # no new tab opened within the timeout — same-page navigation, if any, is in `outcome`

        if new_page is not None:
            try:
                new_page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except PlaywrightError:
                pass
            self.browser_manager.adopt_page(new_page)
            logger.info(
                "application %s: Apply control opened a new tab (%s).", self.application_id, new_page.url,
            )
            return new_page

        if outcome is not None and outcome.advanced:
            logger.info(
                "application %s: Apply control navigated in place — %s", self.application_id, outcome.reason,
            )
            return page

        logger.info("application %s: clicked the Apply control but nothing navigated.", self.application_id)
        return None

    def _kill_switch_engaged(self) -> bool:
        """Fail CLOSED: if there's no callback at all, the kill switch simply
        isn't wired up for this caller (tests, or a caller that doesn't need
        it) and autopilot proceeds as before. If a callback WAS given but it
        raises (DB unreachable, etc.), that is treated as "engaged" — per
        PHASE2_ARCHITECTURE.md Initiative 3, the explicit product requirement
        is "never auto-submit without permission," so an unknown kill-switch
        state must never be read as permission."""
        if self.is_kill_switch_engaged is None:
            return False
        try:
            return bool(self.is_kill_switch_engaged())
        except Exception:  # noqa: BLE001
            logger.exception(
                "application %s: kill switch check failed — failing closed (treating as engaged).",
                self.application_id,
            )
            return True

    def _resolve_trust_level(self) -> str:
        """Fail SAFE (not closed like the kill switch, since there's no
        "engaged" state to fall back to): no callback, an unrecognized
        value, or a raising callback all resolve to `FULL_MANUAL_REVIEW` —
        the one value that can never enable auto-submit — so a broken or
        unwired trust-level check degrades to today's always-review
        behavior, never to a silent opt-in.

        Deliberately optional rather than required: the one production call
        site (`app/api/applications.py::_run_application`) always wires this;
        every other construction site is a test exercising something else
        entirely (OTP entry, an unresolvable blocker, ...) that never reaches
        this method. A required parameter would force ~25 unrelated test
        call sites to supply a value they don't care about, for no real
        safety gain over this debug line — see `decide_action`'s own
        docstring for why `FULL_MANUAL_REVIEW` is the correct universal
        fallback, not just a placeholder."""
        if self.resolve_trust_level is None:
            logger.debug(
                "application %s: no resolve_trust_level callback provided — defaulting to FULL_MANUAL_REVIEW.",
                self.application_id,
            )
            return "FULL_MANUAL_REVIEW"
        try:
            level = self.resolve_trust_level()
        except Exception:  # noqa: BLE001
            logger.exception(
                "application %s: trust level check failed — falling back to FULL_MANUAL_REVIEW.",
                self.application_id,
            )
            return "FULL_MANUAL_REVIEW"
        return level if level in VALID_TRUST_LEVELS else "FULL_MANUAL_REVIEW"

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
            detected_ats_platform=self.detected_ats_platform,
            confidence=0.0,
            screenshot_paths=self._safe_screenshot(page),
            error_log=self._safe_write_error_log(repr(error)),
        )
