"""
AutonomousAgentLoop — the observe -> decide -> act orchestrator.

One instance runs one `AutonomousTask` from start to a terminal/paused state.
It is driven by `runner.py` off the request thread (see that module for why
this codebase doesn't yet have a real Celery broker to hand this to, and
what the TODO migration path looks like).

Browser-session preservation across a human-intervention pause is handled by
simply NOT closing the `BrowserManager`/`Page` while paused — under the
default `AUTOMATION_BROWSER_MODE=cdp`, that page is a tab in the user's own,
already-authenticated Chrome (see `automation/browser/chrome_attach.py`), so
"preserve the session" reduces to "don't call `.close()`", not "serialize
cookies and pray". The loop simply blocks (via `runner.py`'s per-task
resume/cancel signalling) until the human acts, then re-observes the SAME
page — never a new tab, never a re-navigation — before deciding again. This
only survives within this process's lifetime; see `AUTONOMOUS_AGENT.md` for
what a process restart means for an in-flight paused task (the DB status
survives; the literal open tab does not).
"""

from __future__ import annotations

import base64
import logging
import threading
from dataclasses import dataclass

from playwright.sync_api import Page

from app.services import audit_log_repository as audit_log_repo
from app.services import autonomous_task_repository as task_repo
from app.services import chat_repository
from app.services.event_bus import publish_task_event
from app.services import human_interaction_repository as human_interaction_repo
from app.services.autonomous_task_repository import TerminalTaskError
from automation.agents.autonomous.actions import ActionResult, AgentAction, validate_action_grounding
from automation.agents.autonomous.decision import Decision, DecisionError, decide_next_step, normalize_intervention_type
from automation.agents.autonomous.executor import ActionExecutor, is_verification_code_field_name
from automation.agents.autonomous.observer import (
    CONFIRMATION_PHRASES,
    PageState,
    compute_page_completion,
    field_identity,
    observe_page,
)
from automation.applications.page_navigator import PageSignature, capture_page_signature
from automation.ats.detector import ATSDetector
from automation.browser.browser_manager import BrowserAutomationError, BrowserManager
from automation.interfaces import automation_db_session

logger = logging.getLogger(__name__)

#: Hard ceiling on decide/act iterations for one task run, independent of
#: any human pauses (a pause doesn't consume this budget — see `run()`).
#: Exists purely so a model stuck in a loop it can't recognize as stuck
#: (e.g. repeatedly clicking something that never changes the page) fails
#: the task instead of running forever and burning LLM calls.
MAX_ITERATIONS_PER_RESUME = 80

#: Spec §16: a field that has failed this many real (non-blocked) attempts is
#: never retried automatically again for the rest of this task — persisted on
#: `AutonomousTask.field_attempt_ledger`, keyed by `observer.field_identity`,
#: so it survives a resume/process-restart unlike `TaskHandle.unverified_streak`
#: (the same-process fast path, kept alongside this for cheap same-iteration
#: checks). Same value as `field_handlers.py::DEFAULT_MAX_ATTEMPTS`, for
#: consistency with the deterministic engine's own retry ceiling.
MAX_FIELD_ATTEMPTS = 3

#: User-facing copy per closed request type (`VALID_HUMAN_REQUEST_TYPES`) —
#: used whenever a pause doesn't already carry its own message (the
#: deterministic Layers 1/2 detector never writes prose; the LLM's own
#: `intervention.message`, Layer 3, is used as-is when present).
_REQUEST_TYPE_MESSAGES = {
    "OTP_REQUIRED": "Verification required. The application website requires a one-time verification code to continue.",
    "MFA_REQUIRED": "Two-factor authentication required. Please provide the code from your authenticator app or verification method to continue.",
    "CAPTCHA_REQUIRED": "Human action is required to continue. The website is asking you to complete a verification challenge. "
                         "Please complete it in the browser — Autogram will automatically continue once the page is ready.",
    "LOGIN_REQUIRED": "Sign-in required. Please log in to the website in the open browser tab, then let Autogram know you're ready to continue.",
    "USER_CONFIRMATION_REQUIRED": "Your confirmation is needed before Autogram continues with this step.",
    "ANSWER_REQUIRED": "Autogram needs some information from you to continue this application.",
    "MANUAL_ACTION_REQUIRED": "Please complete the next step manually in the browser, then let Autogram know you're ready to continue.",
    "FILE_UPLOAD_REQUIRED": "This application needs a document Autogram doesn't have a local copy of. Please attach it yourself in the open browser tab, then let Autogram continue.",
    "UNKNOWN_BLOCKER": "Autogram is unsure how to safely continue this application and needs your help.",
}

#: Request types whose pause is only worth keeping for as long as the thing
#: being waited on stays valid. A verification code has a real, short lifetime
#: on the website's side, so letting a stale request be answered would just
#: type an already-dead code into the form.
#:
#: EVERY OTHER type is created with NO expiry, deliberately. Signing in,
#: clearing an anti-bot challenge, attaching a file by hand, or answering a
#: question the agent couldn't answer are all things that legitimately take a
#: person minutes — reading email, finding a password manager, walking away
#: and coming back. They were previously capped at the same 10 minutes as an
#: OTP, which meant `POST /human-requests/{id}/respond` started returning 410
#: on a pause that was still perfectly actionable, stranding the task. (The
#: legacy `/resume` + `/answer` routes never checked expiry, so the two
#: response paths also disagreed with each other.)
_SHORT_LIVED_REQUEST_TYPES = frozenset({"OTP_REQUIRED", "MFA_REQUIRED"})


@dataclass
class TaskHandle:
    """The live, in-process resources for one running/paused task — owned by
    `runner.py`'s registry, injected into the loop. Not persisted; this is
    exactly the state that does NOT survive a process restart.

    `pending_secret` is the ONLY place an OTP/MFA code value ever lives on
    the server side: set by `runner.py::deliver_secret` (called from
    `app/api/human_interaction.py`'s `/respond` route) and consumed exactly
    once by `AutonomousAgentLoop._try_consume_pending_secret`, which clears
    it immediately after reading — never written to the DB, a log line, or
    an LLM prompt. See AUTONOMOUS_AGENT.md's OTP section."""

    resume_event: threading.Event
    cancel_requested: threading.Event
    browser_manager: BrowserManager | None = None
    page: Page | None = None
    pending_secret: dict | None = None  # {"request_id": str, "value": str} | None
    #: Set right after a delivered code is filled/submitted deterministically
    #: (`_try_consume_pending_secret`), to the request_id that was just acted
    #: on. Read and cleared exactly once, on the NEXT fresh observation
    #: (`_note_verification_outcome`), purely to label the audit trail
    #: accurately as "accepted" or "rejected" — never affects control flow.
    awaiting_verification_result: str | None = None

    # ---- spec §7/§16/§42: in-process only, never persisted, same lifetime
    # as MAX_ITERATIONS_PER_RESUME ----
    #: The page's structural signature as of the last iteration, so the loop
    #: can tell "did anything actually change since last time" without a
    #: second browser round-trip (`page_navigator.py::capture_page_signature`,
    #: reused as-is — see `_loop_body`).
    last_page_signature: PageSignature | None = None
    #: Whether the LAST executed action (across all `_handle_execute_action`
    #: outcomes, including a blocked one) was confirmed to have an effect.
    #: Starts True so the very first iteration never reads as "already
    #: stalled" before anything has been attempted.
    last_action_verified: bool = True
    #: Consecutive-failure tracking for "the same element ref didn't visibly
    #: change N times in a row" (spec §16 — never repeat a failed field
    #: forever). Reset to (None, 0) whenever a different ref is attempted or
    #: the current one succeeds.
    last_unverified_ref: int | None = None
    unverified_streak: int = 0


