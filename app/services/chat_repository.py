"""
Persistence for the user-visible chat transcript (`ChatMessage`).

This is the presentation layer for human-in-the-loop, and deliberately NOT a
replacement for either of the two records that already exist:

* `HumanInteractionRequest` remains the state machine for a pause (PENDING ->
  RESPONDED -> RESOLVED/EXPIRED), and remains the thing resume/expiry logic
  reads. A chat message may *point at* one, but never drives it.
* `ApplicationAuditLog` remains the append-only compliance trail.

## The secret rule, enforced here rather than trusted

`ChatMessage.content` is persisted prose that every future `GET` returns. An
OTP or MFA code written into it would be exactly the leak the whole HITL design
exists to prevent — and unlike `TaskHandle.pending_secret`, which lives in
memory for milliseconds, this one would survive forever and be re-served on
every page load.

So `record_user_reply` REFUSES to store the reply text for a request whose type
is in `SECRET_HUMAN_REQUEST_TYPES`. Callers that handle a verification code
must use `record_secret_submission`, which writes a fixed, code-free
acknowledgement line. That makes the safe path the easy path and the unsafe one
impossible, instead of relying on every future caller remembering the rule.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.db_models import (
    SECRET_HUMAN_REQUEST_TYPES,
    VALID_CHAT_ROLES,
    ChatMessage,
)

logger = logging.getLogger(__name__)

#: What the transcript shows in place of a verification code the user submitted.
#: Fixed text, never interpolated with anything the user typed.
SECRET_SUBMITTED_PLACEHOLDER = "Verification code submitted."


def _new_id() -> str:
    return f"chatmsg_{uuid.uuid4().hex}"


def _add(
    db: Session,
    *,
    user_id: str,
    role: str,
    content: str,
    application_id: str | None,
    autonomous_task_id: str | None,
    human_request_id: str | None = None,
    safe_metadata: dict | None = None,
) -> ChatMessage:
    if role not in VALID_CHAT_ROLES:
        raise ValueError(f"Unknown chat role: {role!r}")
    # Same mutually-exclusive convention `audit_log_repository.record_event`
    # enforces, for the same reason: one transcript table serves both
    # automation paths, so a row that belongs to neither (or both) is a bug
    # that would otherwise surface much later as a mis-rendered conversation.
    if bool(application_id) == bool(autonomous_task_id):
        raise ValueError("Exactly one of application_id / autonomous_task_id must be set.")

    message = ChatMessage(
        message_id=_new_id(),
        user_id=user_id,
        application_id=application_id,
        autonomous_task_id=autonomous_task_id,
        role=role,
        content=content,
        human_request_id=human_request_id,
        safe_metadata=safe_metadata,
        created_at=datetime.now(timezone.utc),
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def record_agent_message(
    db: Session, *, user_id: str, content: str,
    application_id: str | None = None, autonomous_task_id: str | None = None,
    human_request_id: str | None = None, safe_metadata: dict | None = None,
) -> ChatMessage:
    """Autogram speaking: a question, a pause explanation, a progress note.

    `human_request_id` turns the message into an ANSWERABLE prompt — the
    frontend renders the control matching that request's type (OTP field,
    "CAPTCHA completed" button, free-text box) instead of plain prose.
    """
    return _add(
        db, user_id=user_id, role="agent", content=content,
        application_id=application_id, autonomous_task_id=autonomous_task_id,
        human_request_id=human_request_id, safe_metadata=safe_metadata,
    )


def record_system_message(
    db: Session, *, user_id: str, content: str,
    application_id: str | None = None, autonomous_task_id: str | None = None,
    safe_metadata: dict | None = None,
) -> ChatMessage:
    """A workflow milestone rendered inline in the conversation — "Application
    submitted", "Automation paused". Distinct from `agent` so the UI can style
    it as a status line rather than as something that spoke to the user."""
    return _add(
        db, user_id=user_id, role="system", content=content,
        application_id=application_id, autonomous_task_id=autonomous_task_id,
        safe_metadata=safe_metadata,
    )


def record_user_reply(
    db: Session, *, user_id: str, content: str,
    application_id: str | None = None, autonomous_task_id: str | None = None,
    human_request_id: str | None = None, request_type: str | None = None,
) -> ChatMessage:
    """The human's own words, echoed back into the transcript.

    Raises `ValueError` if `request_type` is a secret-bearing type. That is a
    programming error, not a user error: a caller reaching here with an OTP in
    hand has taken the wrong path and must call `record_secret_submission`
    instead. Failing loudly is the point — silently storing it would be an
    unrecoverable leak, and silently dropping it would hide a real bug.
    """
    if request_type in SECRET_HUMAN_REQUEST_TYPES:
        raise ValueError(
            f"Refusing to persist a chat reply for {request_type!r}: the response carries a "
            "verification code. Use record_secret_submission() instead."
        )
    return _add(
        db, user_id=user_id, role="user", content=content,
        application_id=application_id, autonomous_task_id=autonomous_task_id,
        human_request_id=human_request_id,
    )


def record_secret_submission(
    db: Session, *, user_id: str, human_request_id: str, request_type: str,
    application_id: str | None = None, autonomous_task_id: str | None = None,
) -> ChatMessage:
    """Record THAT a verification code was submitted, never the code.

    The content is a constant — the submitted value is not passed to this
    function at all, so there is no argument through which a code could reach
    the database even by mistake.
    """
    return _add(
        db, user_id=user_id, role="user", content=SECRET_SUBMITTED_PLACEHOLDER,
        application_id=application_id, autonomous_task_id=autonomous_task_id,
        human_request_id=human_request_id,
        safe_metadata={"request_type": request_type, "secret_redacted": True},
    )


def list_for_application(db: Session, application_id: str) -> list[ChatMessage]:
    return (
        db.query(ChatMessage)
        .filter(ChatMessage.application_id == application_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )


def list_for_task(db: Session, task_id: str) -> list[ChatMessage]:
    return (
        db.query(ChatMessage)
        .filter(ChatMessage.autonomous_task_id == task_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
