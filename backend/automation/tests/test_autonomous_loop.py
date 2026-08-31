"""
Integration-style tests for `AutonomousAgentLoop`, with the browser, the LLM
decision step, and the DB-backed repository all faked — these exercise the
orchestration logic itself (observe -> decide -> act, pause/resume, the
submission gate) without needing Playwright, an LLM API key, or Postgres.

Covers the required scenarios:
- a full observe -> decide -> act step actually mutates the fake page and is
  logged to action_history.
- pause for a login wall, then resume, re-observing before continuing.
- pause for an unknown/ambiguous question, then resume via a supplied answer.
- a submit-button click is never dispatched without `auto_submit_approved`.
"""

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

os.environ.setdefault("AUTOMATION_HUMAN_PACING", "0")

import automation.agents.autonomous.loop as loop_mod
from automation.agents.autonomous.actions import AgentAction
from automation.agents.autonomous.decision import Decision, DecisionError
from automation.agents.autonomous.loop import AutonomousAgentLoop, TaskHandle
from automation.agents.autonomous.observer import PageElement, PageState, field_identity


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class FakeTask:
    task_id: str = "task_1"
    user_id: str = "user_1"
    job_url: str = "https://example.com/apply"
    original_objective: str = "Apply for the job."
    candidate_profile: dict = field(default_factory=lambda: {"profile": {"work_authorized": True}})
    confirmed_answers: dict = field(default_factory=dict)
    field_attempt_ledger: dict = field(default_factory=dict)
    action_history: list = field(default_factory=list)
    uploaded_documents: list = field(default_factory=list)
    auto_submit_approved: bool = False
    current_status: str = "CREATED"
    current_browser_state: dict | None = None
    human_intervention: dict | None = None
    final_result: dict | None = None
    error: str | None = None


class FakeTaskRepo:
    def get_by_id(self, db, task_id):
        return db["task"]

    def set_status(self, db, task, status):
        task.current_status = status
        return task

    def update_browser_state(self, db, task, state):
        task.current_browser_state = state
        return task

    def append_action(self, db, task, record):
        task.action_history.append(record)
        return task

    def request_human_intervention(self, db, task, intervention):
        task.human_intervention = intervention
        task.current_status = "WAITING_FOR_HUMAN"
        return task

    def record_field_attempt(self, db, task, field_identity, entry):
        ledger = dict(task.field_attempt_ledger or {})
        ledger[field_identity] = entry
        task.field_attempt_ledger = ledger
        return task

    def record_confirmed_answer(self, db, task, question, answer):
        task.confirmed_answers[question] = answer
        task.human_intervention = None
        task.current_status = "RESUMING"
        return task

    def clear_intervention_for_resume(self, db, task):
        task.human_intervention = None
        task.current_status = "RESUMING"
        return task

    def mark_ready_for_approval(self, db, task, final_result):
        task.final_result = final_result
        task.current_status = "WAITING_FOR_APPROVAL"
        return task

    def approve_submission(self, db, task):
        task.auto_submit_approved = True
        task.current_status = "RESUMING"
        return task

    def mark_completed(self, db, task, final_result):
        task.final_result = final_result
        task.current_status = "COMPLETED"
        return task

    def mark_failed(self, db, task, error, final_result=None):
        task.error = error
        if final_result is not None:
            task.final_result = final_result
        task.current_status = "FAILED"
        return task

    def cancel_task(self, db, task):
        task.current_status = "CANCELLED"
        return task


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

    def set_input_files(self, path):
        # Same attribute as `fill` so upload assertions read the same way.
        self.filled_with = path


class FakePage:
    def __init__(self):
        self.url = "https://example.com/apply"
        self._locators: dict[str, FakeLocator] = {}

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    def locator(self, selector: str) -> FakeLocator:
        return self._locators.setdefault(selector, FakeLocator())


class FakeBrowserManager:
    def __init__(self, user_id, ats_platform):
        self.page = FakePage()

    def launch_context(self):
        return None

    def new_page(self):
        return self.page

    def close(self):
        pass


class FakeDetector:
    @staticmethod
    def detect_from_url(url):
        return None


class FakeRequest:
    def __init__(self, request_id, task_id, request_type, message, safe_metadata):
        self.request_id = request_id
        self.task_id = task_id
        self.request_type = request_type
        self.message = message
        self.safe_metadata = safe_metadata
        self.status = "PENDING"
        self.expires_at = None