class AutonomousAgentLoop:
    def __init__(self, task_id: str, handle: TaskHandle):
        self.task_id = task_id
        self.handle = handle

    # ------------------------------------------------------------------

    def run(self) -> None:
        with automation_db_session() as db:
            task = task_repo.get_by_id(db, self.task_id)
            if task is None:
                logger.error("AutonomousAgentLoop: task %s not found.", self.task_id)
                return

            self._record_audit(db, task, "automation_started", {"status_at_start": task.current_status})
            self._record_chat_milestone(
                db, task, "Autogram started analyzing the job page.",
                {"event": "APPLICATION_STARTED"},
            )
            publish_task_event(task.task_id, "APPLICATION_STARTED")
            try:
                self._ensure_browser(task)
                task_repo.set_status(db, task, "RUNNING")
                self._loop_body(db, task)
            except TerminalTaskError as e:
                # The task reached a terminal status (most likely: a
                # cancellation landed concurrently with this loop iteration)
                # before this iteration's own status write could land. This
                # is NOT a failure — the task already has an honest final
                # status; overwriting it with FAILED would be the actual bug.
                logger.info("Task %s: loop stopped — %s", self.task_id, e)
                self._close_browser()
            except BrowserAutomationError as e:
                logger.exception("Task %s: browser error.", self.task_id)
                self._safe_mark_failed(db, task, f"Browser error: {e}", reason="browser_error")
                self._close_browser()
            except Exception as e:  # noqa: BLE001 — last-resort: never leave a task stuck in RUNNING
                logger.exception("Task %s: unexpected error.", self.task_id)
                self._safe_mark_failed(db, task, f"Unexpected error: {e}", reason="unexpected_error")
                self._close_browser()

    def _safe_mark_failed(self, db, task, message: str, *, reason: str) -> None:
        """Records the failure as robustly as possible — this is the last
        thing standing between a crashed run and a task stuck in RUNNING
        forever, so it must not itself throw.

        Two hazards it handles:

        1. `mark_failed` raises `TerminalTaskError` when the task already
           reached a terminal status (e.g. a concurrent cancellation won the
           race). Overwriting a real CANCELLED/COMPLETED with FAILED would be
           the actual bug, so that case is simply logged.
        2. The session may already be poisoned. If the original error was a
           failed flush — a Postgres **deadlock** between this loop thread and
           an API thread touching `autonomous_tasks`/`human_interaction_requests`
           in the opposite order is the realistic case — then every further
           statement on this session raises `PendingRollbackError` and the
           task would never be marked failed at all. Observed exactly once
           during a heavily-contended real-browser E2E run. So: roll the
           session back first, and never let a bookkeeping failure escape."""
        try:
            db.rollback()  # clears a poisoned transaction; harmless otherwise
        except Exception:  # noqa: BLE001
            logger.exception("Task %s: could not roll back before recording failure.", self.task_id)
        try:
            task_repo.mark_failed(db, task, message)
            self._record_audit(db, task, "automation_failed", {"reason": reason})
            self._record_chat_milestone(
                db, task, f"Automation stopped: {message}",
                {"event": "APPLICATION_FAILED", "reason": reason},
            )
            publish_task_event(task.task_id, "APPLICATION_FAILED", reason=reason)
        except TerminalTaskError:
            logger.info("Task %s: already terminal (%s) — not overwriting with FAILED.", self.task_id, task.current_status)
        except Exception:  # noqa: BLE001 — never mask the original error with a bookkeeping one
            logger.exception("Task %s: could not record the failure (%s).", self.task_id, reason)

    def _ensure_browser(self, task) -> None:
        if self.handle.page is not None:
            return  # already open (a resume) — reuse the SAME tab/session
        manager = BrowserManager(user_id=task.user_id, ats_platform="autonomous_agent")
        manager.launch_context()
        page = manager.new_page()
        navigation = ActionExecutor(page, auto_submit_approved=False).execute(
            AgentAction(action_type="navigate", url=task.job_url)
        )
        if not navigation.success or not navigation.verified:
            manager.close()
            raise BrowserAutomationError(navigation.detail)
        self.handle.browser_manager = manager
        self.handle.page = page

    def _close_browser(self) -> None:
        if self.handle.browser_manager is not None:
            self.handle.browser_manager.close()
        self.handle.browser_manager = None
        self.handle.page = None

    def _ats_hint(self) -> str | None:
        try:
            result = ATSDetector.detect_from_url(self.handle.page.url)
            return result.ats if result and result.ats != "custom" else None
        except Exception:  # noqa: BLE001 — a hint failing is never fatal
            return None

    def _safe_page_signature(self) -> PageSignature | None:
        """`capture_page_signature`, degrading to `None` (never raising) on
        any failure — a page mid-navigation, or a test double that doesn't
        implement every real-Playwright method, must never crash the whole
        task; it just means this iteration can't be compared to the last."""
        try:
            return capture_page_signature(self.handle.page)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------

    def _loop_body(self, db, task) -> None:
        iterations = 0
        while True:
            if self.handle.cancel_requested.is_set():
                task_repo.cancel_task(db, task)
                self._record_audit(db, task, "automation_cancelled", {})
                self._close_browser()
                return

            iterations += 1
            if iterations > MAX_ITERATIONS_PER_RESUME:
                task_repo.mark_failed(
                    db, task,
                    "Exceeded the maximum number of automated steps without reaching a terminal "
                    "state — requesting human review rather than continuing indefinitely.",
                )
                self._record_audit(db, task, "automation_failed", {"reason": "max_iterations_exceeded"})
                self._record_chat_milestone(
                    db, task, "Automation stopped after reaching its safe action limit.",
                    {"event": "APPLICATION_FAILED", "reason": "max_iterations_exceeded"},
                )
                publish_task_event(task.task_id, "APPLICATION_FAILED", reason="max_iterations_exceeded")
                return  # browser left open deliberately: a human may want to look at where it got stuck

            db.refresh(task)  # pick up anything an /answer or /approve call wrote concurrently
            if task.current_status in ("RESUMING", "WAITING_FOR_HUMAN", "WAITING_FOR_APPROVAL"):
                self._record_audit(db, task, "automation_resuming", {"from_status": task.current_status})
                task_repo.set_status(db, task, "RUNNING")

            # A human just submitted an OTP/MFA code via POST /human-requests/{id}/respond
            # (`app/api/human_interaction.py`, `runner.py::deliver_secret`). Handle it
            # deterministically — filling/submitting a verification code is never routed
            # through the LLM decision step, so the raw code never enters a prompt, a log
            # line, or `action_history`. See `_try_consume_pending_secret`.
            secret_outcome = self._try_consume_pending_secret(db, task)
            if secret_outcome is not None:
                if secret_outcome is False:
                    self._wait_for_resume()
                continue

            page_state = observe_page(self.handle.page, ats_hint=self._ats_hint())
            current_signature = self._safe_page_signature()
            page_state.page_signature = repr(current_signature) if current_signature is not None else ""
            # Mirrors the task's own (authoritative) status rather than
            # running a second, driftable classifier for something the DB
            # already tracks correctly — see `observer.py::classify_page_type`
            # for the actual (independent) page_type classification.
            page_state.workflow_state = task.current_status
            task_repo.update_browser_state(db, task, page_state.as_dict())
            # Labels the audit trail as "accepted"/"rejected" for a code that
            # was JUST submitted last iteration — never affects control flow.
            self._note_verification_outcome(db, task, page_state)

            # Layers 1/2 (deterministic, non-LLM) human-blocker detection —
            # see `observer.py::detect_blocker`. Only fall through to the LLM
            # decision step (Layer 3) when this found nothing confident.
            if page_state.blocker_hint is not None:
                blocker = page_state.blocker_hint
                self._record_audit(db, task, "blocker_detected", {"request_type": blocker["request_type"]})
                self._pause_for_human(
                    db, task,
                    {
                        "type": blocker["request_type"],
                        "reason": blocker.get("reason"),
                        "message": self._message_for_request_type(blocker["request_type"], blocker.get("masked_destination")),
                        "information_required": None,
                    },
                    safe_metadata={
                        "masked_destination": blocker.get("masked_destination"),
                        "otp_field_ref": blocker.get("otp_field_ref"),
                        "submit_ref": blocker.get("submit_ref"),
                        "detection_layer": "deterministic",
                    },
                )
                if current_signature is not None:
                    self.handle.last_page_signature = current_signature
                self._wait_for_resume()
                continue

            # Spec §42: if the page is byte-for-byte structurally identical to
            # last iteration AND the last action already came back unverified
            # (no visible effect), a fresh LLM decision call would just
            # re-analyze the same page for the same answer. Escalate instead
            # of spending it — cheap, deterministic, and conservative: this
            # only fires when we already KNOW nothing changed, never a guess
            # (a signature we couldn't read this time is treated as "unknown",
            # never as "unchanged").
            if (
                current_signature is not None
                and self.handle.last_page_signature is not None
                and not self.handle.last_action_verified
                and not current_signature.differs_from(self.handle.last_page_signature)
            ):
                self.handle.last_page_signature = current_signature
                self._record_audit(db, task, "blocker_detected", {
                    "request_type": "UNKNOWN_BLOCKER", "detection_layer": "stalled_page_signature",
                })
                if not self._escalate_with_vision(db, task, page_state, {
                    "type": "UNKNOWN_BLOCKER",
                    "reason": "The page has not changed since the last action, which also had no visible effect.",
                    "message": "Autogram's last action didn't seem to change anything on this page, and stopped "
                               "rather than keep trying blindly. Please check the browser tab and let it know how "
                               "to continue.",
                    "information_required": None,
                }, detection_layer="stalled_page_signature"):
                    self._wait_for_resume()
                continue
            if current_signature is not None:
                self.handle.last_page_signature = current_signature

            try:
                decision = decide_next_step(
                    job_url=task.job_url,
                    original_objective=task.original_objective,
                    resume_text=(task.candidate_profile or {}).get("resume_text", ""),
                    parsed_resume=(task.candidate_profile or {}).get("parsed_resume"),
                    profile=(task.candidate_profile or {}).get("profile"),
                    confirmed_answers=task.confirmed_answers or {},
                    page_state=page_state,
                    action_history=task.action_history or [],
                    uploaded_documents=task.uploaded_documents or [],
                    auto_submit_approved=bool(task.auto_submit_approved),
                )
            except DecisionError as e:
                logger.warning("Task %s: decision step failed (%s) — requesting human intervention.", self.task_id, e)
                self._pause_for_human(db, task, {
                    "type": "other",
                    "reason": f"The agent's decision step failed: {e}",
                    "message": "The automated agent hit an internal error reading this page. "
                               "Please check the browser tab and let it know how to continue, or cancel the task.",
                    "information_required": None,
                })
                self._wait_for_resume()
                continue

            if decision.decision_type == "EXECUTE_ACTION":
                if not self._handle_execute_action(db, task, page_state, decision):
                    self._wait_for_resume()
                continue

            if decision.decision_type == "REQUEST_HUMAN_INTERVENTION":
                if not self._escalate_with_vision(db, task, page_state, decision.intervention, detection_layer="llm_uncertain"):
                    self._wait_for_resume()
                continue

            if decision.decision_type == "APPLICATION_READY_FOR_SUBMISSION":
                task_repo.mark_ready_for_approval(db, task, {
                    "decision": decision.decision_type, "evidence": decision.evidence,
                })
                self._record_chat_milestone(
                    db, task, "The application is ready for final review and submission approval.",
                    {"event": "APPLICATION_READY_FOR_SUBMISSION"},
                )
                publish_task_event(task.task_id, "APPLICATION_READY_FOR_SUBMISSION")
                self._wait_for_resume()
                continue

            if decision.decision_type == "TASK_COMPLETED":
                if not self._page_shows_confirmation(page_state):
                    logger.warning(
                        "Task %s: LLM reported TASK_COMPLETED but no confirmation text was found — "
                        "treating as ready-for-submission instead of trusting the claim.",
                        self.task_id,
                    )
                    task_repo.mark_ready_for_approval(db, task, {
                        "decision": "APPLICATION_READY_FOR_SUBMISSION",
                        "evidence": f"(Downgraded from an unverified TASK_COMPLETED claim) {decision.evidence}",
                    })
                    self._wait_for_resume()
                    continue
                task_repo.mark_completed(db, task, {"decision": decision.decision_type, "evidence": decision.evidence})
                self._record_audit(db, task, "automation_completed", {"evidence": decision.evidence})
                self._record_chat_milestone(
                    db, task, "The application workflow completed successfully.",
                    {"event": "APPLICATION_SUBMITTED"},
                )
                publish_task_event(task.task_id, "APPLICATION_SUBMITTED")
                self._close_browser()
                return

            if decision.decision_type == "TASK_FAILED":
                task_repo.mark_failed(db, task, decision.evidence or "The agent could not complete this task.")
                self._record_audit(db, task, "automation_failed", {"reason": "task_failed", "evidence": decision.evidence})
                self._record_chat_milestone(
                    db, task, f"Automation stopped: {decision.evidence or 'The application could not be completed.'}",
                    {"event": "APPLICATION_FAILED", "reason": "task_failed"},
                )
                publish_task_event(task.task_id, "APPLICATION_FAILED", reason="task_failed")
                self._close_browser()
                return

    def _page_shows_confirmation(self, page_state: PageState) -> bool:
        haystack = f"{page_state.title} {page_state.visible_text}".lower()
        return any(phrase in haystack for phrase in CONFIRMATION_PHRASES)

    def _handle_execute_action(
        self, db, task, page_state: PageState, decision: Decision, *, recover_grounding: bool = True,
    ) -> bool:
        """Returns True if the loop should keep going immediately, False if
        it paused for human input and the caller should wait."""
        action = decision.action
        element_name = None
        match = None
        if action.element_ref is not None:
            match = next((e for e in page_state.elements if e.ref == action.element_ref), None)
            element_name = match.name if match else None

        grounding = validate_action_grounding(
            action,
            page_state,
            canonical_urls=(task.job_url, page_state.url),
        )
        if not grounding.grounded:
            result = ActionResult(
                False, action.action_type,
                f"Rejected ungrounded action: {grounding.reason}",
                blocked_reason="action_not_grounded", verified=False,
                result_code="ACTION_REJECTED", postcondition="action was not dispatched",
            )
            self.handle.last_action_verified = False
            task_repo.append_action(db, task, {
                "action_type": action.action_type, "element_ref": action.element_ref,
                "element_name": element_name, "value": None, "url": action.url,
                "reasoning": decision.reasoning, **result.as_dict(),
            })
            self._record_audit(db, task, "action_rejected", {
                "reason": "action_not_grounded",
                "preferred_element_ref": grounding.preferred_element_ref,
            })
            self._record_chat_action(db, task, action, element_name, result)

            if recover_grounding:
                vision_decision, screenshot = self._attempt_vision_assisted_decision(task, page_state)
                if vision_decision is not None and vision_decision.decision_type == "EXECUTE_ACTION":
                    second_grounding = validate_action_grounding(
                        vision_decision.action,
                        page_state,
                        canonical_urls=(task.job_url, page_state.url),
                    )
                    if second_grounding.grounded:
                        self._record_audit(db, task, "vision_assisted_action", {
                            "detection_layer": "action_grounding",
                        })
                        return self._handle_execute_action(
                            db, task, page_state, vision_decision, recover_grounding=False,
                        )
                intervention = (
                    vision_decision.intervention
                    if vision_decision is not None
                    and vision_decision.decision_type == "REQUEST_HUMAN_INTERVENTION"
                    and vision_decision.intervention
                    else {
                        "type": "UNKNOWN_BLOCKER",
                        "reason": grounding.reason,
                        "message": "Autogram rejected a browser action because it was not grounded in this page. "
                                   "Please check the browser tab and confirm how to continue.",
                        "information_required": None,
                    }
                )
                self._pause_for_human(db, task, intervention, screenshot=screenshot)
                return False

            self._pause_for_human(db, task, {
                "type": "UNKNOWN_BLOCKER",
                "reason": grounding.reason,
                "message": "Autogram could not find a safe observed control for the proposed action. "
                           "Please check the browser tab and confirm how to continue.",
                "information_required": None,
            })
            return False

        # Spec §16, persisted counterpart of the in-memory streak below:
        # never retry a field that already failed MAX_FIELD_ATTEMPTS times
        # earlier in this task, even across a resume/process-restart.
        ledger_entry = (task.field_attempt_ledger or {}).get(field_identity(match)) if match is not None else None
        if ledger_entry is not None and ledger_entry.get("status") == "failed":
            self._record_audit(db, task, "blocker_detected", {
                "request_type": "UNKNOWN_BLOCKER", "detection_layer": "field_ledger",
            })
            return self._escalate_with_vision(db, task, page_state, {
                "type": "UNKNOWN_BLOCKER",
                "reason": f"Field {element_name!r} previously failed {ledger_entry.get('attempts', 0)} time(s) "
                          "earlier in this task and will not be retried automatically.",
                "message": "Autogram already tried this field multiple times earlier without success, and stopped "
                           "rather than keep retrying blindly. Please check the browser tab and let it know how "
                           "to continue.",
                "information_required": None,
            }, detection_layer="field_ledger")

        is_sourced = self._value_is_sourced(task, action.value)
        executor = ActionExecutor(
            self.handle.page,
            auto_submit_approved=bool(task.auto_submit_approved),
            allowed_upload_paths=_uploadable_paths(task),
        )
        result = executor.execute(
            action, element_name=element_name, element_type=_widget_type(match),
            element_semantic_action=(match.semantic_action if match else None), is_sourced=is_sourced,
        )
        # Feeds the page-signature stall check at the top of `_loop_body`:
        # set unconditionally (including a blocked action) so "the model kept
        # proposing something that didn't work" reads as stalled too, not
        # just an executed-but-ineffective one.
        self.handle.last_action_verified = bool(result.success and result.verified)

        # A verification-code-shaped field is never logged with its real
        # attempted value — `executor.py`'s gate already refused to actually
        # write it (see `blocked_reason` below), but this is a categorical,
        # belt-and-suspenders redaction independent of that outcome: nothing
        # the LLM ever proposed for a field matching this pattern is worth
        # more in the audit trail than knowing the agent tried.
        logged_value = "[REDACTED]" if element_name and is_verification_code_field_name(element_name) else action.value
        task_repo.append_action(db, task, {
            "action_type": action.action_type, "element_ref": action.element_ref,
            "element_name": element_name, "value": logged_value, "url": action.url,
            "reasoning": decision.reasoning, **result.as_dict(),
        })
        self._record_chat_action(db, task, action, element_name, result)

        if result.blocked_reason == "verification_code_requires_deterministic_path":
            # The LLM decision step (Layer 3) proposed writing into a field
            # that looks like a verification code — `observer.py::detect_blocker`
            # (Layers 1/2) should have caught this BEFORE the LLM was ever
            # asked to decide anything, so reaching this means that
            # deterministic detection likely missed an unusually-marked-up
            # field. Treat it exactly like the sensitive-field case: pause
            # for a human rather than let the LLM guess or retry.
            self._record_audit(db, task, "blocker_detected", {"request_type": "OTP_REQUIRED", "detection_layer": "executor_fallback"})
            self._pause_for_human(db, task, {
                "type": "OTP_REQUIRED",
                "reason": f"The agent attempted to fill a verification-code-shaped field ({element_name!r}) "
                          "directly — refused, and this pause was raised instead of letting it guess.",
                "message": self._message_for_request_type("OTP_REQUIRED", None),
                "information_required": None,
            })
            return False

        if result.blocked_reason == "upload_path_not_allowed":
            # The LLM named a file that is not one of this task's offered
            # documents. Refused by `executor.py`'s upload allowlist. Almost
            # always means the task has no uploadable résumé on local disk
            # (e.g. STORAGE_BACKEND=s3, or the stored file is missing), so the
            # honest move is to ask the human to attach it themselves rather
            # than let the agent keep guessing at paths.
            self._record_audit(db, task, "blocker_detected", {
                "request_type": "FILE_UPLOAD_REQUIRED", "detection_layer": "upload_allowlist",
            })
            self._pause_for_human(db, task, {
                "type": "FILE_UPLOAD_REQUIRED",
                "reason": result.detail,
                "message": self._message_for_request_type("FILE_UPLOAD_REQUIRED", None),
                "information_required": None,
            })
            return False

        if result.blocked_reason == "sensitive_field_requires_human":
            self._pause_for_human(db, task, {
                "type": "sensitive_confirmation",
                "reason": result.detail,
                "message": f"This application asks a sensitive question ({element_name!r}) the agent cannot "
                           "answer on its own. Please provide the answer to continue.",
                "information_required": element_name,
            })
            return False

        if result.blocked_reason == "submit_requires_approval":
            # Spec §17: don't take the LLM's word that the page is complete —
            # check the deterministic signals `observer.py` already captured
            # (required-but-empty fields, visible validation errors, blocking
            # dialogs) before ever marking a task ready for a human to approve.
            completion = compute_page_completion(page_state)
            if not completion.ready:
                self._record_audit(db, task, "blocker_detected", {
                    "request_type": "UNKNOWN_BLOCKER", "detection_layer": "completion_gate",
                })
                self._pause_for_human(db, task, {
                    "type": "UNKNOWN_BLOCKER",
                    "reason": f"The agent tried to finish this application, but the page is not actually complete "
                              f"yet ({completion.reason}).",
                    "message": f"Autogram tried to finish this application, but the page still shows "
                               f"{completion.reason}. Please check the browser tab and either complete it yourself "
                               "or let Autogram know how to proceed.",
                    "information_required": None,
                })
                return False
            task_repo.mark_ready_for_approval(db, task, {
                "decision": "APPLICATION_READY_FOR_SUBMISSION",
                "evidence": f"All steps before final submission appear complete. {result.detail}",
            })
            return False

        # Spec §16: the same element failing verification three times in a
        # row means "retrying again" is not a plan, it's a loop — escalate
        # rather than let the model keep trying the same thing. The
        # in-memory streak (below) is the cheap same-process fast path; the
        # persisted ledger (`_record_field_attempt`) is what makes "never
        # retry a failed field" survive a resume/process-restart.
        if result.result_code in ("ERROR_PAGE", "NAVIGATION_FAILED"):
            # The browser changed, but into a known-bad destination. Recover
            # deterministically before asking the model to reason again.
            self._track_verification_streak(action, result)
            self._record_field_attempt(db, task, match, action, result)
            recovery_action = AgentAction(action_type="go_back")
            recovery = executor.execute(recovery_action)
            task_repo.append_action(db, task, {
                "action_type": "go_back", "element_ref": None,
                "element_name": None, "value": None, "url": None,
                "reasoning": "Recover from a verified error destination.",
                **recovery.as_dict(),
            })
            self._record_chat_action(db, task, recovery_action, None, recovery)
            self.handle.last_action_verified = bool(recovery.success and recovery.verified)
            if recovery.success and recovery.verified:
                self._record_audit(db, task, "navigation_recovered", {
                    "failed_result": result.result_code,
                })
                return True
            return self._escalate_with_vision(db, task, page_state, {
                "type": "UNKNOWN_BLOCKER",
                "reason": "The last action reached an error page and automatic back-navigation could not be verified.",
                "message": "The application website opened an error page and Autogram could not safely return. "
                           "Please check the browser tab and confirm how to continue.",
                "information_required": None,
            }, detection_layer="navigation_recovery_failed")

        self._track_verification_streak(action, result)
        self._record_field_attempt(db, task, match, action, result)
        if self.handle.unverified_streak >= 3:
            streak_ref = self.handle.last_unverified_ref
            self.handle.unverified_streak = 0
            self.handle.last_unverified_ref = None
            self._record_audit(db, task, "blocker_detected", {
                "request_type": "UNKNOWN_BLOCKER", "detection_layer": "verification_streak",
            })
            return self._escalate_with_vision(db, task, page_state, {
                "type": "UNKNOWN_BLOCKER",
                "reason": f"Element ref {streak_ref} ({element_name!r}) did not visibly change after 3 consecutive attempts.",
                "message": "Autogram tried the same step three times without seeing it take effect, and stopped "
                           "rather than keep retrying blindly. Please check the browser tab and let it know how "
                           "to continue.",
                "information_required": None,
            }, detection_layer="verification_streak")

        return True

    def _record_field_attempt(self, db, task, match, action: AgentAction, result) -> None:
        """Persisted counterpart of `_track_verification_streak` (spec §16).
        Only counts a REAL dispatched attempt — a pre-emptively blocked
        action (verification-code/sensitive-field/upload/submit gates, or the
        field-ledger check above) returns before this is ever reached, since
        it never actually touched the field."""
        if match is None or action.action_type not in ("click", "fill", "select", "check", "uncheck"):
            return
        identity = field_identity(match)
        entry = dict((task.field_attempt_ledger or {}).get(identity) or {})
        entry["attempts"] = entry.get("attempts", 0) + 1
        entry["last_action_type"] = action.action_type
        if result.success and result.verified:
            entry["status"] = "verified"
        elif entry["attempts"] >= MAX_FIELD_ATTEMPTS:
            entry["status"] = "failed"
        else:
            entry["status"] = "attempted"
        task_repo.record_field_attempt(db, task, identity, entry)

    def _track_verification_streak(self, action: AgentAction, result) -> None:
        if action.element_ref is None or action.action_type not in ("click", "fill", "select", "check", "uncheck"):
            self.handle.last_unverified_ref = None
            self.handle.unverified_streak = 0
            return
        if result.blocked_reason is None and not result.verified:
            if self.handle.last_unverified_ref == action.element_ref:
                self.handle.unverified_streak += 1
            else:
                self.handle.last_unverified_ref = action.element_ref
                self.handle.unverified_streak = 1
        else:
            self.handle.last_unverified_ref = None
            self.handle.unverified_streak = 0

    def _value_is_sourced(self, task, value: str | None) -> bool:
        if not value:
            return True  # nothing being written — not a fabrication concern
        haystacks: list[str] = []
        for v in (task.confirmed_answers or {}).values():
            haystacks.append(str(v))
        profile = (task.candidate_profile or {}).get("profile") or {}
        for v in _flatten_values(profile):
            haystacks.append(str(v))
        needle = str(value).strip().lower()
        return any(needle == h.strip().lower() or (needle and needle in h.lower()) for h in haystacks)

    # ------------------------------------------------------------------

    def _message_for_request_type(self, request_type: str, masked_destination: str | None) -> str:
        base = _REQUEST_TYPE_MESSAGES.get(request_type, _REQUEST_TYPE_MESSAGES["UNKNOWN_BLOCKER"])
        if masked_destination and request_type in ("OTP_REQUIRED", "MFA_REQUIRED"):
            return f"{base} A code may have been sent to {masked_destination}."
        return base

    def _attempt_vision_assisted_decision(self, task, page_state: PageState) -> tuple[Decision | None, bytes | None]:
        """Spec §19: before finalizing a human-intervention pause for a
        genuinely ambiguous reason, try once more with a screenshot of the
        current viewport — DOM/accessibility text alone apparently wasn't
        enough. Returns `(None, None)` on any capture/LLM/parse failure —
        this is purely additive and must never make the escalation that was
        already about to happen any worse. Returns `(None, screenshot)` when
        the screenshot was captured but the vision-assisted call itself
        failed, so the caller can still attach it to the human's pause for
        context. A **viewport** screenshot (not full-page), deliberately: it
        matches what a person looking at the browser tab actually sees, and
        keeps the payload small."""
        try:
            screenshot = self.handle.page.screenshot(type="jpeg", quality=60)
        except Exception as e:  # noqa: BLE001 - a failed capture must never block the pause it was trying to avoid
            logger.debug("Task %s: vision-assisted screenshot capture failed (%s).", self.task_id, e)
            return None, None
        try:
            decision = decide_next_step(
                job_url=task.job_url,
                original_objective=task.original_objective,
                resume_text=(task.candidate_profile or {}).get("resume_text", ""),
                parsed_resume=(task.candidate_profile or {}).get("parsed_resume"),
                profile=(task.candidate_profile or {}).get("profile"),
                confirmed_answers=task.confirmed_answers or {},
                page_state=page_state,
                action_history=task.action_history or [],
                uploaded_documents=task.uploaded_documents or [],
                auto_submit_approved=bool(task.auto_submit_approved),
                screenshot=screenshot,
            )
        except DecisionError as e:
            logger.info("Task %s: vision-assisted decision failed (%s) — falling back to the normal pause.", self.task_id, e)
            return None, screenshot
        return decision, screenshot

    def _escalate_with_vision(
        self, db, task, page_state: PageState, fallback_intervention: dict, *, detection_layer: str,
    ) -> bool:
        """Wraps a would-be human-intervention pause with one vision-assisted
        attempt first (spec §19-20) — used ONLY at the three points where the
        loop is about to pause for a genuinely uncertain reason (a stalled
        page, a verification streak, a field-ledger refusal, or the LLM's own
        `REQUEST_HUMAN_INTERVENTION`), never for the objective policy
        refusals (`sensitive_field_requires_human`/`upload_path_not_allowed`/
        `submit_requires_approval`) or the deterministic Layer-1/2 blockers
        (OTP/MFA/CAPTCHA/LOGIN), which already have a clear, correct cause
        and gain nothing from a vision opinion.

        Returns True if the loop should keep going immediately (a
        vision-proposed action executed with no new pause), False if a
        human-intervention pause resulted — either from the vision-proposed
        action itself pausing, or from falling back to
        `fallback_intervention` — matching `_handle_execute_action`'s own
        True/False contract exactly, so callers in both places compose with
        it identically."""
        vision_decision, screenshot = self._attempt_vision_assisted_decision(task, page_state)
        if vision_decision is not None and vision_decision.decision_type == "EXECUTE_ACTION":
            self._record_audit(db, task, "vision_assisted_action", {"detection_layer": detection_layer})
            return self._handle_execute_action(db, task, page_state, vision_decision)
        intervention = (
            vision_decision.intervention
            if vision_decision is not None
            and vision_decision.decision_type == "REQUEST_HUMAN_INTERVENTION"
            and vision_decision.intervention
            else fallback_intervention
        )
        self._pause_for_human(db, task, intervention, screenshot=screenshot)
        return False

    def _record_audit(self, db, task, event_type: str, metadata: dict | None = None) -> None:
        """Best-effort structured audit trail (spec section 15) — never
        includes an OTP/MFA value or any other secret, only ids/types/reasons.
        A logging failure here must never break the automation loop itself."""
        try:
            audit_log_repo.record_event(
                db, user_id=task.user_id, autonomous_task_id=task.task_id,
                event_type=event_type, actor="system", metadata=metadata or {},
            )
        except Exception:  # noqa: BLE001
            logger.exception("Task %s: failed to record audit event %r.", self.task_id, event_type)

    def _record_chat_milestone(self, db, task, content: str, safe_metadata: dict | None = None) -> None:
        """Best-effort durable progress reporting, independent of audit rows."""
        if not hasattr(db, "add") and getattr(chat_repository, "__name__", "").endswith("chat_repository"):
            return  # lightweight unit-test repository double, not a SQLAlchemy session
        try:
            chat_repository.record_system_message(
                db,
                user_id=task.user_id,
                autonomous_task_id=task.task_id,
                content=content,
                safe_metadata=safe_metadata or {},
            )
        except Exception:  # noqa: BLE001 - chat must never abort automation
            logger.exception("Task %s: could not write a chat milestone.", task.task_id)

    def _record_chat_action(self, db, task, action: AgentAction, element_name: str | None, result) -> None:
        label = element_name or action.action_type.replace("_", " ")
        outcome = result.result_code or ("ACTION_SUCCEEDED" if result.success else "ACTION_FAILED")
        if result.success and result.verified:
            content = f"Completed {label}; the page confirmed the action took effect."
        elif result.blocked_reason == "action_not_grounded":
            content = f"Rejected an unsafe ungrounded {action.action_type} action and re-analyzed the page."
        else:
            content = f"Could not complete {label}; the expected page change was not verified."
        self._record_chat_milestone(
            db, task, content,
            {
                "event": "ACTION_RESULT",
                "action_type": action.action_type,
                "action_result": outcome,
                "postcondition_verified": bool(result.success and result.verified),
            },
        )

    def _pause_for_human(
        self, db, task, intervention: dict, *, safe_metadata: dict | None = None, screenshot: bytes | None = None,
    ) -> None:
        """`intervention` is either the LLM's free-form `{type, reason,
        message, information_required[, confidence]}` (Layer 3) or an
        already-normalized dict built from `observer.py::detect_blocker`
        (Layers 1/2) — `normalize_intervention_type` handles both. Creates
        the durable, addressable `HumanInteractionRequest` row (the audit
        trail + the resource `app/api/human_interaction.py` operates on),
        and also updates the task's `human_intervention` snapshot column so
        the existing status-polling endpoints/frontend keep working
        unchanged for non-secret request types.

        `screenshot`, when given by `_escalate_with_vision` (spec §19/§21/§24),
        is attached to the chat message as a small base64 JPEG data URI so
        the human can see what the agent saw without needing a new storage
        backend or serving endpoint — it never reaches a deterministic
        Layer-1/2 blocker (OTP/MFA/CAPTCHA/LOGIN never take this path, since
        vision is only attempted for a genuinely ambiguous escalation) or any
        other persisted column beyond this one chat message."""
        request_type = normalize_intervention_type(intervention)
        message = intervention.get("message") or self._message_for_request_type(
            request_type, (safe_metadata or {}).get("masked_destination")
        )
        screenshot_data_uri = _screenshot_to_data_uri(screenshot) if screenshot else None
        req = human_interaction_repo.create_request(
            db, user_id=task.user_id, task_id=task.task_id, request_type=request_type,
            message=message,
            # `information_required` lives in `safe_metadata` (not just the
            # legacy `human_intervention` snapshot below) so the new
            # `/human-requests/{id}/respond` route can answer a
            # USER_PROVIDED_VALUE / ANSWER_REQUIRED request without needing
            # the task's denormalized snapshot column at all.
            safe_metadata={**(safe_metadata or {}), "information_required": intervention.get("information_required")},
            # See `_SHORT_LIVED_REQUEST_TYPES`: only a verification code has a
            # deadline worth enforcing; everything else waits as long as the
            # human needs.
            expires_in_minutes=(
                human_interaction_repo.DEFAULT_EXPIRY_MINUTES
                if request_type in _SHORT_LIVED_REQUEST_TYPES else None
            ),
        )
        self._record_audit(db, task, "human_request_created", {
            "request_id": req.request_id, "request_type": request_type,
        })
        self._record_audit(db, task, "automation_paused", {"request_id": req.request_id, "request_type": request_type})
        # The user-facing half of the same pause. `message` is the prose the
        # agent already composed for this blocker, so the chat says exactly what
        # the request says — no second wording to drift out of sync.
        #
        # `human_request_id` is what turns this into an ANSWERABLE prompt in the
        # UI: the panel renders the control matching the request type (OTP
        # field, "CAPTCHA completed" button, free-text box) instead of inert
        # prose. Best-effort — a transcript write must never abort a live job
        # application, and the pause itself is already durable above.
        try:
            chat_meta = {"request_type": request_type}
            if screenshot_data_uri:
                chat_meta["screenshot_data_uri"] = screenshot_data_uri
            chat_repository.record_agent_message(
                db, user_id=task.user_id, autonomous_task_id=task.task_id,
                content=message, human_request_id=req.request_id,
                safe_metadata=chat_meta,
            )
        except Exception:
            logger.exception("Task %s: could not write the pause to the chat transcript.", task.task_id)
        publish_task_event(
            task.task_id, "HUMAN_ACTION_REQUIRED",
            request_type=request_type, request_id=req.request_id,
        )
        task_repo.request_human_intervention(db, task, {
            "type": request_type,
            "reason": intervention.get("reason"),
            "message": message,
            "information_required": intervention.get("information_required"),
            "request_id": req.request_id,
            "request_type": request_type,
            "expires_at": req.expires_at.isoformat() if req.expires_at else None,
            "safe_metadata": req.safe_metadata,
        })
        self.handle.resume_event.clear()

    def _try_consume_pending_secret(self, db, task) -> bool | None:
        """If a human just delivered an OTP/MFA code
        (`runner.py::deliver_secret`), fill and submit it deterministically —
        NEVER via the LLM decision step, so the plaintext code never enters
        an LLM prompt, `action_history`, or a log line.

        Returns `None` if there was nothing to consume (the normal case —
        caller proceeds to the usual observe/decide flow). Returns `True` if
        a secret was consumed and no new pause resulted (caller should
        `continue` the loop immediately: the very next iteration re-observes
        and re-decides normally, which is exactly how a rejected code
        surfaces — the same OTP field or error text triggers a fresh,
        independent human-intervention pause rather than the loop ever
        guessing or retrying on its own). Returns `False` if consuming it
        immediately raised a NEW pause (e.g. the field vanished) — the caller
        must `_wait_for_resume()` before continuing, exactly like every other
        pause branch in `_loop_body`."""
        secret = self.handle.pending_secret
        if secret is None:
            return None
        self.handle.pending_secret = None  # clear immediately after reading, before any use
        request_id, value = secret["request_id"], secret["value"]

        page_state = observe_page(self.handle.page, ats_hint=self._ats_hint())
        task_repo.update_browser_state(db, task, page_state.as_dict())
        blocker = page_state.blocker_hint

        if not blocker or blocker.get("otp_field_ref") is None:
            value = None  # drop the local reference to the plaintext code before giving up
            self._record_audit(db, task, "verification_field_lost", {"request_id": request_id})
            stale_req = human_interaction_repo.get_by_id(db, request_id)
            if stale_req is not None and stale_req.status not in ("RESOLVED", "EXPIRED", "CANCELLED", "FAILED"):
                human_interaction_repo.mark_failed(db, stale_req)

            if blocker is not None:
                # The page changed to a DIFFERENT, still-recognizable blocker
                # (e.g. a CAPTCHA replaced the OTP page, or a fresh login
                # wall appeared) — surface THAT blocker accurately rather
                # than guessing it must still be a login issue.
                new_type = blocker["request_type"]
                self._pause_for_human(db, task, {
                    "type": new_type,
                    "reason": f"The page changed after the verification code was submitted (now showing: {new_type}).",
                    "message": self._message_for_request_type(new_type, blocker.get("masked_destination")),
                    "information_required": None,
                }, safe_metadata={
                    "masked_destination": blocker.get("masked_destination"),
                    "otp_field_ref": blocker.get("otp_field_ref"),
                    "submit_ref": blocker.get("submit_ref"),
                    "detection_layer": "deterministic",
                })
            else:
                # Nothing recognizable at all — could mean the application
                # moved on by itself, or something unclassifiable. Ask a
                # human to check rather than assuming either outcome.
                self._pause_for_human(db, task, {
                    "type": "UNKNOWN_BLOCKER",
                    "reason": "The verification field disappeared and no other recognizable blocker was found "
                              "after the code was submitted.",
                    "message": "Autogram couldn't confirm whether your verification code was accepted — "
                               "please check the browser tab and let it know how to continue.",
                    "information_required": None,
                })
            return False

        executor = ActionExecutor(self.handle.page, auto_submit_approved=bool(task.auto_submit_approved))
        fill_result = executor.execute(
            AgentAction(action_type="fill", element_ref=blocker["otp_field_ref"], value=value),
            element_name="verification code", verification_code_write=True,
        )
        task_repo.append_action(db, task, {
            "action_type": "fill", "element_ref": blocker["otp_field_ref"],
            "element_name": "verification code", "value": "[REDACTED]", "url": None,
            "reasoning": "Deterministic verification-code entry — never routed through the LLM.",
            **fill_result.as_dict(),
        })
        value = None  # the plaintext code is never referenced again past this point

        submit_ref = blocker.get("submit_ref")
        if submit_ref is not None and fill_result.success:
            click_result = executor.execute(AgentAction(action_type="click", element_ref=submit_ref), element_name="verify/continue")
            task_repo.append_action(db, task, {
                "action_type": "click", "element_ref": submit_ref, "element_name": "verify/continue",
                "value": None, "url": None, "reasoning": "Deterministic submit of the verification code.",
                **click_result.as_dict(),
            })

        self._record_audit(db, task, "verification_submitted", {"request_id": request_id})
        req = human_interaction_repo.get_by_id(db, request_id)
        if req is not None and req.status not in ("RESOLVED", "EXPIRED", "CANCELLED", "FAILED"):
            human_interaction_repo.mark_resolved(db, req)
        # Whether this was actually ACCEPTED or REJECTED can only be known on
        # the next fresh observation (does the same OTP/MFA blocker come back
        # or not?) — see `_note_verification_outcome`, called right after the
        # next `observe_page()` in `_loop_body`.
        self.handle.awaiting_verification_result = request_id
        return True

    def _note_verification_outcome(self, db, task, page_state: PageState) -> None:
        """Labels the audit trail "verification_accepted" / "verification_rejected"
        for a code that was submitted on the PREVIOUS iteration
        (`_try_consume_pending_secret`), based on this fresh observation —
        purely observational, never affects control flow (the actual
        re-pause-on-rejection behavior already happens unconditionally via
        the normal `blocker_hint` check in `_loop_body`)."""
        request_id = self.handle.awaiting_verification_result
        if request_id is None:
            return
        self.handle.awaiting_verification_result = None
        blocker = page_state.blocker_hint
        if blocker and blocker.get("request_type") in ("OTP_REQUIRED", "MFA_REQUIRED"):
            self._record_audit(db, task, "verification_rejected", {"request_id": request_id})
        else:
            self._record_audit(db, task, "verification_accepted", {"request_id": request_id})

    def _wait_for_resume(self) -> bool:
        """Blocks (browser session untouched) until a human resumes or
        cancels this task.

        Callers must **`continue`** afterwards, never `return`, and may ignore
        the return value. Cancellation is owned exclusively by the single
        check at the top of `_loop_body`, which persists `CANCELLED`, records
        the audit event, and releases the browser.

        This used to be `if not self._wait_for_resume(): return`, which
        returned straight out of `_loop_body` on a cancel — skipping all three
        of those. Because the `/cancel` route only persists the cancellation
        itself when there is NO live handle, a task cancelled *while paused*
        was left stuck in `WAITING_FOR_HUMAN` forever, and its Playwright
        driver process and browser tab leaked. Falling through to the top of
        the loop reuses the one correct cancellation path instead of
        duplicating it at seven call sites. (Found by the real-browser E2E
        run: the leaked drivers accumulated until later tests could no longer
        get a browser.)"""
        while True:
            if self.handle.cancel_requested.is_set():
                return False
            # Poll on a bounded wait rather than an unbounded one so a
            # cancel that arrives while we're already waiting is noticed
            # promptly instead of only at the next resume signal.
            if self.handle.resume_event.wait(timeout=2.0):
                return True


