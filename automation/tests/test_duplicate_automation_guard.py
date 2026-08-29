"""
Regression tests for the cross-path duplicate/concurrent automation guard.

The problem: `POST /agent/tasks` had no duplicate check at all, so N calls with
the same job URL produced N ACTIVE `AutonomousTask` rows — and under the
default `AUTOMATION_BROWSER_MODE=cdp`, N browser tabs independently filling the
same application form. Reproduced against the real database before the fix:
three concurrent `RUNNING` tasks on one URL. The deterministic path protected
itself (`uq_applications_user_job_url`) but knew nothing about the autonomous
path, and vice versa.

Two layers are under test here:

* the PARTIAL unique index `uq_autonomous_tasks_active_job`, which is what
  makes two simultaneous same-path inserts unable to both commit, and which
  drops terminal rows so retries still work;
* `app/services/automation_ownership.py`, the small read-plus-advisory-lock
  boundary that covers the CROSS-path case a single index cannot span.

The DB-level tests need real Postgres and skip cleanly without it — the same
convention `test_human_interaction_race_conditions.py` uses. Race safety is
NOT asserted from mocks: `test_concurrent_*` runs genuine threads against the
real index.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal, engine
from app.models.db_models import Application, AutonomousTask, User
from app.services import application_repository, automation_ownership
from app.services import autonomous_task_repository as task_repo
from app.services.application_repository import compute_job_url_hash

JOB_URL = "https://careers.example.com/jobs/12345/apply"


# ---------------------------------------------------------------------------
# Job identity / normalization (no DB needed)
# ---------------------------------------------------------------------------

def test_job_key_reuses_the_deterministic_hash():
    """Both paths MUST identify a job identically — normalizing differently in
    each would make cross-path detection silently miss."""
    assert automation_ownership.job_key(JOB_URL) == compute_job_url_hash(JOB_URL)


def test_job_key_folds_case_and_whitespace_only():
    """The supported normalization, and nothing beyond it."""
    assert automation_ownership.job_key("  https://X.example.com/Jobs/1/Apply  ") == \
           automation_ownership.job_key("https://x.example.com/jobs/1/apply")


@pytest.mark.parametrize("a,b", [
    # Query strings routinely ARE the posting identity on real career sites
    # (`?gh_jid=`, `?jobId=`, Workday params), so they must never be stripped.
    ("https://boards.example.com/apply?gh_jid=111", "https://boards.example.com/apply?gh_jid=222"),
    ("https://x.example.com/job?jobId=5", "https://x.example.com/job?jobId=6"),
    # A trailing slash is NOT collapsed: two paths that differ only by one are
    # left distinct rather than risk merging genuinely different postings.
    ("https://x.example.com/jobs/1", "https://x.example.com/jobs/1/"),
    # Different postings entirely.
    ("https://x.example.com/jobs/1", "https://x.example.com/jobs/2"),
])
def test_job_key_does_not_merge_distinct_postings(a, b):
    assert automation_ownership.job_key(a) != automation_ownership.job_key(b)


def test_active_statuses_are_derived_not_relisted():
    """The guard's notion of "active" must be the exact complement of the
    terminal set. Deriving it (rather than maintaining a second literal list)
    is what stops a newly-added status from silently escaping the guard."""
    from app.models.db_models import (
        AUTONOMOUS_TASK_ACTIVE_STATUSES,
        AUTONOMOUS_TASK_TERMINAL_STATUSES,
        VALID_AUTONOMOUS_TASK_STATUSES,
    )

    assert AUTONOMOUS_TASK_ACTIVE_STATUSES | AUTONOMOUS_TASK_TERMINAL_STATUSES == \
           set(VALID_AUTONOMOUS_TASK_STATUSES)
    assert not (AUTONOMOUS_TASK_ACTIVE_STATUSES & AUTONOMOUS_TASK_TERMINAL_STATUSES)


def test_model_index_predicate_matches_the_terminal_status_set():
    """Three places must agree on which statuses are terminal: the model's
    `AUTONOMOUS_TASK_TERMINAL_STATUSES`, the `postgresql_where` on
    `uq_autonomous_tasks_active_job`, and the migration's `_ACTIVE_PREDICATE`.
    If they diverge the database and the application disagree about who owns a
    job — so pin them together here rather than trusting three hand-written
    lists to stay in sync."""
    from app.models.db_models import AUTONOMOUS_TASK_TERMINAL_STATUSES, AutonomousTask

    index = next(
        ix for ix in AutonomousTask.__table__.indexes
        if ix.name == "uq_autonomous_tasks_active_job"
    )
    assert index.unique
    assert [c.name for c in index.columns] == ["user_id", "job_url_hash"]

    predicate = str(index.dialect_options["postgresql"]["where"])
    for status in AUTONOMOUS_TASK_TERMINAL_STATUSES:
        assert status in predicate, f"{status} missing from the index predicate"
    # And no ACTIVE status is accidentally excluded by the predicate.
    from app.models.db_models import AUTONOMOUS_TASK_ACTIVE_STATUSES
    for status in AUTONOMOUS_TASK_ACTIVE_STATUSES:
        assert status not in predicate, f"{status} must not be excluded by the index"


def test_advisory_lock_key_is_stable_and_in_range():
    k1 = automation_ownership._advisory_lock_key("user_1", "hash_1")
    k2 = automation_ownership._advisory_lock_key("user_1", "hash_1")
    assert k1 == k2                                    # stable
    assert -(2 ** 63) <= k1 < 2 ** 63                  # fits Postgres bigint
    assert k1 != automation_ownership._advisory_lock_key("user_2", "hash_1")


# ---------------------------------------------------------------------------
# DB-backed: the real index and the real ownership lookup
# ---------------------------------------------------------------------------

def _db_available() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


db_required = pytest.mark.skipif(
    not _db_available(),
    reason="No reachable Postgres — these exercise the real partial unique index.",
)


@pytest.fixture
def user():
    db = SessionLocal()
    uid = f"dupguard_{uuid.uuid4().hex[:10]}"
    db.add(User(user_id=uid, email=f"{uid}@example.com", password_hash="x"))
    db.commit()
    yield db, uid
    db.rollback()
    db.query(AutonomousTask).filter(AutonomousTask.user_id == uid).delete()
    db.query(Application).filter(Application.user_id == uid).delete()
    db.query(User).filter(User.user_id == uid).delete()
    db.commit()
    db.close()


def _make_task(db, uid, url=JOB_URL, status="RUNNING"):
    t = task_repo.create_task(db, user_id=uid, job_url=url, original_objective="test")
    if status != "CREATED":
        task_repo.set_status(db, t, status)
    return t


# --- CASE A: autonomous vs autonomous -------------------------------------

@db_required
def test_job_url_hash_is_populated_on_create(user):
    db, uid = user
    t = _make_task(db, uid)
    assert t.job_url_hash == compute_job_url_hash(JOB_URL)


@db_required
@pytest.mark.parametrize("active_status", [
    "CREATED", "ANALYZING_JOB", "RUNNING", "WAITING_FOR_HUMAN", "WAITING_FOR_APPROVAL", "RESUMING",
])
def test_a_second_active_task_for_the_same_job_is_refused_by_the_database(user, active_status):
    """The core guarantee, at every active status — this is what stops a second
    browser tab from ever being opened for the same application."""
    db, uid = user
    _make_task(db, uid, status=active_status)

    with pytest.raises(IntegrityError):
        _make_task(db, uid, status="CREATED")
    db.rollback()


@db_required
def test_the_active_task_is_discoverable_by_url(user):
    db, uid = user
    first = _make_task(db, uid)
    found = task_repo.get_active_for_user_and_url(db, uid, JOB_URL)
    assert found is not None and found.task_id == first.task_id
    # Case/whitespace variants resolve to the same job.
    assert task_repo.get_active_for_user_and_url(db, uid, f"  {JOB_URL.upper()}  ") is not None


@db_required
def test_a_different_job_url_is_unaffected(user):
    db, uid = user
    _make_task(db, uid)
    other = _make_task(db, uid, url="https://careers.example.com/jobs/99999/apply")
    assert other.task_id  # committed fine
    assert task_repo.get_active_for_user_and_url(db, uid, "https://careers.example.com/jobs/99999/apply")


@db_required
def test_another_users_task_on_the_same_url_is_unaffected(user):
    """The guard is per-(user, job) — one user's automation must never block
    another's."""
    db, uid = user
    _make_task(db, uid)
    other_uid = f"dupguard_other_{uuid.uuid4().hex[:8]}"
    db.add(User(user_id=other_uid, email=f"{other_uid}@example.com", password_hash="x"))
    db.commit()
    try:
        t = _make_task(db, other_uid)
        assert t.task_id
    finally:
        db.query(AutonomousTask).filter(AutonomousTask.user_id == other_uid).delete()
        db.query(User).filter(User.user_id == other_uid).delete()
        db.commit()


