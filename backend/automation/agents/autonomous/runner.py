"""
Background execution for `AutonomousAgentLoop`.

**Deviation from the repo's stated convention, and why:** `AUTONOMOUS_AGENT.md`
/ this module's docstring both note that `automation/workers/celery_app.py`
is still an empty stub — Phase 4's Celery/Redis wiring was never actually
built (no `REDIS_URL` in `app/core/config.py`, no Celery `Task` classes
anywhere in the repo). Blocking `POST /agent/tasks` on a synchronous browser
run is not acceptable either. Rather than invent a parallel half-finished
Celery integration that can't be exercised without a broker to test against,
this module runs the loop on a plain daemon `threading.Thread` inside the
FastAPI process — the same "background work, in-process" shape
`app/api/resumes.py::_run_extraction` already uses via `BackgroundTasks`,
just with a manually-managed thread because this job is long-running and
needs external pause/resume signalling, which `BackgroundTasks` doesn't
support.

TODO (tracked, not silently skipped): once `automation/workers/celery_app.py`
is actually wired to a broker, migrate `start_task_background` to
`apply_async` and replace `_REGISTRY`'s in-process `threading.Event`
pause/resume signalling with a Celery-visible mechanism (e.g. polling
`AutonomousTask.current_status` from within the task, or a Redis pub/sub
channel) — the `TaskHandle`/`AutonomousAgentLoop` split above was kept
deliberately narrow so that swap only touches this file, not `loop.py`.

Known limitation this implies: an in-flight PAUSED task's actual open
browser tab does not survive a process restart (the `TaskHandle` living only
in `_REGISTRY`, in memory) — the DB row does, so the API still reports
accurate status, but resuming after a restart currently starts a fresh tab
at `job_url` rather than continuing the literal same page. Documented in
`AUTONOMOUS_AGENT.md`.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy.exc import SQLAlchemyError

from app.services import audit_log_repository as audit_log_repo
from app.services import autonomous_task_repository as task_repo
from app.services import human_interaction_repository as human_interaction_repo
from automation.agents.autonomous.loop import AutonomousAgentLoop, TaskHandle

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, TaskHandle] = {}
_REGISTRY_LOCK = threading.Lock()


def _get_or_create_handle(task_id: str) -> TaskHandle:
    with _REGISTRY_LOCK:
        handle = _REGISTRY.get(task_id)
        if handle is None:
            handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
            _REGISTRY[task_id] = handle
        return handle


def is_running(task_id: str) -> bool:
    with _REGISTRY_LOCK:
        return task_id in _REGISTRY


def start_task_background(task_id: str) -> None:
    """Starts (or resumes into) the loop for `task_id` on a daemon thread.
    Safe to call again for a task that's already running/paused — it's a
    no-op in that case (the resume endpoints call `signal_resume` instead)."""
    if is_running(task_id):
        logger.debug("start_task_background: task %s already has a live handle.", task_id)
        return
    handle = _get_or_create_handle(task_id)
    loop = AutonomousAgentLoop(task_id, handle)
    thread = threading.Thread(target=_run_and_cleanup, args=(task_id, loop), daemon=True, name=f"agent-task-{task_id}")
    thread.start()


def deliver_secret(task_id: str, request_id: str, value: str) -> bool:
    """Hands a transient OTP/MFA code to the live in-process loop for
    `task_id` and wakes it. Returns False (delivers nothing) if there is no
    live handle — e.g. the process restarted since the task paused — in
    which case the caller (`app/api/human_interaction.py`) must NOT fall back
    to `start_task_background` the way the plain `/resume` route does: a
    fresh tab has no verification field to fill, so silently "resuming" would
    just fail confusingly. The caller instead surfaces a new `LOGIN_REQUIRED`
    request asking the human to restart, per AUTONOMOUS_AGENT.md.

    The value lives only in `TaskHandle.pending_secret` in this process's
    memory, for exactly as long as it takes the loop to notice and consume
    it (`AutonomousAgentLoop._try_consume_pending_secret`, which clears the
    slot immediately after reading) — never persisted, logged, or returned."""
    with _REGISTRY_LOCK:
        handle = _REGISTRY.get(task_id)
    if handle is None:
        return False
    handle.pending_secret = {"request_id": request_id, "value": value}
    handle.resume_event.set()
    return True


def signal_resume(task_id: str) -> bool:
    """Wakes a paused task's loop. Returns False if the task has no live
    in-process handle (e.g. this process restarted after the task paused) —
    the caller (`app/api/autonomous_agent.py`) falls back to
    `start_task_background`, which begins a fresh run picking up the task's
    persisted state (confirmed answers, RESUMING status) but a NEW browser
    tab — see the module docstring's known limitation."""
    with _REGISTRY_LOCK:
        handle = _REGISTRY.get(task_id)
    if handle is None:
        return False
    handle.resume_event.set()
    return True


