"""
`app/services/metrics_repository.py` — the aggregation layer behind
`GET /metrics/summary` (the four success metrics named in the original
planning doc). Needs real Postgres (real aggregate queries across real
tables, not something worth faking); skips cleanly without one, same
convention `test_duplicate_automation_guard.py` uses.

Each test builds a handful of throwaway rows against a fresh, uniquely-named
user and tears them down afterward — no shared fixtures with production data.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.database import SessionLocal, engine
from app.models.db_models import (
    Application,
    ApplicationQuestion,
    AutomationRun,
    AutonomousTask,
    HumanInteractionRequest,
    User,
)
from app.services import application_repository, autonomous_task_repository as task_repo
from app.services import metrics_repository
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
    uid = f"metrics_{uuid.uuid4().hex[:10]}"
    db.add(User(user_id=uid, email=f"{uid}@example.com", password_hash="x"))
    db.commit()
    yield db, uid
    db.rollback()
    db.query(HumanInteractionRequest).filter(HumanInteractionRequest.user_id == uid).delete()
    db.query(AutonomousTask).filter(AutonomousTask.user_id == uid).delete()
    db.query(Application).filter(Application.user_id == uid).delete()
    db.query(User).filter(User.user_id == uid).delete()
    db.commit()
    db.close()


def _make_application(db, uid, url, *, status, hours_ago_created=5, applied=False) -> Application:
    now = datetime.now(timezone.utc)
    app = Application(
        application_id=f"metrics_app_{uuid.uuid4().hex[:10]}",
        user_id=uid, job_url=url, job_url_hash=compute_job_url_hash(url),
        status=status, autopilot_enabled=False,
        created_at=now - timedelta(hours=hours_ago_created),
        updated_at=now,
        applied_date=now if applied else None,
    )
    db.add(app)
    db.commit()
    return app


# ---------------------------------------------------------------------------
# deterministic_metrics
# ---------------------------------------------------------------------------

@db_required
def test_deterministic_metrics_are_all_none_with_no_applications(user):
    db, uid = user
    result = metrics_repository.deterministic_metrics(db, uid)
    assert result == {
        "total": 0, "median_hours_to_outcome": None,
        "clean_submission_rate": None, "auto_answered_question_rate": None,
    }


@db_required
def test_deterministic_median_hours_only_counts_terminal_applications(user):
    db, uid = user
    _make_application(db, uid, "https://a.example.com/1", status="applied", hours_ago_created=10, applied=True)
    _make_application(db, uid, "https://a.example.com/2", status="processing", hours_ago_created=999)  # not terminal

    result = metrics_repository.deterministic_metrics(db, uid)

    assert result["total"] == 2
    # Only the applied one is terminal — its median comes out ~10h, nowhere
    # near the 999h the still-processing one would have dragged it to.
    assert 9.9 <= result["median_hours_to_outcome"] <= 10.1


@db_required
def test_clean_submission_rate_excludes_applications_that_needed_review(user):
    db, uid = user
    clean = _make_application(db, uid, "https://a.example.com/clean", status="applied", applied=True)
    escalated = _make_application(db, uid, "https://a.example.com/escalated", status="applied", applied=True)
    db.add(AutomationRun(
        run_id=str(uuid.uuid4()), application_id=escalated.application_id,
        status="needs_review", screenshot_paths=[],
    ))
    db.add(AutomationRun(
        run_id=str(uuid.uuid4()), application_id=clean.application_id,
        status="applied", screenshot_paths=[],
    ))
    db.commit()

    result = metrics_repository.deterministic_metrics(db, uid)

    assert result["clean_submission_rate"] == 0.5


@db_required
def test_auto_answered_question_rate_counts_only_deterministic_sources(user):
    db, uid = user
    app = _make_application(db, uid, "https://a.example.com/q", status="needs_review")
    for source in ("profile", "answer_memory", "llm", "needs_user_input", "human"):
        db.add(ApplicationQuestion(
            question_id=str(uuid.uuid4()), application_id=app.application_id,
            question_text=f"q-{source}", field_type="text", source=source,
            confidence=0.9, confidence_level="HIGH",
        ))
    db.commit()

    result = metrics_repository.deterministic_metrics(db, uid)

    # 3 of 5 sources (profile, answer_memory, llm) didn't need a human.
    assert result["auto_answered_question_rate"] == 0.6


# ---------------------------------------------------------------------------
# autonomous_metrics
# ---------------------------------------------------------------------------

@db_required
def test_autonomous_metrics_are_all_none_with_no_tasks(user):
    db, uid = user
    result = metrics_repository.autonomous_metrics(db, uid)
    assert result == {
        "total": 0, "median_hours_to_outcome": None,
        "hitl_resolution_rate": None, "fully_autonomous_completion_rate": None,
    }


@db_required
def test_hitl_resolution_rate_excludes_still_pending_requests(user):
    db, uid = user
    task = task_repo.create_task(db, user_id=uid, job_url="https://a.example.com/agent", original_objective="test")
    db.add(HumanInteractionRequest(
        request_id=str(uuid.uuid4()), user_id=uid, task_id=task.task_id,
        request_type="LOGIN_REQUIRED", status="RESOLVED", message="x",
    ))
    db.add(HumanInteractionRequest(
        request_id=str(uuid.uuid4()), user_id=uid, task_id=task.task_id,
        request_type="CAPTCHA_REQUIRED", status="EXPIRED", message="x",
    ))
    db.add(HumanInteractionRequest(
        request_id=str(uuid.uuid4()), user_id=uid, task_id=task.task_id,
        request_type="ANSWER_REQUIRED", status="PENDING", message="x",
    ))
    db.commit()

    result = metrics_repository.autonomous_metrics(db, uid)

    # 1 resolved, 1 expired, 1 still-pending (excluded from the denominator).
    assert result["hitl_resolution_rate"] == 0.5


@db_required
def test_fully_autonomous_completion_rate_excludes_tasks_that_ever_needed_a_human(user):
    db, uid = user
    solo = task_repo.create_task(db, user_id=uid, job_url="https://a.example.com/solo", original_objective="test")
    task_repo.set_status(db, solo, "COMPLETED")

    needed_human = task_repo.create_task(db, user_id=uid, job_url="https://a.example.com/needed-human", original_objective="test")
    db.add(HumanInteractionRequest(
        request_id=str(uuid.uuid4()), user_id=uid, task_id=needed_human.task_id,
        request_type="LOGIN_REQUIRED", status="RESOLVED", message="x",
    ))
    db.commit()
    task_repo.set_status(db, needed_human, "COMPLETED")

    result = metrics_repository.autonomous_metrics(db, uid)

    assert result["total"] == 2
    assert result["fully_autonomous_completion_rate"] == 0.5