class FakeHumanInteractionRepo:
    """Stands in for `app/services/human_interaction_repository.py` — records
    just enough for tests to assert a request was created/resolved, without
    needing a real DB session (the fake `db`/`DictDb` used throughout this
    file has no `add`/`commit`/`refresh`)."""

    def __init__(self):
        self.requests: dict[str, FakeRequest] = {}
        self._n = 0

    #: Mirrors the real module constant the loop reads.
    DEFAULT_EXPIRY_MINUTES = 10

    def create_request(self, db, *, user_id, task_id, request_type, message, safe_metadata=None,
                       expires_in_minutes=DEFAULT_EXPIRY_MINUTES, **_kw):
        self._n += 1
        req = FakeRequest(f"hreq_{self._n}", task_id, request_type, message, safe_metadata or {})
        # Recorded so a test can assert the type-appropriate expiry policy.
        req.expires_in_minutes = expires_in_minutes
        self.requests[req.request_id] = req
        return req

    def get_by_id(self, db, request_id):
        return self.requests.get(request_id)

    def mark_resolved(self, db, req):
        req.status = "RESOLVED"
        return req

    def mark_failed(self, db, req):
        req.status = "FAILED"
        return req


class FakeChatRepo:
    """Captures agent messages so a test can assert on what the chat panel
    would actually render — the real `chat_repository` needs a live SQLAlchemy
    session, which `DictDb` isn't, and `_pause_for_human` deliberately
    swallows that failure (a transcript write must never abort a live
    application) — so without this fake, the write silently no-ops in every
    other test in this file, which is fine for them but hides the one thing
    the vision-screenshot tests need to check."""

    def __init__(self):
        self.messages: list[dict] = []

    def record_agent_message(self, db, *, user_id, autonomous_task_id, content, human_request_id=None, safe_metadata=None):
        self.messages.append({
            "content": content, "human_request_id": human_request_id,
            "safe_metadata": safe_metadata or {},
        })

    def record_system_message(self, db, *, user_id, autonomous_task_id, content, safe_metadata=None):
        self.messages.append({
            "content": content, "human_request_id": None,
            "safe_metadata": safe_metadata or {},
        })


class FakeAuditLogRepo:
    def __init__(self):
        self.events = []

    def record_event(self, db, **kwargs):
        self.events.append(kwargs)


def _fake_db_session_factory(fake_db):
    @contextmanager
    def _session():
        yield fake_db
    return _session


def _element(ref, name):
    return PageElement(ref=ref, tag="input", type="text", name=name, value=None,
                        required=False, disabled=False, checked=None, options=None)


def _page_state(elements=None, text="", blocker_hint=None):
    return PageState(url="https://example.com/apply", title="Apply", visible_text=text,
                      elements=elements or [], blocker_hint=blocker_hint)


class DictDb(dict):
    #: db.refresh(task) is a no-op here — the fake repo mutates the SAME
    #: object in place, so there is nothing to re-fetch from a real DB.
    def refresh(self, obj):
        pass

    #: `_safe_mark_failed` rolls back before recording a failure (to clear a
    #: session poisoned by e.g. a deadlock) — a no-op against this fake.
    def rollback(self):
        pass


def _install_fakes(monkeypatch, task, decisions):
    """Patches every external dependency `loop.py` imports, returns the fake
    db (so a test can also poke `task` state directly if needed)."""
    fake_db = DictDb(task=task)
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "automation_db_session", _fake_db_session_factory(fake_db))
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    monkeypatch.setattr(loop_mod, "ATSDetector", FakeDetector)
    monkeypatch.setattr(loop_mod, "observe_page", lambda page, ats_hint=None: decisions["page_states"].pop(0))

    calls = {"n": 0}

    def fake_decide(**kwargs):
        i = calls["n"]
        calls["n"] += 1
        return decisions["decisions"][i]

    monkeypatch.setattr(loop_mod, "decide_next_step", fake_decide)
    return fake_db


# ---------------------------------------------------------------------------
# 1. Basic observe -> decide -> act
# ---------------------------------------------------------------------------

def test_execute_action_is_dispatched_and_logged(monkeypatch):
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    page_state = _page_state(elements=[_element(0, "First name")])
    decision = Decision(decision_type="EXECUTE_ACTION", action=AgentAction(action_type="fill", element_ref=0, value="Jane"))

    fake_db = DictDb(task=task)
    chat_repo = FakeChatRepo()
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "chat_repository", chat_repo)
    monkeypatch.setattr(loop_mod, "automation_db_session", _fake_db_session_factory(fake_db))
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    monkeypatch.setattr(loop_mod, "ATSDetector", FakeDetector)

    loop._ensure_browser(task)
    still_going = loop._handle_execute_action(fake_db, task, page_state, decision)

    assert still_going is True
    assert handle.page.locator('[data-agent-ref="0"]').filled_with == "Jane"
    assert task.action_history[-1]["action_type"] == "fill"
    assert task.action_history[-1]["success"] is True
    assert chat_repo.messages[-1]["safe_metadata"]["event"] == "ACTION_RESULT"


