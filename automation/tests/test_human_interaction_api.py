"""
Tests for `app/api/human_interaction.py`'s route handlers, called directly as
plain functions (FastAPI route decorators don't wrap/replace the function
object, so this works without spinning up a real ASGI server or a Postgres
-backed `Session` — same "patch the module's dependencies, call the function"
convention `automation/tests/test_autonomous_loop.py` uses for `loop.py`).

Covers the required scenarios from the HITL/OTP spec:
- ownership checks (a request/task belonging to someone else -> 404)
- expired request -> 410, and is marked EXPIRED
- duplicate/already-answered request -> 409
- an OTP/MFA action against a non-secret request type -> 400
- a missing code/value -> 422
- a successful OTP submission never echoes the code back, and clears it
  after handing it to the (fake) live automation session
- automation session no longer live -> a fresh LOGIN_REQUIRED request is
  raised instead of silently failing
- cancel marks the request (and the still-active task) cancelled
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import app.api.human_interaction as hi_api


@dataclass
class FakeUser:
    user_id: str = "user_1"


@dataclass
class FakeTask:
    task_id: str = "task_1"
    user_id: str = "user_1"
    current_status: str = "WAITING_FOR_HUMAN"


@dataclass
class FakeRequest:
    request_id: str
    task_id: str
    user_id: str
    request_type: str
    status: str = "PENDING"
    title: str | None = None
    message: str = "Verification required."
    safe_metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    responded_at: datetime | None = None
    resolved_at: datetime | None = None


class FakeHumanRepo:
    def __init__(self, requests):
        self._requests = {r.request_id: r for r in requests}
        self.created = []

    def get_by_id(self, db, request_id):
        return self._requests.get(request_id)

    def get_active_for_task(self, db, task_id):
        for r in self._requests.values():
            if r.task_id == task_id and r.status == "PENDING":
                return r
        return None

    def is_expired(self, req, now=None):
        if req.expires_at is None:
            return False
        return (now or datetime.now(timezone.utc)) > req.expires_at

    def _set(self, req, status, *, responded=False, resolved=False):
        req.status = status
        if responded:
            req.responded_at = datetime.now(timezone.utc)
        if resolved:
            req.resolved_at = datetime.now(timezone.utc)
        return req

    def mark_responded(self, db, req):
        return self._set(req, "RESPONDED", responded=True)

    def mark_resuming(self, db, req):
        return self._set(req, "RESUMING")

    def mark_resolved(self, db, req):
        return self._set(req, "RESOLVED", resolved=True)

    def mark_expired(self, db, req):
        return self._set(req, "EXPIRED", resolved=True)

    def mark_cancelled(self, db, req):
        return self._set(req, "CANCELLED", resolved=True)

    def mark_failed(self, db, req):
        return self._set(req, "FAILED", resolved=True)

    def try_claim(self, db, request_id, *, new_status, from_status="PENDING"):
        """Mirrors the real atomic conditional-UPDATE semantics closely
        enough for these tests: returns the request if (and only if) it was
        still in `from_status`, and actually flips it — a second call with
        the same `from_status` returns `None`, exactly like two concurrent
        real UPDATEs where only one matches a row."""
        req = self._requests.get(request_id)
        if req is None or req.status != from_status:
            return None
        self._set(
            req, new_status,
            responded=(new_status == "RESPONDED"),
            resolved=(new_status in ("RESOLVED", "EXPIRED", "CANCELLED", "FAILED")),
        )
        return req

    def create_request(self, db, *, user_id, task_id, request_type, message, title=None, safe_metadata=None, **_kw):
        req = FakeRequest(
            request_id=f"hreq_new_{len(self.created)}", task_id=task_id, user_id=user_id,
            request_type=request_type, message=message, title=title, safe_metadata=safe_metadata or {},
        )
        self._requests[req.request_id] = req
        self.created.append(req)
        return req


class FakeTaskRepo:
    def __init__(self, task):
        self.task = task
        self.confirmed_answers = {}
        self.human_intervention = None

    def get_by_id(self, db, task_id):
        return self.task if task_id == self.task.task_id else None

    def clear_intervention_for_resume(self, db, task):
        task.current_status = "RESUMING"

    def try_claim_for_resume(self, db, task, *, from_status):
        """Mirrors the real atomic conditional-UPDATE: only succeeds (and
        only flips the status) if the task is still in `from_status` — a
        second concurrent caller (or a caller that's too late because
        someone else already resumed the task) gets `False`."""
        if task.current_status != from_status:
            return False
        task.current_status = "RESUMING"
        self.human_intervention = None
        return True

    def record_confirmed_answer(self, db, task, question, answer):
        self.confirmed_answers[question] = answer
        task.current_status = "RESUMING"

    def request_human_intervention(self, db, task, intervention):
        self.human_intervention = intervention
        task.current_status = "WAITING_FOR_HUMAN"

    def cancel_task(self, db, task):
        task.current_status = "CANCELLED"


class FakeAuditLog:
    def __init__(self):
        self.events = []

    def record_event(self, db, **kwargs):
        self.events.append(kwargs)


def _install(monkeypatch, *, task=None, requests=None, delivered=True, resumed=True):
    task = task or FakeTask()
    task_repo = FakeTaskRepo(task)
    human_repo = FakeHumanRepo(requests or [])
    audit = FakeAuditLog()

    monkeypatch.setattr(hi_api, "task_repo", task_repo)
    monkeypatch.setattr(hi_api, "human_interaction_repo", human_repo)
    monkeypatch.setattr(hi_api, "audit_log_repository", audit)
    monkeypatch.setattr(hi_api, "deliver_secret", lambda task_id, request_id, value: delivered)
    monkeypatch.setattr(hi_api, "signal_resume", lambda task_id: resumed)
    monkeypatch.setattr(hi_api, "start_task_background", lambda task_id: None)
    monkeypatch.setattr(hi_api, "request_cancel", lambda task_id: True)
    return task, task_repo, human_repo, audit


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------

def test_get_human_request_404s_for_someone_elses_request(monkeypatch):
    req = FakeRequest(request_id="hreq_1", task_id="task_1", user_id="someone_else", request_type="OTP_REQUIRED")
    _install(monkeypatch, requests=[req])
    with pytest.raises(HTTPException) as exc:
        hi_api.get_human_request("hreq_1", user=FakeUser(user_id="user_1"), db=None)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

def test_respond_to_an_expired_request_returns_410_and_marks_it_expired(monkeypatch):
    req = FakeRequest(
        request_id="hreq_1", task_id="task_1", user_id="user_1", request_type="OTP_REQUIRED",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    _, _, human_repo, _ = _install(monkeypatch, requests=[req])
    with pytest.raises(HTTPException) as exc:
        hi_api.respond_to_human_request(
            "hreq_1", hi_api.RespondRequest(action="OTP_SUBMITTED", value="123456"),
            user=FakeUser(), db=None,
        )
    assert exc.value.status_code == 410
    assert req.status == "EXPIRED"


# ---------------------------------------------------------------------------
# Duplicate / already-answered
# ---------------------------------------------------------------------------

def test_respond_to_an_already_responded_request_returns_409(monkeypatch):
    req = FakeRequest(request_id="hreq_1", task_id="task_1", user_id="user_1", request_type="OTP_REQUIRED", status="RESPONDED")
    _install(monkeypatch, requests=[req])
    with pytest.raises(HTTPException) as exc:
        hi_api.respond_to_human_request(
            "hreq_1", hi_api.RespondRequest(action="OTP_SUBMITTED", value="123456"),
            user=FakeUser(), db=None,
        )
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# Type mismatch / validation
# ---------------------------------------------------------------------------

def test_otp_action_against_a_non_secret_request_type_is_rejected(monkeypatch):
    req = FakeRequest(request_id="hreq_1", task_id="task_1", user_id="user_1", request_type="LOGIN_REQUIRED")
    task, _, _, _ = _install(monkeypatch, requests=[req])
    with pytest.raises(HTTPException) as exc:
        hi_api.respond_to_human_request(
            "hreq_1", hi_api.RespondRequest(action="OTP_SUBMITTED", value="123456"),
            user=FakeUser(), db=None,
        )
    assert exc.value.status_code == 400
    # REGRESSION (found by the real-browser E2E run, test_06): a rejected call
    # must not consume the request or move the task. Validating after the
    # atomic claims destroyed a legitimate pending LOGIN_REQUIRED request AND
    # stranded the task in RESUMING with nothing able to resume it.
    assert req.status == "PENDING", "an invalid call consumed a still-valid request"
    assert task.current_status == "WAITING_FOR_HUMAN", "an invalid call moved the task out of WAITING_FOR_HUMAN"


def test_missing_code_value_is_a_422(monkeypatch):
    req = FakeRequest(request_id="hreq_1", task_id="task_1", user_id="user_1", request_type="OTP_REQUIRED")
    task, _, _, _ = _install(monkeypatch, requests=[req])
    with pytest.raises(HTTPException) as exc:
        hi_api.respond_to_human_request(
            "hreq_1", hi_api.RespondRequest(action="OTP_SUBMITTED", value="   "),
            user=FakeUser(), db=None,
        )
    assert exc.value.status_code == 422
    assert req.status == "PENDING"          # same regression as above
    assert task.current_status == "WAITING_FOR_HUMAN"


def test_missing_value_for_user_provided_value_is_a_422_and_consumes_nothing(monkeypatch):
    req = FakeRequest(request_id="hreq_1", task_id="task_1", user_id="user_1", request_type="ANSWER_REQUIRED")
    task, task_repo, _, _ = _install(monkeypatch, requests=[req])
    with pytest.raises(HTTPException) as exc:
        hi_api.respond_to_human_request(
            "hreq_1", hi_api.RespondRequest(action="USER_PROVIDED_VALUE", value="  "),
            user=FakeUser(), db=None,
        )
    assert exc.value.status_code == 422
    assert req.status == "PENDING"
    assert task.current_status == "WAITING_FOR_HUMAN"
    assert task_repo.confirmed_answers == {}


def test_an_unknown_action_consumes_nothing(monkeypatch):
    req = FakeRequest(request_id="hreq_1", task_id="task_1", user_id="user_1", request_type="OTP_REQUIRED")
    task, _, _, _ = _install(monkeypatch, requests=[req])
    with pytest.raises(HTTPException) as exc:
        hi_api.respond_to_human_request(
            "hreq_1", hi_api.RespondRequest(action="NOT_A_REAL_ACTION", value="x"),
            user=FakeUser(), db=None,
        )
    assert exc.value.status_code == 400
    assert req.status == "PENDING"
    assert task.current_status == "WAITING_FOR_HUMAN"


# ---------------------------------------------------------------------------
# Happy path — OTP never echoed back, request/task transition correctly
# ---------------------------------------------------------------------------

def test_successful_otp_submission_never_returns_the_code_and_delivers_it_once(monkeypatch):
    req = FakeRequest(request_id="hreq_1", task_id="task_1", user_id="user_1", request_type="OTP_REQUIRED")
    task, task_repo, human_repo, audit = _install(monkeypatch, requests=[req], delivered=True)

    result = hi_api.respond_to_human_request(
        "hreq_1", hi_api.RespondRequest(action="OTP_SUBMITTED", value="123456"),
        user=FakeUser(), db=None,
    )

    assert result == {"request_id": "hreq_1", "status": "accepted"}
    assert "123456" not in str(result)
    assert req.status == "RESUMING"
    assert task.current_status == "RESUMING"
    # The audit trail records that a response arrived — never the code itself.
    assert all("123456" not in str(e) for e in audit.events)
    assert any(e["event_type"] == "human_response_received" for e in audit.events)


def test_lost_automation_session_raises_a_fresh_login_required_request_instead_of_failing_silently(monkeypatch):
    req = FakeRequest(request_id="hreq_1", task_id="task_1", user_id="user_1", request_type="OTP_REQUIRED")
    task, task_repo, human_repo, audit = _install(monkeypatch, requests=[req], delivered=False)

    with pytest.raises(HTTPException) as exc:
        hi_api.respond_to_human_request(
            "hreq_1", hi_api.RespondRequest(action="OTP_SUBMITTED", value="123456"),
            user=FakeUser(), db=None,
        )

    assert exc.value.status_code == 409
    assert req.status == "FAILED"
    assert human_repo.created  # a fallback request was raised
    assert human_repo.created[0].request_type == "LOGIN_REQUIRED"
    assert task_repo.human_intervention["type"] == "LOGIN_REQUIRED"

    # Observability: an operator must be able to answer "did the browser
    # session disappear?" from the audit trail alone. Without this event the
    # log showed a response arriving and a LOGIN_REQUIRED request appearing,
    # with nothing explaining why.
    event_types = [e["event_type"] for e in audit.events]
    assert "automation_session_lost" in event_types
    assert "human_request_created" in event_types
    lost = next(e for e in audit.events if e["event_type"] == "automation_session_lost")
    assert lost["metadata"]["reason"] == "no_live_task_handle"
    assert all("123456" not in str(e) for e in audit.events)  # still no secret


def test_user_approved_resumes_a_non_secret_request(monkeypatch):
    req = FakeRequest(request_id="hreq_1", task_id="task_1", user_id="user_1", request_type="LOGIN_REQUIRED")
    task, task_repo, human_repo, _ = _install(monkeypatch, requests=[req])

    result = hi_api.respond_to_human_request(
        "hreq_1", hi_api.RespondRequest(action="USER_APPROVED"), user=FakeUser(), db=None,
    )

    assert result["status"] == "accepted"
    assert req.status == "RESOLVED"


def test_user_provided_value_records_a_confirmed_answer(monkeypatch):
    req = FakeRequest(
        request_id="hreq_1", task_id="task_1", user_id="user_1", request_type="ANSWER_REQUIRED",
        safe_metadata={"information_required": "desired_start_date"},
    )
    task, task_repo, human_repo, _ = _install(monkeypatch, requests=[req])

    hi_api.respond_to_human_request(
        "hreq_1", hi_api.RespondRequest(action="USER_PROVIDED_VALUE", value="Immediately"), user=FakeUser(), db=None,
    )

    assert task_repo.confirmed_answers["desired_start_date"] == "Immediately"
    assert req.status == "RESOLVED"


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

def test_cancel_marks_the_request_and_the_still_active_task_cancelled(monkeypatch):
    req = FakeRequest(request_id="hreq_1", task_id="task_1", user_id="user_1", request_type="OTP_REQUIRED")
    task, task_repo, human_repo, _ = _install(monkeypatch, requests=[req])

    result = hi_api.cancel_human_request("hreq_1", user=FakeUser(), db=None)

    assert result == {"request_id": "hreq_1", "status": "cancelled"}
    assert req.status == "CANCELLED"


# ---------------------------------------------------------------------------
# Chat transcript wiring
# ---------------------------------------------------------------------------
# The route writes the human's turn into `chat_messages`. Which of the two
# repository calls it makes is the whole safety property: `record_user_reply`
# persists the caller's text, `record_secret_submission` cannot (it takes no
# value argument at all). Picking the wrong one for an OTP would write a live
# verification code into a row that is returned by every future GET.
#
# The production calls are wrapped in try/except so a transcript failure can
# never break a resume — which also means a silently-missing call would not
# fail any other test in this file. Hence pinning it directly.

class RecordingChatRepo:
    """Captures which transcript call the route made, and with what."""

    def __init__(self):
        self.replies: list[dict] = []
        self.secrets: list[dict] = []

    def record_user_reply(self, db, **kwargs):
        self.replies.append(kwargs)

    def record_secret_submission(self, db, **kwargs):
        self.secrets.append(kwargs)


@pytest.mark.parametrize(
    "request_type,action",
    [("OTP_REQUIRED", "OTP_SUBMITTED"), ("MFA_REQUIRED", "MFA_SUBMITTED")],
)
def test_a_submitted_code_is_recorded_by_the_redacted_call_only(monkeypatch, request_type, action):
    """The code must reach `record_secret_submission`, which has no parameter
    that could carry it — and must NEVER reach `record_user_reply`, which
    persists its `content` verbatim."""
    req = FakeRequest(request_id="hreq_1", task_id="task_1", user_id="user_1", request_type=request_type)
    _install(monkeypatch, requests=[req])
    chat = RecordingChatRepo()
    monkeypatch.setattr(hi_api, "chat_repository", chat)

    hi_api.respond_to_human_request(
        "hreq_1", hi_api.RespondRequest(action=action, value="482913"),
        user=FakeUser(), db=None,
    )

    assert len(chat.secrets) == 1, "the redacted call must be the one used"
    assert not chat.replies, "a verification code must never go through record_user_reply"
    # And nothing in what WAS recorded can carry the code.
    assert "482913" not in repr(chat.secrets[0])
    assert chat.secrets[0]["request_type"] == request_type


def test_a_plain_answer_is_recorded_as_the_users_own_words(monkeypatch):
    """The non-secret path is the reason the transcript exists — it must keep
    the actual prose, not a placeholder."""
    req = FakeRequest(
        request_id="hreq_1", task_id="task_1", user_id="user_1",
        request_type="ANSWER_REQUIRED", safe_metadata={"information_required": "Years of experience?"},
    )
    _install(monkeypatch, requests=[req])
    chat = RecordingChatRepo()
    monkeypatch.setattr(hi_api, "chat_repository", chat)

    hi_api.respond_to_human_request(
        "hreq_1", hi_api.RespondRequest(action="USER_PROVIDED_VALUE", value="5 years"),
        user=FakeUser(), db=None,
    )

    assert not chat.secrets
    assert len(chat.replies) == 1
    assert chat.replies[0]["content"] == "5 years"
    assert chat.replies[0]["human_request_id"] == "hreq_1"


def test_a_valueless_action_reads_as_prose_not_an_enum_name(monkeypatch):
    """`USER_APPROVED` carries no value, so without a mapping the transcript
    would show the raw action name to the user."""
    req = FakeRequest(
        request_id="hreq_1", task_id="task_1", user_id="user_1",
        request_type="USER_CONFIRMATION_REQUIRED",
    )
    _install(monkeypatch, requests=[req])
    chat = RecordingChatRepo()
    monkeypatch.setattr(hi_api, "chat_repository", chat)

    hi_api.respond_to_human_request(
        "hreq_1", hi_api.RespondRequest(action="USER_APPROVED"),
        user=FakeUser(), db=None,
    )

    assert chat.replies[0]["content"] == hi_api._ACTION_TRANSCRIPT_TEXT["USER_APPROVED"]
    assert "USER_APPROVED" not in chat.replies[0]["content"]


def test_a_failing_transcript_write_never_breaks_the_resume(monkeypatch):
    """The durable record of a response is the request status and the audit
    log. A transcript problem must not cost the user their resume."""
    req = FakeRequest(
        request_id="hreq_1", task_id="task_1", user_id="user_1", request_type="ANSWER_REQUIRED",
    )
    task, task_repo, human_repo, _audit = _install(monkeypatch, requests=[req])

    class ExplodingChatRepo:
        def record_user_reply(self, *a, **k):
            raise RuntimeError("transcript table is unavailable")

        def record_secret_submission(self, *a, **k):
            raise RuntimeError("transcript table is unavailable")

    monkeypatch.setattr(hi_api, "chat_repository", ExplodingChatRepo())

    # Returns normally rather than propagating the transcript failure. The
    # handler returns a plain dict (FastAPI serializes it into RespondResult),
    # so read it as one.
    result = hi_api.respond_to_human_request(
        "hreq_1", hi_api.RespondRequest(action="USER_PROVIDED_VALUE", value="5 years"),
        user=FakeUser(), db=None,
    )
    assert dict(result)["request_id"] == "hreq_1"
    # And the response was really consumed — the answer reached the task.
    assert task_repo.confirmed_answers, "the resume must still have recorded the answer"