# --- CASES D & E: terminal states ----------------------------------------

@db_required
@pytest.mark.parametrize("terminal_status", ["FAILED", "CANCELLED"])
def test_a_retry_is_allowed_after_a_terminal_task(user, terminal_status):
    """CASE E. The index is PARTIAL, so a terminal task drops out of it and a
    fresh attempt inserts cleanly — a plain unique constraint would have barred
    the job forever after one failure."""
    db, uid = user
    first = _make_task(db, uid, status="RUNNING")
    if terminal_status == "FAILED":
        task_repo.mark_failed(db, first, "boom")
    else:
        task_repo.cancel_task(db, first)

    retry = _make_task(db, uid, status="CREATED")
    assert retry.task_id != first.task_id
    assert task_repo.get_active_for_user_and_url(db, uid, JOB_URL).task_id == retry.task_id


@db_required
def test_a_new_task_is_allowed_after_a_completed_task(user):
    """CASE D. The autonomous guard is about CONCURRENCY, not lifetime
    de-duplication: a COMPLETED task no longer owns a browser, so it does not
    block. Permanent "already applied" semantics remain the deterministic
    path's `COMPLETED_STATUSES` rule on `Application`, so the two systems do
    not assert contradictory duplicate rules."""
    db, uid = user
    first = _make_task(db, uid, status="RUNNING")
    task_repo.mark_completed(db, first, {"evidence": "done"})

    again = _make_task(db, uid, status="CREATED")
    assert again.task_id != first.task_id