def test_task_start_and_failure_are_written_to_durable_chat_milestones(monkeypatch):
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)
    chat_repo = FakeChatRepo()
    _install_fakes(monkeypatch, task, {
        "page_states": [_page_state(text="Job description")],
        "decisions": [Decision(decision_type="TASK_FAILED", evidence="Application entry unavailable.")],
    })
    monkeypatch.setattr(loop_mod, "chat_repository", chat_repo)
    monkeypatch.setattr(loop_mod, "publish_task_event", lambda *a, **kw: None)

    loop.run()

    assert task.current_status == "FAILED"
    assert len(chat_repo.messages) == 2
    assert chat_repo.messages[0]["safe_metadata"]["event"] == "APPLICATION_STARTED"
    assert chat_repo.messages[1]["safe_metadata"]["event"] == "APPLICATION_FAILED"


# ---------------------------------------------------------------------------
# 2. No auto-submit without approval
# ---------------------------------------------------------------------------

def test_submit_click_without_approval_is_never_dispatched(monkeypatch):
    task = FakeTask(auto_submit_approved=False)
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    fake_db = DictDb(task=task)
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    loop._ensure_browser(task)

    page_state = _page_state(elements=[_element(5, "Submit Application")])
    decision = Decision(decision_type="EXECUTE_ACTION", action=AgentAction(action_type="click", element_ref=5))

    still_going = loop._handle_execute_action(fake_db, task, page_state, decision)

    assert still_going is False  # paused, waiting for approval
    assert handle.page.locator('[data-agent-ref="5"]').clicked is False
    assert task.current_status == "WAITING_FOR_APPROVAL"


def test_expiry_is_only_applied_to_verification_code_requests(monkeypatch):
    """A verification code has a real, short lifetime on the site's side, so
    its pause expires. Signing in / clearing a challenge / answering a
    question legitimately take a person minutes, so those pauses must NOT
    expire — capping them at the OTP window made `/respond` start returning
    410 on a pause that was still perfectly actionable."""
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    repo = FakeHumanInteractionRepo()
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", repo)
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    db = DictDb(task=task)

    expiries: dict[str, object] = {}
    for request_type in ("OTP_REQUIRED", "MFA_REQUIRED", "LOGIN_REQUIRED",
                         "CAPTCHA_REQUIRED", "MANUAL_ACTION_REQUIRED",
                         "ANSWER_REQUIRED", "USER_CONFIRMATION_REQUIRED", "UNKNOWN_BLOCKER"):
        task.current_status = "RUNNING"  # so the pause is allowed each time
        loop._pause_for_human(db, task, {"type": request_type, "message": "x"})
        expiries[request_type] = repo.requests[task.human_intervention["request_id"]].expires_in_minutes

    assert expiries["OTP_REQUIRED"] == 10
    assert expiries["MFA_REQUIRED"] == 10
    for non_secret in ("LOGIN_REQUIRED", "CAPTCHA_REQUIRED", "MANUAL_ACTION_REQUIRED",
                       "ANSWER_REQUIRED", "USER_CONFIRMATION_REQUIRED", "UNKNOWN_BLOCKER"):
        assert expiries[non_secret] is None, f"{non_secret} should not expire, got {expiries[non_secret]}"


def test_safe_mark_failed_still_records_on_a_poisoned_session(monkeypatch):
    """REGRESSION (observed once during a contended real-browser E2E run): a
    Postgres deadlock between the loop thread and an API thread poisons the
    loop's session, so every later statement raises `PendingRollbackError` —
    and the task would never be marked FAILED, leaving it stuck in RUNNING
    forever. `_safe_mark_failed` must roll back first, and must never itself
    raise."""
    task = FakeTask(current_status="RUNNING")
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())

    rolled_back = {"n": 0}

    class PoisonedDb(DictDb):
        def rollback(self):
            rolled_back["n"] += 1

    db = PoisonedDb(task=task)
    loop._safe_mark_failed(db, task, "boom", reason="unexpected_error")

    assert rolled_back["n"] == 1, "did not roll back the poisoned session first"
    assert task.current_status == "FAILED"
    assert task.error == "boom"


def test_safe_mark_failed_never_raises_even_if_the_write_fails(monkeypatch):
    """The very last resort: if recording the failure ALSO fails, that must
    not propagate — it would replace the real error with a bookkeeping one."""
    task = FakeTask(current_status="RUNNING")
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    class ExplodingRepo(FakeTaskRepo):
        def mark_failed(self, db, task, error, final_result=None):
            raise RuntimeError("DB is gone")

    monkeypatch.setattr(loop_mod, "task_repo", ExplodingRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())

    # Must not raise.
    loop._safe_mark_failed(DictDb(task=task), task, "boom", reason="unexpected_error")