def _widget_type(element) -> str | None:
    """The widget-semantics type `executor.py` needs to pick a verification
    strategy: `element.type` prefers a native HTML `type` ATTRIBUTE when
    present, which for a real custom combobox is very commonly "text" (see
    `PageElement.role`'s docstring) — so "combobox"/"option" are recognized
    via EITHER `type` or `role` here, and everything else just passes
    `element.type` through unchanged."""
    if element is None:
        return None
    if element.type == "option" or element.role == "option":
        return "option"
    if element.type == "combobox" or element.role == "combobox":
        return "combobox"
    return element.type


def _screenshot_to_data_uri(screenshot: bytes) -> str:
    """A vision-assisted screenshot (spec §19/§21/§24), inlined for the chat
    UI. No new storage backend or serving endpoint: `_attempt_vision_assisted_
    decision` already captures a JPEG at quality 60, so the encoded payload
    stays small enough for a JSONB column and a `<img>` tag."""
    return "data:image/jpeg;base64," + base64.b64encode(screenshot).decode("ascii")


def _uploadable_paths(task) -> list[str]:
    """The local file paths an `upload_file` action may use for this task —
    fed to `ActionExecutor`'s allowlist gate. Read from
    `AutonomousTask.uploaded_documents`, which
    `app/api/autonomous_agent.py::_build_uploadable_documents` populates at
    task creation with the candidate's résumé when (and only when) it resolves
    to a real local file."""
    out: list[str] = []
    for doc in (task.uploaded_documents or []):
        if isinstance(doc, dict) and doc.get("file_path"):
            out.append(str(doc["file_path"]))
    return out


def _flatten_values(obj) -> list:
    out = []
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_flatten_values(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_flatten_values(v))
    elif obj is not None:
        out.append(obj)
    return out
