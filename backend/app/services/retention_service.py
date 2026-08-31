"""
§9 data retention — the actual purge logic, run either by a scheduled job
(`app/core/scheduler.py`, every user, every category) or on demand by one
user (`POST /profile/retention-policy/purge-now`, that user only). Every
window comes from `retention_repository` (per-user, falling back to the
global defaults documented there).

Deletion order is always: delete the on-disk file(s) first, confirm success,
THEN delete/clear the DB row(s) that reference them — never the other way
around. If a file delete fails, the DB row is left alone too, so the next
run retries both together rather than leaving an orphaned file with nothing
in the database still tracking it.

Scope, stated plainly:
- Screenshots/traces/error-logs exist ONLY for the deterministic (per-ATS-
  adapter) pipeline, under `logs/<application_id>/` — the autonomous agent
  never writes a file to disk (its vision-fallback screenshots are inlined
  as base64 into `ChatMessage.safe_metadata`, already covered by the normal
  chat-message lifecycle, not by this module).
- "Run/step history" purges `AutomationRun` rows for the deterministic path,
  and clears (not deletes — the task row itself survives, same as an
  `Application` row survives its own `AutomationRun` rows being purged)
  `AutonomousTask.action_history`/`current_browser_state` for the autonomous
  path.
- Deliberately no document-retention category, and no `document_retention_
  days` column (removed via migration `e3f4a5b6c7d8` after a brief life as
  a permanently-unenforceable setting — see that migration and
  `RetentionPolicy`'s docstring). There is no per-application generated
  résumé or cover letter in this codebase (that feature was removed) — a
  résumé is always a reference to the user's own permanent document
  library, and auto-deleting one the user may still want to reuse for a
  FUTURE application would be actively harmful, not a retention cleanup.
  `automation/tests/test_retention_service.py::
  test_retention_purge_never_touches_profile_documents` locks this in as an
  invariant. Restoring the column (and real enforcement) is a genuine
  follow-up only if/when per-application generated documents exist again.
- Only genuinely terminal runs/tasks are ever touched — `_TERMINAL_
  APPLICATION_STATUSES`/`_TERMINAL_TASK_STATUSES` mirror the same constants
  `app/services/metrics_repository.py` already uses, so "what counts as
  finished" can never quietly drift between the two features.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import AUTOMATION_LOGS_DIR
from app.models.db_models import AutomationRun, AutonomousTask, HumanInteractionRequest, RetentionPurgeLog, User
from app.services import retention_repository

logger = logging.getLogger(__name__)

_TERMINAL_APPLICATION_STATUSES = ("applied", "failed", "cancelled")
_TERMINAL_TASK_STATUSES = ("COMPLETED", "FAILED", "CANCELLED")
_CONCLUDED_REQUEST_STATUSES = ("RESPONDED", "RESOLVED", "EXPIRED", "CANCELLED", "FAILED")

#: How long a `RetentionPurgeLog` row survives — a fixed, global setting
#: (this log describes SYSTEM-wide job executions, not one user's data, so
#: it isn't part of `RetentionPolicy`). One year is generous on purpose:
#: this is an audit trail for "did the purge job actually run and what did
#: it do", cheap to keep, expensive to need and not have.
PURGE_LOG_RETENTION_DAYS = 365


class PurgeResult:
    def __init__(self, category: str):
        self.category = category
        self.records_purged = 0
        self.files_deleted = 0
        self.files_failed = 0
        self.error: str | None = None

    def as_dict(self) -> dict:
        return {
            "category": self.category, "records_purged": self.records_purged,
            "files_deleted": self.files_deleted, "files_failed": self.files_failed, "error": self.error,
        }


def _run_dir(application_id: str) -> Path:
    return Path(AUTOMATION_LOGS_DIR) / application_id


def _find_terminal_applications(db: Session, user_id: str, cutoff: datetime):
    from app.models.db_models import Application  # local import: avoid a cycle with application_repository

    return (
        db.query(Application)
        .filter(
            Application.user_id == user_id,
            Application.status.in_(_TERMINAL_APPLICATION_STATUSES),
            Application.updated_at < cutoff,
        )
        .all()
    )


def purge_screenshots_for_user(db: Session, user_id: str, policy, *, now: datetime, result: PurgeResult) -> None:
    """Deletes `screenshot*.png`/`vision-field-*.png` files once an
    application is both terminal AND past this window — and, when any were
    actually removed, clears the now-stale `AutomationRun.screenshot_paths`
    references so the UI never points at a file that's gone."""
    cutoff = now - timedelta(days=policy.screenshot_retention_days)
    for application in _find_terminal_applications(db, user_id, cutoff):
        run_dir = _run_dir(application.application_id)
        if not run_dir.exists():
            continue
        deleted_any = False
        for pattern in ("screenshot*.png", "vision-field-*.png"):
            for f in run_dir.glob(pattern):
                try:
                    f.unlink()
                    result.files_deleted += 1
                    deleted_any = True
                except OSError:
                    result.files_failed += 1
        if deleted_any:
            db.query(AutomationRun).filter(AutomationRun.application_id == application.application_id).update(
                {"screenshot_paths": []}, synchronize_session=False,
            )
            result.records_purged += 1
    db.commit()