def test_cancelling_a_paused_task_persists_cancelled_and_releases_the_browser(monkeypatch):
    """REGRESSION (found by the real-browser E2E run): a task cancelled while
    it was PAUSED waiting for a human used to `return` straight out of
    `_loop_body` from inside `_wait_for_resume`, skipping `cancel_task`, the
    audit event, AND `_close_browser()`. Since `/cancel` only persists the
    cancellation itself when there is no live handle, the task was left stuck
    in WAITING_FOR_HUMAN forever and its Playwright driver + browser tab
    leaked (which is what eventually starved the E2E suite of browsers)."""
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    page_states = [_page_state(
        text="Enter verification code",
        blocker_hint={"request_type": "OTP_REQUIRED", "otp_field_ref": 0, "submit_ref": 1},
    )]
    _install_fakes(monkeypatch, task, {"page_states": page_states, "decisions": []})

    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    while task.current_status != "WAITING_FOR_HUMAN" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert task.current_status == "WAITING_FOR_HUMAN"
    assert handle.page is not None  # browser is open while paused

    # Exactly what `runner.request_cancel` does.
    handle.cancel_requested.set()
    handle.resume_event.set()
    thread.join(timeout=5)
    assert not thread.is_alive()

    # The loop itself now persists the terminal state...
    assert task.current_status == "CANCELLED"
    # ...records it...
    assert "automation_cancelled" in [e["event_type"] for e in loop_mod.audit_log_repo.events]
    # ...and releases the browser instead of leaking it.
    assert handle.page is None
    assert handle.browser_manager is None


def test_llm_decided_verification_code_fill_is_refused_redacted_and_pauses(monkeypatch):
    """The executor-gate fallback (`executor.py`'s verification-code gate):
    if the deterministic detector somehow missed an OTP field and the LLM
    proposes filling it, the write is refused, the attempted value is
    REDACTED in action_history, and a fresh OTP pause is raised — the loop
    never guesses or proceeds."""
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    fake_db = DictDb(task=task)
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    loop._ensure_browser(task)

    page_state = _page_state(elements=[_element(3, "Verification code")])
    decision = Decision(
        decision_type="EXECUTE_ACTION",
        action=AgentAction(action_type="fill", element_ref=3, value="654321"),
    )

    still_going = loop._handle_execute_action(fake_db, task, page_state, decision)

    assert still_going is False
    assert task.current_status == "WAITING_FOR_HUMAN"
    assert handle.page.locator('[data-agent-ref="3"]').filled_with is None  # never written
    assert task.current_status == "WAITING_FOR_HUMAN"
    assert task.human_intervention["request_type"] == "OTP_REQUIRED"
    # The attempted value is redacted everywhere it could have been recorded.
    assert task.action_history[-1]["value"] == "[REDACTED]"
    assert not any("654321" in str(a) for a in task.action_history)


# ---------------------------------------------------------------------------
# 3. Pause for login, then resume — full run() through a background thread
# ---------------------------------------------------------------------------

def test_pause_for_login_then_resume_re_observes_and_completes(monkeypatch):
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    page_states = [
        _page_state(text="Please sign in to continue"),  # observed before the login-wall decision
        _page_state(text="Application submitted — thank you for applying!"),  # observed after resume
    ]
    decisions = [
        Decision(decision_type="REQUEST_HUMAN_INTERVENTION", intervention={
            "type": "authentication", "reason": "login wall",
            "message": "Please log in to the job site, then continue.",
        }),
        Decision(decision_type="TASK_COMPLETED", evidence="Confirmation page shown."),
    ]
    _install_fakes(monkeypatch, task, {"page_states": page_states, "decisions": decisions})

    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    while task.current_status != "WAITING_FOR_HUMAN" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert task.current_status == "WAITING_FOR_HUMAN"
    # "authentication" (the LLM's free-form vocabulary) is normalized to the
    # closed request-type vocabulary — see `decision.py::normalize_intervention_type`.
    assert task.human_intervention["type"] == "LOGIN_REQUIRED"
    assert task.human_intervention["request_id"]

    # Human "logs in" and resumes — same TaskHandle, same page, no re-navigation.
    handle.resume_event.set()
    thread.join(timeout=5)

    assert task.current_status == "COMPLETED"
    assert task.final_result["evidence"] == "Confirmation page shown."


# ---------------------------------------------------------------------------
# 4. Pause for an unknown/ambiguous question, then resume via an answer
# ---------------------------------------------------------------------------

def test_pause_for_ambiguous_question_then_answer_resumes(monkeypatch):
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    page_states = [
        _page_state(text="What is your desired start date?"),
        _page_state(text="Application submitted — thank you for applying!"),
    ]
    decisions = [
        Decision(decision_type="REQUEST_HUMAN_INTERVENTION", intervention={
            "type": "ambiguous_question", "reason": "no confirmed start date on file",
            "message": "What start date should the agent enter?",
            "information_required": "desired_start_date",
        }),
        Decision(decision_type="TASK_COMPLETED", evidence="Confirmation page shown."),
    ]
    _install_fakes(monkeypatch, task, {"page_states": page_states, "decisions": decisions})

    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    while task.current_status != "WAITING_FOR_HUMAN" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert task.current_status == "WAITING_FOR_HUMAN"
    assert task.human_intervention["information_required"] == "desired_start_date"

    # Human answers via POST .../answer — this is exactly what
    # `app/services/autonomous_task_repository.py::record_confirmed_answer` does.
    task.confirmed_answers["desired_start_date"] = "Immediately"
    task.human_intervention = None
    task.current_status = "RESUMING"
    handle.resume_event.set()
    thread.join(timeout=5)

    assert task.current_status == "COMPLETED"
    assert task.confirmed_answers["desired_start_date"] == "Immediately"