# --- CASES B & C: cross-path ---------------------------------------------

@db_required
def test_find_active_automation_reports_an_active_autonomous_task(user):
    db, uid = user
    t = _make_task(db, uid, status="WAITING_FOR_HUMAN")
    active = automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL)
    assert active is not None
    assert active.path == "autonomous"
    assert active.task_id == t.task_id
    assert active.status == "WAITING_FOR_HUMAN"
    assert active.is_autonomous


@db_required
@pytest.mark.parametrize("app_status", ["pending", "processing", "copilot_review"])
def test_find_active_automation_reports_an_in_progress_application(user, app_status):
    """CASE B — the deterministic path holds the job."""
    db, uid = user
    application = application_repository.create_application(
        db, user_id=uid, job_url=JOB_URL, autopilot_enabled=False,
        company="Acme", position="SWE", source="server_automation",
    )
    application.status = app_status
    db.commit()

    active = automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL)
    assert active is not None
    assert active.path == "deterministic"
    assert active.application_id == application.application_id
    assert not active.is_autonomous


@db_required
@pytest.mark.parametrize("app_status", ["failed", "manual_required", "needs_review", "applied", "cancelled"])
def test_a_non_in_progress_application_does_not_block(user, app_status):
    """Retryable/terminal application statuses are NOT active automation — the
    deterministic route's own retry and completed rules own those cases, and
    duplicating them here would create contradictory duplicate semantics."""
    db, uid = user
    application = application_repository.create_application(
        db, user_id=uid, job_url=JOB_URL, autopilot_enabled=False,
        company="Acme", position="SWE", source="server_automation",
    )
    application.status = app_status
    db.commit()

    assert automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL) is None


@db_required
def test_find_active_automation_is_silent_when_nothing_is_running(user):
    db, uid = user
    assert automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL) is None


