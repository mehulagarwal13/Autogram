"""
Integration tests for the atomic-claim race-condition guards added in the
hardening pass: `human_interaction_repository.try_claim`,
`autonomous_task_repository.try_claim_for_resume`, the terminal-state guard
(`TerminalTaskError`), and startup orphan reconciliation
(`runner.py::reconcile_orphaned_tasks_on_startup`).

These specifically need a REAL database — the whole point is proving two
concurrent sessions racing a conditional `UPDATE` against the same row only
ever let one of them win. Skipped (not failed) if no real Postgres is
reachable, the same convention `conftest.py`'s `browser`/`page`/
`requires_chromium` fixtures already use for an optional real dependency
(Chromium) that isn't guaranteed to be present in every environment this
suite runs in.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text

from app.core.database import SessionLocal, engine
from app.models.db_models import AutonomousTask, HumanInteractionRequest, User
from app.services import audit_log_repository
from app.services import autonomous_task_repository as task_repo
from app.services import human_interaction_repository as human_interaction_repo
from app.services.autonomous_task_repository import TerminalTaskError
from automation.agents.autonomous.runner import reconcile_orphaned_tasks_on_startup


def _real_db_available() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _real_db_available(),
    reason="No reachable Postgres in this environment — these tests exercise real DB-level atomicity.",
)


@pytest.fixture
def user_and_task():
    """A throwaway user + task, cleaned up after the test regardless of
    outcome."""
    db = SessionLocal()
    uid = f"racetest_{uuid.uuid4().hex[:10]}"
    user = User(user_id=uid, email=f"{uid}@example.com", password_hash="x")
    db.add(user)
    db.commit()
    task = task_repo.create_task(db, user_id=uid, job_url="https://example.com/apply", original_objective="test")
    yield db, task
    db.query(HumanInteractionRequest).filter(HumanInteractionRequest.task_id == task.task_id).delete()
    db.query(AutonomousTask).filter(AutonomousTask.task_id == task.task_id).delete()
    db.query(User).filter(User.user_id == uid).delete()
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# 1. Duplicate response / cancellation race on a HumanInteractionRequest
# ---------------------------------------------------------------------------

def test_two_concurrent_claims_on_the_same_request_only_one_wins(user_and_task):
    db, task = user_and_task
    req = human_interaction_repo.create_request(
        db, user_id=task.user_id, task_id=task.task_id, request_type="OTP_REQUIRED", message="Enter the code",
    )

    results = [None, None]

    def _claim(i, new_status):
        thread_db = SessionLocal()
        try:
            claimed = human_interaction_repo.try_claim(thread_db, req.request_id, new_status=new_status)
            results[i] = claimed.status if claimed is not None else None
        finally:
            thread_db.close()

    t1 = threading.Thread(target=_claim, args=(0, "RESPONDED"))
    t2 = threading.Thread(target=_claim, args=(1, "CANCELLED"))  # simulates /respond racing /cancel
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Exactly one of the two calls actually changed the row.
    non_none = [r for r in results if r is not None]
    assert len(non_none) == 1, f"expected exactly one winner, got {results}"

    # `db.expire_all()`: this session's identity map still holds the ORIGINAL
    # in-memory `req` object from `create_request` above — a plain query for
    # the same primary key returns that SAME cached Python object without
    # re-populating it from the row (standard SQLAlchemy behavior; it never
    # clobbers an already-loaded object's attributes on its own). The two
    # threads' claims used their OWN separate sessions and DID persist
    # correctly — this is purely about forcing THIS session to re-read,
    # exactly like every mutating repository function already does via
    # `db.refresh(...)` after its own commits.
    db.expire_all()
    fresh = human_interaction_repo.get_by_id(db, req.request_id)
    assert fresh.status in ("RESPONDED", "CANCELLED")
    assert fresh.status == non_none[0]


def test_claim_fails_once_a_request_is_already_resolved(user_and_task):
    db, task = user_and_task
    req = human_interaction_repo.create_request(
        db, user_id=task.user_id, task_id=task.task_id, request_type="LOGIN_REQUIRED", message="x",
    )
    first = human_interaction_repo.try_claim(db, req.request_id, new_status="RESPONDED")
    assert first is not None

    second = human_interaction_repo.try_claim(db, req.request_id, new_status="RESPONDED")
    assert second is None  # already RESPONDED — cannot be claimed from PENDING again


# ---------------------------------------------------------------------------
# 2. Duplicate resume race on an AutonomousTask (old routes vs new /respond)
# ---------------------------------------------------------------------------

def test_two_concurrent_resume_claims_on_the_same_task_only_one_wins(user_and_task):
    db, task = user_and_task
    task_repo.request_human_intervention(db, task, {"type": "LOGIN_REQUIRED", "message": "sign in"})
    assert task.current_status == "WAITING_FOR_HUMAN"

    results = [None, None]

    def _resume(i):
        thread_db = SessionLocal()
        try:
            thread_task = task_repo.get_by_id(thread_db, task.task_id)
            results[i] = task_repo.try_claim_for_resume(thread_db, thread_task, from_status="WAITING_FOR_HUMAN")
        finally:
            thread_db.close()

    t1 = threading.Thread(target=_resume, args=(0,))
    t2 = threading.Thread(target=_resume, args=(1,))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert sorted(results) == [False, True], f"expected exactly one True, got {results}"

    db.expire_all()  # see the identical comment in the request-level race test above
    fresh = task_repo.get_by_id(db, task.task_id)
    assert fresh.current_status == "RESUMING"
    assert fresh.human_intervention is None


def test_resume_claim_fails_against_a_task_that_is_not_in_the_expected_status(user_and_task):
    db, task = user_and_task
    assert task.current_status == "CREATED"
    assert task_repo.try_claim_for_resume(db, task, from_status="WAITING_FOR_HUMAN") is False


# ---------------------------------------------------------------------------
# 3. Terminal-state guard
# ---------------------------------------------------------------------------

def test_terminal_task_cannot_be_resurrected(user_and_task):
    db, task = user_and_task
    task_repo.mark_completed(db, task, {"decision": "TASK_COMPLETED", "evidence": "done"})
    assert task.current_status == "COMPLETED"

    with pytest.raises(TerminalTaskError):
        task_repo.set_status(db, task, "RUNNING")
    with pytest.raises(TerminalTaskError):
        task_repo.mark_failed(db, task, "should not apply")
    with pytest.raises(TerminalTaskError):
        task_repo.request_human_intervention(db, task, {"type": "LOGIN_REQUIRED", "message": "x"})

    # Still COMPLETED after every rejected attempt.
    fresh = task_repo.get_by_id(db, task.task_id)
    assert fresh.current_status == "COMPLETED"


def test_cancelling_an_already_terminal_task_is_a_harmless_no_op(user_and_task):
    db, task = user_and_task
    task_repo.mark_completed(db, task, {"decision": "TASK_COMPLETED", "evidence": "done"})
    result = task_repo.cancel_task(db, task)  # must NOT raise
    assert result.current_status == "COMPLETED"  # unchanged — cancel never overwrites a real terminal outcome


# ---------------------------------------------------------------------------
# 4. Startup orphan reconciliation
# ---------------------------------------------------------------------------

def test_orphaned_running_task_is_failed_at_startup_and_its_pending_request_expired(user_and_task):
    db, task = user_and_task
    task_repo.set_status(db, task, "RUNNING")
    req = human_interaction_repo.create_request(
        db, user_id=task.user_id, task_id=task.task_id, request_type="OTP_REQUIRED", message="x",
    )
    # Simulate the sliver-of-a-race the docstring calls out: a request that
    # exists even though the task itself never made it to WAITING_FOR_HUMAN.

    count = reconcile_orphaned_tasks_on_startup(db)
    assert count >= 1

    fresh_task = task_repo.get_by_id(db, task.task_id)
    assert fresh_task.current_status == "FAILED"
    assert "restart" in fresh_task.error.lower()

    fresh_req = human_interaction_repo.get_by_id(db, req.request_id)
    assert fresh_req.status == "EXPIRED"


def test_waiting_for_human_task_is_left_alone_by_reconciliation(user_and_task):
    db, task = user_and_task
    task_repo.request_human_intervention(db, task, {"type": "LOGIN_REQUIRED", "message": "sign in"})
    assert task.current_status == "WAITING_FOR_HUMAN"

    reconcile_orphaned_tasks_on_startup(db)

    fresh = task_repo.get_by_id(db, task.task_id)
    assert fresh.current_status == "WAITING_FOR_HUMAN"  # untouched — has its own correct restart handling
