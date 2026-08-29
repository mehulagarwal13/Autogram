"""
Append-only decision/approval audit trail — HITL platform
(PHASE2_ARCHITECTURE.md Initiative 3).

Distinct from `automation_runs` (execution mechanics: screenshots, trace,
error log) and `application_questions` (individual answers): this is the
compliance-facing record of who decided what and when — autopilot run
started, human approved/rejected, kill switch triggered. HARD RULE: this
module exposes `record_event`/`list_for_application`/`list_for_autonomous_task`
only. No update, no delete — ever. A mutable audit log defeats the entire
point of keeping one.

Shared between the deterministic per-ATS path (`application_id`) and the
general-purpose autonomous agent (`autonomous_task_id`) — see
`ApplicationAuditLog`'s docstring for why this is one table rather than two.
Exactly one of `application_id` / `autonomous_task_id` must be supplied.

HARD RULE #2 (autonomous-agent events specifically): `metadata` must never
contain an OTP/MFA/password value or any other transient secret — see
`automation/agents/autonomous/loop.py` and `AUTONOMOUS_AGENT.md`'s OTP
section. Only safe context (request_id, request_type, masked destination,
success/failure) belongs here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.db_models import ApplicationAuditLog


def record_event(
    db: Session,
    *,
    user_id: str,
    event_type: str,
    actor: str,
    application_id: str | None = None,
    autonomous_task_id: str | None = None,
    metadata: dict | None = None,
) -> ApplicationAuditLog:
    if not application_id and not autonomous_task_id:
        raise ValueError("record_event requires application_id or autonomous_task_id.")
    entry = ApplicationAuditLog(
        log_id=str(uuid.uuid4()),
        application_id=application_id,
        autonomous_task_id=autonomous_task_id,
        user_id=user_id,
        event_type=event_type,
        actor=actor,
        event_metadata=metadata,
        created_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def list_for_application(db: Session, application_id: str) -> list[ApplicationAuditLog]:
    return (
        db.query(ApplicationAuditLog)
        .filter(ApplicationAuditLog.application_id == application_id)
        .order_by(ApplicationAuditLog.created_at.desc())
        .all()
    )


def list_for_autonomous_task(db: Session, task_id: str) -> list[ApplicationAuditLog]:
    return (
        db.query(ApplicationAuditLog)
        .filter(ApplicationAuditLog.autonomous_task_id == task_id)
        .order_by(ApplicationAuditLog.created_at.desc())
        .all()
    )