@db_required
def test_exclude_task_id_lets_a_caller_ignore_its_own_row(user):
    db, uid = user
    t = _make_task(db, uid)
    assert automation_ownership.find_active_automation(
        db, user_id=uid, job_url=JOB_URL, exclude_task_id=t.task_id
    ) is None


# --- Lifetime duplicate protection (distinct from concurrency) -----------

@db_required
def test_a_completed_autonomous_task_is_reported_as_submitted(user):
    """CASE A. `COMPLETED` has one call site, gated on an observed
    confirmation page, so it genuinely means submitted."""
    db, uid = user
    t = _make_task(db, uid, status="RUNNING")
    task_repo.mark_completed(db, t, {"decision": "TASK_COMPLETED", "evidence": "thank you for applying"})

    submitted = automation_ownership.find_submitted_application(db, user_id=uid, job_url=JOB_URL)
    assert submitted is not None
    assert submitted.path == "autonomous"
    assert submitted.task_id == t.task_id
    assert submitted.is_autonomous
    # ...and it is NOT reported as active automation — the two are separate
    # concepts and must stay so.
    assert automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL) is None


@db_required
def test_an_applied_application_is_reported_as_submitted(user):
    """CASE B. `applied` is only reached via `submit_and_confirm` ->
    `wait_for_submission_confirmation`, and stamps `applied_date`."""
    db, uid = user
    application = application_repository.create_application(
        db, user_id=uid, job_url=JOB_URL, autopilot_enabled=False,
        company="Acme", position="SWE", source="server_automation",
    )
    application.status = "applied"
    application.applied_date = datetime.now(timezone.utc)
    db.commit()

    submitted = automation_ownership.find_submitted_application(db, user_id=uid, job_url=JOB_URL)
    assert submitted is not None
    assert submitted.path == "deterministic"
    assert submitted.application_id == application.application_id
    assert submitted.submitted_at is not None
    assert not submitted.is_autonomous


@db_required
@pytest.mark.parametrize("non_submitted_status", [
    # STEP 4: none of these mean "submitted", and each is a state the task
    # brief explicitly warned against conflating with success.
    "CREATED",               # task started
    "RUNNING",               # form partially filled
    "WAITING_FOR_HUMAN",     # waiting for an OTP
    "WAITING_FOR_APPROVAL",  # user stopped before submission / draft
    "FAILED",                # task failed
    "CANCELLED",             # task cancelled
])
def test_no_autonomous_status_other_than_completed_counts_as_submitted(user, non_submitted_status):
    db, uid = user
    _make_task(db, uid, status=non_submitted_status)
    assert automation_ownership.find_submitted_application(db, user_id=uid, job_url=JOB_URL) is None


@db_required
@pytest.mark.parametrize("non_submitted_status", [
    "pending", "processing", "copilot_review",          # still in progress
    "failed", "manual_required", "needs_review",         # CASE F: retryable
    "cancelled",
])
def test_no_application_status_other_than_applied_counts_as_submitted(user, non_submitted_status):
    """CASE F. Retryable statuses must NOT acquire lifetime-block semantics —
    that would break this route's existing retry-on-the-same-row behavior."""
    db, uid = user
    application = application_repository.create_application(
        db, user_id=uid, job_url=JOB_URL, autopilot_enabled=False,
        company="Acme", position="SWE", source="server_automation",
    )
    application.status = non_submitted_status
    db.commit()
    assert automation_ownership.find_submitted_application(db, user_id=uid, job_url=JOB_URL) is None


@db_required
def test_submitted_check_is_scoped_to_the_user(user):
    """A submission by one user must never block another user."""
    db, uid = user
    t = _make_task(db, uid, status="RUNNING")
    task_repo.mark_completed(db, t, {"evidence": "done"})

    other_uid = f"dupguard_other_{uuid.uuid4().hex[:8]}"
    db.add(User(user_id=other_uid, email=f"{other_uid}@example.com", password_hash="x"))
    db.commit()
    try:
        assert automation_ownership.find_submitted_application(
            db, user_id=other_uid, job_url=JOB_URL
        ) is None
    finally:
        db.query(User).filter(User.user_id == other_uid).delete()
        db.commit()


