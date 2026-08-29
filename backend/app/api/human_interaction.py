"""
API surface for `HumanInteractionRequest` (see `app/models/db_models.py`) —
the durable, addressable record of one human-in-the-loop pause the
autonomous agent (`automation/agents/autonomous/loop.py`) raised: OTP, MFA,
CAPTCHA, login, an ambiguous question, or any other blocker.

Distinct from (and additive to) `app/api/autonomous_agent.py`'s existing
`/agent/tasks/{id}/resume|answer|approve` routes, which keep working
unchanged for every non-secret intervention. This router adds the
structured, typed, addressable request/response flow the OTP/MFA case
specifically needs — see AUTONOMOUS_AGENT.md's "Human-in-the-loop" section.

Endpoints:
    GET  /agent/tasks/{task_id}/human-request     the task's current active (PENDING) request, if any
    GET  /human-requests/{request_id}             a specific request's status/metadata
    POST /human-requests/{request_id}/respond     structured response: {action, value?}
    POST /human-requests/{request_id}/cancel      cancel the request (and the task, if still active)

HARD RULE: no route here ever returns a submitted OTP/MFA value back to the
caller — `/respond` returns only `{request_id, status}` (see spec section 7).
`RespondRequest.value` for a secret-bearing action is read exactly once (by
`runner.py::deliver_secret`) and dropped; it is never logged, never written
to `HumanInteractionRequest`, and never included in this module's own log
lines (see the explicit `del`/redaction below).
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.db_models import (
    AUTONOMOUS_TASK_TERMINAL_STATUSES,
    SECRET_HUMAN_REQUEST_TYPES,
    HumanInteractionRequest,
    User,
)
from app.services import audit_log_repository
from app.services import chat_repository
from app.services import autonomous_task_repository as task_repo
from app.services import human_interaction_repository as human_interaction_repo
from app.services.event_bus import publish_task_event
from automation.agents.autonomous.runner import (
    deliver_secret,
    request_cancel,
    signal_resume,
    start_task_background,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["human-interaction"])

#: Actions whose `value` is a transient secret (a verification code) — never
#: persisted, logged, or echoed back. See module docstring.
_SECRET_ACTIONS = frozenset({"OTP_SUBMITTED", "MFA_SUBMITTED"})
_VALID_ACTIONS = _SECRET_ACTIONS | {"USER_APPROVED", "USER_PROVIDED_VALUE"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class HumanRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request_id: str
    task_id: str
    request_type: str
    status: str
    title: str | None = None
    message: str
    safe_metadata: dict = {}
    created_at: datetime
    expires_at: datetime | None = None
    responded_at: datetime | None = None
    resolved_at: datetime | None = None


class RespondRequest(BaseModel):
    action: str  # OTP_SUBMITTED | MFA_SUBMITTED | USER_APPROVED | USER_PROVIDED_VALUE
    value: str | None = None  # transient secret for *_SUBMITTED; a plain answer for USER_PROVIDED_VALUE


class RespondResult(BaseModel):
    request_id: str
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_owned_request(db: Session, request_id: str, user: User) -> HumanInteractionRequest:
    req = human_interaction_repo.get_by_id(db, request_id)
    if req is None or req.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Human interaction request not found.")
    return req


def _get_owned_task(db: Session, task_id: str, user: User):
    task = task_repo.get_by_id(db, task_id)
    if task is None or task.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Autonomous task not found.")
    return task


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/agent/tasks/{task_id}/human-request", response_model=HumanRequestResponse)
def get_active_human_request(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_owned_task(db, task_id, user)
    req = human_interaction_repo.get_active_for_task(db, task_id)
    if req is None:
        raise HTTPException(status_code=404, detail="No active human interaction request for this task.")
    return req


@router.get("/human-requests/{request_id}", response_model=HumanRequestResponse)
def get_human_request(request_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _get_owned_request(db, request_id, user)


#: How a no-value action reads in the conversation. Without this the transcript
#: would show a raw enum name, which describes the API rather than what the
#: human actually did.
#:
#: Only `USER_APPROVED` needs an entry: of the four members of `_VALID_ACTIONS`,
#: the two secret ones take the redacted branch and `USER_PROVIDED_VALUE`
#: carries the user's own words. The `.get(action, action)` fallback covers any
#: future valueless action rather than crashing on one.
#:
#: Note the CAPTCHA and login "I've handled it" turns do NOT arrive here — they
#: go through `POST /agent/tasks/{id}/resume`, which records its own line.
_ACTION_TRANSCRIPT_TEXT = {
    "USER_APPROVED": "Approved — go ahead and submit.",
}


@router.post("/human-requests/{request_id}/respond", response_model=RespondResult)
def respond_to_human_request(
    request_id: str, body: RespondRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    req = _get_owned_request(db, request_id, user)

    if human_interaction_repo.is_expired(req):
        expired = human_interaction_repo.try_claim(db, request_id, new_status="EXPIRED", from_status="PENDING")
        if expired is not None:
            audit_log_repository.record_event(
                db, user_id=user.user_id, autonomous_task_id=req.task_id, event_type="human_request_expired",
                actor="system", metadata={"request_id": req.request_id},
            )
        raise HTTPException(status_code=410, detail="This request has expired. Please wait for the agent to raise a new one.")

    action = body.action
    if action not in _VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action!r}.")

    task = _get_owned_task(db, req.task_id, user)

    # --- Validate the PAYLOAD before claiming anything ---------------------
    # Every check that can reject this call on its own merits must happen
    # BEFORE the two atomic claims below, because a claim is destructive:
    # it moves the request out of PENDING and the task out of
    # WAITING_FOR_HUMAN. Validating afterwards meant a plainly-invalid call
    # (a verification code sent to a LOGIN_REQUIRED request, or an empty
    # value) destroyed a perfectly good pending request AND stranded the task
    # in RESUMING with nothing left to resume it — the user's only recourse
    # being to cancel and start over. Found by the real-browser E2E run
    # (`test_06_login_required_pauses_and_resumes_after_manual_login`).
    value: str | None = None
    if action in _SECRET_ACTIONS:
        if req.request_type not in SECRET_HUMAN_REQUEST_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"This request does not accept a verification code (type: {req.request_type}).",
            )
        value = (body.value or "").strip()
        if not value:
            raise HTTPException(status_code=422, detail="A verification code is required.")
    elif action == "USER_PROVIDED_VALUE":
        value = (body.value or "").strip()
        if not value:
            raise HTTPException(status_code=422, detail="A value is required for this action.")

    # --- Race-condition guard #1: claim the REQUEST atomically -------------
    # A single conditional `UPDATE ... WHERE status = 'PENDING'` — whichever
    # of two concurrent /respond calls (or a /respond racing a /cancel) wins
    # this actually changes the row; the loser's WHERE clause matches zero
    # rows and gets a clean 409 here, before ever touching deliver_secret,
    # confirmed_answers, or a resume signal.
    claimed = human_interaction_repo.try_claim(db, request_id, new_status="RESPONDED")
    if claimed is None:
        raise HTTPException(status_code=409, detail="This request has already been answered, cancelled, or expired.")
    req = claimed

    # Audit the fact a response arrived — never the value itself.
    audit_log_repository.record_event(
        db, user_id=user.user_id, autonomous_task_id=task.task_id, event_type="human_response_received",
        actor=user.user_id, metadata={"request_id": req.request_id, "action": action},
    )

    # --- Race-condition guard #2: claim the TASK atomically ----------------
    # Guards the SAME window against the legacy `/resume`/`/answer`/`/approve`
    # routes (`app/api/autonomous_agent.py`), which don't reference a
    # request_id and so can't be serialized by guard #1 alone — both sides of
    # a "resume this task" race must go through `try_claim_for_resume`.
    if not task_repo.try_claim_for_resume(db, task, from_status="WAITING_FOR_HUMAN"):
        human_interaction_repo.mark_failed(db, req)
        raise HTTPException(
            status_code=409,
            detail=f"This task is no longer waiting for human input (status: {task.current_status}) — "
                   "it may have already been resumed by another response.",
        )

    # Echo the human's turn into the transcript, immediately after the claims
    # succeed and BEFORE any branch consumes `value`. Two different calls on
    # purpose:
    #
    #   secret actions  -> `record_secret_submission`, which takes NO value
    #                      argument at all, so the code cannot reach the
    #                      database even by mistake. The transcript records
    #                      that a code was submitted, never the code.
    #   everything else -> the user's actual prose, which is the whole point of
    #                      having a conversation to look back on.
    #
    # Best-effort: a transcript write must never break a resume that already
    # claimed the request — the durable record of this action is the audit log
    # and the request's own status.
    try:
        if action in _SECRET_ACTIONS:
            chat_repository.record_secret_submission(
                db, user_id=user.user_id, autonomous_task_id=task.task_id,
                human_request_id=req.request_id, request_type=req.request_type,
            )
        else:
            chat_repository.record_user_reply(
                db, user_id=user.user_id, autonomous_task_id=task.task_id,
                human_request_id=req.request_id, request_type=req.request_type,
                content=value or _ACTION_TRANSCRIPT_TEXT.get(action, action),
            )
    except Exception:
        logger.exception("Request %s: could not write the response to the chat transcript.", req.request_id)
    publish_task_event(
        task.task_id, "HUMAN_ACTION_COMPLETED",
        request_type=req.request_type, request_id=req.request_id, action=action,
    )

    if action in _SECRET_ACTIONS:
        # `value` was validated (present, non-empty, correct request type)
        # BEFORE the claims above — nothing left to reject here.

        # Mark RESUMING *before* handing the secret over. `deliver_secret`
        # sets the loop's resume event, so the loop thread can wake, consume
        # the code, and reach its own `mark_resolved` before this thread runs
        # its next statement. Doing this write afterwards let a late
        # unconditional RESUMING clobber the loop's terminal RESOLVED, leaving
        # a fully-consumed request stuck in a non-terminal status forever.
        # (Found by `test_04_correct_otp_resumes_and_continues`.)
        human_interaction_repo.mark_resuming(db, req)

        delivered = deliver_secret(task.task_id, req.request_id, value)
        value = None  # never referenced again — see module docstring

        if not delivered:
            # The process restarted since this task paused — there is no live
            # tab to fill the code into. Per AUTONOMOUS_AGENT.md/spec section 9,
            # surface a fresh LOGIN_REQUIRED request rather than silently
            # starting a new tab that can't use the code the user just entered.
            human_interaction_repo.mark_failed(db, req)
            # Name this condition explicitly in the audit trail. Without it an
            # operator reading the log sees `human_response_received` followed
            # by a LOGIN_REQUIRED `human_request_created` and no explanation —
            # i.e. no way to answer "did the browser session disappear?", which
            # is exactly what happened here.
            audit_log_repository.record_event(
                db, user_id=user.user_id, autonomous_task_id=task.task_id,
                event_type="automation_session_lost", actor="system",
                metadata={"request_id": req.request_id, "request_type": req.request_type,
                          "reason": "no_live_task_handle"},
            )
            fallback = human_interaction_repo.create_request(
                db, user_id=user.user_id, task_id=task.task_id, request_type="LOGIN_REQUIRED",
                message="The automation session was no longer available to receive your verification code. "
                        "Please continue the application manually in the browser, or restart the task.",
                # A sign-in pause waits as long as the human needs — see
                # `loop.py::_SHORT_LIVED_REQUEST_TYPES`.
                expires_in_minutes=None,
            )
            audit_log_repository.record_event(
                db, user_id=user.user_id, autonomous_task_id=task.task_id,
                event_type="human_request_created", actor="system",
                metadata={"request_id": fallback.request_id, "request_type": "LOGIN_REQUIRED"},
            )
            task_repo.request_human_intervention(db, task, {
                "type": "LOGIN_REQUIRED",
                "reason": "The automation session was lost while delivering a verification code.",
                "message": fallback.message, "information_required": None,
                "request_id": fallback.request_id, "request_type": "LOGIN_REQUIRED",
                "safe_metadata": fallback.safe_metadata,
            })
            raise HTTPException(status_code=409, detail="The automation session is no longer available; a new request was created.")

        # NOTE: deliberately NOT marking RESUMING here — that already happened
        # above, before `deliver_secret`. From this point the loop thread owns
        # this request's remaining lifecycle (RESOLVED on a successful
        # consumption, FAILED if the verification field turned out to be gone).

    elif action == "USER_APPROVED":
        if not signal_resume(task.task_id):
            start_task_background(task.task_id)
        human_interaction_repo.mark_resolved(db, req)

    else:  # USER_PROVIDED_VALUE — `value` was validated before the claims
        question = (req.safe_metadata or {}).get("information_required") or req.title or req.message
        task_repo.record_confirmed_answer(db, task, question, value)
        if not signal_resume(task.task_id):
            start_task_background(task.task_id)
        human_interaction_repo.mark_resolved(db, req)

    return {"request_id": req.request_id, "status": "accepted"}


@router.post("/human-requests/{request_id}/cancel", response_model=RespondResult)
def cancel_human_request(request_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    req = _get_owned_request(db, request_id, user)

    # Atomic claim — same guard as /respond: whichever of a concurrent
    # /cancel and /respond actually changes the row wins; the loser gets a
    # clean, current-status response rather than racing to overwrite the row.
    claimed = human_interaction_repo.try_claim(db, request_id, new_status="CANCELLED")
    if claimed is None:
        current = human_interaction_repo.get_by_id(db, request_id) or req
        return {"request_id": current.request_id, "status": current.status.lower()}
    req = claimed

    task = task_repo.get_by_id(db, req.task_id)
    if task is not None and task.user_id == user.user_id and task.current_status not in AUTONOMOUS_TASK_TERMINAL_STATUSES:
        # Signal the loop AND persist unconditionally — a loop blocked waiting
        # for a human never runs its own cancellation path. Same fix and same
        # reasoning as `app/api/autonomous_agent.py::cancel_task`.
        request_cancel(task.task_id)
        task_repo.cancel_task(db, task)
        audit_log_repository.record_event(
            db, user_id=user.user_id, autonomous_task_id=task.task_id, event_type="automation_cancelled",
            actor=user.user_id, metadata={"request_id": req.request_id},
        )
    return {"request_id": req.request_id, "status": "cancelled"}