def request_cancel(task_id: str) -> bool:
    with _REGISTRY_LOCK:
        handle = _REGISTRY.get(task_id)
    if handle is None:
        return False
    handle.cancel_requested.set()
    handle.resume_event.set()  # wake it up if it was blocked waiting on a human
    return True


def _run_and_cleanup(task_id: str, loop: AutonomousAgentLoop) -> None:
    try:
        loop.run()
    finally:
        with _REGISTRY_LOCK:
            _REGISTRY.pop(task_id, None)


def reconcile_orphaned_tasks_on_startup(db) -> int:
    """Called once, at process startup (`app/main.py`), BEFORE anything can
    call `start_task_background`/`signal_resume`/`deliver_secret` — at that
    moment `_REGISTRY` is guaranteed empty (this is a fresh process), so any
    `AutonomousTask` still `RUNNING`/`RESUMING` in the database was left
    mid-flight by a PREVIOUS process that died without a live thread to ever
    finish, pause, or fail it. Left alone, such a task would sit in that
    status forever: nothing in this codebase re-scans for "tasks nobody is
    driving anymore."

    This never pretends automation "resumed" — it fails the task explicitly,
    safely, and observably, with a message telling the human to start a new
    task rather than silently vanishing or (worse) misreporting progress.
    `WAITING_FOR_HUMAN`/`WAITING_FOR_APPROVAL` tasks are untouched here — see
    `task_repo.list_orphaned_running_tasks`'s docstring for why those already
    have correct restart handling elsewhere.

    Also expires any `HumanInteractionRequest` left `PENDING` for one of
    these tasks (there normally isn't one — a RUNNING/RESUMING task has no
    active pause by definition — but a request created an instant before the
    crash, before the task's own status write landed, is possible; this is
    cheap defense against that sliver of a race).

    Returns the number of tasks reconciled (for a one-line startup log)."""
    # Snapshot the columns needed, rather than iterating live ORM objects.
    # `SessionLocal` leaves `expire_on_commit` at its default, so the first
    # claim below expires every remaining object in this list; touching one
    # afterwards re-SELECTs it one row at a time, and raises
    # `ObjectDeletedError` for any row that has since gone — which would abort
    # the whole pass and leave the rest of the orphans blocking their jobs.
    # (Observed for real: a concurrently-deleted task took down reconciliation.)
    # A snapshot cannot go stale in a way that matters, because
    # `try_claim_orphan_failed` re-checks the status at write time.
    orphaned = [
        (t.task_id, t.user_id, t.current_status, bool(t.auto_submit_approved))
        for t in task_repo.list_orphaned_running_tasks(db)
    ]
    reconciled = 0
    for task_id, user_id, prior_status, approved in orphaned:
        # ERROR ISOLATION. One unreconcilable task must not abort the pass and
        # leave every later orphan blocking its job — that is the exact failure
        # this whole function exists to prevent, so it must not be able to
        # happen to the fix itself.
        #
        # Deliberately NOT a bare `except Exception: pass`. `SQLAlchemyError`
        # covers the database-level things a single row can genuinely hit
        # (a row deleted underneath us, a serialization failure, a constraint
        # surprise) and is recorded and skipped. Anything else — a TypeError, an
        # AttributeError, a bug in this function — is left to propagate to
        # `reconcile_orphaned_automation_on_startup`, which logs it with a
        # traceback. Swallowing those would turn a programming bug into a
        # permanently silent no-op.
        try:
            reconciled += _reconcile_one_task(db, task_id, user_id, prior_status, approved)
        except SQLAlchemyError:
            db.rollback()
            logger.exception(
                "Task %s: could not be reconciled (was %s) — skipping and continuing.",
                task_id, prior_status,
            )
    return reconciled


