"""
Append-only decision/approval audit trail — HITL platform
(PHASE2_ARCHITECTURE.md Initiative 3).

Distinct from `automation_runs` (execution mechanics: screenshots, trace,
error log) and `application_questions` (individual answers): this is the
compliance-facing record of who decided what and when — autopilot run
started, human approved/rejected, kill switch triggered. HARD RULE: this
module exposes `record_event`/`list_for_application` only. No update, no
delete — ever. A mutable audit log defeats the entire point of keeping one.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.db_models import ApplicationAuditLog


def record_event(
    db: Session,
    *,
    application_id: str,
    user_id: str,
    event_type: str,
    actor: str,
    metadata: dict | None = None,
) -> ApplicationAuditLog:
    entry = ApplicationAuditLog(
        log_id=str(uuid.uuid4()),
        application_id=application_id,
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
