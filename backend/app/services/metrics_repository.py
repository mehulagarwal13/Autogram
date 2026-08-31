"""
Aggregation queries backing `GET /metrics/summary` — the four success
metrics named in the original Autogram planning doc (median time-to-outcome,
HITL resolution rate, a submission-accuracy proxy, and a field-mapping-
confidence proxy). Every input already existed for another purpose (action
history, confidence scores, answer sources, HITL timestamps); this module
only aggregates it — no new instrumentation.

Two blocks, not one: the deterministic (`Application`/`ApplicationQuestion`/
`AutomationRun`) and autonomous (`AutonomousTask`/`HumanInteractionRequest`)
paths are separate systems with separate data shapes (see
`automation/applications/application_flow_manager.py` vs
`automation/agents/autonomous/loop.py`) — a single blended number would
obscure which engine it actually describes.

Honesty note: "submission accuracy" as the plan literally defined it ("% of
applications submitted with zero user-reported field errors") is NOT
computable — there is no user-facing "report a field error" mechanism
anywhere in the app. `clean_submission_rate`/`fully_autonomous_completion_
rate` below are the closest honest proxies available from existing data
("did this reach a terminal outcome without ever needing a human to
review/intervene"), not a measurement of actual field correctness — see
their own docstrings.
"""

from __future__ import annotations

from statistics import median

from sqlalchemy.orm import Session

from app.models.db_models import (
    Application,
    ApplicationQuestion,
    AutomationRun,
    AutonomousTask,
    HumanInteractionRequest,
)

#: `Application.status` values that mean "this attempt is actually done" —
#: `needs_review`/`manual_required`/`copilot_review` still await a human, so
#: including them would count an in-limbo attempt as if it had an outcome.
_TERMINAL_APPLICATION_STATUSES = ("applied", "failed", "cancelled")
_TERMINAL_TASK_STATUSES = ("COMPLETED", "FAILED", "CANCELLED")
_RESOLVED_REQUEST_STATUSES = ("RESPONDED", "RESOLVED")
#: PENDING/RESUMING requests haven't concluded yet — excluded from the
#: resolution-rate denominator rather than counted as unresolved.
_CONCLUDED_REQUEST_STATUSES = ("RESPONDED", "RESOLVED", "EXPIRED", "CANCELLED", "FAILED")
_NEEDED_HUMAN_QUESTION_SOURCES = ("needs_user_input", "human")
_REVIEW_TRIGGERING_RUN_STATUSES = ("needs_review", "manual_required")


def _median_hours(deltas: list) -> float | None:
    if not deltas:
        return None
    return round(median(d.total_seconds() / 3600 for d in deltas), 2)


def deterministic_metrics(db: Session, user_id: str) -> dict:
    """The per-ATS-adapter pipeline's numbers (`Application` rows)."""
    applications = db.query(Application).filter(Application.user_id == user_id).all()
    total = len(applications)
    if total == 0:
        return {
            "total": 0, "median_hours_to_outcome": None,
            "clean_submission_rate": None, "auto_answered_question_rate": None,
        }

    terminal = [a for a in applications if a.status in _TERMINAL_APPLICATION_STATUSES]
    deltas = [
        (a.applied_date or a.updated_at) - a.created_at
        for a in terminal
        if (a.applied_date or a.updated_at) and a.created_at
    ]

    application_ids = [a.application_id for a in applications]
    escalated_ids = {
        row[0] for row in (
            db.query(AutomationRun.application_id)
            .filter(
                AutomationRun.application_id.in_(application_ids),
                AutomationRun.status.in_(_REVIEW_TRIGGERING_RUN_STATUSES),
            )
            .distinct()
            .all()
        )
    }
    applied = [a for a in applications if a.status == "applied"]
    clean_submission_rate = (
        round(sum(1 for a in applied if a.application_id not in escalated_ids) / len(applied), 3)
        if applied else None
    )

    questions = [
        row[0] for row in
        db.query(ApplicationQuestion.source).filter(ApplicationQuestion.application_id.in_(application_ids)).all()
    ]
    auto_answered_rate = (
        round(sum(1 for source in questions if source not in _NEEDED_HUMAN_QUESTION_SOURCES) / len(questions), 3)
        if questions else None
    )

    return {
        "total": total,
        "median_hours_to_outcome": _median_hours(deltas),
        "clean_submission_rate": clean_submission_rate,
        "auto_answered_question_rate": auto_answered_rate,
    }


def autonomous_metrics(db: Session, user_id: str) -> dict:
    """The observe->decide->act agent's numbers (`AutonomousTask` rows)."""
    tasks = db.query(AutonomousTask).filter(AutonomousTask.user_id == user_id).all()
    total = len(tasks)
    if total == 0:
        return {
            "total": 0, "median_hours_to_outcome": None,
            "hitl_resolution_rate": None, "fully_autonomous_completion_rate": None,
        }

    terminal = [t for t in tasks if t.current_status in _TERMINAL_TASK_STATUSES]
    deltas = [t.updated_at - t.created_at for t in terminal if t.updated_at and t.created_at]

    task_ids = [t.task_id for t in tasks]
    requests = (
        db.query(HumanInteractionRequest.task_id, HumanInteractionRequest.status)
        .filter(HumanInteractionRequest.task_id.in_(task_ids))
        .all()
    )
    concluded = [status for _tid, status in requests if status in _CONCLUDED_REQUEST_STATUSES]
    hitl_resolution_rate = (
        round(sum(1 for status in concluded if status in _RESOLVED_REQUEST_STATUSES) / len(concluded), 3)
        if concluded else None
    )

    completed = [t for t in tasks if t.current_status == "COMPLETED"]
    tasks_with_any_request = {tid for tid, _status in requests}
    fully_autonomous_rate = (
        round(sum(1 for t in completed if t.task_id not in tasks_with_any_request) / len(completed), 3)
        if completed else None
    )

    return {
        "total": total,
        "median_hours_to_outcome": _median_hours(deltas),
        "hitl_resolution_rate": hitl_resolution_rate,
        "fully_autonomous_completion_rate": fully_autonomous_rate,
    }