def _reconcile_one_task(db, task_id: str, user_id: str, prior_status: str, approved: bool) -> int:
    """Reconcile exactly ONE orphaned task. Returns 1 if it was claimed and
    failed here, 0 if another worker had already moved it on (an expected
    race, not an error). Split out of the loop above purely so a per-task
    failure can be isolated without wrapping the whole body in a `try`."""
    # Submission ambiguity. The executor refuses to click a final submit
    # control unless `auto_submit_approved` is set
    # (`executor.py::is_submit_control_name`), and only
    # `POST /agent/tasks/{id}/approve` ever sets it. That flag is a
    # PERSISTED column, so it tells us — after the fact, from the row alone
    # — whether this task could possibly have submitted before it died.
    #
    # It stays FAILED either way: FAILED releases the job (the orphan is the
    # bug being fixed) and never resubmits anything by itself, since only a
    # deliberate new user action starts another task. What changes is that
    # the user is TOLD the outcome is uncertain, instead of being handed a
    # bare "interrupted, start a new task" that quietly invites a duplicate
    # application to a real employer.
    may_have_submitted = approved and prior_status in ("RUNNING", "RESUMING")
    if may_have_submitted:
        error = (
            "Automation was interrupted by a server restart after you had approved submission, "
            "so its outcome could not be recorded. This application MAY OR MAY NOT have been "
            "submitted — please check on the employer's site before starting a new task, since "
            "applying again when the first one went through would submit twice."
        )
    else:
        error = (
            "Automation was interrupted by a server restart while this task was in progress "
            "(no in-progress browser session survives a restart). Nothing was submitted — "
            "please start a new task."
        )
    # Conditional UPDATE rather than `mark_failed`: two recovery passes
    # cannot both claim this task, and a status that changed underneath us
    # (including COMPLETED) is never clobbered. See
    # `task_repo.try_claim_orphan_failed`.
    if not task_repo.try_claim_orphan_failed(db, task_id, from_status=prior_status, error=error):
        logger.info(
            "Task %s: no longer %s at reconciliation time — left alone.", task_id, prior_status,
        )
        return 0
    active_request = human_interaction_repo.get_active_for_task(db, task_id)
    if active_request is not None:
        # Read the id BEFORE `mark_expired` commits. Afterwards the object
        # is expired, and touching it re-SELECTs the row — raising
        # `ObjectDeletedError` if it has since been deleted. Same hazard
        # that took down the task loop above; the fix is the same.
        request_id = active_request.request_id
        human_interaction_repo.mark_expired(db, active_request)
        audit_log_repo.record_event(
            db, user_id=user_id, autonomous_task_id=task_id,
            event_type="human_request_expired", actor="system",
            metadata={"request_id": request_id, "reason": "orphaned_at_startup"},
        )
    audit_log_repo.record_event(
        db, user_id=user_id, autonomous_task_id=task_id,
        event_type="automation_failed", actor="system",
        metadata={
            "reason": "orphaned_at_startup",
            "prior_status": prior_status,
            # Recorded so "could this one have reached the employer?" is
            # answerable later from the audit trail alone, rather than
            # having to re-derive it from a status that is now just FAILED.
            "may_have_submitted": may_have_submitted,
        },
    )
    logger.warning(
        "Task %s: reconciled orphaned %s task to FAILED at startup (may_have_submitted=%s).",
        task_id, prior_status, may_have_submitted,
    )
    return 1
