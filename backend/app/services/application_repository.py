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

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.db_models import Application, AutomationRun, VALID_APPLICATION_STATUSES

# HITL platform — every DB status mapped to the cleaner vocabulary the
# dashboard/API expose (`ApplicationResponse.display_status`). Purely a
# presentation-layer mapping: the underlying `Application.status` values,
# `decide_action`, and the RETRYABLE/IN_PROGRESS/COMPLETED sets in
# `app/api/applications.py` are all unchanged. "DISCOVERED" has no `Application`
# row at all in this system — a job is "discovered" as a `MatchResult` and
# becomes an `Application` (status `pending` -> `READY`) only once the user
# starts it, so there is deliberately no DB status that maps to it.
DISPLAY_STATUS_MAP = {
    "pending": "READY",
    "processing": "IN_PROGRESS",
    "manual_required": "WAITING_FOR_HUMAN",
    "needs_review": "WAITING_FOR_REVIEW",
    "copilot_review": "READY_TO_SUBMIT",
    "applied": "SUBMITTED",
    "failed": "FAILED",
    "cancelled": "CANCELLED",
}

# The "action needed" queue (`GET /applications/reviews`) — every status where
# a human still has something to do before this application is done.
REVIEW_QUEUE_STATUSES = frozenset({"manual_required", "needs_review", "copilot_review"})


def display_status(application: Application) -> str:
    return DISPLAY_STATUS_MAP.get(application.status, application.status.upper())


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


#: Statuses where an attempt never actually submitted and is therefore
#: resumable IN PLACE — mirrors `app/api/applications.py::RETRYABLE_STATUSES`.
#: Kept here so the "which attempt can be retried?" query and the route's own
#: branch cannot drift apart.
_RETRYABLE_STATUSES = ("failed", "manual_required", "needs_review")


def get_by_user_and_url(db: Session, user_id: str, job_url: str) -> Application | None:
    """The MOST RECENT attempt for this (user, job), or `None`.

    ⚠ Ambiguous by nature now that a job can have several attempts (see
    `Application.__table_args__`): "the" application for a job is no longer a
    well-defined thing. Prefer one of the explicit lookups below, which say
    which attempt they mean:

    * `get_retryable_attempt_for_job` — an unfinished attempt to resume;
    * `automation_ownership.find_active_automation` — who is automating now;
    * `automation_ownership.find_submitted_application` — the latest success;
    * `get_by_id` — a specific attempt.

    Retained (a) because external callers and tests still reference it, and
    (b) as the honest "latest attempt, whatever it is" query. It is explicitly
    ordered newest-first so it is at least deterministic — it previously used a
    bare `.first()` with no ordering, which returned an arbitrary row the
    moment more than one existed.
    """
    job_url_hash = compute_job_url_hash(job_url)
    return (
        db.query(Application)
        .filter(Application.user_id == user_id, Application.job_url_hash == job_url_hash)
        .order_by(Application.created_at.desc())
        .first()
    )


def get_retryable_attempt_for_job(db: Session, user_id: str, job_url: str) -> Application | None:
    """The most recent attempt for this (user, job) that can be retried IN
    PLACE — i.e. one that never submitted (`failed`, `manual_required`,
    `needs_review`).

    This is what `POST /applications/start` uses to decide "resume the
    existing attempt" vs "insert a new one", and it is deliberately narrow:
    an `applied` attempt must never be selected here, because retrying it
    would reset `status`/`applied_date` and erase the submission history. An
    in-progress attempt is likewise excluded — that case is already refused
    upstream by the active-automation guard.
    """
    job_url_hash = compute_job_url_hash(job_url)
    return (
        db.query(Application)
        .filter(
            Application.user_id == user_id,
            Application.job_url_hash == job_url_hash,
            Application.status.in_(_RETRYABLE_STATUSES),
        )
        .order_by(Application.created_at.desc())
        .first()
    )