def purge_run_history_for_user(db: Session, user_id: str, policy, *, now: datetime, result: PurgeResult) -> None:
    """Deterministic: once past this window, removes the REST of
    `logs/<application_id>/` (trace.zip, error.log — whatever
    `purge_screenshots_for_user` already left behind) and every
    `AutomationRun` row for that application, then the now-empty directory
    itself. A failed directory delete skips the DB deletion for that one
    application entirely — no orphaned rows on a storage-delete failure —
    and is retried on the next run.

    Autonomous: clears `action_history`/`current_browser_state` on terminal
    tasks past the same window. The task row itself survives (status,
    `final_result`, timestamps) — only the detailed step-by-step trail is
    dropped, mirroring how an `Application` row outlives its own purged
    `AutomationRun` history."""
    cutoff = now - timedelta(days=policy.run_history_retention_days)

    for application in _find_terminal_applications(db, user_id, cutoff):
        run_dir = _run_dir(application.application_id)
        if run_dir.exists():
            try:
                shutil.rmtree(run_dir)
            except OSError:
                result.files_failed += 1
                continue
            result.files_deleted += 1
        deleted = (
            db.query(AutomationRun)
            .filter(AutomationRun.application_id == application.application_id)
            .delete(synchronize_session=False)
        )
        result.records_purged += deleted
    db.commit()

    tasks = (
        db.query(AutonomousTask)
        .filter(
            AutonomousTask.user_id == user_id,
            AutonomousTask.current_status.in_(_TERMINAL_TASK_STATUSES),
            AutonomousTask.updated_at < cutoff,
        )
        .all()
    )
    for task in tasks:
        if task.action_history or task.current_browser_state is not None:
            task.action_history = []
            task.current_browser_state = None
            result.records_purged += 1
    db.commit()


def purge_hitl_requests_for_user(db: Session, user_id: str, policy, *, now: datetime, result: PurgeResult) -> None:
    """No files involved — `HumanInteractionRequest` never holds a secret
    (enforced structurally elsewhere), so this is a plain row delete once a
    request has concluded (never a still-`PENDING`/`RESUMING` one) and aged
    past the window."""
    cutoff = now - timedelta(days=policy.hitl_request_retention_days)
    deleted = (
        db.query(HumanInteractionRequest)
        .filter(
            HumanInteractionRequest.user_id == user_id,
            HumanInteractionRequest.status.in_(_CONCLUDED_REQUEST_STATUSES),
            HumanInteractionRequest.created_at < cutoff,
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    result.records_purged += deleted


def _record_purge_log(db: Session, results: list[PurgeResult]) -> None:
    for result in results:
        db.add(RetentionPurgeLog(
            purge_id=f"purge_{uuid.uuid4().hex[:12]}",
            category=result.category,
            records_purged=result.records_purged,
            files_deleted=result.files_deleted,
            files_failed=result.files_failed,
            error=result.error,
        ))
    db.commit()


def purge_old_purge_logs(db: Session) -> PurgeResult:
    """The purge log's own retention rule — a fixed global window, not
    per-user (see `PURGE_LOG_RETENTION_DAYS`)."""
    result = PurgeResult("purge_log")
    cutoff = datetime.now(timezone.utc) - timedelta(days=PURGE_LOG_RETENTION_DAYS)
    result.records_purged = (
        db.query(RetentionPurgeLog).filter(RetentionPurgeLog.run_at < cutoff).delete(synchronize_session=False)
    )
    db.commit()
    return result


def run_purge_for_user(db: Session, user_id: str, *, now: datetime | None = None) -> list[dict]:
    """User-triggered purge (`POST /profile/retention-policy/purge-now`) —
    this user's own data only. Writes the same `RetentionPurgeLog` rows a
    scheduled pass would, just with this one user's counts."""
    now = now or datetime.now(timezone.utc)
    policy = retention_repository.get_policy(db, user_id)
    results = [PurgeResult("screenshots"), PurgeResult("run_history"), PurgeResult("hitl_requests")]
    try:
        purge_screenshots_for_user(db, user_id, policy, now=now, result=results[0])
        purge_run_history_for_user(db, user_id, policy, now=now, result=results[1])
        purge_hitl_requests_for_user(db, user_id, policy, now=now, result=results[2])
    except Exception as e:  # noqa: BLE001 - a failed purge must be recorded, not silently lost
        logger.exception("Retention purge failed for user %s.", user_id)
        for r in results:
            if r.error is None and r.records_purged == 0 and r.files_deleted == 0:
                r.error = str(e)
    _record_purge_log(db, results)
    return [r.as_dict() for r in results]


def run_purge_for_all_users(db: Session) -> list[dict]:
    """The scheduled job's entry point — every user, in one pass, each with
    their own resolved policy window."""
    now = datetime.now(timezone.utc)
    user_ids = [row[0] for row in db.query(User.user_id).all()]
    policies = retention_repository.get_all_policies(db)
    default_policy = retention_repository.get_default_policy()

    totals = {"screenshots": PurgeResult("screenshots"), "run_history": PurgeResult("run_history"), "hitl_requests": PurgeResult("hitl_requests")}
    failed_users = 0
    for user_id in user_ids:
        policy = policies.get(user_id, default_policy)
        try:
            purge_screenshots_for_user(db, user_id, policy, now=now, result=totals["screenshots"])
            purge_run_history_for_user(db, user_id, policy, now=now, result=totals["run_history"])
            purge_hitl_requests_for_user(db, user_id, policy, now=now, result=totals["hitl_requests"])
        except Exception:  # noqa: BLE001 - one user's failure must never stop the pass for everyone else
            logger.exception("Retention purge failed for user %s — continuing with the rest.", user_id)
            failed_users += 1

    if failed_users:
        note = f"{failed_users} user(s) failed during this pass — see server logs."
        for r in totals.values():
            r.error = r.error or note

    results = list(totals.values()) + [purge_old_purge_logs(db)]
    _record_purge_log(db, results)
    return [r.as_dict() for r in results]
