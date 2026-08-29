"""
Regression tests for `app/api/autonomous_agent.py`'s legacy
`/resume`/`/answer`/`/approve`/`/cancel` routes after the hardening pass —
called directly as plain functions (same convention as
`test_human_interaction_api.py`), with `task_repo`/`human_interaction_repo`/
the runner functions faked so no real DB/browser is needed.

Covers:
- a pending OTP/MFA request cannot be bypassed via the generic `/resume`
- a pending OTP/MFA request cannot be answered (and so cannot leak into
  `confirmed_answers`) via the generic `/answer`
- a normal (non-secret) intervention still resumes correctly via both routes
- `/approve` still gates on WAITING_FOR_APPROVAL independently of any
  human-interaction-request machinery
- `/cancel` records an audit event and is idempotent on an already-terminal task
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import HTTPException

import app.api.autonomous_agent as agent_api


@dataclass
class FakeUser:
    user_id: str = "user_1"


@dataclass
class FakeTask:
    task_id: str = "task_1"
    user_id: str = "user_1"
    current_status: str = "WAITING_FOR_HUMAN"
    human_intervention: dict | None = None
    confirmed_answers: dict = field(default_factory=dict)
    auto_submit_approved: bool = False


class FakeHumanInteractionRepo:
    def __init__(self):
        self.resolved = []
        self.cancelled = []

    def get_active_for_task(self, db, task_id):
        return None  # not exercised by these tests — _resolve_active_request just no-ops

    def mark_resolved(self, db, req):
        self.resolved.append(req)

    def mark_cancelled(self, db, req):
        self.cancelled.append(req)


class FakeTaskRepo:
    def __init__(self, task):
        self.task = task

    def get_by_id(self, db, task_id):
        return self.task if task_id == self.task.task_id else None

    def try_claim_for_resume(self, db, task, *, from_status):
        if task.current_status != from_status:
            return False
        task.current_status = "RESUMING"
        task.human_intervention = None
        return True

    def clear_intervention_for_resume(self, db, task):
        task.current_status = "RESUMING"
        task.human_intervention = None

    def record_confirmed_answer(self, db, task, question, answer):
        task.confirmed_answers[question] = answer
        task.current_status = "RESUMING"

    def approve_submission(self, db, task):
        task.auto_submit_approved = True
        task.current_status = "RESUMING"

    def cancel_task(self, db, task):
        task.current_status = "CANCELLED"


class FakeAuditLog:
    def __init__(self):
        self.events = []

    def record_event(self, db, **kwargs):
        self.events.append(kwargs)


def _install(monkeypatch, task, *, resumed=True):
    task_repo = FakeTaskRepo(task)
    human_repo = FakeHumanInteractionRepo()
    audit = FakeAuditLog()
    monkeypatch.setattr(agent_api, "task_repo", task_repo)
    monkeypatch.setattr(agent_api, "human_interaction_repo", human_repo)
    monkeypatch.setattr(agent_api, "audit_log_repository", audit)
    monkeypatch.setattr(agent_api, "signal_resume", lambda task_id: resumed)
    monkeypatch.setattr(agent_api, "start_task_background", lambda task_id: None)
    monkeypatch.setattr(agent_api, "request_cancel", lambda task_id: True)
    return task_repo, human_repo, audit


# ---------------------------------------------------------------------------
# A pending OTP/MFA request cannot be bypassed via the generic routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("request_type", ["OTP_REQUIRED", "MFA_REQUIRED"])
def test_legacy_resume_cannot_bypass_a_pending_verification_code(monkeypatch, request_type):
    task = FakeTask(human_intervention={"type": request_type, "request_type": request_type, "message": "code needed"})
    _install(monkeypatch, task)

    with pytest.raises(HTTPException) as exc:
        agent_api.resume_task("task_1", user=FakeUser(), db=None)

    assert exc.value.status_code == 409
    assert task.current_status == "WAITING_FOR_HUMAN"  # never resumed


@pytest.mark.parametrize("request_type", ["OTP_REQUIRED", "MFA_REQUIRED"])
def test_legacy_answer_cannot_smuggle_a_verification_code_into_confirmed_answers(monkeypatch, request_type):
    """The critical leak this closes: without the guard, POSTing
    {"question": "verification code", "answer": "123456"} to the generic
    /answer route while an OTP/MFA request is active would have permanently
    written the code into `confirmed_answers` — returned by every future
    GET /agent/tasks/{id}."""
    task = FakeTask(human_intervention={"type": request_type, "request_type": request_type, "message": "code needed"})
    _install(monkeypatch, task)

    with pytest.raises(HTTPException) as exc:
        agent_api.answer_question(
            "task_1", agent_api.AnswerRequest(question="verification code", answer="123456"),
            user=FakeUser(), db=None,
        )

    assert exc.value.status_code == 409
    assert "123456" not in task.confirmed_answers.values()
    assert task.confirmed_answers == {}
    assert task.current_status == "WAITING_FOR_HUMAN"


# ---------------------------------------------------------------------------
# Normal (non-secret) interventions are unaffected
# ---------------------------------------------------------------------------

def test_resume_still_works_for_a_non_secret_intervention(monkeypatch):
    task = FakeTask(human_intervention={"type": "LOGIN_REQUIRED", "request_type": "LOGIN_REQUIRED", "message": "sign in"})
    _install(monkeypatch, task)

    result = agent_api.resume_task("task_1", user=FakeUser(), db=None)

    assert result.current_status == "RESUMING"


def test_answer_still_works_for_an_ordinary_question(monkeypatch):
    task = FakeTask(human_intervention={"type": "ANSWER_REQUIRED", "request_type": "ANSWER_REQUIRED", "message": "?"})
    _install(monkeypatch, task)

    result = agent_api.answer_question(
        "task_1", agent_api.AnswerRequest(question="desired_start_date", answer="Immediately"),
        user=FakeUser(), db=None,
    )

    assert result.confirmed_answers["desired_start_date"] == "Immediately"
    assert result.current_status == "RESUMING"


def test_duplicate_resume_is_rejected_by_the_atomic_claim(monkeypatch):
    """Two calls to /resume for the same pause — the second must fail
    cleanly rather than resuming the task twice."""
    task = FakeTask(human_intervention={"type": "LOGIN_REQUIRED", "request_type": "LOGIN_REQUIRED", "message": "sign in"})
    _install(monkeypatch, task)

    agent_api.resume_task("task_1", user=FakeUser(), db=None)
    assert task.current_status == "RESUMING"

    with pytest.raises(HTTPException) as exc:
        agent_api.resume_task("task_1", user=FakeUser(), db=None)
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# /approve and /cancel
# ---------------------------------------------------------------------------

def test_approve_gates_on_waiting_for_approval_independently(monkeypatch):
    task = FakeTask(current_status="RUNNING")
    _install(monkeypatch, task)
    with pytest.raises(HTTPException) as exc:
        agent_api.approve_submission("task_1", user=FakeUser(), db=None)
    assert exc.value.status_code == 409
    assert task.auto_submit_approved is False


def test_cancel_records_an_audit_event(monkeypatch):
    task = FakeTask(current_status="RUNNING")
    task_repo, human_repo, audit = _install(monkeypatch, task)

    agent_api.cancel_task("task_1", user=FakeUser(), db=None)

    assert any(e["event_type"] == "automation_cancelled" for e in audit.events)


def test_cancel_persists_cancelled_even_when_a_live_loop_handle_exists(monkeypatch):
    """REGRESSION (found by the real-browser E2E run): this route used to
    persist CANCELLED only when `request_cancel` reported no live handle,
    trusting a live loop to do it. A loop blocked in `_wait_for_resume`
    (i.e. every paused task — the most likely moment a user cancels) never
    reached that code, so the task stayed WAITING_FOR_HUMAN forever."""
    task = FakeTask(current_status="WAITING_FOR_HUMAN",
                    human_intervention={"type": "LOGIN_REQUIRED", "request_type": "LOGIN_REQUIRED", "message": "x"})
    _install(monkeypatch, task)
    # `request_cancel` returning True == "a live loop handle exists".
    monkeypatch.setattr(agent_api, "request_cancel", lambda task_id: True)

    result = agent_api.cancel_task("task_1", user=FakeUser(), db=None)

    assert result.current_status == "CANCELLED", "cancel did not persist with a live handle present"


def test_cancel_is_idempotent_on_an_already_terminal_task(monkeypatch):
    task = FakeTask(current_status="COMPLETED")
    _install(monkeypatch, task)

    result = agent_api.cancel_task("task_1", user=FakeUser(), db=None)

    assert result.current_status == "COMPLETED"  # untouched, no error