# ---------------------------------------------------------------------------
# 5. Spec §17: the completion gate before ready-for-approval
# ---------------------------------------------------------------------------

def test_submit_blocked_pending_approval_on_an_incomplete_page_escalates_instead_of_marking_ready(monkeypatch):
    """`submit_requires_approval` (the executor refused the click because
    `auto_submit_approved` is False) must not, by itself, mean "offer it up
    for approval" — `compute_page_completion` is checked first, and a still-
    missing required field must escalate to a human instead."""
    task = FakeTask(auto_submit_approved=False)
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    fake_db = DictDb(task=task)
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    loop._ensure_browser(task)

    required_empty = PageElement(ref=1, tag="input", type="text", name="Phone Number", value="",
                                  required=True, disabled=False, checked=None, options=None)
    submit_btn = _element(5, "Submit Application")
    page_state = _page_state(elements=[required_empty, submit_btn])
    decision = Decision(decision_type="EXECUTE_ACTION", action=AgentAction(action_type="click", element_ref=5))

    still_going = loop._handle_execute_action(fake_db, task, page_state, decision)

    assert still_going is False
    assert task.current_status == "WAITING_FOR_HUMAN", "must not skip straight to WAITING_FOR_APPROVAL"
    assert task.human_intervention["type"] == "UNKNOWN_BLOCKER"
    assert handle.page.locator('[data-agent-ref="5"]').clicked is False


def test_submit_blocked_pending_approval_on_a_complete_page_marks_ready_for_approval(monkeypatch):
    """The counterpart: once the page really is complete, the same refused
    click DOES mark the task ready for a human to approve."""
    task = FakeTask(auto_submit_approved=False)
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    fake_db = DictDb(task=task)
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    loop._ensure_browser(task)

    filled = PageElement(ref=1, tag="input", type="text", name="Phone Number", value="555-1234",
                          required=True, disabled=False, checked=None, options=None)
    submit_btn = _element(5, "Submit Application")
    page_state = _page_state(elements=[filled, submit_btn])
    decision = Decision(decision_type="EXECUTE_ACTION", action=AgentAction(action_type="click", element_ref=5))

    still_going = loop._handle_execute_action(fake_db, task, page_state, decision)

    assert still_going is False
    assert task.current_status == "WAITING_FOR_APPROVAL"


# ---------------------------------------------------------------------------
# 6. Spec §16: the verification-streak escalation (in-memory, same-process)
# ---------------------------------------------------------------------------

def test_three_consecutive_unverified_attempts_on_the_same_ref_escalate(monkeypatch):
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    fake_db = DictDb(task=task)
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    loop._ensure_browser(task)

    # A FakePage has no real DOM to diff, so `capture_page_signature` always
    # comes back unreadable and every click reports `verified=False` — the
    # exact "nothing observably changed" case the streak counter is for. No
    # `screenshot` method on FakePage means the new vision-assisted retry
    # (Workstream 2) safely no-ops via AttributeError, so this test exercises
    # ONLY the streak logic, unaffected by that addition.
    page_state = _page_state(elements=[_element(0, "Mystery Button")])
    decision = Decision(decision_type="EXECUTE_ACTION", action=AgentAction(action_type="click", element_ref=0))

    assert loop._handle_execute_action(fake_db, task, page_state, decision) is True
    assert loop._handle_execute_action(fake_db, task, page_state, decision) is True
    still_going = loop._handle_execute_action(fake_db, task, page_state, decision)

    assert still_going is False
    assert task.current_status == "WAITING_FOR_HUMAN"
    assert task.human_intervention["type"] == "UNKNOWN_BLOCKER"
    assert "Mystery Button" in task.human_intervention["reason"]


def test_ungrounded_navigation_is_rejected_before_browser_dispatch(monkeypatch):
    task = FakeTask(job_url="https://example.com/jobs/123")
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)
    fake_db = DictDb(task=task)
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "publish_task_event", lambda *a, **kw: None)
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    loop._ensure_browser(task)

    apply = PageElement(
        ref=12, tag="button", type="button", name="Apply Now", value=None,
        required=False, disabled=False, checked=None, options=None,
        semantic_action="APPLY", action_confidence="HIGH",
    )
    state = PageState(
        url=task.job_url, title="Engineer", visible_text="Job description",
        elements=[apply], page_type="JOB_LISTING",
    )
    decision = Decision(
        decision_type="EXECUTE_ACTION",
        action=AgentAction(action_type="navigate", url="https://example.com/jobs/123/apply"),
    )

    keep_going = loop._handle_execute_action(fake_db, task, state, decision)

    assert keep_going is False
    assert handle.page.url == task.job_url
    assert task.action_history[-1]["action_result"] == "ACTION_REJECTED"
    assert task.action_history[-1]["action_attempted"] is False
    assert task.current_status == "WAITING_FOR_HUMAN"