def list_attempts_for_job(db: Session, user_id: str, job_url: str) -> list[Application]:
    """Every attempt for this (user, job), newest first — the full history a
    re-application preserves rather than overwrites."""
    job_url_hash = compute_job_url_hash(job_url)
    return (
        db.query(Application)
        .filter(Application.user_id == user_id, Application.job_url_hash == job_url_hash)
        .order_by(Application.created_at.desc())
        .all()
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
    source: str = "server_automation",
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
        source=source,
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


def retry_application(
    db: Session,
    application: Application,
    *,
    company: str | None,
    position: str | None,
    autopilot_enabled: bool,
    source: str = "server_automation",
) -> Application:
    """Re-arms a `failed`/`manual_required`/`needs_review` Application for
    another attempt on the SAME row: keeps `application_id`, `created_at`,
    `job_url`/`job_url_hash` (so the unique constraint stays satisfied and
    its `AutomationRun` history in `apply_run_result` stays intact), but
    clears every field the previous attempt left behind and refreshes the
    per-run fields from this new request — exactly what a brand-new
    Application would get, just without inserting a second row. `source` is
    refreshed too, so retrying via the extension after a server-automation
    attempt failed (or vice versa) switches delivery mechanism cleanly."""
    application.status = "pending"
    application.failure_reason = None
    application.confidence_score = 0.0
    application.company = company
    application.position = position
    application.autopilot_enabled = autopilot_enabled
    application.source = source
    application.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(application)
    return application


def mark_processing(db: Session, application: Application) -> Application:
    application.status = "processing"
    application.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(application)
    return application


def report_status(
    db: Session, application: Application, *, status: str, reason: str | None = None, confidence: float | None = None,
) -> Application:
    """`POST /applications/{id}/report-status` — the browser extension's own
    self-reported progress (there is no server-side Playwright run to derive
    this from, since `source == "browser_extension"`; see
    `app/api/applications.py::start_application`). One endpoint covers both
    "waiting on you for a CAPTCHA" (`status="manual_required"`) and a final
    outcome (`applied`/`failed`/`needs_review`/`copilot_review`/`cancelled`)
    after the human clicks submit on the real page themselves — the caller
    validates `status` against `VALID_APPLICATION_STATUSES` before this is
    ever called."""
    if status not in VALID_APPLICATION_STATUSES:
        raise ValueError(f"Invalid status {status!r}. Must be one of {sorted(VALID_APPLICATION_STATUSES)}.")
    application.status = status
    application.failure_reason = reason
    if confidence is not None:
        application.confidence_score = confidence
    if status == "applied":
        application.applied_date = datetime.now(timezone.utc)
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
    # Observability only (never read for any safety decision): the pre-flight
    # guess, kept separate from `ats_platform` above so a run resolved by
    # GenericAdapter is never displayed as though a dedicated adapter ran it
    # (see `automation.interfaces.ApplicationRunResult.detected_ats_platform`).
    # Falls back to `result.ats_platform` for older/hand-built results that
    # never set it, so this column is never spuriously blank for those.
    application.detected_ats_platform = getattr(result, "detected_ats_platform", None) or result.ats_platform
    application.status = result.status
    application.confidence_score = result.confidence
    application.pages_completed = getattr(result, "pages_completed", None)
    # "Apply from Job Link": fills in company/position only when the caller
    # never supplied one (a bare pasted URL, no hints) — never overwrites an
    # explicit value the user or a match card already gave us.
    if not application.company and getattr(result, "detected_company", None):
        application.company = result.detected_company
    if not application.position and getattr(result, "detected_position", None):
        application.position = result.detected_position
    if result.status == "applied":
        application.applied_date = datetime.now(timezone.utc)
    # manual_required's reason matters just as much as failed's — e.g. "this
    # field is missing from your profile" is exactly what should surface on
    # GET /applications/{id}, not just buried in the per-run AutomationRun row.
    application.failure_reason = result.error_log if result.status in ("failed", "manual_required") else None
    application.updated_at = datetime.now(timezone.utc)

    run = AutomationRun(
        run_id=str(uuid.uuid4()),
        application_id=application.application_id,
        finished_at=datetime.now(timezone.utc),
        status=result.status,
        screenshot_paths=result.screenshot_paths or [],
        trace_path=result.trace_path,
        error_log=result.error_log,
        log_lines=getattr(result, "log_lines", None) or None,
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


# ---------- HITL platform: dashboard, review queue, cancel, duplicate check ----------

def get_overview_counts(db: Session, user_id: str) -> dict:
    """Per-user counts for the dashboard's overview tiles (§9). One GROUP BY
    query, then bucketed through the same `DISPLAY_STATUS_MAP` everything
    else uses, so the tiles can never disagree with an application's own
    `display_status`."""
    rows = (
        db.query(Application.status, func.count(Application.application_id))
        .filter(Application.user_id == user_id)
        .group_by(Application.status)
        .all()
    )
    by_status = {status: count for status, count in rows}
    total = sum(by_status.values())

    def _count(*statuses: str) -> int:
        return sum(by_status.get(s, 0) for s in statuses)

    return {
        "total": total,
        "submitted": _count("applied"),
        "in_progress": _count("pending", "processing"),
        "waiting_for_human": _count("manual_required"),
        "waiting_for_review": _count("needs_review", "copilot_review"),
        "failed": _count("failed"),
        "cancelled": _count("cancelled"),
    }


def list_reviews_for_user(db: Session, user_id: str) -> list[Application]:
    """The "action needed" queue — every application of this user's still
    waiting on a human, oldest first (so the longest-waiting review surfaces
    first, not last)."""
    return (
        db.query(Application)
        .filter(Application.user_id == user_id, Application.status.in_(REVIEW_QUEUE_STATUSES))
        .order_by(Application.updated_at.asc())
        .all()
    )


def mark_cancelled(db: Session, application: Application, *, reason: str | None = None) -> Application:
    """A human explicitly declined to continue (reject / go-back-and-edit
    before submission) — distinct from `failed` (nothing malfunctioned)."""
    application.status = "cancelled"
    application.failure_reason = reason
    application.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(application)
    return application


def mark_waiting_for_human(db: Session, application_id: str, *, reason: str | None = None) -> None:
    """Live status update from *inside* a running `ApplicationFlowManager`
    (see its CAPTCHA/human-gate wait-and-poll). Deliberately takes an
    `application_id`, not an already-loaded `Application`/session: the flow
    manager runs on its own dedicated thread (see
    `app/api/applications.py::_run_on_dedicated_thread`) and must never touch
    the outer request's SQLAlchemy session, which belongs to a different
    thread and may already be closed by the time this fires. Callers pass a
    short-lived `SessionLocal()` scoped to just this one write."""
    application = get_by_id(db, application_id)
    if application is None:
        return
    application.status = "manual_required"
    application.failure_reason = reason
    application.updated_at = datetime.now(timezone.utc)
    db.commit()


def find_possible_duplicate(db: Session, user_id: str, *, company: str | None, position: str | None) -> Application | None:
    """Secondary duplicate signal (§14): the URL-based `UniqueConstraint` is
    the hard, authoritative check `POST /applications/start` already enforces;
    this is a soft, best-effort warning using company+position when a
    candidate might be applying to the same role via a different link (a
    second job board listing, a referral link, etc.). Case-insensitive,
    excludes `cancelled`/`failed` attempts (those were never a real
    application on file) and requires both fields since either alone is too
    common a string to mean anything."""
    if not company or not position:
        return None
    return (
        db.query(Application)
        .filter(
            Application.user_id == user_id,
            func.lower(Application.company) == company.strip().lower(),
            func.lower(Application.position) == position.strip().lower(),
            Application.status.notin_(("cancelled", "failed")),
        )
        .order_by(Application.created_at.desc())
        .first()
    )
