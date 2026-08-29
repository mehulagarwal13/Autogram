"""
Persistence for `HumanInteractionRequest` (see `app/models/db_models.py`) —
the durable, individually-addressable record of one human-in-the-loop pause
raised by the autonomous agent (`automation/agents/autonomous/loop.py`):
OTP, MFA, CAPTCHA, login, an ambiguous question, or any other blocker it
can't safely continue past on its own.

Same layering convention as every other `*_repository.py` module here: plain
functions taking a `Session`, no business logic beyond simple state
transitions, called from `app/api/human_interaction.py` and from
`automation/agents/autonomous/loop.py` (via `automation.interfaces
.automation_db_session()` when running off the request thread).

HARD RULE: no function here ever takes or returns a secret value (OTP/MFA
code). `safe_metadata` is for non-secret context only — see
`app/models/db_models.py::HumanInteractionRequest`'s docstring and
`automation/agents/autonomous/runner.py::deliver_secret` for where the actual
transient value goes (in-process only, never persisted).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.db_models import (
    HUMAN_REQUEST_TERMINAL_STATUSES,
    HumanInteractionRequest,
    VALID_HUMAN_REQUEST_TYPES,
)

#: How long a request stays answerable before it silently expires. Chosen to
#: comfortably cover "check your email/phone and come back", without leaving
#: a stale request answerable indefinitely after the site's own OTP expired.
DEFAULT_EXPIRY_MINUTES = 10


def _new_id() -> str:
    return f"hreq_{uuid.uuid4().hex}"


def create_request(
    db: Session,
    *,
    user_id: str,
    task_id: str,
    request_type: str,
    message: str,
    title: str | None = None,
    safe_metadata: dict | None = None,
    expires_in_minutes: int | None = DEFAULT_EXPIRY_MINUTES,
) -> HumanInteractionRequest:
    if request_type not in VALID_HUMAN_REQUEST_TYPES:
        raise ValueError(f"Unknown human interaction request type: {request_type!r}")
    now = datetime.now(timezone.utc)
    req = HumanInteractionRequest(
        request_id=_new_id(),
        user_id=user_id,
        task_id=task_id,
        request_type=request_type,
        status="PENDING",
        title=title,
        message=message,
        safe_metadata=safe_metadata or {},
        created_at=now,
        expires_at=(now + timedelta(minutes=expires_in_minutes)) if expires_in_minutes else None,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def get_by_id(db: Session, request_id: str) -> HumanInteractionRequest | None:
    return db.query(HumanInteractionRequest).filter(HumanInteractionRequest.request_id == request_id).first()


def get_active_for_task(db: Session, task_id: str) -> HumanInteractionRequest | None:
    """The most recent still-`PENDING` request for a task, if any — what the
    frontend's `GET /agent/tasks/{id}/human-request` polls for."""
    return (
        db.query(HumanInteractionRequest)
        .filter(HumanInteractionRequest.task_id == task_id, HumanInteractionRequest.status == "PENDING")
        .order_by(HumanInteractionRequest.created_at.desc())
        .first()
    )


def is_expired(req: HumanInteractionRequest, *, now: datetime | None = None) -> bool:
    if req.expires_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    expires_at = req.expires_at if req.expires_at.tzinfo else req.expires_at.replace(tzinfo=timezone.utc)
    return now > expires_at


def try_claim(db: Session, request_id: str, *, new_status: str, from_status: str = "PENDING") -> HumanInteractionRequest | None:
    """Atomically transitions ONE request from `from_status` to `new_status`
    via a single conditional `UPDATE ... WHERE request_id = ? AND status = ?`
    — the authoritative guard against a race between two concurrent
    responses to the same request (duplicate OTP submission), or a response
    racing a cancellation. Whichever caller's UPDATE actually matches a row
    wins; the loser's `WHERE status = from_status` matches zero rows because
    the winner already changed it, so this returns `None` for the loser
    without ever touching `deliver_secret`/`signal_resume`/`confirmed_answers`.

    This is the ONLY place `HumanInteractionRequest.status` should be moved
    OUT of `PENDING` from request-handling code (`app/api/human_interaction.py`)
    — `mark_responded`/`mark_resuming`/etc. below remain for the SUBSEQUENT,
    now-single-owner transitions a winner makes after a successful claim
    (there is no race left to guard once only the winner is still running)."""
    now = datetime.now(timezone.utc)
    values: dict = {"status": new_status}
    if new_status == "RESPONDED":
        values["responded_at"] = now
    if new_status in ("RESOLVED", "EXPIRED", "CANCELLED", "FAILED"):
        values["resolved_at"] = now
    rows_matched = (
        db.query(HumanInteractionRequest)
        .filter(HumanInteractionRequest.request_id == request_id, HumanInteractionRequest.status == from_status)
        .update(values, synchronize_session=False)
    )
    db.commit()
    if rows_matched != 1:
        return None
    return get_by_id(db, request_id)


def _transition(db: Session, req: HumanInteractionRequest, *, status: str, responded_at: bool = False, resolved_at: bool = False) -> HumanInteractionRequest:
    req.status = status
    now = datetime.now(timezone.utc)
    if responded_at:
        req.responded_at = now
    if resolved_at:
        req.resolved_at = now
    db.commit()
    db.refresh(req)
    return req


def mark_responded(db: Session, req: HumanInteractionRequest) -> HumanInteractionRequest:
    return _transition(db, req, status="RESPONDED", responded_at=True)


def mark_resuming(db: Session, req: HumanInteractionRequest) -> HumanInteractionRequest:
    """RESUMING is a strictly intermediate status, so unlike the other
    `mark_*` helpers this one refuses to move a request that has already
    reached a TERMINAL status. Reason: the automation loop and the
    `/respond` request thread run concurrently — the loop is woken by
    `deliver_secret` and can complete the whole consumption (ending in
    RESOLVED, or FAILED if the field vanished) before the API thread executes
    its next statement. An unconditional write here would drag a finished
    request back to a non-terminal state. Call ordering in
    `app/api/human_interaction.py` avoids that window; this makes it
    structurally impossible."""
    if req.status in HUMAN_REQUEST_TERMINAL_STATUSES:
        return req
    return _transition(db, req, status="RESUMING")


def mark_resolved(db: Session, req: HumanInteractionRequest) -> HumanInteractionRequest:
    return _transition(db, req, status="RESOLVED", resolved_at=True)


def mark_expired(db: Session, req: HumanInteractionRequest) -> HumanInteractionRequest:
    return _transition(db, req, status="EXPIRED", resolved_at=True)


def mark_cancelled(db: Session, req: HumanInteractionRequest) -> HumanInteractionRequest:
    return _transition(db, req, status="CANCELLED", resolved_at=True)


def mark_failed(db: Session, req: HumanInteractionRequest) -> HumanInteractionRequest:
    return _transition(db, req, status="FAILED", resolved_at=True)
