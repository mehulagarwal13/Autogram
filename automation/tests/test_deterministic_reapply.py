"""
Deterministic-path deliberate re-application.

The gap this covers: `Application` carried a FULL
`UNIQUE (user_id, job_url_hash)`, so a job could hold exactly one row forever.
The only re-attempt mechanism, `retry_application`, resets that SAME row to
`pending` — fine for an attempt that never submitted, but for an `applied` one
it would erase `status` and `applied_date`, i.e. the record that the user ever
applied. A second deterministic attempt was therefore unrepresentable.

The constraint is now PARTIAL (`uq_applications_active_job`, over
`pending`/`processing`/`copilot_review`), so finished attempts drop out of it
and a new attempt can be inserted alongside them. Neither guarantee the old
constraint provided was lost — see `Application.__table_args__`.

DB-backed tests need real Postgres and skip cleanly without it, the same
convention `test_duplicate_automation_guard.py` uses. Concurrency is proven
with real threads against the real index, never with mocks.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal, engine
from app.models.db_models import Application, AutomationRun, AutonomousTask, User
from app.services import application_repository, automation_ownership
from app.services import autonomous_task_repository as task_repo

JOB_URL = "https://boards.example.com/acme/jobs/777"


# ---------------------------------------------------------------------------
# Definitions that must not drift (no DB needed)
# ---------------------------------------------------------------------------

def test_the_partial_index_predicate_matches_in_progress_statuses():
    """Three places must agree on "active" for the deterministic path: the
    route's `IN_PROGRESS_STATUSES`, the `postgresql_where` on
    `uq_applications_active_job`, and the migration's `_ACTIVE_PREDICATE`. If
    they diverge, the database and the application disagree about who owns a
    job."""
    from app.api.applications import COMPLETED_STATUSES, IN_PROGRESS_STATUSES, RETRYABLE_STATUSES
    from app.models.db_models import Application as ApplicationModel

    index = next(
        ix for ix in ApplicationModel.__table__.indexes
        if ix.name == "uq_applications_active_job"
    )
    assert index.unique
    assert [c.name for c in index.columns] == ["user_id", "job_url_hash"]

    predicate = str(index.dialect_options["postgresql"]["where"])
    for status in IN_PROGRESS_STATUSES:
        assert status in predicate, f"{status} is active but missing from the index"
    # Submitted and retryable attempts must NOT be covered — that is what lets
    # history accumulate and retries keep working.
    for status in COMPLETED_STATUSES | RETRYABLE_STATUSES:
        assert status not in predicate, f"{status} must not be in the active index"


def test_retryable_statuses_agree_between_route_and_repository():
    from app.api.applications import RETRYABLE_STATUSES

    assert set(application_repository._RETRYABLE_STATUSES) == set(RETRYABLE_STATUSES)


def test_both_start_routes_share_one_acknowledgement_schema():
    """One consent vocabulary, not two incompatible ones."""
    from app.api.autonomous_agent import ReapplyAcknowledgement as AutonomousAck
    from app.models.application import ReapplyAcknowledgement as SharedAck

    assert AutonomousAck is SharedAck


# ---------------------------------------------------------------------------
# DB-backed
# ---------------------------------------------------------------------------

def _db_available() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


db_required = pytest.mark.skipif(
    not _db_available(), reason="No reachable Postgres — these exercise the real partial index.",
)


@pytest.fixture
def user():
    db = SessionLocal()
    uid = f"detreapply_{uuid.uuid4().hex[:10]}"
    db.add(User(user_id=uid, email=f"{uid}@example.com", password_hash="x"))
    db.commit()
    yield db, uid
    db.rollback()
    db.query(AutomationRun).filter(
        AutomationRun.application_id.in_(
            db.query(Application.application_id).filter(Application.user_id == uid)
        )
    ).delete(synchronize_session=False)
    db.query(AutonomousTask).filter(AutonomousTask.user_id == uid).delete()
    db.query(Application).filter(Application.user_id == uid).delete()
    db.query(User).filter(User.user_id == uid).delete()
    db.commit()
    db.close()


def _naive(dt):
    """`Application.applied_date` is a naive `DateTime` column, so Postgres
    hands the value back without tzinfo even though the app writes an aware
    UTC datetime (`apply_run_result` uses `datetime.now(timezone.utc)`).
    Compare like-for-like rather than asserting a tzinfo the column cannot
    store — the point of these assertions is that the VALUE is unchanged."""
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo is not None else dt


def _make_application(db, uid, *, status="pending", applied_at=None, url=JOB_URL):
    app = application_repository.create_application(
        db, user_id=uid, job_url=url, autopilot_enabled=False,
        company="Acme", position="SWE", source="server_automation",
    )
    if status != "pending":
        app.status = status
    if applied_at is not None:
        app.applied_date = applied_at
    db.commit()
    db.refresh(app)
    return app


# --- The core: multiple attempts, history preserved -----------------------

@db_required
def test_a_second_attempt_can_now_be_created_alongside_an_applied_one(user):
    """What the full unique constraint made impossible."""
    db, uid = user
    first = _make_application(db, uid, status="applied", applied_at=datetime.now(timezone.utc))
    second = _make_application(db, uid, status="pending")

    assert second.application_id != first.application_id
    attempts = application_repository.list_attempts_for_job(db, uid, JOB_URL)
    assert len(attempts) == 2


@db_required
def test_the_original_applied_attempt_is_untouched_by_a_re_application(user):
    """§4, the most important requirement: preserve complete history."""
    db, uid = user
    t1 = datetime.now(timezone.utc) - timedelta(days=3)
    first = _make_application(db, uid, status="applied", applied_at=t1)
    # Give it a run record, to prove per-attempt history stays attached.
    db.add(AutomationRun(
        run_id=f"run_{uuid.uuid4().hex[:10]}", application_id=first.application_id,
        status="applied", started_at=t1,
    ))
    db.commit()
    original_id, original_created = first.application_id, first.created_at

    second = _make_application(db, uid, status="pending")
    db.expire_all()

    unchanged = application_repository.get_by_id(db, original_id)
    assert unchanged.status == "applied"                 # not reset to pending
    assert _naive(unchanged.applied_date) == _naive(t1)   # not overwritten
    assert unchanged.created_at == original_created
    assert unchanged.application_id == original_id != second.application_id
    # Its run history is still attached to IT, not to the new attempt.
    runs = db.query(AutomationRun).filter(AutomationRun.application_id == original_id).all()
    assert len(runs) == 1
    assert db.query(AutomationRun).filter(
        AutomationRun.application_id == second.application_id
    ).count() == 0


# --- The active guarantee survives ---------------------------------------

@db_required
@pytest.mark.parametrize("active_status", ["pending", "processing", "copilot_review"])
def test_two_active_attempts_for_one_job_are_still_refused(user, active_status):
    """The half of the old constraint that must NOT be weakened."""
    db, uid = user
    _make_application(db, uid, status=active_status)
    with pytest.raises(IntegrityError):
        _make_application(db, uid, status="pending")
    db.rollback()


@db_required
def test_an_applied_attempt_does_not_block_the_index(user):
    db, uid = user
    _make_application(db, uid, status="applied", applied_at=datetime.now(timezone.utc))
    assert _make_application(db, uid, status="pending").application_id


@db_required
@pytest.mark.parametrize("retryable", ["failed", "manual_required", "needs_review"])
def test_a_retryable_attempt_does_not_block_the_index(user, retryable):
    db, uid = user
    _make_application(db, uid, status=retryable)
    assert _make_application(db, uid, status="pending").application_id


# --- Lookup semantics (§7, §8) -------------------------------------------

@db_required
@pytest.mark.parametrize("retryable", ["failed", "manual_required", "needs_review"])
def test_get_retryable_attempt_returns_only_resumable_attempts(user, retryable):
    db, uid = user
    app = _make_application(db, uid, status=retryable)
    found = application_repository.get_retryable_attempt_for_job(db, uid, JOB_URL)
    assert found is not None and found.application_id == app.application_id


@db_required
@pytest.mark.parametrize("non_retryable", ["applied", "pending", "processing", "copilot_review", "cancelled"])
def test_get_retryable_attempt_never_returns_a_non_resumable_attempt(user, non_retryable):
    """Critically it must never return an `applied` row — retrying that would
    reset its status and erase the submission."""
    db, uid = user
    _make_application(db, uid, status=non_retryable,
                      applied_at=datetime.now(timezone.utc) if non_retryable == "applied" else None)
    assert application_repository.get_retryable_attempt_for_job(db, uid, JOB_URL) is None


@db_required
def test_find_submitted_application_returns_the_LATEST_submission(user):
    """§8. This is what makes an acknowledgement self-invalidating: once a
    newer attempt succeeds, one naming the older attempt stops matching."""
    db, uid = user
    older = _make_application(
        db, uid, status="applied", applied_at=datetime.now(timezone.utc) - timedelta(days=5))
    newer = _make_application(
        db, uid, status="applied", applied_at=datetime.now(timezone.utc))

    submitted = automation_ownership.find_submitted_application(db, user_id=uid, job_url=JOB_URL)
    assert submitted is not None
    assert submitted.application_id == newer.application_id
    assert submitted.application_id != older.application_id


@db_required
def test_an_acknowledgement_for_a_superseded_attempt_is_rejected(user):
    db, uid = user
    older = _make_application(
        db, uid, status="applied", applied_at=datetime.now(timezone.utc) - timedelta(days=5))
    _make_application(db, uid, status="applied", applied_at=datetime.now(timezone.utc))

    submitted = automation_ownership.find_submitted_application(db, user_id=uid, job_url=JOB_URL)
    from app.models.application import ReapplyAcknowledgement

    stale = ReapplyAcknowledgement(path="deterministic", application_id=older.application_id)
    with pytest.raises(automation_ownership.ReapplyAcknowledgementError):
        automation_ownership.validate_reapply_acknowledgement(stale, submitted)

    # ...while one naming the current submission is accepted.
    current = ReapplyAcknowledgement(path="deterministic", application_id=submitted.application_id)
    automation_ownership.validate_reapply_acknowledgement(current, submitted)  # no raise


@db_required
def test_another_users_application_id_can_never_authorize_a_re_application(user):
    """Cross-user isolation: `find_submitted_application` is user-scoped, so a
    foreign id is not merely rejected — it can never appear as the expected
    value in the first place."""
    db, uid = user
    other_uid = f"detreapply_other_{uuid.uuid4().hex[:8]}"
    db.add(User(user_id=other_uid, email=f"{other_uid}@example.com", password_hash="x"))
    db.commit()
    try:
        foreign = _make_application(db, other_uid, status="applied",
                                    applied_at=datetime.now(timezone.utc))
        mine = _make_application(db, uid, status="applied",
                                 applied_at=datetime.now(timezone.utc))
        submitted = automation_ownership.find_submitted_application(db, user_id=uid, job_url=JOB_URL)
        assert submitted.application_id == mine.application_id  # never the other user's

        from app.models.application import ReapplyAcknowledgement
        with pytest.raises(automation_ownership.ReapplyAcknowledgementError):
            automation_ownership.validate_reapply_acknowledgement(
                ReapplyAcknowledgement(path="deterministic", application_id=foreign.application_id),
                submitted,
            )
    finally:
        db.query(Application).filter(Application.user_id == other_uid).delete()
        db.query(User).filter(User.user_id == other_uid).delete()
        db.commit()


@db_required
def test_a_different_user_is_unaffected_by_this_users_active_attempt(user):
    db, uid = user
    _make_application(db, uid, status="processing")
    other_uid = f"detreapply_other_{uuid.uuid4().hex[:8]}"
    db.add(User(user_id=other_uid, email=f"{other_uid}@example.com", password_hash="x"))
    db.commit()
    try:
        assert _make_application(db, other_uid, status="pending").application_id
    finally:
        db.query(Application).filter(Application.user_id == other_uid).delete()
        db.query(User).filter(User.user_id == other_uid).delete()
        db.commit()


# --- Cross-path (§6 CASE A) ----------------------------------------------

@db_required
def test_a_completed_autonomous_task_is_reported_to_the_deterministic_path(user):
    """CASE A: prior AUTONOMOUS submission, acknowledged from the
    deterministic route."""
    db, uid = user
    t = task_repo.create_task(db, user_id=uid, job_url=JOB_URL, original_objective="x")
    task_repo.set_status(db, t, "RUNNING")
    task_repo.mark_completed(db, t, {"evidence": "confirmed"})

    submitted = automation_ownership.find_submitted_application(db, user_id=uid, job_url=JOB_URL)
    assert submitted is not None and submitted.is_autonomous
    assert submitted.task_id == t.task_id

    from app.models.application import ReapplyAcknowledgement
    automation_ownership.validate_reapply_acknowledgement(
        ReapplyAcknowledgement(path="autonomous", task_id=t.task_id), submitted,
    )  # no raise
    # A deterministic-shaped acknowledgement must NOT satisfy an autonomous
    # submission.
    with pytest.raises(automation_ownership.ReapplyAcknowledgementError):
        automation_ownership.validate_reapply_acknowledgement(
            ReapplyAcknowledgement(path="deterministic", application_id=t.task_id), submitted,
        )


# --- Concurrency, real threads against the real index --------------------

@db_required
def test_five_concurrent_reapply_attempts_yield_one_active_attempt(user):
    """§9/§15. Five genuine threads, five sessions, all past the lifetime
    guard — the partial index still permits exactly one active attempt, and the
    previously-applied rows are untouched."""
    db, uid = user
    applied_at = datetime.now(timezone.utc) - timedelta(days=1)
    original = _make_application(db, uid, status="applied", applied_at=applied_at)

    threads_count = 5
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads_count)

    def _attempt():
        barrier.wait()
        s = SessionLocal()
        try:
            application_repository.create_application(
                s, user_id=uid, job_url=JOB_URL, autopilot_enabled=False,
                company="Acme", position="SWE", source="server_automation",
            )
            with lock:
                outcomes.append("created")
        except IntegrityError:
            s.rollback()
            with lock:
                outcomes.append("refused")
        finally:
            s.close()

    threads = [threading.Thread(target=_attempt) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert outcomes.count("created") == 1, f"expected one winner, got {outcomes}"
    assert outcomes.count("refused") == threads_count - 1, outcomes

    db.expire_all()
    from app.api.applications import IN_PROGRESS_STATUSES
    active = db.query(Application).filter(
        Application.user_id == uid,
        Application.status.in_(tuple(IN_PROGRESS_STATUSES)),
    ).all()
    assert len(active) == 1

    # The original submission survived the race untouched.
    survivor = application_repository.get_by_id(db, original.application_id)
    assert survivor.status == "applied"
    assert _naive(survivor.applied_date) == _naive(applied_at)


@db_required
def test_a_start_can_never_observe_a_job_as_neither_active_nor_submitted(user):
    """§9's "there must never be an observation where the job appears not
    active AND not submitted between valid transitions".

    It cannot, structurally: the active-status set and the submitted marker are
    on the SAME ROW and `apply_run_result` moves between them in ONE
    transaction, so a reader sees either `processing` (active) or `applied`
    (submitted) — never a gap. Verified by hammering the start-side checks from
    another session while the completion commits."""
    db, uid = user
    app = _make_application(db, uid, status="processing")

    observations: list[str] = []
    stop = threading.Event()
    started = threading.Event()

    def _watch():
        s = SessionLocal()
        try:
            while not stop.is_set():
                started.set()
                active = automation_ownership.find_active_automation(s, user_id=uid, job_url=JOB_URL)
                submitted = automation_ownership.find_submitted_application(s, user_id=uid, job_url=JOB_URL)
                observations.append(
                    "blocked-active" if active is not None
                    else "blocked-submitted" if submitted is not None
                    else "ALLOWED"
                )
                s.commit()
        finally:
            s.close()

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()
    started.wait(timeout=10)

    # The real transition, in one transaction.
    app.status = "applied"
    app.applied_date = datetime.now(timezone.utc)
    db.commit()

    for _ in range(60):
        if "blocked-submitted" in observations:
            break
        threading.Event().wait(0.05)
    stop.set()
    watcher.join(timeout=20)

    assert observations, "the watcher never ran"
    assert "ALLOWED" not in observations, f"a duplicate start was permitted mid-transition: {observations[:20]}"
    assert "blocked-submitted" in observations, observations[-5:]