@db_required
def test_submitted_check_is_scoped_to_the_job(user):
    db, uid = user
    t = _make_task(db, uid, status="RUNNING")
    task_repo.mark_completed(db, t, {"evidence": "done"})
    assert automation_ownership.find_submitted_application(
        db, user_id=uid, job_url="https://careers.example.com/jobs/99999/apply"
    ) is None


@db_required
def test_completed_does_not_block_the_partial_index_but_does_block_the_route(user):
    """The exact separation the brief demanded: COMPLETED must NOT be folded
    into the active partial unique index (that would give it FAILED/CANCELLED
    retry semantics), yet it must still stop a new start at the route level."""
    db, uid = user
    t = _make_task(db, uid, status="RUNNING")
    task_repo.mark_completed(db, t, {"evidence": "done"})

    # The INDEX still permits the insert — concurrency semantics unchanged.
    retry = _make_task(db, uid, status="CREATED")
    assert retry.task_id

    # But the ROUTE's lifetime guard would have refused it, because the
    # completed task is discoverable as submitted.
    db.query(AutonomousTask).filter(AutonomousTask.task_id == retry.task_id).delete()
    db.commit()
    assert automation_ownership.find_submitted_application(db, user_id=uid, job_url=JOB_URL) is not None


# --- The real concurrency proof ------------------------------------------

