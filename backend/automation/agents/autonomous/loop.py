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
from automation.agents.autonomous.actions import AgentAction
from automation.agents.autonomous.decision import Decision, DecisionError, decide_next_step, normalize_intervention_type
from automation.agents.autonomous.executor import ActionExecutor, is_verification_code_field_name
from automation.agents.autonomous.observer import PageState, observe_page
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

#: Phrases that plausibly indicate a genuine post-submit confirmation page —
#: checked before ever accepting a TASK_COMPLETED decision from the LLM (see
#: spec: "must never report TASK_COMPLETED unless the browser actually shows
#: a post-submit confirmation"). Deliberately permissive (many false
#: positives are fine — a false positive here still requires an actual
#: navigation and page load to have happened); the failure mode this guards
#: against is a false negative (LLM claims success on an unchanged form).
CONFIRMATION_PHRASES = [
    "application submitted", "successfully submitted", "thank you for applying",
    "thank you for your application", "application received", "your application has been",
    "we have received your application", "application complete",
]

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
        page.goto(task.job_url, wait_until="domcontentloaded", timeout=30000)
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
                self._wait_for_resume()
                continue

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
                self._pause_for_human(db, task, decision.intervention)
                self._wait_for_resume()
                continue

            if decision.decision_type == "APPLICATION_READY_FOR_SUBMISSION":
                task_repo.mark_ready_for_approval(db, task, {
                    "decision": decision.decision_type, "evidence": decision.evidence,
                })
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
                self._close_browser()
                return

            if decision.decision_type == "TASK_FAILED":
                task_repo.mark_failed(db, task, decision.evidence or "The agent could not complete this task.")
                self._record_audit(db, task, "automation_failed", {"reason": "task_failed", "evidence": decision.evidence})
                self._close_browser()
                return

    def _page_shows_confirmation(self, page_state: PageState) -> bool:
        haystack = f"{page_state.title} {page_state.visible_text}".lower()
        return any(phrase in haystack for phrase in CONFIRMATION_PHRASES)

    def _handle_execute_action(self, db, task, page_state: PageState, decision: Decision) -> bool:
        """Returns True if the loop should keep going immediately, False if
        it paused for human input and the caller should wait."""
        action = decision.action
        element_name = None
        if action.element_ref is not None:
            match = next((e for e in page_state.elements if e.ref == action.element_ref), None)
            element_name = match.name if match else None

        is_sourced = self._value_is_sourced(task, action.value)
        executor = ActionExecutor(
            self.handle.page,
            auto_submit_approved=bool(task.auto_submit_approved),
            allowed_upload_paths=_uploadable_paths(task),
        )
        result = executor.execute(action, element_name=element_name, is_sourced=is_sourced)

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
                "request_type": "MANUAL_ACTION_REQUIRED", "detection_layer": "upload_allowlist",
            })
            self._pause_for_human(db, task, {
                "type": "MANUAL_ACTION_REQUIRED",
                "reason": result.detail,
                "message": "This application needs a file uploaded that Autogram doesn't have a local copy of. "
                           "Please attach it yourself in the open browser tab, then let Autogram continue.",
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
            task_repo.mark_ready_for_approval(db, task, {
                "decision": "APPLICATION_READY_FOR_SUBMISSION",
                "evidence": f"All steps before final submission appear complete. {result.detail}",
            })
            return False

        return True

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

    def _pause_for_human(self, db, task, intervention: dict, *, safe_metadata: dict | None = None) -> None:
        """`intervention` is either the LLM's free-form `{type, reason,
        message, information_required[, confidence]}` (Layer 3) or an
        already-normalized dict built from `observer.py::detect_blocker`
        (Layers 1/2) — `normalize_intervention_type` handles both. Creates
        the durable, addressable `HumanInteractionRequest` row (the audit
        trail + the resource `app/api/human_interaction.py` operates on),
        and also updates the task's `human_intervention` snapshot column so
        the existing status-polling endpoints/frontend keep working
        unchanged for non-secret request types."""
        request_type = normalize_intervention_type(intervention)
        message = intervention.get("message") or self._message_for_request_type(
            request_type, (safe_metadata or {}).get("masked_destination")
        )
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
            chat_repository.record_agent_message(
                db, user_id=task.user_id, autonomous_task_id=task.task_id,
                content=message, human_request_id=req.request_id,
                safe_metadata={"request_type": request_type},
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