# ---------------------------------------------------------------------------
# 7. Spec §16: the persisted field-attempt ledger (survives a resume/restart)
# ---------------------------------------------------------------------------

def test_a_field_already_marked_failed_in_the_ledger_is_never_retried(monkeypatch):
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    fake_db = DictDb(task=task)
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    loop._ensure_browser(task)

    element = _element(0, "Cursed Field")
    # As if this exact field failed 3 times in an EARLIER resume/process —
    # `TaskHandle.unverified_streak` would have been reset to 0 by then, so
    # only the persisted ledger remembers this.
    task.field_attempt_ledger = {field_identity(element): {"status": "failed", "attempts": 3, "last_action_type": "fill"}}

    page_state = _page_state(elements=[element])
    decision = Decision(decision_type="EXECUTE_ACTION", action=AgentAction(action_type="fill", element_ref=0, value="x"))

    still_going = loop._handle_execute_action(fake_db, task, page_state, decision)

    assert still_going is False
    assert handle.page.locator('[data-agent-ref="0"]').filled_with is None, "must never even attempt the field"
    assert task.current_status == "WAITING_FOR_HUMAN"
    assert task.human_intervention["type"] == "UNKNOWN_BLOCKER"
    assert not task.action_history, "a refused-before-dispatch field must not appear in action_history"


def test_a_field_that_fails_three_times_is_marked_failed_in_the_persisted_ledger(monkeypatch):
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    fake_db = DictDb(task=task)
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    loop._ensure_browser(task)

    element = _element(0, "Mystery Button")
    page_state = _page_state(elements=[element])
    decision = Decision(decision_type="EXECUTE_ACTION", action=AgentAction(action_type="click", element_ref=0))

    loop._handle_execute_action(fake_db, task, page_state, decision)
    loop._handle_execute_action(fake_db, task, page_state, decision)
    loop._handle_execute_action(fake_db, task, page_state, decision)

    entry = task.field_attempt_ledger[field_identity(element)]
    assert entry["status"] == "failed"
    assert entry["attempts"] == 3


# ---------------------------------------------------------------------------
# 8. Spec §19-20: the vision-assisted retry before pausing
# ---------------------------------------------------------------------------

def test_escalate_with_vision_executes_a_vision_proposed_action_without_pausing(monkeypatch):
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    fake_db = DictDb(task=task)
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    loop._ensure_browser(task)
    # Only THIS test's page gets a working `screenshot()` — deliberately not
    # added to the shared `FakePage`, so every other test keeps exercising
    # the "no screenshot available" fallback (see the streak test above).
    handle.page.screenshot = lambda **kwargs: b"fake-jpeg-bytes"

    seen_screenshots = []

    def fake_decide(**kwargs):
        seen_screenshots.append(kwargs.get("screenshot"))
        return Decision(decision_type="EXECUTE_ACTION", action=AgentAction(action_type="fill", element_ref=0, value="Jane"))

    monkeypatch.setattr(loop_mod, "decide_next_step", fake_decide)

    page_state = _page_state(elements=[_element(0, "First name")])
    keep_going = loop._escalate_with_vision(
        fake_db, task, page_state, {"type": "UNKNOWN_BLOCKER", "message": "fallback"}, detection_layer="test",
    )

    assert keep_going is True
    assert handle.page.locator('[data-agent-ref="0"]').filled_with == "Jane"
    assert seen_screenshots == [b"fake-jpeg-bytes"]
    assert task.current_status != "WAITING_FOR_HUMAN"
    assert any(e["event_type"] == "vision_assisted_action" for e in loop_mod.audit_log_repo.events)


def test_escalate_with_vision_falls_back_and_attaches_the_screenshot_to_chat(monkeypatch):
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    fake_db = DictDb(task=task)
    chat_repo = FakeChatRepo()
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "chat_repository", chat_repo)
    monkeypatch.setattr(loop_mod, "publish_task_event", lambda *a, **kw: None)
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    loop._ensure_browser(task)
    handle.page.screenshot = lambda **kwargs: b"fake-jpeg-bytes"

    def fake_decide(**kwargs):
        raise DecisionError("the model returned nothing usable")

    monkeypatch.setattr(loop_mod, "decide_next_step", fake_decide)

    fallback = {"type": "UNKNOWN_BLOCKER", "message": "Autogram is stuck."}
    keep_going = loop._escalate_with_vision(fake_db, task, _page_state(), fallback, detection_layer="test")

    assert keep_going is False
    assert task.current_status == "WAITING_FOR_HUMAN"
    assert chat_repo.messages, "the pause must still write to the chat transcript"
    data_uri = chat_repo.messages[-1]["safe_metadata"].get("screenshot_data_uri")
    assert data_uri is not None
    assert data_uri.startswith("data:image/jpeg;base64,")


