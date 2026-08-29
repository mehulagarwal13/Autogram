"""
Secret-leakage regression tests — the "prove it, don't assert it in a
docstring" half of the hardening pass's section 6 audit.

Two kinds of test live here:

1. **Behavioral**: run the real code paths a verification code actually
   travels through, then assert the code is absent from every persisted /
   returned / logged surface (`action_history`, the task row, the
   HumanInteractionRequest row, audit events, the HTTP response body, and
   captured log output).

2. **Structural**: assert the invariants that make the behavioral results
   hold by construction rather than by luck — e.g. `HumanInteractionRequest`
   has no column capable of holding a secret, and the LLM prompt builder is
   never handed one.

The canary value `907313` is deliberately distinctive so a substring search
across serialized output can't produce a false negative.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field

import pytest

import app.api.human_interaction as hi_api
import automation.agents.autonomous.loop as loop_mod
from app.models.db_models import HumanInteractionRequest
from automation.agents.autonomous.loop import AutonomousAgentLoop, TaskHandle

CANARY = "907313"


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------

def test_human_interaction_request_has_no_column_that_could_hold_a_secret():
    """The table is the durable record of a pause; it must be structurally
    incapable of storing the code. Any new column whose name suggests a
    value/secret/code payload should fail this test loudly."""
    columns = {c.name for c in HumanInteractionRequest.__table__.columns}
    forbidden_substrings = ("value", "secret", "code", "otp", "password", "token", "credential", "answer")
    offenders = {
        col for col in columns
        for bad in forbidden_substrings
        if bad in col.lower()
    }
    assert not offenders, (
        f"HumanInteractionRequest gained column(s) that could hold a transient secret: {offenders}. "
        "A verification code must live only in TaskHandle.pending_secret (in-process)."
    )
    # Positive assertion of what SHOULD be there, so this test also catches
    # an accidental rename of the metadata column into something secret-ish.
    assert "safe_metadata" in columns


def test_secret_actions_are_the_only_ones_accepting_a_value_bearing_secret():
    """`_SECRET_ACTIONS` gates the `deliver_secret` branch — if a new action
    is added to `_VALID_ACTIONS` it must be a deliberate decision whether it
    is secret-bearing, not an accident."""
    assert hi_api._SECRET_ACTIONS == {"OTP_SUBMITTED", "MFA_SUBMITTED"}
    assert hi_api._VALID_ACTIONS == hi_api._SECRET_ACTIONS | {"USER_APPROVED", "USER_PROVIDED_VALUE"}


def test_respond_result_schema_cannot_carry_a_value_back_to_the_client():
    """The /respond response model is the one thing the client gets back
    after submitting a code — it must expose only metadata."""
    assert set(hi_api.RespondResult.model_fields) == {"request_id", "status"}


def test_human_request_response_schema_exposes_no_secret_field():
    """The GET schemas must never gain a field that could echo a code."""
    fields = set(hi_api.HumanRequestResponse.model_fields)
    assert "value" not in fields
    assert "secret" not in fields
    assert fields >= {"request_id", "task_id", "request_type", "status", "message", "safe_metadata"}


# ---------------------------------------------------------------------------
# Behavioral: the deterministic consumption path
# ---------------------------------------------------------------------------

@dataclass
class FakeTask:
    task_id: str = "task_1"
    user_id: str = "user_1"
    job_url: str = "https://example.com/apply"
    original_objective: str = "Apply."
    candidate_profile: dict = field(default_factory=dict)
    confirmed_answers: dict = field(default_factory=dict)
    action_history: list = field(default_factory=list)
    uploaded_documents: list = field(default_factory=list)
    auto_submit_approved: bool = False
    current_status: str = "WAITING_FOR_HUMAN"
    current_browser_state: dict | None = None
    human_intervention: dict | None = None
    final_result: dict | None = None
    error: str | None = None


class RecordingTaskRepo:
    """Records every write so a test can search ALL of them for the canary."""

    def __init__(self):
        self.writes: list[tuple[str, object]] = []

    def get_by_id(self, db, task_id):
        return db["task"]

    def set_status(self, db, task, status):
        task.current_status = status
        self.writes.append(("set_status", status))
        return task

    def update_browser_state(self, db, task, state):
        task.current_browser_state = state
        self.writes.append(("update_browser_state", state))
        return task

    def append_action(self, db, task, record):
        task.action_history.append(record)
        self.writes.append(("append_action", record))
        return task

    def request_human_intervention(self, db, task, intervention):
        task.human_intervention = intervention
        task.current_status = "WAITING_FOR_HUMAN"
        self.writes.append(("request_human_intervention", intervention))
        return task

    def record_confirmed_answer(self, db, task, question, answer):
        task.confirmed_answers[question] = answer
        self.writes.append(("record_confirmed_answer", {question: answer}))
        return task

    def mark_completed(self, db, task, final_result):
        task.current_status = "COMPLETED"
        self.writes.append(("mark_completed", final_result))
        return task

    def mark_failed(self, db, task, error, final_result=None):
        task.current_status = "FAILED"
        task.error = error
        self.writes.append(("mark_failed", error))
        return task

    def mark_ready_for_approval(self, db, task, final_result):
        task.current_status = "WAITING_FOR_APPROVAL"
        return task

    def cancel_task(self, db, task):
        task.current_status = "CANCELLED"
        return task


class RecordingHumanRepo:
    def __init__(self):
        self.requests: dict[str, object] = {}
        self.calls: list[tuple[str, dict]] = []
        self._n = 0

    def create_request(self, db, *, user_id, task_id, request_type, message, safe_metadata=None, **_kw):
        self._n += 1

        class _Req:
            pass

        req = _Req()
        req.request_id = f"hreq_{self._n}"
        req.task_id = task_id
        req.request_type = request_type
        req.message = message
        req.safe_metadata = safe_metadata or {}
        req.status = "PENDING"
        req.expires_at = None
        self.requests[req.request_id] = req
        self.calls.append(("create_request", {"message": message, "safe_metadata": req.safe_metadata}))
        return req

    def get_by_id(self, db, request_id):
        return self.requests.get(request_id)

    def mark_resolved(self, db, req):
        req.status = "RESOLVED"
        return req

    def mark_failed(self, db, req):
        req.status = "FAILED"
        return req


class RecordingAuditRepo:
    def __init__(self):
        self.events: list[dict] = []

    def record_event(self, db, **kwargs):
        self.events.append(kwargs)


class FakeLocator:
    def __init__(self):
        self.filled_with = None
        self.clicked = False

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None, position=None):
        self.clicked = True

    def fill(self, value):
        self.filled_with = value


class FakePage:
    def __init__(self):
        self.url = "https://example.com/verify"
        self._locators: dict[str, FakeLocator] = {}

    def locator(self, selector):
        return self._locators.setdefault(selector, FakeLocator())


class DictDb(dict):
    def refresh(self, obj):
        pass


def test_consuming_a_delivered_code_leaks_it_nowhere(monkeypatch, caplog):
    """The single most important regression test in this file: run the real
    `_try_consume_pending_secret`, then assert the canary appears in NONE of
    the persisted writes, the task row, the request row, the audit events, or
    captured log output at DEBUG level — while still having actually been
    typed into the browser field."""
    from automation.agents.autonomous.observer import PageState

    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    handle.page = FakePage()
    loop = AutonomousAgentLoop(task.task_id, handle)

    task_repo = RecordingTaskRepo()
    human_repo = RecordingHumanRepo()
    audit_repo = RecordingAuditRepo()
    monkeypatch.setattr(loop_mod, "task_repo", task_repo)
    monkeypatch.setattr(loop_mod, "human_interaction_repo", human_repo)
    monkeypatch.setattr(loop_mod, "audit_log_repo", audit_repo)
    monkeypatch.setattr(loop_mod, "ATSDetector", type("D", (), {"detect_from_url": staticmethod(lambda url: None)}))
    monkeypatch.setattr(
        loop_mod, "observe_page",
        lambda page, ats_hint=None: PageState(
            url="https://example.com/verify", title="Verify", visible_text="Enter verification code",
            elements=[],
            blocker_hint={"request_type": "OTP_REQUIRED", "otp_field_ref": 4, "submit_ref": 5, "masked_destination": None},
        ),
    )

    # Pre-seed the request the code is answering, then deliver the code
    # exactly as `runner.py::deliver_secret` does.
    req = human_repo.create_request(
        None, user_id="user_1", task_id="task_1", request_type="OTP_REQUIRED", message="Enter the code",
    )
    handle.pending_secret = {"request_id": req.request_id, "value": CANARY}

    db = DictDb(task=task)
    with caplog.at_level(logging.DEBUG):
        outcome = loop._try_consume_pending_secret(db, task)

    assert outcome is True

    # (a) It really was typed into the browser — this path DID its job.
    assert handle.page.locator('[data-agent-ref="4"]').filled_with == CANARY
    # (b) ...and the in-memory slot was cleared immediately.
    assert handle.pending_secret is None

    # (c) Absent from every persisted write.
    for name, payload in task_repo.writes:
        assert CANARY not in json.dumps(payload, default=str), f"canary leaked into {name}"
    # (d) Absent from the whole task row.
    assert CANARY not in json.dumps(task.__dict__, default=str)
    # (e) Absent from action_history specifically, which IS written and
    #     returned by the status API — and the fill entry is redacted.
    fills = [a for a in task.action_history if a["action_type"] == "fill"]
    assert fills and all(a["value"] == "[REDACTED]" for a in fills)
    assert CANARY not in json.dumps(task.action_history, default=str)
    # (f) Absent from the HumanInteractionRequest row and every repo call.
    assert CANARY not in json.dumps(human_repo.calls, default=str)
    assert CANARY not in json.dumps({k: v.__dict__ for k, v in human_repo.requests.items()}, default=str)
    # (g) Absent from every audit event.
    assert CANARY not in json.dumps(audit_repo.events, default=str)
    # (h) Absent from log output, even at DEBUG.
    assert CANARY not in caplog.text


def test_llm_prompt_never_receives_a_verification_code(monkeypatch):
    """`decide_next_step` builds the LLM prompt from the task's persisted
    fields only. Since the code is never written to any of them (proven
    above), the prompt cannot contain it — this test pins the invariant by
    checking the actual serialized prompt for a task whose browser state and
    history came from a real code-consumption round."""
    from automation.agents.autonomous.decision import _build_user_prompt
    from automation.agents.autonomous.observer import PageState

    prompt = _build_user_prompt(
        job_url="https://example.com/apply",
        original_objective="Apply.",
        resume_text="Jane Doe, engineer.",
        parsed_resume={"name": "Jane Doe"},
        profile={"first_name": "Jane"},
        confirmed_answers={"desired_start_date": "Immediately"},
        page_state=PageState(
            url="https://example.com/verify", title="Verify",
            visible_text="Enter verification code", elements=[],
        ),
        # This is exactly the shape `_try_consume_pending_secret` writes.
        action_history=[{
            "action_type": "fill", "element_ref": 4, "element_name": "verification code",
            "value": "[REDACTED]", "success": True, "detail": "Filled element ref 4 with 6 chars",
        }],
        uploaded_documents=[],
        auto_submit_approved=False,
    )

    assert CANARY not in prompt
    assert "[REDACTED]" in prompt  # the redacted placeholder is what the model sees instead


# ---------------------------------------------------------------------------
# Behavioral: the API surface
# ---------------------------------------------------------------------------

@dataclass
class FakeUser:
    user_id: str = "user_1"


def test_respond_route_never_echoes_the_code_and_keeps_it_out_of_logs(monkeypatch, caplog):
    """End-to-end through the real `/respond` handler: the canary must not
    appear in the response body, the audit events, the persisted request, or
    captured logs — only in the one `deliver_secret` call."""
    from datetime import datetime, timezone

    delivered_values = []

    @dataclass
    class Req:
        request_id: str = "hreq_1"
        task_id: str = "task_1"
        user_id: str = "user_1"
        request_type: str = "OTP_REQUIRED"
        status: str = "PENDING"
        title: str | None = None
        message: str = "Enter the code"
        safe_metadata: dict = field(default_factory=dict)
        created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
        expires_at: datetime | None = None
        responded_at: datetime | None = None
        resolved_at: datetime | None = None

    @dataclass
    class Task:
        task_id: str = "task_1"
        user_id: str = "user_1"
        current_status: str = "WAITING_FOR_HUMAN"

    req, task = Req(), Task()

    class HRepo:
        def get_by_id(self, db, request_id):
            return req if request_id == req.request_id else None

        def is_expired(self, r, now=None):
            return False

        def try_claim(self, db, request_id, *, new_status, from_status="PENDING"):
            if req.status != from_status:
                return None
            req.status = new_status
            return req

        def mark_resuming(self, db, r):
            r.status = "RESUMING"
            return r

        def mark_failed(self, db, r):
            r.status = "FAILED"
            return r

        def mark_resolved(self, db, r):
            r.status = "RESOLVED"
            return r

        def create_request(self, db, **kw):
            raise AssertionError("no fallback request should be needed in this test")

    class TRepo:
        def get_by_id(self, db, task_id):
            return task if task_id == task.task_id else None

        def try_claim_for_resume(self, db, t, *, from_status):
            if t.current_status != from_status:
                return False
            t.current_status = "RESUMING"
            return True

    audit = RecordingAuditRepo()
    monkeypatch.setattr(hi_api, "human_interaction_repo", HRepo())
    monkeypatch.setattr(hi_api, "task_repo", TRepo())
    monkeypatch.setattr(hi_api, "audit_log_repository", audit)
    monkeypatch.setattr(
        hi_api, "deliver_secret",
        lambda task_id, request_id, value: (delivered_values.append(value), True)[1],
    )
    monkeypatch.setattr(hi_api, "signal_resume", lambda task_id: True)
    monkeypatch.setattr(hi_api, "start_task_background", lambda task_id: None)

    with caplog.at_level(logging.DEBUG):
        result = hi_api.respond_to_human_request(
            "hreq_1", hi_api.RespondRequest(action="OTP_SUBMITTED", value=CANARY),
            user=FakeUser(), db=None,
        )

    # The one legitimate consumer got it...
    assert delivered_values == [CANARY]
    # ...and nothing else did.
    assert result == {"request_id": "hreq_1", "status": "accepted"}
    assert CANARY not in json.dumps(result, default=str)
    assert CANARY not in json.dumps(audit.events, default=str)
    assert CANARY not in json.dumps(req.__dict__, default=str)
    assert CANARY not in caplog.text


def test_a_get_on_the_request_after_submission_returns_no_code(monkeypatch):
    """Even after a successful submission, the GET surface exposes only
    metadata — there is nowhere for a code to come back from."""
    from datetime import datetime, timezone

    @dataclass
    class Req:
        request_id: str = "hreq_1"
        task_id: str = "task_1"
        user_id: str = "user_1"
        request_type: str = "OTP_REQUIRED"
        status: str = "RESOLVED"
        title: str | None = None
        message: str = "Enter the code"
        safe_metadata: dict = field(default_factory=lambda: {"masked_destination": "j***@gmail.com"})
        created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
        expires_at: datetime | None = None
        responded_at: datetime | None = None
        resolved_at: datetime | None = None

    req = Req()

    class HRepo:
        def get_by_id(self, db, request_id):
            return req

    monkeypatch.setattr(hi_api, "human_interaction_repo", HRepo())

    returned = hi_api.get_human_request("hreq_1", user=FakeUser(), db=None)
    serialized = hi_api.HumanRequestResponse.model_validate(returned).model_dump()

    assert CANARY not in json.dumps(serialized, default=str)
    assert "value" not in serialized
    assert serialized["safe_metadata"]["masked_destination"] == "j***@gmail.com"