@db_required
def test_concurrent_task_creation_for_the_same_job_only_one_wins(user):
    """NOT a mocked test: N genuine threads, N separate sessions, one real
    partial unique index. Exactly one INSERT may commit."""
    db, uid = user
    threads_count = 6
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads_count)

    def _attempt():
        barrier.wait()  # maximize overlap
        s = SessionLocal()
        try:
            task_repo.create_task(s, user_id=uid, job_url=JOB_URL, original_objective="race")
            with lock:
                outcomes.append("created")
        except IntegrityError:
            s.rollback()
            with lock:
                outcomes.append("refused")
        except Exception as e:  # noqa: BLE001 - surface anything unexpected
            s.rollback()
            with lock:
                outcomes.append(f"error:{type(e).__name__}")
        finally:
            s.close()

    threads = [threading.Thread(target=_attempt) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert outcomes.count("created") == 1, f"expected exactly one winner, got {outcomes}"
    assert outcomes.count("refused") == threads_count - 1, outcomes

    db.expire_all()
    active = db.query(AutonomousTask).filter(
        AutonomousTask.user_id == uid,
        AutonomousTask.current_status.notin_(("COMPLETED", "FAILED", "CANCELLED")),
    ).all()
    assert len(active) == 1, f"{len(active)} active tasks survived the race"


@db_required
def test_advisory_lock_serializes_two_start_transactions(user):
    """The cross-path half: a unique index cannot span two tables, so
    `reserve_job_automation` is what makes "check both, then insert" atomic.
    Proves the second holder genuinely waits for the first transaction."""
    db, uid = user
    order: list[str] = []
    first_locked = threading.Event()
    release_first = threading.Event()

    def _holder():
        s = SessionLocal()
        try:
            automation_ownership.reserve_job_automation(s, user_id=uid, job_url=JOB_URL)
            order.append("first-acquired")
            first_locked.set()
            release_first.wait(timeout=30)
            s.commit()  # advisory_xact_lock releases here
            order.append("first-released")
        finally:
            s.close()

    def _waiter():
        first_locked.wait(timeout=30)
        s = SessionLocal()
        try:
            automation_ownership.reserve_job_automation(s, user_id=uid, job_url=JOB_URL)
            order.append("second-acquired")
            s.commit()
        finally:
            s.close()

    th1 = threading.Thread(target=_holder)
    th2 = threading.Thread(target=_waiter)
    th1.start(); th2.start()
    first_locked.wait(timeout=30)

    # THE guarantee: while the first transaction holds the lock, the second
    # cannot get past `reserve_job_automation`.
    th2.join(timeout=4)
    assert "second-acquired" not in order, f"lock did not block: {order}"
    assert th2.is_alive(), "the waiter should still be blocked on the lock"

    # And it proceeds once the holder's transaction ends.
    release_first.set()
    th1.join(timeout=30)
    th2.join(timeout=30)
    assert "second-acquired" in order, f"waiter never acquired after release: {order}"

    # NOTE: deliberately NOT asserting the exact order of "first-released" and
    # "second-acquired". `s.commit()` releases the lock BEFORE the holder's next
    # statement runs, so the freed waiter legitimately races the holder's own
    # bookkeeping append. An earlier version asserted a strict ordering here and
    # failed ~2 runs in 3 — which looked like "advisory locks are broken on
    # Neon's pooler" but was purely this append race. (Verified separately:
    # session B's `pg_try_advisory_xact_lock` returns False while session A
    # holds it, so the lock really is honoured through the pooler.)


@db_required
def test_submission_completing_cannot_be_raced_by_a_new_start(user):
    """STEP 8. The feared interleaving is:

        Task A commits its submission success
        Task B checks "already submitted?" -> not yet -> starts a duplicate

    It cannot happen, and the reason is structural rather than lucky: the
    active-status set and the submitted marker live on the SAME ROW, and
    `mark_completed` moves between them in ONE transaction. So a concurrent
    start observes the row either BEFORE the commit (status RUNNING -> caught
    by the concurrency guard) or AFTER it (status COMPLETED -> caught by the
    lifetime guard). There is no third state in which the row is neither
    active nor submitted.

    Asserted by hammering a start-side check from another session while the
    completion commits, and requiring that EVERY observation was blocked by
    one guard or the other.
    """
    db, uid = user
    task = _make_task(db, uid, status="RUNNING")

    # First, deterministically: while the task is RUNNING a start is blocked by
    # the CONCURRENCY guard. Checked synchronously so this half can never be
    # lost to a timing race with the commit below.
    probe = SessionLocal()
    try:
        assert automation_ownership.find_active_automation(
            probe, user_id=uid, job_url=JOB_URL
        ) is not None, "a RUNNING task was not seen as active"
        assert automation_ownership.find_submitted_application(
            probe, user_id=uid, job_url=JOB_URL
        ) is None, "a RUNNING task must not yet count as submitted"
    finally:
        probe.close()

    observations: list[str] = []
    stop = threading.Event()
    started = threading.Event()

    def _would_start_be_allowed():
        """Exactly what a start route does: reserve, then both checks."""
        s = SessionLocal()
        try:
            while not stop.is_set():
                started.set()
                automation_ownership.reserve_job_automation(s, user_id=uid, job_url=JOB_URL)
                active = automation_ownership.find_active_automation(s, user_id=uid, job_url=JOB_URL)
                submitted = automation_ownership.find_submitted_application(
                    s, user_id=uid, job_url=JOB_URL
                )
                if active is not None:
                    observations.append("blocked-active")
                elif submitted is not None:
                    observations.append("blocked-submitted")
                else:
                    observations.append("ALLOWED")
                s.commit()  # release the advisory lock, then look again
        finally:
            s.close()

    watcher = threading.Thread(target=_would_start_be_allowed, daemon=True)
    watcher.start()
    started.wait(timeout=10)

    # Commit the submission while the watcher is hammering the same checks.
    task_repo.mark_completed(db, task, {"decision": "TASK_COMPLETED", "evidence": "confirmed"})

    # Let it observe the post-commit world too, then stop.
    for _ in range(50):
        if "blocked-submitted" in observations:
            break
        threading.Event().wait(0.05)
    stop.set()
    watcher.join(timeout=20)

    assert observations, "the watcher never ran"
    # THE guarantee: no observation, at any point across the transition, ever
    # concluded that a new automation could start.
    assert "ALLOWED" not in observations, (
        "a start slipped through the RUNNING -> COMPLETED transition: "
        f"{observations[:20]}"
    )
    # The post-commit world is reached and blocked by the LIFETIME guard.
    assert "blocked-submitted" in observations, observations[-5:]
    # NOTE: not asserting "blocked-active" appears here. The watcher may not get
    # its first query in before `mark_completed` commits (each round-trip to a
    # cloud database is ~100ms+), and that is a scheduling detail, not a
    # behavior. The pre-commit half is covered deterministically by the
    # synchronous probe above instead.
    assert set(observations) <= {"blocked-active", "blocked-submitted"}, observations


# ---------------------------------------------------------------------------
# Explicit re-application, against the REAL index
# ---------------------------------------------------------------------------

@db_required
def test_reapply_after_a_completed_task_preserves_the_original_row(user):
    """STEP 7: historical truth. A re-application must create a NEW attempt and
    leave the original COMPLETED task exactly as it was — never flipped back to
    a non-terminal status, never rewritten to pretend it didn't happen."""
    db, uid = user
    first = _make_task(db, uid, status="RUNNING")
    task_repo.mark_completed(db, first, {"decision": "TASK_COMPLETED", "evidence": "confirmed"})
    original_status = first.current_status
    original_final = dict(first.final_result or {})

    # The re-application: a brand new row. Permitted by the PARTIAL index
    # precisely because the old row is terminal and therefore outside it.
    second = _make_task(db, uid, status="CREATED")
    assert second.task_id != first.task_id

    db.expire_all()
    unchanged = task_repo.get_by_id(db, first.task_id)
    assert unchanged.current_status == original_status == "COMPLETED"
    assert dict(unchanged.final_result or {}) == original_final
    # And the original is still discoverable as a submission, so the history is
    # intact rather than merely un-deleted.
    rows = db.query(AutonomousTask).filter(AutonomousTask.user_id == uid).all()
    assert len(rows) == 2, "the re-application replaced the original instead of adding to it"


@db_required
def test_two_simultaneous_reapply_attempts_yield_at_most_one_active_task(user):
    """STEP 11.4 + the double-click case. Two genuine threads, two sessions,
    both holding a valid acknowledgement — the partial unique index still
    permits only one ACTIVE task."""
    db, uid = user
    first = _make_task(db, uid, status="RUNNING")
    task_repo.mark_completed(db, first, {"evidence": "confirmed"})

    threads_count = 5
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads_count)

    def _attempt():
        barrier.wait()
        s = SessionLocal()
        try:
            # What the route does once an acknowledgement validates.
            task_repo.create_task(s, user_id=uid, job_url=JOB_URL, original_objective="reapply")
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
    db.expire_all()
    active = db.query(AutonomousTask).filter(
        AutonomousTask.user_id == uid,
        AutonomousTask.current_status.notin_(("COMPLETED", "FAILED", "CANCELLED")),
    ).all()
    assert len(active) == 1, f"{len(active)} active tasks after concurrent re-apply"
    # The original COMPLETED task is still there, untouched.
    assert task_repo.get_by_id(db, first.task_id).current_status == "COMPLETED"