def test_escalate_with_vision_never_attaches_a_screenshot_when_capture_fails(monkeypatch):
    """No `screenshot` method on the fake page (the normal case for every
    other test in this file) must fall back cleanly with no screenshot
    anywhere — never crash, never fabricate one."""
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    fake_db = DictDb(task=task)
    chat_repo = FakeChatRepo()
    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "chat_repository", chat_repo)
    monkeypatch.setattr(loop_mod, "publish_task_event", lambda *a, **kw: None)
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    loop._ensure_browser(task)

    fallback = {"type": "UNKNOWN_BLOCKER", "message": "Autogram is stuck."}
    keep_going = loop._escalate_with_vision(fake_db, task, _page_state(), fallback, detection_layer="test")

    assert keep_going is False
    assert "screenshot_data_uri" not in chat_repo.messages[-1]["safe_metadata"]


def test_task_completed_is_downgraded_without_confirmation_text(monkeypatch):
    """Spec: never report TASK_COMPLETED unless the browser actually shows a
    post-submit confirmation — a bare LLM claim is not enough."""
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    page_states = [_page_state(text="Review your application before submitting.")]
    decisions = [Decision(decision_type="TASK_COMPLETED", evidence="I clicked submit.")]
    _install_fakes(monkeypatch, task, {"page_states": page_states, "decisions": decisions})

    # The downgrade lands the task in WAITING_FOR_APPROVAL, which pauses the
    # loop — run it on a background thread and cancel once we've observed
    # the downgrade, rather than blocking this test on a human response that
    # will never come.
    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    while task.current_status != "WAITING_FOR_APPROVAL" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert task.current_status == "WAITING_FOR_APPROVAL"
    assert "Downgraded" in task.final_result["evidence"]

    handle.cancel_requested.set()
    handle.resume_event.set()
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 5. Deterministic (non-LLM) OTP/MFA blocker detection + secret handling
# ---------------------------------------------------------------------------

def test_deterministic_otp_blocker_pauses_without_ever_calling_the_llm(monkeypatch):
    """A page whose `PageState.blocker_hint` already identifies OTP_REQUIRED
    (Layers 1/2, `observer.py::detect_blocker`) must pause immediately —
    `decide_next_step` (the LLM, Layer 3) is never called for that iteration."""
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    page_states = [_page_state(
        text="Enter the verification code sent to j***@gmail.com",
        blocker_hint={
            "request_type": "OTP_REQUIRED", "reason": "code field detected",
            "otp_field_ref": 3, "submit_ref": 4, "masked_destination": "j***@gmail.com",
        },
    )]
    # Deliberately empty — if `decide_next_step` were called despite the
    # blocker hint, popping from this list would raise IndexError and fail
    # the test loudly rather than silently passing.
    _install_fakes(monkeypatch, task, {"page_states": page_states, "decisions": []})

    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while task.current_status != "WAITING_FOR_HUMAN" and time.monotonic() < deadline:
        time.sleep(0.02)

    assert task.current_status == "WAITING_FOR_HUMAN"
    assert task.human_intervention["request_type"] == "OTP_REQUIRED"
    assert task.human_intervention["request_id"]
    assert "j***@gmail.com" in task.human_intervention["message"]

    requests = list(loop_mod.human_interaction_repo.requests.values())
    assert len(requests) == 1
    assert requests[0].request_type == "OTP_REQUIRED"
    assert requests[0].status == "PENDING"

    event_types = [e["event_type"] for e in loop_mod.audit_log_repo.events]
    assert "blocker_detected" in event_types
    assert "human_request_created" in event_types
    assert "automation_paused" in event_types

    handle.cancel_requested.set()
    handle.resume_event.set()
    thread.join(timeout=5)


