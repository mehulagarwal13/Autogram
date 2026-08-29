"""
Crash-safe automation ownership: empirical reproduction, then recovery.

These tests were written AUDIT-FIRST. Every failure mode below was reproduced
against a real Postgres database before any recovery code existed, by injecting
the real failure (a raising `_run_on_dedicated_thread`, a background task that
never runs, a status committed with no live thread behind it) rather than by
asserting on what the code looked like.

## What the audit established

Both paths persist "this attempt owns this job" as a STATUS, and drive it from
memory:

* autonomous — `runner.py::_REGISTRY`, a module-level dict of `TaskHandle`,
  each with the `threading.Event`s that carry resume/cancel, plus the daemon
  thread actually running `AutonomousAgentLoop`;
* deterministic — FastAPI `BackgroundTasks` dispatching `_run_application`,
  which launches Playwright on a one-off thread, plus
  `application_flow_manager._OPEN_REVIEW_SESSIONS` holding a `copilot_review`
  run's still-open browser.

None of that survives a process exit, but the STATUS does — and
`automation_ownership.find_active_automation` reads only the status. So a
status that outlives the thread behind it is indistinguishable, to every
caller, from automation that is genuinely running.

The autonomous path already had `reconcile_orphaned_tasks_on_startup` for
exactly this, covering `RUNNING`/`RESUMING`. The gaps this file reproduces are
the states that reconciliation did not cover, and the deterministic path, which
had no reconciliation at all.

## Why `processing` must NOT be recovered to `failed`

`failed` is in `RETRYABLE_STATUSES`, and `retry_application` resets that row to
`pending` and runs the automation again. A run that crashed at `processing` may
have clicked Submit already — the employer's system is outside our transaction,
so the database genuinely cannot tell. Recovering to `failed` would therefore
invite a duplicate submission to a real employer.

`needs_review` is the status this repo ALREADY uses for exactly this ambiguity
(`application_flow_manager.submit_and_confirm`: submit clicked, no confirmation
detected -> `needs_review`, never silently retried). Recovery reuses it rather
than inventing a new state, so the crash case and the in-run case tell the user
the same thing.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text

from app.core.database import SessionLocal, engine
from app.models.db_models import (
    AUTONOMOUS_TASK_ACTIVE_STATUSES,
    Application,
    AutomationRun,
    AutonomousTask,
    HumanInteractionRequest,
    User,
)
from app.services import application_repository, automation_ownership, automation_recovery
from app.services import autonomous_task_repository as task_repo
from automation.agents.autonomous import runner

JOB_URL = "https://careers.example.com/jobs/crash-recovery/apply"


def _db_available() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


db_required = pytest.mark.skipif(
    not _db_available(), reason="No reachable Postgres — these exercise real ownership rows.",
)


@pytest.fixture
def user():
    db = SessionLocal()
    uid = f"crashrec_{uuid.uuid4().hex[:10]}"
    db.add(User(user_id=uid, email=f"{uid}@example.com", password_hash="x"))
    db.commit()
    yield db, uid
    db.rollback()
    db.query(HumanInteractionRequest).filter(HumanInteractionRequest.user_id == uid).delete()
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


def _make_application(db, uid, *, status="pending", url=JOB_URL):
    app = application_repository.create_application(
        db, user_id=uid, job_url=url, autopilot_enabled=False,
        company="Acme", position="SWE", source="server_automation",
    )
    if status != "pending":
        app.status = status
        db.commit()
        db.refresh(app)
    return app


def _make_task(db, uid, *, status="CREATED", url=JOB_URL, approved=False):
    task = task_repo.create_task(
        db, user_id=uid, job_url=url, original_objective="apply",
        candidate_profile={"profile": {}}, job_information={},
    )
    if status != "CREATED":
        task.current_status = status
    task.auto_submit_approved = approved
    db.commit()
    db.refresh(task)
    return task


# ---------------------------------------------------------------------------
# The premise, pinned: "active" is derived from STATUS ALONE
# ---------------------------------------------------------------------------
# Everything else in this file only matters because of this. If ownership ever
# started consulting a liveness signal, these reproductions would stop being
# reproductions and this test would say so.

def test_active_ownership_is_derived_from_status_with_no_liveness_check():
    """`find_active_automation` reads statuses. It has no notion of whether a
    thread, process, or browser is actually behind them — which is precisely
    why a status left behind by a dead process keeps blocking the job."""
    import inspect

    source = inspect.getsource(automation_ownership.find_active_automation)
    for liveness in ("is_running", "_REGISTRY", "heartbeat", "pid", "is_alive"):
        assert liveness not in source, (
            f"find_active_automation now consults {liveness!r} — the orphan reproductions "
            "in this file assume ownership is status-only and must be revisited."
        )


def test_analyzing_job_and_created_are_active_statuses():
    """Both are non-terminal, so both are in the derived active set and both
    block the job — the reason a crash in either is not merely untidy."""
    assert "CREATED" in AUTONOMOUS_TASK_ACTIVE_STATUSES
    assert "ANALYZING_JOB" in AUTONOMOUS_TASK_ACTIVE_STATUSES


# ---------------------------------------------------------------------------
# CASE A — row committed, executor never starts
# ---------------------------------------------------------------------------

@db_required
def test_case_a_deterministic_pending_with_no_background_task_blocks_the_job(user):
    """`POST /applications/start` commits `pending`, THEN hands
    `_run_application` to `BackgroundTasks`. A process that dies in between
    leaves `pending` with nothing that will ever pick it up.

    No browser can exist here: `mark_processing` is the first thing
    `_run_application` does, well before Playwright launches. So this state is
    unambiguously "never executed".
    """
    db, uid = user
    app = _make_application(db, uid, status="pending")

    # The orphan, as reproduced before recovery existed: it owns the job.
    active = automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL)
    assert active is not None, "a pending attempt must own the job"
    assert active.path == "deterministic"
    assert active.application_id == app.application_id

    # And now: startup recovery releases it. `failed`, not `needs_review`,
    # because no browser was ever opened so there is no submission ambiguity.
    assert automation_recovery.reconcile_orphaned_applications_on_startup(db) >= 1
    db.expire_all()
    assert application_repository.get_by_id(db, app.application_id).status == "failed"
    assert automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL) is None
    # Recovery must never invent a submission.
    assert automation_ownership.find_submitted_application(db, user_id=uid, job_url=JOB_URL) is None


@db_required
def test_case_a_autonomous_created_is_not_covered_by_the_existing_reconciler(user):
    """`POST /agent/tasks` commits `CREATED`, then `ANALYZING_JOB`, then starts
    the thread. `list_orphaned_running_tasks` only looks at
    `RUNNING`/`RESUMING`, so a crash before the thread starts is invisible to
    the reconciler that exists.
    """
    db, uid = user
    task = _make_task(db, uid, status="CREATED")

    assert automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL) is not None
    # Before the fix the reconciler filtered on ("RUNNING", "RESUMING") only,
    # so this task was invisible to it and blocked the job forever.
    assert task.task_id in [t.task_id for t in task_repo.list_orphaned_running_tasks(db)]

    assert runner.reconcile_orphaned_tasks_on_startup(db) >= 1
    db.expire_all()
    assert task_repo.get_by_id(db, task.task_id).current_status == "FAILED"
    assert automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL) is None


@db_required
def test_case_a_autonomous_analyzing_job_is_not_covered_by_the_existing_reconciler(user):
    """Same window, one status later — `set_status(ANALYZING_JOB)` commits
    before `start_task_background` is even called."""
    db, uid = user
    task = _make_task(db, uid, status="ANALYZING_JOB")

    assert automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL) is not None
    assert runner.reconcile_orphaned_tasks_on_startup(db) >= 1
    db.expire_all()
    assert task_repo.get_by_id(db, task.task_id).current_status == "FAILED"
    assert automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL) is None


# ---------------------------------------------------------------------------
# CASE B — the executor dies DURING execution
# ---------------------------------------------------------------------------

@db_required
def test_case_b_deterministic_run_crash_leaves_the_row_processing_forever(user, monkeypatch):
    """The strongest reproduction here, because it injects a REAL exception
    into the real `_run_application` rather than simulating a kill.

    `_run_application`'s outer `except Exception` logs, rolls back, and
    returns — it never writes a terminal status. So any unexpected failure
    after `mark_processing` (Playwright dying, a driver error, the result
    write failing) leaves `processing` persisted with no thread behind it.
    """
    import app.api.applications as applications_api

    db, uid = user
    app = _make_application(db, uid, status="pending")

    # ATS detection is stubbed because the real one navigates a real browser to
    # `job_url`, and a test URL cannot resolve — that failure has its OWN
    # guarded branch (-> `failed`), which is not the window under test. Stubbing
    # it to the "custom"/generic outcome is what a real reachable job page would
    # produce, and lets the run proceed to the browser-run step.
    monkeypatch.setattr(
        applications_api, "detect_ats_for_url", lambda url: {"ats": applications_api.FALLBACK_ATS},
    )
    # Fail exactly where the real browser run happens. `mark_processing` has
    # already committed by this point, which is the whole premise.
    def _boom(fn):
        raise RuntimeError("simulated Playwright/driver death mid-run")

    monkeypatch.setattr(applications_api, "_run_on_dedicated_thread", _boom)
    # A profile and resume must exist or the run bails out early with `failed`
    # (its own guarded path), which is also not the window under test.
    resume_document_id = _install_profile_and_resume(db, uid)

    applications_api._run_application(app.application_id, resume_document_id)

    db.expire_all()
    refreshed = application_repository.get_by_id(db, app.application_id)
    # BEFORE the fix this asserted `status == "processing"` and passed: the
    # handler logged, rolled back, and left ownership behind with the process
    # still alive and no restart coming.
    assert refreshed.status == "needs_review", (
        f"a crashed run must release ownership, got {refreshed.status!r}"
    )
    # `needs_review`, NEVER `failed`: the run may already have clicked Submit,
    # and `failed` would let `retry_application` resubmit automatically.
    assert refreshed.status != "failed"
    assert refreshed.failure_reason and "may or may not have been submitted" in refreshed.failure_reason
    assert automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL) is None
    # The uncertainty is on the audit trail, not just in a message.
    assert _audit_metadata(db, app.application_id, "automation_recovered")["may_have_submitted"] is True


@db_required
def test_case_b_autonomous_running_IS_covered_by_the_existing_reconciler(user):
    """The half that already worked, pinned so recovery work cannot regress
    it. `_REGISTRY` is empty in a fresh process, so a persisted `RUNNING` had
    a thread that died with the previous process."""
    db, uid = user
    task = _make_task(db, uid, status="RUNNING")

    assert not runner.is_running(task.task_id), "no live handle for a row we wrote directly"
    orphans = [t.task_id for t in task_repo.list_orphaned_running_tasks(db)]
    assert task.task_id in orphans

    reconciled = runner.reconcile_orphaned_tasks_on_startup(db)
    assert reconciled >= 1
    db.expire_all()
    assert task_repo.get_by_id(db, task.task_id).current_status == "FAILED"
    assert automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL) is None


# ---------------------------------------------------------------------------
# CASE C / CASE D — legitimate persisted pauses must NOT be expired
# ---------------------------------------------------------------------------

@db_required
@pytest.mark.parametrize("status", ["WAITING_FOR_HUMAN", "WAITING_FOR_APPROVAL"])
def test_cases_c_and_d_waiting_states_survive_restart_untouched(user, status):
    """A human pause is a legitimate persisted state, not an orphan: the
    resume/approve/cancel routes all still work after a restart (they fall
    back to `start_task_background` when `signal_resume` finds no handle, and
    `cancel_task` persists unconditionally). Recovery must leave them alone.
    """
    db, uid = user
    task = _make_task(db, uid, status=status)

    runner.reconcile_orphaned_tasks_on_startup(db)
    db.expire_all()
    assert task_repo.get_by_id(db, task.task_id).current_status == status, (
        f"{status} must never be expired by recovery — a human is legitimately mid-loop"
    )


@db_required
def test_case_c_pending_human_request_survives_a_restart(user):
    """The request row is durable, so the human can still answer after a
    restart even though the `TaskHandle` that was waiting on it is gone."""
    from app.services import human_interaction_repository as hitl_repo

    db, uid = user
    task = _make_task(db, uid, status="WAITING_FOR_HUMAN")
    request = hitl_repo.create_request(
        db, user_id=uid, task_id=task.task_id,
        request_type="LOGIN_REQUIRED", message="Please log in.",
    )

    db.expire_all()
    still_there = hitl_repo.get_active_for_task(db, task.task_id)
    assert still_there is not None and still_there.request_id == request.request_id
    assert still_there.status == "PENDING"


# ---------------------------------------------------------------------------
# Submission safety — the reason `processing` is not recovered to `failed`
# ---------------------------------------------------------------------------

@db_required
def test_submitted_history_is_never_rolled_back_by_recovery(user):
    """`applied` and `COMPLETED` are outside every recovery query. Pinned
    because rolling one back would destroy the record of a real submission and
    silently re-open the job to automatic re-application."""
    db, uid = user
    app = _make_application(db, uid, status="applied", url=JOB_URL + "?a=1")
    task = _make_task(db, uid, status="COMPLETED", url=JOB_URL + "?a=2")

    runner.reconcile_orphaned_tasks_on_startup(db)
    db.expire_all()
    assert application_repository.get_by_id(db, app.application_id).status == "applied"
    assert task_repo.get_by_id(db, task.task_id).current_status == "COMPLETED"


# ---------------------------------------------------------------------------
# helpers that need a real profile/resume for `_run_application`
# ---------------------------------------------------------------------------

def _install_profile_and_resume(db, uid):
    from app.models.db_models import CandidateProfile, ProfileDocument

    profile = CandidateProfile(
        profile_id=f"prof_{uuid.uuid4().hex[:10]}", user_id=uid,
        full_name="Crash Test", email=f"{uid}@example.com",
    )
    db.add(profile)
    db.commit()
    doc = ProfileDocument(
        document_id=f"doc_{uuid.uuid4().hex[:10]}", profile_id=profile.profile_id,
        document_type="resume", original_filename="cv.pdf",
        stored_path="storage/cv.pdf", file_hash=uuid.uuid4().hex,
    )
    db.add(doc)
    db.commit()
    return doc.document_id


def _audit_metadata(db, application_id, event_type):
    from app.models.db_models import ApplicationAuditLog

    db.expire_all()
    entry = (
        db.query(ApplicationAuditLog)
        .filter(
            ApplicationAuditLog.application_id == application_id,
            ApplicationAuditLog.event_type == event_type,
        )
        .order_by(ApplicationAuditLog.created_at.desc())
        .first()
    )
    assert entry is not None, f"no {event_type} audit event for {application_id}"
    return entry.event_metadata or {}


# ---------------------------------------------------------------------------
# Recovery scope, pinned against drift
# ---------------------------------------------------------------------------

def test_every_in_progress_status_has_a_recovery_rule():
    """The whole class of bug here is "an active status nothing recovers". A
    new member of `IN_PROGRESS_STATUSES` without a recovery rule would be
    exactly that, silently — so the two sets are pinned equal."""
    from app.api.applications import IN_PROGRESS_STATUSES

    assert set(automation_recovery.DETERMINISTIC_RECOVERY) == set(IN_PROGRESS_STATUSES)


def test_recovery_targets_are_never_active_statuses():
    """Recovering into another active status would just move the orphan."""
    from app.api.applications import IN_PROGRESS_STATUSES

    for from_status, (to_status, _reason) in automation_recovery.DETERMINISTIC_RECOVERY.items():
        assert to_status not in IN_PROGRESS_STATUSES, (
            f"{from_status} -> {to_status} leaves the job still owned"
        )


def test_processing_is_never_recovered_to_failed():
    """The single most important rule in this module. `failed` is retryable and
    `retry_application` re-runs the automation, so recovering a possibly-
    submitted attempt to `failed` would invite a duplicate application."""
    to_status, reason = automation_recovery.DETERMINISTIC_RECOVERY["processing"]
    assert to_status == "needs_review"
    assert "may or may not have been submitted" in reason


def test_orphanable_autonomous_statuses_exclude_human_pauses_and_terminals():
    """Legitimate persisted pauses must never be expired by recovery, and a
    terminal status must never be re-opened."""
    from app.models.db_models import (
        AUTONOMOUS_TASK_PAUSED_STATUSES,
        AUTONOMOUS_TASK_TERMINAL_STATUSES,
    )

    orphanable = set(task_repo.ORPHANABLE_EXECUTING_STATUSES)
    assert not (orphanable & AUTONOMOUS_TASK_PAUSED_STATUSES)
    assert not (orphanable & AUTONOMOUS_TASK_TERMINAL_STATUSES)
    # And it must cover every remaining active status, or a gap reappears.
    assert orphanable == AUTONOMOUS_TASK_ACTIVE_STATUSES - AUTONOMOUS_TASK_PAUSED_STATUSES


# ---------------------------------------------------------------------------
# Recovery outcomes per state, against real Postgres
# ---------------------------------------------------------------------------

@db_required
@pytest.mark.parametrize(
    "from_status,expected",
    [("pending", "failed"), ("processing", "needs_review"), ("copilot_review", "needs_review")],
)
def test_startup_recovery_resolves_each_orphaned_state(user, from_status, expected):
    db, uid = user
    app = _make_application(db, uid, status=from_status)

    assert automation_recovery.reconcile_orphaned_applications_on_startup(db) >= 1
    db.expire_all()
    refreshed = application_repository.get_by_id(db, app.application_id)
    assert refreshed.status == expected
    assert refreshed.failure_reason, "recovery must explain itself on the row"
    assert automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL) is None
    meta = _audit_metadata(db, app.application_id, "automation_recovered")
    assert meta["prior_status"] == from_status
    assert meta["new_status"] == expected
    # Only a run that reached `processing` could have clicked Submit.
    assert meta["may_have_submitted"] is (from_status == "processing")


@db_required
def test_copilot_review_recovery_is_reported_as_not_submitted(user):
    """A `copilot_review` run was waiting for approval, so `submit_and_confirm`
    was never called — there is no ambiguity, only a lost browser."""
    db, uid = user
    app = _make_application(db, uid, status="copilot_review")

    automation_recovery.reconcile_orphaned_applications_on_startup(db)
    db.expire_all()
    assert application_repository.get_by_id(db, app.application_id).status == "needs_review"
    assert _audit_metadata(db, app.application_id, "automation_recovered")["may_have_submitted"] is False


@db_required
def test_recovery_never_touches_an_applied_or_completed_row(user):
    """Pinned hard: recovery must not roll back submission history, and must
    not re-open a terminal task."""
    db, uid = user
    applied = _make_application(db, uid, status="applied", url=JOB_URL + "?x=1")
    cancelled = _make_application(db, uid, status="cancelled", url=JOB_URL + "?x=2")
    done = _make_task(db, uid, status="COMPLETED", url=JOB_URL + "?x=3")

    automation_recovery.reconcile_orphaned_automation_on_startup(db)
    db.expire_all()
    assert application_repository.get_by_id(db, applied.application_id).status == "applied"
    assert application_repository.get_by_id(db, cancelled.application_id).status == "cancelled"
    assert task_repo.get_by_id(db, done.task_id).current_status == "COMPLETED"


@db_required
def test_autonomous_recovery_flags_a_possible_submission(user):
    """A task that had been APPROVED to submit and then died mid-run is the one
    autonomous case where the employer may already have the application. It
    still fails (releasing the job) but says so explicitly."""
    from app.models.db_models import ApplicationAuditLog

    db, uid = user
    task = _make_task(db, uid, status="RUNNING", approved=True)

    assert runner.reconcile_orphaned_tasks_on_startup(db) >= 1
    db.expire_all()
    refreshed = task_repo.get_by_id(db, task.task_id)
    assert refreshed.current_status == "FAILED"
    assert "MAY OR MAY NOT have been submitted" in refreshed.error
    entry = (
        db.query(ApplicationAuditLog)
        .filter(
            ApplicationAuditLog.autonomous_task_id == task.task_id,
            ApplicationAuditLog.event_type == "automation_failed",
        )
        .first()
    )
    assert entry.event_metadata["may_have_submitted"] is True


@db_required
def test_autonomous_recovery_of_an_unapproved_task_states_nothing_was_submitted(user):
    """Without approval the executor refuses to click any submit control, so
    this case is unambiguous and must not be described as uncertain."""
    db, uid = user
    task = _make_task(db, uid, status="RUNNING", approved=False)

    runner.reconcile_orphaned_tasks_on_startup(db)
    db.expire_all()
    error = task_repo.get_by_id(db, task.task_id).error
    assert "Nothing was submitted" in error
    assert "MAY OR MAY NOT" not in error


# ---------------------------------------------------------------------------
# CONCURRENCY — real Postgres sessions
# ---------------------------------------------------------------------------

@db_required
def test_two_recovery_workers_cannot_both_recover_one_attempt(user):
    """Step 6: multiple recovery passes must not independently recover the same
    attempt. The conditional UPDATE decides it — the loser matches zero rows."""
    db, uid = user
    app = _make_application(db, uid, status="processing")

    results: list = []
    errors: list = []
    barrier = threading.Barrier(4)

    def _recover():
        session = SessionLocal()
        try:
            barrier.wait(timeout=30)
            results.append(
                automation_recovery.try_recover_application(
                    session, application_id=app.application_id, from_status="processing",
                )
            )
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            session.close()

    threads = [threading.Thread(target=_recover) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"recovery raised under contention: {errors}"
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"exactly one worker must recover, got {results}"
    assert winners[0] == "needs_review"
    db.expire_all()
    assert application_repository.get_by_id(db, app.application_id).status == "needs_review"


@db_required
def test_recovery_never_leaves_a_job_owned_by_two_attempts(user):
    """Step 6: recovery racing a fresh start must never produce two active
    owners. The start either loses (the stale attempt is still authoritative,
    409) or wins after recovery released the job — never both."""
    db, uid = user
    _make_application(db, uid, status="processing")

    observed: list = []
    errors: list = []
    barrier = threading.Barrier(2)

    def _recover():
        session = SessionLocal()
        try:
            barrier.wait(timeout=30)
            automation_recovery.reconcile_orphaned_applications_on_startup(session)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            session.close()

    def _start():
        session = SessionLocal()
        try:
            barrier.wait(timeout=30)
            # Exactly what the route does: take the lock, then look.
            automation_ownership.reserve_job_automation(session, user_id=uid, job_url=JOB_URL)
            active = automation_ownership.find_active_automation(session, user_id=uid, job_url=JOB_URL)
            observed.append(active.status if active else None)
            session.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            session.close()

    threads = [threading.Thread(target=_recover), threading.Thread(target=_start)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"race raised: {errors}"
    # Either it still saw the stale attempt as the owner, or recovery had
    # already released the job. Both are correct; a second ACTIVE row is not.
    assert observed and observed[0] in ("processing", None), observed
    db.expire_all()
    remaining = (
        db.query(Application)
        .filter(Application.user_id == uid, Application.status.in_(("pending", "processing", "copilot_review")))
        .count()
    )
    assert remaining <= 1, "a job must never be owned by two active attempts"


@db_required
def test_a_stale_active_attempt_still_blocks_a_re_application_until_recovery(user):
    """Step 6 / Step 5: "active always wins" must survive this work. An
    acknowledged re-application cannot slip past a still-active stale attempt
    just because that attempt is secretly dead."""
    db, uid = user
    _make_application(db, uid, status="applied", url=JOB_URL)
    # A stale ACTIVE attempt on the same job, from a crashed retry.
    _make_application(db, uid, status="processing", url=JOB_URL)

    active = automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL)
    assert active is not None and active.status == "processing", (
        "the stale attempt must remain authoritative until recovery resolves it"
    )

    automation_recovery.reconcile_orphaned_applications_on_startup(db)
    db.expire_all()
    assert automation_ownership.find_active_automation(db, user_id=uid, job_url=JOB_URL) is None
    # And the successful submission is still on file, so a re-application still
    # requires an explicit acknowledgement.
    submitted = automation_ownership.find_submitted_application(db, user_id=uid, job_url=JOB_URL)
    assert submitted is not None and submitted.path == "deterministic"
