"""
`app/services/retention_service.py` — §9 data retention purge logic. Needs
real Postgres (real terminal-state filtering, real cascade behavior) and a
real filesystem (real delete-then-confirm ordering); skips cleanly without
Postgres, same convention `test_duplicate_automation_guard.py` uses.

Covers exactly what was asked for: past-window purge, within-window
retention, in-flight (non-terminal) protection, a storage-delete failure
never orphaning a DB row, and purge-log correctness.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.database import SessionLocal, engine
from app.models.db_models import (
    Application,
    AutomationRun,
    AutonomousTask,
    CandidateProfile,
    HumanInteractionRequest,
    ProfileDocument,
    RetentionPolicy,
    RetentionPurgeLog,
    User,
)
from app.services import retention_repository, retention_service
from app.services import autonomous_task_repository as task_repo
from app.services.application_repository import compute_job_url_hash


def _db_available() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


db_required = pytest.mark.skipif(not _db_available(), reason="No reachable Postgres.")


@pytest.fixture
def user():
    db = SessionLocal()
    uid = f"retention_{uuid.uuid4().hex[:10]}"
    db.add(User(user_id=uid, email=f"{uid}@example.com", password_hash="x"))
    db.commit()
    yield db, uid
    db.rollback()
    db.query(HumanInteractionRequest).filter(HumanInteractionRequest.user_id == uid).delete()
    db.query(AutonomousTask).filter(AutonomousTask.user_id == uid).delete()
    db.query(Application).filter(Application.user_id == uid).delete()
    db.query(RetentionPolicy).filter(RetentionPolicy.user_id == uid).delete()
    db.query(User).filter(User.user_id == uid).delete()
    db.commit()
    db.close()


@pytest.fixture
def logs_dir(monkeypatch, tmp_path):
    """Points the service at a throwaway directory instead of the real
    `logs/` — monkeypatches the NAME BOUND in `retention_service`'s own
    module namespace, since it imported the constant by value at import
    time."""
    monkeypatch.setattr(retention_service, "AUTOMATION_LOGS_DIR", str(tmp_path))
    return tmp_path


def _application(db, uid, url, *, status, days_old) -> Application:
    now = datetime.now(timezone.utc)
    app = Application(
        application_id=f"retention_app_{uuid.uuid4().hex[:10]}",
        user_id=uid, job_url=url, job_url_hash=compute_job_url_hash(url),
        status=status, autopilot_enabled=False,
        created_at=now - timedelta(days=days_old),
        updated_at=now - timedelta(days=days_old),
    )
    db.add(app)
    db.commit()
    return app


def _write_run_files(logs_dir, application_id, *, screenshots=1, vision_fields=0, trace=True, error_log=True):
    run_dir = logs_dir / application_id
    run_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, screenshots + 1):
        (run_dir / f"screenshot{i}.png").write_bytes(b"fake")
    for i in range(1, vision_fields + 1):
        (run_dir / f"vision-field-{i}.png").write_bytes(b"fake")
    if trace:
        (run_dir / "trace.zip").write_bytes(b"fake")
    if error_log:
        (run_dir / "error.log").write_text("boom")
    return run_dir


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# purge_screenshots_for_user
# ---------------------------------------------------------------------------

@db_required
def test_screenshots_past_the_window_are_deleted_and_references_cleared(user, logs_dir):
    db, uid = user
    app = _application(db, uid, "https://a.example.com/1", status="applied", days_old=40)  # > 30 default
    run_dir = _write_run_files(logs_dir, app.application_id, screenshots=1, vision_fields=1)
    db.add(AutomationRun(run_id=str(uuid.uuid4()), application_id=app.application_id, status="applied", screenshot_paths=["screenshot1.png"]))
    db.commit()

    policy = retention_repository.get_default_policy()
    result = retention_service.PurgeResult("screenshots")
    retention_service.purge_screenshots_for_user(db, uid, policy, now=_now(), result=result)

    assert not (run_dir / "screenshot1.png").exists()
    assert not (run_dir / "vision-field-1.png").exists()
    assert (run_dir / "trace.zip").exists()  # not this category's job
    assert (run_dir / "error.log").exists()
    run = db.query(AutomationRun).filter(AutomationRun.application_id == app.application_id).first()
    assert run.screenshot_paths == []
    assert result.files_deleted == 2
    assert result.records_purged == 1


@db_required
def test_screenshots_within_the_window_are_left_alone(user, logs_dir):
    db, uid = user
    app = _application(db, uid, "https://a.example.com/2", status="applied", days_old=5)  # < 30 default
    run_dir = _write_run_files(logs_dir, app.application_id)

    policy = retention_repository.get_default_policy()
    result = retention_service.PurgeResult("screenshots")
    retention_service.purge_screenshots_for_user(db, uid, policy, now=_now(), result=result)

    assert (run_dir / "screenshot1.png").exists()
    assert result.files_deleted == 0


@db_required
def test_screenshots_are_never_touched_for_a_non_terminal_application(user, logs_dir):
    """In-flight protection: `processing` is not terminal, no matter how old
    `updated_at` claims to be."""
    db, uid = user
    app = _application(db, uid, "https://a.example.com/3", status="processing", days_old=999)
    run_dir = _write_run_files(logs_dir, app.application_id)

    policy = retention_repository.get_default_policy()
    result = retention_service.PurgeResult("screenshots")
    retention_service.purge_screenshots_for_user(db, uid, policy, now=_now(), result=result)

    assert (run_dir / "screenshot1.png").exists()
    assert result.files_deleted == 0


@db_required
def test_screenshots_use_a_customized_shorter_window(user, logs_dir):
    db, uid = user
    retention_repository.update_policy(db, uid, screenshot_retention_days=1)
    app = _application(db, uid, "https://a.example.com/4", status="applied", days_old=5)  # < 30 default, > 1 custom
    run_dir = _write_run_files(logs_dir, app.application_id)

    policy = retention_repository.get_policy(db, uid)
    result = retention_service.PurgeResult("screenshots")
    retention_service.purge_screenshots_for_user(db, uid, policy, now=_now(), result=result)

    assert not (run_dir / "screenshot1.png").exists()


# ---------------------------------------------------------------------------
# purge_run_history_for_user
# ---------------------------------------------------------------------------

@db_required
def test_run_history_past_the_window_removes_the_directory_and_the_run_rows_but_not_the_application(user, logs_dir):
    db, uid = user
    app = _application(db, uid, "https://a.example.com/5", status="applied", days_old=100)  # > 90 default
    run_dir = _write_run_files(logs_dir, app.application_id)
    db.add(AutomationRun(run_id=str(uuid.uuid4()), application_id=app.application_id, status="applied"))
    db.commit()

    policy = retention_repository.get_default_policy()
    result = retention_service.PurgeResult("run_history")
    retention_service.purge_run_history_for_user(db, uid, policy, now=_now(), result=result)

    assert not run_dir.exists()
    assert db.query(AutomationRun).filter(AutomationRun.application_id == app.application_id).count() == 0
    assert db.query(Application).filter(Application.application_id == app.application_id).first() is not None
    assert result.records_purged == 1


@db_required
def test_run_history_within_the_window_is_left_alone(user, logs_dir):
    db, uid = user
    app = _application(db, uid, "https://a.example.com/6", status="applied", days_old=10)  # < 90 default
    run_dir = _write_run_files(logs_dir, app.application_id)
    db.add(AutomationRun(run_id=str(uuid.uuid4()), application_id=app.application_id, status="applied"))
    db.commit()

    policy = retention_repository.get_default_policy()
    result = retention_service.PurgeResult("run_history")
    retention_service.purge_run_history_for_user(db, uid, policy, now=_now(), result=result)

    assert run_dir.exists()
    assert db.query(AutomationRun).filter(AutomationRun.application_id == app.application_id).count() == 1


@db_required
def test_run_history_storage_failure_leaves_the_automation_run_row_intact(user, logs_dir, monkeypatch):
    """The core invariant: a failed file delete must never be followed by a
    DB delete — that would orphan the file with nothing left tracking it."""
    db, uid = user
    app = _application(db, uid, "https://a.example.com/7", status="applied", days_old=100)
    _write_run_files(logs_dir, app.application_id)
    db.add(AutomationRun(run_id=str(uuid.uuid4()), application_id=app.application_id, status="applied"))
    db.commit()

    def _boom(path):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(retention_service.shutil, "rmtree", _boom)

    policy = retention_repository.get_default_policy()
    result = retention_service.PurgeResult("run_history")
    retention_service.purge_run_history_for_user(db, uid, policy, now=_now(), result=result)

    assert db.query(AutomationRun).filter(AutomationRun.application_id == app.application_id).count() == 1
    assert result.files_failed == 1
    assert result.records_purged == 0


@db_required
def test_run_history_clears_autonomous_task_history_but_keeps_the_task(user):
    db, uid = user
    task = task_repo.create_task(db, user_id=uid, job_url="https://a.example.com/agent-1", original_objective="test")
    task_repo.append_action(db, task, {"action_type": "fill", "value": "x"})
    task_repo.update_browser_state(db, task, {"url": "https://a.example.com/agent-1"})
    task_repo.set_status(db, task, "COMPLETED")
    old = datetime.now(timezone.utc) - timedelta(days=100)
    db.query(AutonomousTask).filter(AutonomousTask.task_id == task.task_id).update({"updated_at": old})
    db.commit()

    policy = retention_repository.get_default_policy()
    result = retention_service.PurgeResult("run_history")
    retention_service.purge_run_history_for_user(db, uid, policy, now=_now(), result=result)

    refreshed = db.query(AutonomousTask).filter(AutonomousTask.task_id == task.task_id).first()
    assert refreshed is not None
    assert refreshed.current_status == "COMPLETED"
    assert refreshed.action_history == []
    assert refreshed.current_browser_state is None
    assert result.records_purged == 1


@db_required
def test_run_history_protects_a_non_terminal_autonomous_task(user):
    db, uid = user
    task = task_repo.create_task(db, user_id=uid, job_url="https://a.example.com/agent-2", original_objective="test")
    task_repo.append_action(db, task, {"action_type": "fill", "value": "x"})
    old = datetime.now(timezone.utc) - timedelta(days=999)
    db.query(AutonomousTask).filter(AutonomousTask.task_id == task.task_id).update({"updated_at": old})
    db.commit()

    policy = retention_repository.get_default_policy()
    result = retention_service.PurgeResult("run_history")
    retention_service.purge_run_history_for_user(db, uid, policy, now=_now(), result=result)

    refreshed = db.query(AutonomousTask).filter(AutonomousTask.task_id == task.task_id).first()
    assert refreshed.action_history != []


# ---------------------------------------------------------------------------
# purge_hitl_requests_for_user
# ---------------------------------------------------------------------------

def _hitl_request(db, uid, task_id, *, status, days_old):
    old = datetime.now(timezone.utc) - timedelta(days=days_old)
    req = HumanInteractionRequest(
        request_id=str(uuid.uuid4()), user_id=uid, task_id=task_id,
        request_type="LOGIN_REQUIRED", status=status, message="x", created_at=old,
    )
    db.add(req)
    db.commit()
    return req


@db_required
def test_hitl_requests_past_the_window_are_removed_once_concluded(user):
    db, uid = user
    task = task_repo.create_task(db, user_id=uid, job_url="https://a.example.com/agent-3", original_objective="test")
    req = _hitl_request(db, uid, task.task_id, status="RESOLVED", days_old=20)  # > 14 default
    request_id = req.request_id  # captured now: purging expires `req` on commit

    policy = retention_repository.get_default_policy()
    result = retention_service.PurgeResult("hitl_requests")
    retention_service.purge_hitl_requests_for_user(db, uid, policy, now=_now(), result=result)

    assert db.query(HumanInteractionRequest).filter(HumanInteractionRequest.request_id == request_id).first() is None
    assert result.records_purged == 1


@db_required
def test_hitl_requests_within_the_window_are_left_alone(user):
    db, uid = user
    task = task_repo.create_task(db, user_id=uid, job_url="https://a.example.com/agent-4", original_objective="test")
    req = _hitl_request(db, uid, task.task_id, status="RESOLVED", days_old=3)  # < 14 default

    policy = retention_repository.get_default_policy()
    result = retention_service.PurgeResult("hitl_requests")
    retention_service.purge_hitl_requests_for_user(db, uid, policy, now=_now(), result=result)

    assert db.query(HumanInteractionRequest).filter(HumanInteractionRequest.request_id == req.request_id).first() is not None


@db_required
def test_hitl_requests_never_purge_a_still_pending_one_regardless_of_age(user):
    db, uid = user
    task = task_repo.create_task(db, user_id=uid, job_url="https://a.example.com/agent-5", original_objective="test")
    req = _hitl_request(db, uid, task.task_id, status="PENDING", days_old=999)

    policy = retention_repository.get_default_policy()
    result = retention_service.PurgeResult("hitl_requests")
    retention_service.purge_hitl_requests_for_user(db, uid, policy, now=_now(), result=result)

    assert db.query(HumanInteractionRequest).filter(HumanInteractionRequest.request_id == req.request_id).first() is not None


# ---------------------------------------------------------------------------
# run_purge_for_user / purge log
# ---------------------------------------------------------------------------

@db_required
def test_run_purge_for_user_writes_one_log_row_per_category(user, logs_dir):
    db, uid = user
    before = db.query(RetentionPurgeLog).count()

    results = retention_service.run_purge_for_user(db, uid)

    assert {r["category"] for r in results} == {"screenshots", "run_history", "hitl_requests"}
    after = db.query(RetentionPurgeLog).count()
    assert after - before == 3


@db_required
def test_purge_old_purge_logs_respects_its_own_retention_window(user):
    db, uid = user
    old_id = f"purge_{uuid.uuid4().hex[:10]}"
    recent_id = f"purge_{uuid.uuid4().hex[:10]}"
    db.add(RetentionPurgeLog(
        purge_id=old_id, category="screenshots",
        run_at=datetime.now(timezone.utc) - timedelta(days=retention_service.PURGE_LOG_RETENTION_DAYS + 10),
    ))
    db.add(RetentionPurgeLog(purge_id=recent_id, category="screenshots", run_at=datetime.now(timezone.utc)))
    db.commit()

    try:
        retention_service.purge_old_purge_logs(db)

        assert db.query(RetentionPurgeLog).filter(RetentionPurgeLog.purge_id == old_id).first() is None
        assert db.query(RetentionPurgeLog).filter(RetentionPurgeLog.purge_id == recent_id).first() is not None
    finally:
        db.query(RetentionPurgeLog).filter(RetentionPurgeLog.purge_id == recent_id).delete()
        db.commit()


# ---------------------------------------------------------------------------
# ProfileDocument is never touched by the retention purge (invariant guard)
#
# `RetentionPolicy` briefly had a `document_retention_days` column; it was
# removed (migration `e3f4a5b6c7d8`) once confirmed permanently
# unenforceable — there is no per-application generated résumé/cover-letter
# in this codebase, `Application.resume_used` is a plain FK into the user's
# own PERMANENT `ProfileDocument` library, and purging one just because one
# particular old application referenced it would delete a file the user may
# still want to reuse for a FUTURE application (the opposite of a retention
# cleanup). The setting is gone; the real risk it was guarding against —
# some future purge pass touching `ProfileDocument` at all — is not, so this
# test survives the field's removal in a form that no longer references it:
# regardless of how aggressive every OTHER window is, or how old/terminal
# the referencing application is, a document and the reference to it must
# both come through untouched. If a real per-application-document feature
# is ever built, THIS test is what should fail first and force a deliberate
# decision about it, not silently pass.
# ---------------------------------------------------------------------------

@db_required
def test_retention_purge_never_touches_profile_documents(user, logs_dir):
    db, uid = user
    profile = CandidateProfile(profile_id=f"profile_{uid}", user_id=uid, first_name="A", last_name="B", email=f"{uid}@example.com")
    db.add(profile)
    db.commit()
    doc = ProfileDocument(
        document_id=f"doc_{uuid.uuid4().hex[:10]}", profile_id=profile.profile_id,
        document_type="resume", original_filename="resume.pdf", stored_path="storage/documents/resume/x.pdf",
        file_hash="abc123", is_default=True,
    )
    db.add(doc)
    db.commit()
    document_id = doc.document_id

    # Every other window set to its most aggressive possible value, and an
    # application old enough that every OTHER category would already have
    # purged it, referencing this document.
    retention_repository.update_policy(
        db, uid, screenshot_retention_days=1, run_history_retention_days=1, hitl_request_retention_days=1,
    )
    app = _application(db, uid, "https://a.example.com/doc-test", status="applied", days_old=999)
    app.resume_used = document_id
    db.commit()
    application_id = app.application_id

    retention_service.run_purge_for_user(db, uid)

    assert db.query(ProfileDocument).filter(ProfileDocument.document_id == document_id).first() is not None
    refreshed_app = db.query(Application).filter(Application.application_id == application_id).first()
    assert refreshed_app.resume_used == document_id


@db_required
def test_retention_policy_response_has_no_document_retention_field(user):
    """Schema-level guard for the API surface: the response must never
    resurrect a field that no longer has a backing column — this would
    raise `AttributeError` immediately if `RetentionPolicy` ever grew
    `document_retention_days` back without updating the read path, or
    vice versa."""
    db, uid = user
    policy = retention_repository.get_policy(db, uid)

    assert not hasattr(policy, "document_retention_days")