def test_pending_secret_is_filled_deterministically_redacted_and_never_guessed_on_rejection(monkeypatch):
    """End-to-end: OTP detected -> human delivers a code (never seen by the
    LLM) -> filled + submitted deterministically -> site rejects it (the same
    blocker reappears) -> a NEW, independent pause is raised (never a blind
    retry) -> human delivers a second, correct code -> accepted -> task
    completes."""
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    otp_blocker = {
        "request_type": "OTP_REQUIRED", "reason": "code field detected",
        "otp_field_ref": 7, "submit_ref": 8, "masked_destination": None,
    }
    page_states = [
        _page_state(text="Enter verification code", blocker_hint=otp_blocker),   # initial detection
        _page_state(text="Enter verification code", blocker_hint=otp_blocker),   # re-observed to fill the (wrong) code
        _page_state(text="Invalid verification code", blocker_hint=otp_blocker),  # site rejected it — same field still there
        _page_state(text="Enter verification code", blocker_hint=otp_blocker),   # re-observed to fill the (correct) code
        _page_state(text="Application submitted — thank you for applying!"),      # accepted
    ]
    decisions = [Decision(decision_type="TASK_COMPLETED", evidence="Confirmation page shown.")]
    _install_fakes(monkeypatch, task, {"page_states": page_states, "decisions": decisions})

    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    while task.current_status != "WAITING_FOR_HUMAN" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert task.current_status == "WAITING_FOR_HUMAN"
    first_request_id = task.human_intervention["request_id"]

    # Human submits a (wrong) code — delivered straight to the handle, exactly
    # like `runner.py::deliver_secret` does from `POST /human-requests/{id}/respond`.
    handle.pending_secret = {"request_id": first_request_id, "value": "000000"}
    handle.resume_event.set()

    deadline = time.monotonic() + 5
    while (
        len(loop_mod.human_interaction_repo.requests) < 2
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)

    # The wrong code was filled and submitted deterministically — and its
    # value is redacted everywhere it's ever logged.
    fill_entries = [a for a in task.action_history if a["action_type"] == "fill"]
    assert fill_entries and all(a["value"] == "[REDACTED]" for a in fill_entries)
    assert not any("000000" in str(a) for a in task.action_history)

    # Rejected -> a brand-new, independent pause (never a guessed retry).
    assert task.current_status == "WAITING_FOR_HUMAN"
    second_request_id = task.human_intervention["request_id"]
    assert second_request_id != first_request_id
    assert task.human_intervention["request_type"] == "OTP_REQUIRED"

    first_request = loop_mod.human_interaction_repo.requests[first_request_id]
    assert first_request.status == "RESOLVED"  # consumed, even though the code was wrong

    # Human submits the correct code.
    handle.pending_secret = {"request_id": second_request_id, "value": "123456"}
    handle.resume_event.set()
    thread.join(timeout=5)

    assert task.current_status == "COMPLETED"
    assert not any("123456" in str(a) for a in task.action_history)


def test_field_vanishing_after_secret_delivery_raises_a_fresh_unknown_blocker_pause(monkeypatch):
    """If the verification field can't be relocated after a code is
    delivered (the page changed unexpectedly) and nothing else recognizable
    is on the page either, the loop must never guess what happened — it
    raises a fresh, honest UNKNOWN_BLOCKER pause instead of assuming any
    specific cause."""
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    otp_blocker = {"request_type": "OTP_REQUIRED", "otp_field_ref": 1, "submit_ref": 2}
    page_states = [
        _page_state(text="Enter verification code", blocker_hint=otp_blocker),
        _page_state(text="Something else entirely"),  # the field is gone on re-observe
    ]
    _install_fakes(monkeypatch, task, {"page_states": page_states, "decisions": []})

    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while task.current_status != "WAITING_FOR_HUMAN" and time.monotonic() < deadline:
        time.sleep(0.02)
    request_id = task.human_intervention["request_id"]

    handle.pending_secret = {"request_id": request_id, "value": "999999"}
    handle.resume_event.set()

    deadline = time.monotonic() + 5
    while task.human_intervention.get("request_id") == request_id and time.monotonic() < deadline:
        time.sleep(0.02)

    assert task.current_status == "WAITING_FOR_HUMAN"
    assert task.human_intervention["type"] == "UNKNOWN_BLOCKER"
    assert not any("999999" in str(a) for a in task.action_history)

    handle.cancel_requested.set()
    handle.resume_event.set()
    thread.join(timeout=5)


def test_captcha_replacing_the_otp_page_is_reported_accurately_not_as_login_required(monkeypatch):
    """If a DIFFERENT, still-recognizable blocker (e.g. a CAPTCHA) has
    replaced the OTP page by the time a delivered code is consumed, the
    fresh pause must reflect what's ACTUALLY there — not a generic guess."""
    task = FakeTask()
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    otp_blocker = {"request_type": "OTP_REQUIRED", "otp_field_ref": 1, "submit_ref": 2}
    captcha_blocker = {"request_type": "CAPTCHA_REQUIRED", "otp_field_ref": None, "submit_ref": None}
    page_states = [
        _page_state(text="Enter verification code", blocker_hint=otp_blocker),
        _page_state(text="Verify you are human", blocker_hint=captcha_blocker),
    ]
    _install_fakes(monkeypatch, task, {"page_states": page_states, "decisions": []})

    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while task.current_status != "WAITING_FOR_HUMAN" and time.monotonic() < deadline:
        time.sleep(0.02)
    request_id = task.human_intervention["request_id"]

    handle.pending_secret = {"request_id": request_id, "value": "999999"}
    handle.resume_event.set()

    deadline = time.monotonic() + 5
    while task.human_intervention.get("request_id") == request_id and time.monotonic() < deadline:
        time.sleep(0.02)

    assert task.current_status == "WAITING_FOR_HUMAN"
    assert task.human_intervention["type"] == "CAPTCHA_REQUIRED"
    assert not any("999999" in str(a) for a in task.action_history)

    handle.cancel_requested.set()
    handle.resume_event.set()
    thread.join(timeout=5)
