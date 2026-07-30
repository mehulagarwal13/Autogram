"""
Data access for the auto-apply tracking system: `Application` (one row per
(user, job) apply attempt) and `AutomationRun` (one row per actual
`ApplicationFlowManager.run()` attempt against that application).

This module is the boundary `automation.interfaces` docstring describes:
`automation/` never opens a DB session or imports these models itself for
its return value — it hands back a plain `ApplicationRunResult`
(`automation/interfaces.py`), and `app/api/applications.py` calls
`apply_run_result()` here to persist it. Idempotency (never double-apply to
the same job) is enforced via `job_url_hash` + the DB's own unique
constraint (`uq_applications_user_job_url`), the same pattern
`job_ingestion.py::compute_dedup_key` uses for cross-source job dedup.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.db_models import Application, AutomationRun


def compute_job_url_hash(job_url: str) -> str:
    """Same shape as `app/services/file_storage.py::compute_file_hash` and
    `job_ingestion.py::compute_dedup_key` — sha256 over the normalized
    (lower-cased, trimmed) URL, so trivially different casings/whitespace
    don't slip past the idempotency check."""
    normalized = job_url.strip().lower()
    return hashlib.sha256(normalized.encode()).hexdigest()


# ---------- applications ----------

def get_by_id(db: Session, application_id: str) -> Application | None:
    return db.query(Application).filter(Application.application_id == application_id).first()


def get_by_user_and_url(db: Session, user_id: str, job_url: str) -> Application | None:
    """The idempotency check `POST /applications/start` runs before creating
    anything new — returns the existing attempt for this (user, job), if any."""
    job_url_hash = compute_job_url_hash(job_url)
    return (
        db.query(Application)
        .filter(Application.user_id == user_id, Application.job_url_hash == job_url_hash)
        .first()
    )


def list_for_user(db: Session, user_id: str) -> list[Application]:
    return (
        db.query(Application)
        .filter(Application.user_id == user_id)
        .order_by(Application.created_at.desc())
        .all()
    )


def create_application(
    db: Session,
    user_id: str,
    job_url: str,
    *,
    autopilot_enabled: bool = False,
    company: str | None = None,
    position: str | None = None,
) -> Application:
    application = Application(
        application_id=str(uuid.uuid4()),
        user_id=user_id,
        job_url=job_url,
        job_url_hash=compute_job_url_hash(job_url),
        company=company,
        position=position,
        status="pending",
        autopilot_enabled=autopilot_enabled,
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


def mark_processing(db: Session, application: Application) -> Application:
    application.status = "processing"
    application.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(application)
    return application


def mark_unsupported_ats(db: Session, application: Application, *, ats_platform: str, confidence: float) -> Application:
    """No `ATSAdapter` is implemented yet for `ats_platform` (see
    `automation/ats/registry.py`) — routes straight to `needs_review` instead
    of attempting automation and hitting a stub's `NotImplementedError`."""
    application.ats_platform = ats_platform
    application.confidence_score = confidence
    application.status = "needs_review"
    application.failure_reason = f"No automation adapter implemented yet for '{ats_platform}' (Phase 7)."
    application.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(application)
    return application


def apply_run_result(db: Session, application: Application, result) -> Application:
    """Persists an `automation.interfaces.ApplicationRunResult` (duck-typed —
    accepts anything with `.status`/`.ats_platform`/`.confidence`/
    `.screenshot_paths`/`.trace_path`/`.error_log`): updates the `Application`
    row and appends a new `AutomationRun` row so a retried application keeps
    its full run history (§14 Logging and Debugging)."""
    application.ats_platform = result.ats_platform
    application.status = result.status
    application.confidence_score = result.confidence
    if result.status == "applied":
        application.applied_date = datetime.now(timezone.utc)
    application.failure_reason = result.error_log if result.status == "failed" else None
    application.updated_at = datetime.now(timezone.utc)

    run = AutomationRun(
        run_id=str(uuid.uuid4()),
        application_id=application.application_id,
        finished_at=datetime.now(timezone.utc),
        status=result.status,
        screenshot_paths=result.screenshot_paths or [],
        trace_path=result.trace_path,
        error_log=result.error_log,
        retry_count=count_runs(db, application.application_id),
    )
    db.add(run)
    db.commit()
    db.refresh(application)
    return application


# ---------- automation runs ----------

def list_runs(db: Session, application_id: str) -> list[AutomationRun]:
    return (
        db.query(AutomationRun)
        .filter(AutomationRun.application_id == application_id)
        .order_by(AutomationRun.started_at.desc())
        .all()
    )


def count_runs(db: Session, application_id: str) -> int:
    return db.query(AutomationRun).filter(AutomationRun.application_id == application_id).count()