@db_required
def test_reapply_is_still_blocked_while_an_earlier_attempt_is_active(user):
    """STEP 3, at the database level: the override relaxes the LIFETIME guard,
    so active ownership must still refuse — the index is what enforces it."""
    db, uid = user
    completed = _make_task(db, uid, status="RUNNING")
    task_repo.mark_completed(db, completed, {"evidence": "confirmed"})
    # A re-application is under way...
    live = _make_task(db, uid, status="RUNNING")

    # ...so `find_active_automation` reports it, which is what the route checks
    # BEFORE it ever looks at an acknowledgement.
    active = automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL)
    assert active is not None and active.task_id == live.task_id

    # And the database refuses a third regardless.
    with pytest.raises(IntegrityError):
        _make_task(db, uid, status="CREATED")
    db.rollback()


@db_required
def test_after_a_reapply_completes_the_newest_submission_is_the_one_reported(user):
    """Why a stale acknowledgement stops working without a token store: the
    "current" submission advances to the newest COMPLETED task, so an
    acknowledgement naming the older one no longer matches."""
    db, uid = user
    first = _make_task(db, uid, status="RUNNING")
    task_repo.mark_completed(db, first, {"evidence": "first"})

    second = _make_task(db, uid, status="RUNNING")
    task_repo.mark_completed(db, second, {"evidence": "second"})

    submitted = automation_ownership.find_submitted_application(db, user_id=uid, job_url=JOB_URL)
    assert submitted is not None
    assert submitted.task_id == second.task_id, "the newest submission must be the one reported"
    assert submitted.task_id != first.task_id
