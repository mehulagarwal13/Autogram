"""
Data access for `AutonomousTask` (see `app/models/db_models.py`) — the
general-purpose autonomous browser agent's persistence. Same layering
convention as every other `*_repository.py` module here: plain functions
taking a `Session`, no business logic beyond simple state transitions, called
from `app/api/autonomous_agent.py` and from
`automation/agents/autonomous/loop.py` (via `automation.interfaces
.automation_db_session()` when running off the request thread).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.db_models import (
    AUTONOMOUS_TASK_ACTIVE_STATUSES,
    AUTONOMOUS_TASK_TERMINAL_STATUSES,
    AutonomousTask,
    VALID_AUTONOMOUS_TASK_STATUSES,
)
from app.services.application_repository import compute_job_url_hash


class TerminalTaskError(Exception):
    """Raised when code tries to transition a task that has ALREADY reached
    a terminal status (`COMPLETED`/`FAILED`/`CANCELLED`) into something else.
    Terminal states are final — a stray/late loop iteration, a race between
    a cancellation and a completion, or a duplicate resume signal must never
    resurrect a task that has already finished. Callers (`loop.py`) treat
    this as "the task already ended, stop quietly" — never as a crash."""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _guard_not_terminal(task: AutonomousTask, action: str) -> None:
    if task.current_status in AUTONOMOUS_TASK_TERMINAL_STATUSES:
        raise TerminalTaskError(
            f"Task {task.task_id} is already {task.current_status}; refusing to apply {action!r}."
        )


def create_task(
    db: Session,
    *,
    user_id: str,
    job_url: str,
    original_objective: str,
    candidate_profile: dict | None = None,
    job_information: dict | None = None,
    uploaded_documents: list[dict] | None = None,
) -> AutonomousTask:
    task = AutonomousTask(
        task_id=_new_id("agent_task"),
        user_id=user_id,
        job_url=job_url,
        # Reuses the deterministic path's hash so both systems identify a job
        # identically — see `app/services/automation_ownership.py`. Also what
        # the partial unique index `uq_autonomous_tasks_active_job` is built
        # on, so a duplicate active task fails at the DB rather than opening a
        # second browser tab.
        job_url_hash=compute_job_url_hash(job_url),
        original_objective=original_objective,
        candidate_profile=candidate_profile or {},
        job_information=job_information,
        current_status="CREATED",
        action_history=[],
        application_progress={},
        confirmed_answers={},
        # The `upload_file` allowlist for this task — see
        # `app/api/autonomous_agent.py::_build_uploadable_documents`.
        uploaded_documents=uploaded_documents or [],
        auto_submit_approved=False,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def get_by_id(db: Session, task_id: str) -> AutonomousTask | None:
    return db.query(AutonomousTask).filter(AutonomousTask.task_id == task_id).first()


def get_active_for_user_and_url(db: Session, user_id: str, job_url: str) -> AutonomousTask | None:
    """The still-active autonomous task for this (user, job), if any — the
    autonomous counterpart to
    `application_repository.get_by_user_and_url`, used by
    `POST /agent/tasks` before it creates anything or opens a browser.

    "Active" excludes COMPLETED/FAILED/CANCELLED, so a retry after a failure
    or a cancellation is allowed (and matches exactly what the partial unique
    index `uq_autonomous_tasks_active_job` enforces)."""
    return (
        db.query(AutonomousTask)
        .filter(
            AutonomousTask.user_id == user_id,
            AutonomousTask.job_url_hash == compute_job_url_hash(job_url),
            AutonomousTask.current_status.in_(tuple(AUTONOMOUS_TASK_ACTIVE_STATUSES)),
        )
        .order_by(AutonomousTask.created_at.desc())
        .first()
    )


def list_for_user(db: Session, user_id: str) -> list[AutonomousTask]:
    return (
        db.query(AutonomousTask)
        .filter(AutonomousTask.user_id == user_id)
        .order_by(AutonomousTask.created_at.desc())
        .all()
    )


def set_status(db: Session, task: AutonomousTask, status: str) -> AutonomousTask:
    if status not in VALID_AUTONOMOUS_TASK_STATUSES:
        raise ValueError(f"Unknown autonomous task status: {status!r}")
    _guard_not_terminal(task, f"set_status({status!r})")
    task.current_status = status
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


def update_browser_state(db: Session, task: AutonomousTask, page_state: dict) -> AutonomousTask:
    task.current_browser_state = page_state
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


def append_action(db: Session, task: AutonomousTask, action_record: dict) -> AutonomousTask:
    """Appends one entry to `action_history`. Reassigns the whole list
    (rather than mutating in place) so SQLAlchemy's JSONB change-tracking —
    which does not see in-place mutation of a mutable Python object — reliably
    flags the column dirty."""
    history = list(task.action_history or [])
    history.append(action_record)
    task.action_history = history
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


def update_progress(db: Session, task: AutonomousTask, progress: dict) -> AutonomousTask:
    task.application_progress = progress
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


def request_human_intervention(db: Session, task: AutonomousTask, intervention: dict) -> AutonomousTask:
    _guard_not_terminal(task, "request_human_intervention")
    task.human_intervention = intervention
    task.current_status = "WAITING_FOR_HUMAN"
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


def record_confirmed_answer(db: Session, task: AutonomousTask, question: str, answer: str) -> AutonomousTask:
    """Stores a human-supplied answer scoped to THIS task only (see
    `AUTONOMOUS_AGENT.md`'s no-invention policy) — never written into the
    global profile or `answer_cache` from here. Clears any pending
    intervention and moves the task back to RESUMING so the loop re-observes
    the page from scratch before continuing, per spec.

    Callers (`app/api/autonomous_agent.py`, `app/api/human_interaction.py`)
    MUST have already won `try_claim_for_resume` for this task before calling
    this — this function only writes `confirmed_answers`/`human_intervention`,
    it does not itself guard against a second concurrent caller."""
    _guard_not_terminal(task, "record_confirmed_answer")
    answers = dict(task.confirmed_answers or {})
    answers[question] = answer
    task.confirmed_answers = answers
    task.human_intervention = None
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


def clear_intervention_for_resume(db: Session, task: AutonomousTask) -> AutonomousTask:
    """Used for interventions that don't carry a specific answer (e.g. "I
    logged in / solved the CAPTCHA, continue"). Callers should already have
    won `try_claim_for_resume` (below) — that call already flipped
    `current_status` to RESUMING and cleared `human_intervention`, so this is
    now mostly a compatibility no-op kept for any caller that hasn't been
    migrated to the atomic claim yet. Idempotent either way."""
    _guard_not_terminal(task, "clear_intervention_for_resume")
    task.human_intervention = None
    task.current_status = "RESUMING"
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


def try_claim_for_resume(db: Session, task: AutonomousTask, *, from_status: str) -> bool:
    """Atomically transitions `task.current_status` from `from_status` (e.g.
    `WAITING_FOR_HUMAN` or `WAITING_FOR_APPROVAL`) to `RESUMING` via a single
    conditional `UPDATE ... WHERE task_id = ? AND current_status = ?`, also
    clearing `human_intervention`. This is the authoritative guard against
    two concurrent "resume this task" calls racing each other — whether both
    are the same route (two `/respond` calls), or different routes (the
    legacy `/resume`/`/answer`/`/approve` racing the new
    `/human-requests/{id}/respond`). The loser's UPDATE matches zero rows
    (the winner already changed `current_status`) and this returns `False`
    — the caller MUST treat that as "someone else already resumed this
    task" and stop immediately: never call `signal_resume`/`deliver_secret`,
    never write `confirmed_answers`, never touch `auto_submit_approved`.

    Every route that can resume a paused task (`app/api/autonomous_agent.py`'s
    `/resume`, `/answer`, `/approve`, and `app/api/human_interaction.py`'s
    `/respond`) MUST call this FIRST and bail out on `False` before doing
    anything else."""
    rows_matched = (
        db.query(AutonomousTask)
        .filter(AutonomousTask.task_id == task.task_id, AutonomousTask.current_status == from_status)
        .update(
            {"current_status": "RESUMING", "human_intervention": None, "updated_at": datetime.now(timezone.utc)},
            synchronize_session=False,
        )
    )
    db.commit()
    db.refresh(task)
    return rows_matched == 1


def mark_ready_for_approval(db: Session, task: AutonomousTask, final_result: dict) -> AutonomousTask:
    _guard_not_terminal(task, "mark_ready_for_approval")
    task.final_result = final_result
    task.current_status = "WAITING_FOR_APPROVAL"
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


def approve_submission(db: Session, task: AutonomousTask) -> AutonomousTask:
    """Explicit per-task consent to click the final submit control — the ONLY
    place `auto_submit_approved` is ever set True. Moves the task back to
    RESUMING so the loop re-observes the review page before clicking
    anything, never assuming the page is unchanged since it stopped.
    Callers should already have won `try_claim_for_resume`."""
    _guard_not_terminal(task, "approve_submission")
    task.auto_submit_approved = True
    task.current_status = "RESUMING"
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


def mark_completed(db: Session, task: AutonomousTask, final_result: dict) -> AutonomousTask:
    _guard_not_terminal(task, "mark_completed")
    task.final_result = final_result
    task.current_status = "COMPLETED"
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


def mark_failed(db: Session, task: AutonomousTask, error: str, final_result: dict | None = None) -> AutonomousTask:
    _guard_not_terminal(task, "mark_failed")
    task.error = error
    if final_result is not None:
        task.final_result = final_result
    task.current_status = "FAILED"
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


def cancel_task(db: Session, task: AutonomousTask) -> AutonomousTask:
    """Idempotent by design (unlike the other transitions above, which raise
    `TerminalTaskError`): cancelling an already-terminal task is a completely
    ordinary thing for a client to do (e.g. a double-click, or a cancel that
    arrives just after the task finished on its own) and should just report
    the task's real final state rather than erroring."""
    if task.current_status in AUTONOMOUS_TASK_TERMINAL_STATUSES:
        return task
    task.current_status = "CANCELLED"
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


#: Statuses a task can hold ONLY while an in-process thread is driving it (or
#: is about to be). Every one of them is non-terminal, so every one of them
#: blocks the job via `uq_autonomous_tasks_active_job` /
#: `automation_ownership.find_active_automation`.
#:
#: `CREATED` and `ANALYZING_JOB` were added after the crash-recovery audit
#: (`automation/tests/test_crash_recovery.py`) reproduced them as permanent
#: orphans: `POST /agent/tasks` commits `CREATED`, then commits
#: `ANALYZING_JOB`, and only THEN calls `start_task_background` — so a process
#: that dies anywhere in that window left an active task no thread ever owned,
#: and the previous `("RUNNING", "RESUMING")` filter could not see it.
#:
#: Deliberately NOT the whole active set: `WAITING_FOR_HUMAN` and
#: `WAITING_FOR_APPROVAL` are excluded, because those are legitimate PERSISTED
#: pauses rather than orphans. They already have correct, tested restart
#: handling — `signal_resume`/`deliver_secret` return `False` when no live
#: handle exists and the routes fall back accordingly, and `cancel_task`
#: persists unconditionally — so a human re-engaging with them is the normal
#: way they move forward. Expiring them on restart would destroy a pause a
#: user is actively working through.
ORPHANABLE_EXECUTING_STATUSES = ("CREATED", "ANALYZING_JOB", "RUNNING", "RESUMING")


def try_claim_orphan_failed(
    db: Session, task_id: str, *, from_status: str, error: str,
) -> bool:
    """Atomically fail ONE orphaned task via a conditional
    `UPDATE ... WHERE task_id = ? AND current_status = ?`, mirroring
    `try_claim_for_resume`'s pattern. Returns False if it matched no rows.

    Takes a `task_id` rather than an ORM object — unlike its neighbours — because
    its caller iterates a LIST of orphans across commits. `SessionLocal` leaves
    `expire_on_commit` at its default, so this function's own commit expires
    every other object the caller is still holding; touching one afterwards
    re-SELECTs it, and raises `ObjectDeletedError` if that row has since gone.
    Working by id keeps the caller free of live ORM state entirely.

    Used instead of `mark_failed` by startup reconciliation for two reasons:

    * two recovery passes cannot both recover the same task — the loser matches
      zero rows and backs off, where `mark_failed` would raise
      `TerminalTaskError` on the second attempt (a hard error for what is
      really just a lost race);
    * it cannot clobber a status that changed underneath it — including, most
      importantly, `COMPLETED`. A submitted task is structurally unreachable
      here because `from_status` is always one of
      `ORPHANABLE_EXECUTING_STATUSES`, so recovery can never roll back
      submission history."""
    rows_matched = (
        db.query(AutonomousTask)
        .filter(AutonomousTask.task_id == task_id, AutonomousTask.current_status == from_status)
        .update(
            {
                "current_status": "FAILED",
                "error": error,
                "updated_at": datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return bool(rows_matched)


def list_orphaned_running_tasks(db: Session) -> list[AutonomousTask]:
    """Tasks stuck in an executing status with no chance of ever being picked
    up again — used ONLY at process startup (see
    `app/main.py`/`automation/agents/autonomous/runner.py::reconcile_orphaned_tasks_on_startup`).

    Reasoning: `runner.py`'s in-process registry (`_REGISTRY`) is what actually
    drives a task through these statuses, and it is, by construction, EMPTY the
    instant this process starts — so any task found here at that moment was
    left mid-flight by a PREVIOUS process that died (crash, deploy, restart)
    without a live thread to ever finish or pause it.

    See `ORPHANABLE_EXECUTING_STATUSES` for which statuses qualify and, more
    importantly, which are deliberately excluded."""
    return (
        db.query(AutonomousTask)
        .filter(AutonomousTask.current_status.in_(ORPHANABLE_EXECUTING_STATUSES))
        .all()
    )
