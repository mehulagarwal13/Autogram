"""
Reconciler under adversarial conditions: concurrent workers, concurrent
deletion, concurrent update, repeated passes, and error isolation.

Everything here runs against REAL Postgres with REAL concurrent sessions,
because the failure this file exists to prevent — an ORM object touched after
a commit that expired it — is invisible to a mocked session. It was found in
production code by a shared-database test, not by inspection.

The governing rule for every scenario below: a lost race is NORMAL and must be
absorbed silently; a programming bug must stay loud. A reconciler that swallows
everything is worse than none, because it turns "jobs are permanently blocked"
into "jobs are permanently blocked and nothing is logged".
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text

from app.core.database import SessionLocal, engine
from app.models.db_models import (
    Application,
    ApplicationAuditLog,
    AutonomousTask,
    ChatMessage,
    HumanInteractionRequest,
    User,
)
from app.services import application_repository, automation_recovery
from app.services import autonomous_task_repository as task_repo
from app.services import human_interaction_repository as hitl_repo
from automation.agents.autonomous import runner

JOB = "https://careers.example.com/jobs/reconciler-concurrency/apply"


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
    uid = f"reconc_{uuid.uuid4().hex[:10]}"
    db.add(User(user_id=uid, email=f"{uid}@example.com", password_hash="x"))
    db.commit()
    yield db, uid
    db.rollback()
    db.query(ChatMessage).filter(ChatMessage.user_id == uid).delete()
    db.query(ApplicationAuditLog).filter(ApplicationAuditLog.user_id == uid).delete()
    db.query(HumanInteractionRequest).filter(HumanInteractionRequest.user_id == uid).delete()
    db.query(AutonomousTask).filter(AutonomousTask.user_id == uid).delete()
    db.query(Application).filter(Application.user_id == uid).delete()
    db.query(User).filter(User.user_id == uid).delete()
    db.commit()
    db.close()


def _task(db, uid, *, status="RUNNING", url=JOB, approved=False):
    t = task_repo.create_task(
        db, user_id=uid, job_url=url, original_objective="apply",
        candidate_profile={"profile": {}}, job_information={},
    )
    t.current_status = status
    t.auto_submit_approved = approved
    db.commit()
    db.refresh(t)
    return t


def _application(db, uid, *, status="processing", url=JOB):
    a = application_repository.create_application(
        db, user_id=uid, job_url=url, autopilot_enabled=False,
        company="Acme", position="SWE", source="server_automation",
    )
    if status != "pending":
        a.status = status
        db.commit()
        db.refresh(a)
    return a


def _status_of(task_id):
    db = SessionLocal()
    try:
        row = task_repo.get_by_id(db, task_id)
        return row.current_status if row else None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# §2 — Two reconcilers, one orphan
# ---------------------------------------------------------------------------

@db_required
def test_two_reconcilers_claiming_one_task_produce_exactly_one_transition(user):
    """The conditional UPDATE is the arbiter. Both passes run to completion —
    the loser must not raise, and must not double-count."""
    db, uid = user
    task = _task(db, uid, status="RUNNING")

    counts: list[int] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def _pass():
        session = SessionLocal()
        try:
            barrier.wait(timeout=30)
            counts.append(runner.reconcile_orphaned_tasks_on_startup(session))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            session.close()

    threads = [threading.Thread(target=_pass) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)

    assert not errors, f"a concurrency race must never raise: {errors}"
    assert _status_of(task.task_id) == "FAILED"

    # Exactly one pass may claim THIS task. Both counts include other users'
    # orphans (the reconciler is global), so assert on the audit trail for this
    # task instead — that is what "no duplicate processing" actually means.
    db.expire_all()
    events = (
        db.query(ApplicationAuditLog)
        .filter(
            ApplicationAuditLog.autonomous_task_id == task.task_id,
            ApplicationAuditLog.event_type == "automation_failed",
        )
        .count()
    )
    assert events == 1, f"expected exactly one terminal transition, got {events}"


@db_required
def test_a_lost_claim_is_not_counted_as_work_done(user):
    """`_reconcile_one_task` returns 0 for a task another worker already moved
    on. Counting it would overstate what happened in the startup log."""
    db, uid = user
    task = _task(db, uid, status="RUNNING")
    # Simulate the winner having already transitioned it.
    task_repo.try_claim_orphan_failed(db, task.task_id, from_status="RUNNING", error="won by someone else")

    assert runner._reconcile_one_task(db, task.task_id, uid, "RUNNING", False) == 0


# ---------------------------------------------------------------------------
# §3 — Concurrent deletion (the shape that exposed the original bug)
# ---------------------------------------------------------------------------

@db_required
def test_a_task_deleted_mid_pass_does_not_abort_the_remaining_orphans(user, caplog):
    """THE original regression, kept deliberately.

    Before the fix the reconciler iterated live ORM objects; the first commit
    expired them, and touching a since-deleted one raised `ObjectDeletedError`,
    aborting the pass and leaving every later orphan blocking its job.

    The assertion that matters is not "no exception" — it is that C was still
    processed after B vanished.
    """
    db, uid = user
    a = _task(db, uid, status="RUNNING", url=JOB + "?a")
    b = _task(db, uid, status="RUNNING", url=JOB + "?b")
    c = _task(db, uid, status="RUNNING", url=JOB + "?c")

    # Snapshot happens inside the reconciler; delete B from ANOTHER session
    # after that snapshot exists but before B is processed. Deleting it before
    # the call is equivalent for this purpose: the snapshot is taken from a
    # query, so B is simply gone by the time its turn arrives.
    original = task_repo.list_orphaned_running_tasks

    def _delete_b_after_snapshot(session):
        rows = original(session)
        other = SessionLocal()
        try:
            other.query(AutonomousTask).filter(AutonomousTask.task_id == b.task_id).delete()
            other.commit()
        finally:
            other.close()
        return rows

    task_repo.list_orphaned_running_tasks = _delete_b_after_snapshot
    try:
        fresh = SessionLocal()
        try:
            runner.reconcile_orphaned_tasks_on_startup(fresh)
        finally:
            fresh.close()
    finally:
        task_repo.list_orphaned_running_tasks = original

    assert _status_of(a.task_id) == "FAILED", "A must be processed"
    assert _status_of(b.task_id) is None, "B was deleted concurrently"
    assert _status_of(c.task_id) == "FAILED", (
        "C must STILL be processed — a vanished task must not abort the pass"
    )
    # The discriminating assertion. Without it this test passes even with the
    # ORIGINAL bug reintroduced, because `ObjectDeletedError` subclasses
    # `SQLAlchemyError` and the per-task error isolation catches it — C still
    # gets processed, so the outcome assertions above look identical.
    #
    # A correctly-snapshotting reconciler never TOUCHES a deleted row: B simply
    # matches zero rows in the conditional UPDATE and is skipped silently. So
    # "nothing was logged as an error" is what actually distinguishes the fix
    # from the bug being papered over by the safety net.
    assert not [r for r in caplog.records if r.levelname == "ERROR"], (
        "a deleted row must be skipped silently, not caught as a database error — "
        "an ObjectDeletedError here means the reconciler is touching expired ORM state again"
    )


# ---------------------------------------------------------------------------
# §4 — Concurrent update
# ---------------------------------------------------------------------------

@db_required
@pytest.mark.parametrize("newer_status", ["COMPLETED", "CANCELLED", "WAITING_FOR_HUMAN"])
def test_a_status_changed_underneath_the_reconciler_is_never_overwritten(user, newer_status):
    """Compare-and-set: the claim is `WHERE current_status = <snapshot>`, so a
    newer state — including a real SUBMISSION — survives untouched."""
    db, uid = user
    task = _task(db, uid, status="RUNNING")

    # Another worker moves it on after our snapshot was taken.
    other = SessionLocal()
    try:
        other.query(AutonomousTask).filter(AutonomousTask.task_id == task.task_id).update(
            {"current_status": newer_status}, synchronize_session=False,
        )
        other.commit()
    finally:
        other.close()

    # Reconcile using the STALE snapshot value, exactly as a real pass would.
    claimed = runner._reconcile_one_task(db, task.task_id, uid, "RUNNING", False)

    assert claimed == 0, "a stale claim must not report work"
    assert _status_of(task.task_id) == newer_status, "newer state must win"


# ---------------------------------------------------------------------------
# §6 — Idempotency
# ---------------------------------------------------------------------------

@db_required
def test_running_reconciliation_three_times_does_no_duplicate_work(user):
    """§6 verbatim: reconcile(); reconcile(); reconcile(). Passes two and three
    must be no-ops for anything pass one already handled."""
    db, uid = user
    task = _task(db, uid, status="RUNNING")
    app_row = _application(db, uid, status="processing", url=JOB + "?det")

    for _ in range(3):
        session = SessionLocal()
        try:
            automation_recovery.reconcile_orphaned_automation_on_startup(session)
        finally:
            session.close()

    db.expire_all()

    def _count(event_type, **filters):
        q = db.query(ApplicationAuditLog).filter(ApplicationAuditLog.event_type == event_type)
        for col, val in filters.items():
            q = q.filter(getattr(ApplicationAuditLog, col) == val)
        return q.count()

    assert _count("automation_failed", autonomous_task_id=task.task_id) == 1, "no duplicate events"
    assert _count("automation_recovered", application_id=app_row.application_id) == 1
    assert _status_of(task.task_id) == "FAILED"
    assert application_repository.get_by_id(db, app_row.application_id).status == "needs_review"
    # No duplicate chat noise either — recovery is a state transition, not a
    # conversation turn, so it must not manufacture transcript entries at all.
    assert db.query(ChatMessage).filter(ChatMessage.autonomous_task_id == task.task_id).count() == 0


@db_required
def test_repeated_recovery_never_creates_a_second_human_request(user):
    """§6: "No duplicate human requests". Recovery EXPIRES a pending request;
    it must never raise a new one, which would look to the user like the agent
    asking the same question twice after a restart."""
    db, uid = user
    task = _task(db, uid, status="RUNNING")
    hitl_repo.create_request(
        db, user_id=uid, task_id=task.task_id,
        request_type="LOGIN_REQUIRED", message="Please log in.",
    )

    for _ in range(3):
        session = SessionLocal()
        try:
            runner.reconcile_orphaned_tasks_on_startup(session)
        finally:
            session.close()

    db.expire_all()
    requests = db.query(HumanInteractionRequest).filter(
        HumanInteractionRequest.task_id == task.task_id
    ).all()
    assert len(requests) == 1, "recovery must not create additional HITL requests"
    assert requests[0].status == "EXPIRED"


# ---------------------------------------------------------------------------
# §5 — Crash during reconciliation, then restart
# ---------------------------------------------------------------------------

@db_required
def test_a_crash_midway_through_a_pass_is_completed_by_the_next_pass(user):
    """A reconciler that dies partway leaves the rest orphaned. The next
    startup must finish the job — and must not redo the part already done."""
    db, uid = user
    a = _task(db, uid, status="RUNNING", url=JOB + "?x")
    b = _task(db, uid, status="RUNNING", url=JOB + "?y")

    # Crash after the FIRST task is claimed.
    real = runner._reconcile_one_task
    calls = {"n": 0}

    def _crash_after_first(*args, **kwargs):
        calls["n"] += 1
        result = real(*args, **kwargs)
        if calls["n"] == 1:
            raise KeyboardInterrupt("worker killed mid-pass")
        return result

    runner._reconcile_one_task = _crash_after_first
    try:
        session = SessionLocal()
        try:
            with pytest.raises(KeyboardInterrupt):
                runner.reconcile_orphaned_tasks_on_startup(session)
        finally:
            session.close()
    finally:
        runner._reconcile_one_task = real

    done_first = [t for t in (a, b) if _status_of(t.task_id) == "FAILED"]
    assert len(done_first) == 1, "exactly one should have been claimed before the crash"

    # Restart: the second pass finishes the remainder.
    session = SessionLocal()
    try:
        runner.reconcile_orphaned_tasks_on_startup(session)
    finally:
        session.close()

    assert _status_of(a.task_id) == "FAILED"
    assert _status_of(b.task_id) == "FAILED"
    db.expire_all()
    for t in (a, b):
        assert db.query(ApplicationAuditLog).filter(
            ApplicationAuditLog.autonomous_task_id == t.task_id,
            ApplicationAuditLog.event_type == "automation_failed",
        ).count() == 1, "the completed half must not be reprocessed"


# ---------------------------------------------------------------------------
# §9 — A mixed pass completes end to end
# ---------------------------------------------------------------------------

@db_required
def test_a_mixed_pass_processes_every_recoverable_task(user):
    """§9's exact table: A recoverable, B concurrently deleted, C recoverable,
    D already handled. The whole pass must complete."""
    db, uid = user
    a = _task(db, uid, status="RUNNING", url=JOB + "?A")
    b = _task(db, uid, status="RUNNING", url=JOB + "?B")
    c = _task(db, uid, status="ANALYZING_JOB", url=JOB + "?C")
    d = _task(db, uid, status="COMPLETED", url=JOB + "?D")

    original = task_repo.list_orphaned_running_tasks

    def _delete_b(session):
        rows = original(session)
        other = SessionLocal()
        try:
            other.query(AutonomousTask).filter(AutonomousTask.task_id == b.task_id).delete()
            other.commit()
        finally:
            other.close()
        return rows

    task_repo.list_orphaned_running_tasks = _delete_b
    try:
        session = SessionLocal()
        try:
            runner.reconcile_orphaned_tasks_on_startup(session)
        finally:
            session.close()
    finally:
        task_repo.list_orphaned_running_tasks = original

    assert _status_of(a.task_id) == "FAILED", "A processed"
    assert _status_of(b.task_id) is None, "B ignored safely"
    assert _status_of(c.task_id) == "FAILED", "C processed"
    assert _status_of(d.task_id) == "COMPLETED", "D untouched — terminal is final"


# ---------------------------------------------------------------------------
# §10 — Error isolation, without hiding programming bugs
# ---------------------------------------------------------------------------

@db_required
def test_a_database_error_on_one_task_does_not_stop_the_others(user):
    """A per-row database failure is recorded and skipped."""
    from sqlalchemy.exc import OperationalError

    db, uid = user
    a = _task(db, uid, status="RUNNING", url=JOB + "?e1")
    b = _task(db, uid, status="RUNNING", url=JOB + "?e2")

    real = runner._reconcile_one_task
    seen: list[str] = []

    def _explode_on_first(session, task_id, *args, **kwargs):
        seen.append(task_id)
        if len(seen) == 1:
            raise OperationalError("SELECT", {}, Exception("connection reset"))
        return real(session, task_id, *args, **kwargs)

    runner._reconcile_one_task = _explode_on_first
    try:
        session = SessionLocal()
        try:
            runner.reconcile_orphaned_tasks_on_startup(session)
        finally:
            session.close()
    finally:
        runner._reconcile_one_task = real

    statuses = {_status_of(a.task_id), _status_of(b.task_id)}
    assert "FAILED" in statuses, "the task after the failure must still be processed"


@db_required
def test_a_programming_bug_is_not_swallowed(user):
    """The counterpart, and the more important half: `except SQLAlchemyError`
    is deliberately narrow. A TypeError/AttributeError from a real bug must
    propagate — being silently absorbed is how a broken reconciler reports
    "recovered 0" forever while jobs stay blocked."""
    db, uid = user
    _task(db, uid, status="RUNNING")

    real = runner._reconcile_one_task
    runner._reconcile_one_task = lambda *a, **k: (_ for _ in ()).throw(TypeError("a real bug"))
    try:
        session = SessionLocal()
        try:
            with pytest.raises(TypeError, match="a real bug"):
                runner.reconcile_orphaned_tasks_on_startup(session)
        finally:
            session.close()
    finally:
        runner._reconcile_one_task = real


# ---------------------------------------------------------------------------
# §11 — State consistency: no impossible combinations
# ---------------------------------------------------------------------------

@db_required
def test_recovery_never_leaves_a_resolved_request_on_a_waiting_task(user):
    """§11's "HumanRequest = RESOLVED, AgentTask = waiting forever". After
    recovery the task is terminal and its request is terminal — neither can be
    left waiting on the other."""
    db, uid = user
    task = _task(db, uid, status="RUNNING")
    hitl_repo.create_request(
        db, user_id=uid, task_id=task.task_id,
        request_type="CAPTCHA_REQUIRED", message="Please solve the CAPTCHA.",
    )

    session = SessionLocal()
    try:
        runner.reconcile_orphaned_tasks_on_startup(session)
    finally:
        session.close()

    db.expire_all()
    assert task_repo.get_by_id(db, task.task_id).current_status == "FAILED"
    req = db.query(HumanInteractionRequest).filter(
        HumanInteractionRequest.task_id == task.task_id
    ).one()
    assert req.status == "EXPIRED", "a pause on a dead task must not stay answerable"


@db_required
def test_a_submitted_application_is_never_reopened_by_any_number_of_passes(user):
    """§11 / §16: `applied` and `COMPLETED` are terminal. Repeated recovery
    must never roll one back into an active state, which would re-open the job
    to automatic re-application."""
    db, uid = user
    applied = _application(db, uid, status="applied", url=JOB + "?s1")
    done = _task(db, uid, status="COMPLETED", url=JOB + "?s2")

    for _ in range(3):
        session = SessionLocal()
        try:
            automation_recovery.reconcile_orphaned_automation_on_startup(session)
        finally:
            session.close()

    db.expire_all()
    assert application_repository.get_by_id(db, applied.application_id).status == "applied"
    assert task_repo.get_by_id(db, done.task_id).current_status == "COMPLETED"


# ---------------------------------------------------------------------------
# §13 / §14 — OTP and CAPTCHA across a restart
# ---------------------------------------------------------------------------

@db_required
@pytest.mark.parametrize("request_type", ["OTP_REQUIRED", "MFA_REQUIRED"])
def test_recovery_of_a_secret_request_never_persists_or_replays_a_code(user, request_type):
    """§13. The code only ever lived in `TaskHandle.pending_secret`, which died
    with the process. Recovery must expire the request rather than resurrect a
    verification it cannot complete — and must leave nothing code-shaped
    anywhere in the database."""
    db, uid = user
    task = _task(db, uid, status="RUNNING")
    req = hitl_repo.create_request(
        db, user_id=uid, task_id=task.task_id,
        request_type=request_type, message="Enter the code we sent you.",
    )

    session = SessionLocal()
    try:
        runner.reconcile_orphaned_tasks_on_startup(session)
    finally:
        session.close()

    db.expire_all()
    refreshed = db.query(HumanInteractionRequest).filter(
        HumanInteractionRequest.request_id == req.request_id
    ).one()
    assert refreshed.status == "EXPIRED", "a code cannot be delivered to a dead process"
    # Nothing anywhere holds a value that could be replayed.
    blob = f"{refreshed.message}{refreshed.safe_metadata}{task_repo.get_by_id(db, task.task_id).error}"
    assert not any(ch.isdigit() and len(w) >= 4 and w.isdigit() for w in blob.split() for ch in w), (
        f"something code-shaped survived recovery: {blob!r}"
    )


@db_required
def test_captcha_recovery_never_claims_the_captcha_was_solved(user):
    """§14. Recovery must reach an explicit failed state, not silently continue
    as though the CAPTCHA had been cleared."""
    db, uid = user
    task = _task(db, uid, status="RUNNING")
    hitl_repo.create_request(
        db, user_id=uid, task_id=task.task_id,
        request_type="CAPTCHA_REQUIRED", message="Please solve the CAPTCHA in the browser.",
    )

    session = SessionLocal()
    try:
        runner.reconcile_orphaned_tasks_on_startup(session)
    finally:
        session.close()

    db.expire_all()
    refreshed = task_repo.get_by_id(db, task.task_id)
    assert refreshed.current_status == "FAILED"
    assert "restart" in refreshed.error.lower() or "interrupted" in refreshed.error.lower()
    # And the job is released, so the user can deliberately start again.
    from app.services import automation_ownership

    assert automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB) is None


# ---------------------------------------------------------------------------
# §15 — A dead browser must not leave the app looking alive
# ---------------------------------------------------------------------------

@db_required
def test_no_attempt_is_left_looking_active_after_a_full_restart(user):
    """§15. The strongest end-state assertion in this file: after recovery,
    NOTHING for this user is still in a status that claims a browser is
    driving it — because none is."""
    from app.api.applications import IN_PROGRESS_STATUSES
    from app.models.db_models import AUTONOMOUS_TASK_PAUSED_STATUSES

    db, uid = user
    _task(db, uid, status="RUNNING", url=JOB + "?b1")
    _task(db, uid, status="ANALYZING_JOB", url=JOB + "?b2")
    _application(db, uid, status="processing", url=JOB + "?b3")
    _application(db, uid, status="copilot_review", url=JOB + "?b4")

    session = SessionLocal()
    try:
        automation_recovery.reconcile_orphaned_automation_on_startup(session)
    finally:
        session.close()

    db.expire_all()
    live_apps = db.query(Application).filter(
        Application.user_id == uid, Application.status.in_(tuple(IN_PROGRESS_STATUSES)),
    ).count()
    assert live_apps == 0, "an application still claims to be running with no browser"

    live_tasks = [
        t.current_status for t in db.query(AutonomousTask).filter(AutonomousTask.user_id == uid).all()
        if t.current_status in ("CREATED", "ANALYZING_JOB", "RUNNING", "RESUMING")
    ]
    assert not live_tasks, f"tasks still claim to be executing: {live_tasks}"
    # Human pauses are the ONE thing allowed to survive — they are waiting on a
    # person, not on a dead browser.
    assert AUTONOMOUS_TASK_PAUSED_STATUSES == {"WAITING_FOR_HUMAN", "WAITING_FOR_APPROVAL"}


# ---------------------------------------------------------------------------
# §12 — Chat transcript stays truthful across a crash
# ---------------------------------------------------------------------------

@db_required
def test_recovery_does_not_duplicate_the_agents_question_in_the_transcript(user):
    """§12. The agent asked once. A restart must not make it look like it asked
    twice — a duplicated "please provide your information" would tell the user
    the system lost their answer when it simply restarted.

    Recovery is a STATE TRANSITION, not a conversation turn, so it must add no
    agent message at all.
    """
    from app.services import chat_repository

    db, uid = user
    task = _task(db, uid, status="RUNNING")
    req = hitl_repo.create_request(
        db, user_id=uid, task_id=task.task_id,
        request_type="ANSWER_REQUIRED", message="What is your expected salary?",
    )
    chat_repository.record_agent_message(
        db, user_id=uid, autonomous_task_id=task.task_id,
        content="What is your expected salary?", human_request_id=req.request_id,
    )

    for _ in range(3):
        session = SessionLocal()
        try:
            runner.reconcile_orphaned_tasks_on_startup(session)
        finally:
            session.close()

    db.expire_all()
    messages = chat_repository.list_for_task(db, task.task_id)
    asked = [m for m in messages if m.content == "What is your expected salary?"]
    assert len(asked) == 1, f"the question must appear exactly once, saw {len(asked)}"
    assert all(m.role != "user" for m in messages), "recovery must not fabricate a human reply"


@db_required
def test_the_transcript_never_contradicts_the_persisted_lifecycle(user):
    """§12/§11. Every message that points at a human request must point at one
    that really exists — a transcript prompt referencing a vanished request
    would render an answerable control the backend would reject."""
    from app.services import chat_repository

    db, uid = user
    task = _task(db, uid, status="RUNNING")
    req = hitl_repo.create_request(
        db, user_id=uid, task_id=task.task_id,
        request_type="CAPTCHA_REQUIRED", message="Please solve the CAPTCHA.",
    )
    chat_repository.record_agent_message(
        db, user_id=uid, autonomous_task_id=task.task_id,
        content="Please solve the CAPTCHA.", human_request_id=req.request_id,
    )

    session = SessionLocal()
    try:
        runner.reconcile_orphaned_tasks_on_startup(session)
    finally:
        session.close()

    db.expire_all()
    for message in chat_repository.list_for_task(db, task.task_id):
        if message.human_request_id is None:
            continue
        referenced = db.query(HumanInteractionRequest).filter(
            HumanInteractionRequest.request_id == message.human_request_id
        ).first()
        assert referenced is not None, "a prompt references a request that no longer exists"
        # And the task is terminal, so that prompt must no longer be answerable.
        assert referenced.status == "EXPIRED"
    assert task_repo.get_by_id(db, task.task_id).current_status == "FAILED"


# ---------------------------------------------------------------------------
# §17 — Event consistency after recovery
# ---------------------------------------------------------------------------

@db_required
def test_recovery_never_emits_a_submission_event(user):
    """§17. Recovery resolves ownership; it never submits. Emitting
    APPLICATION_SUBMITTED here would tell a watching browser the application
    went through when nothing of the sort happened."""
    from app.services import event_bus

    db, uid = user
    app_row = _application(db, uid, status="processing", url=JOB + "?ev")

    captured: list[str] = []
    real_publish = event_bus.bus.publish
    event_bus.bus.publish = lambda event: captured.append(event.event_type)
    try:
        session = SessionLocal()
        try:
            automation_recovery.reconcile_orphaned_applications_on_startup(session)
        finally:
            session.close()
    finally:
        event_bus.bus.publish = real_publish

    assert "APPLICATION_SUBMITTED" not in captured
    db.expire_all()
    assert application_repository.get_by_id(db, app_row.application_id).status == "needs_review"


@db_required
def test_repeated_recovery_does_not_re_emit_events_for_settled_work(user):
    """§17. Passes two and three claim nothing, so they must announce nothing —
    otherwise a reconnecting client would see a burst of transitions that never
    actually happened."""
    from app.services import event_bus

    db, uid = user
    _application(db, uid, status="processing", url=JOB + "?ev2")

    counts: list[int] = []
    real_publish = event_bus.bus.publish
    for _ in range(3):
        captured: list[str] = []
        event_bus.bus.publish = lambda event, sink=captured: sink.append(event.event_type)
        try:
            session = SessionLocal()
            try:
                automation_recovery.reconcile_orphaned_applications_on_startup(session)
            finally:
                session.close()
        finally:
            event_bus.bus.publish = real_publish
        counts.append(len(captured))

    assert counts[1] == 0 and counts[2] == 0, (
        f"only the first pass may emit events for this row, got {counts}"
    )


# ---------------------------------------------------------------------------
# §16 — Duplicate final submission
# ---------------------------------------------------------------------------

@db_required
def test_two_concurrent_approvals_of_one_task_yield_at_most_one_submission(user):
    """§16. A timed-out approve that the frontend retries, or two tabs clicking
    at once, must not submit twice. `try_claim_for_resume` is the arbiter — the
    same conditional UPDATE every other resume path goes through."""
    db, uid = user
    task = _task(db, uid, status="WAITING_FOR_APPROVAL")

    winners: list[bool] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def _approve():
        session = SessionLocal()
        try:
            row = task_repo.get_by_id(session, task.task_id)
            barrier.wait(timeout=30)
            winners.append(
                task_repo.try_claim_for_resume(session, row, from_status="WAITING_FOR_APPROVAL")
            )
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            session.close()

    threads = [threading.Thread(target=_approve) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"a duplicate approval must not raise: {errors}"
    assert sum(1 for w in winners if w) == 1, (
        f"exactly one approval may proceed to submission, got {winners}"
    )
