"""
API-level tests for the duplicate/concurrent automation guard on
`POST /agent/tasks` — called directly as plain functions, the same convention
`test_human_interaction_api.py` uses, with the repositories and the runner
faked so no DB or browser is needed.

The point of these (as distinct from `test_duplicate_automation_guard.py`,
which exercises the real index) is the ORDERING and the CLEANUP: a rejected
duplicate must be refused *before* anything is allocated — no task row, and
above all no `BrowserManager`/Playwright session/Chrome tab, since two tabs
filling one application form is the whole failure being prevented.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

import app.api.autonomous_agent as agent_api
from app.services import automation_ownership as real_ownership
from app.services.automation_ownership import ActiveAutomation, SubmittedApplication


@dataclass
class FakeUser:
    user_id: str = "user_1"


@dataclass
class FakeTask:
    task_id: str = "task_new"
    user_id: str = "user_1"
    job_url: str = "https://careers.example.com/jobs/1/apply"
    original_objective: str = "apply"
    current_status: str = "CREATED"
    current_browser_state: dict | None = None
    action_history: list = field(default_factory=list)
    application_progress: dict = field(default_factory=dict)
    human_intervention: dict | None = None
    confirmed_answers: dict = field(default_factory=dict)
    uploaded_documents: list = field(default_factory=list)
    final_result: dict | None = None
    error: str | None = None
    auto_submit_approved: bool = False


class RecordingTaskRepo:
    """Records whether a task row was created at all."""

    def __init__(self, *, raise_integrity: bool = False):
        self.created: list[dict] = []
        self.statuses: list[str] = []
        self.raise_integrity = raise_integrity

    def create_task(self, db, **kwargs):
        if self.raise_integrity:
            raise IntegrityError("INSERT", {}, Exception("uq_autonomous_tasks_active_job"))
        self.created.append(kwargs)
        return FakeTask(job_url=kwargs["job_url"], user_id=kwargs["user_id"])

    def set_status(self, db, task, status):
        self.statuses.append(status)
        task.current_status = status
        return task


class FakeOwnership:
    """Stands in for `app/services/automation_ownership.py`.

    `lookups` is a per-call sequence, because the IntegrityError backstop
    queries TWICE with different expected answers: nothing active at check
    time (we then lose the insert race), and the winner on the retry after
    rollback. A single fixed value cannot express that."""

    def __init__(
        self,
        active: ActiveAutomation | None,
        lookups: list | None = None,
        submitted: SubmittedApplication | None = None,
    ):
        self.active = active
        self.submitted = submitted
        self.lookups = list(lookups) if lookups is not None else None
        self.reserved: list[str] = []
        self.lookup_calls = 0
        self.submitted_lookup_calls = 0

    def reserve_job_automation(self, db, *, user_id, job_url):
        self.reserved.append(job_url)
        return "fake-key"

    def find_active_automation(self, db, *, user_id, job_url, exclude_task_id=None):
        self.lookup_calls += 1
        if self.lookups is not None:
            return self.lookups.pop(0) if self.lookups else None
        return self.active

    def find_submitted_application(self, db, *, user_id, job_url):
        self.submitted_lookup_calls += 1
        return self.submitted

    # Acknowledgement matching is PURE — no DB, no session — so the fake
    # delegates to the real implementation instead of reimplementing the
    # rules. A hand-written copy could drift from the shared validator and
    # make the mismatch tests below pass vacuously against a stub that is
    # stricter (or laxer) than what actually runs in production.
    ReapplyAcknowledgementError = real_ownership.ReapplyAcknowledgementError

    @staticmethod
    def validate_reapply_acknowledgement(acknowledgement, submitted):
        return real_ownership.validate_reapply_acknowledgement(acknowledgement, submitted)

    def job_key(self, job_url):
        # The real one is `compute_job_url_hash`; the audit event only needs a
        # stable non-sensitive identifier, so delegate to it for fidelity.
        from app.services.application_repository import compute_job_url_hash

        return compute_job_url_hash(job_url)

    # The route references these dataclasses through the module object too.
    ActiveAutomation = ActiveAutomation
    SubmittedApplication = SubmittedApplication


class FakeDb:
    def __init__(self):
        self.rolled_back = 0

    def rollback(self):
        self.rolled_back += 1


class FakeAuditLog:
    def __init__(self):
        self.events: list[dict] = []

    def record_event(self, db, **kwargs):
        self.events.append(kwargs)


def _install(monkeypatch, *, active=None, raise_integrity=False, lookups=None, submitted=None):
    task_repo = RecordingTaskRepo(raise_integrity=raise_integrity)
    ownership = FakeOwnership(active, lookups=lookups, submitted=submitted)
    audit = FakeAuditLog()
    started: list[str] = []

    monkeypatch.setattr(agent_api, "task_repo", task_repo)
    monkeypatch.setattr(agent_api, "automation_ownership", ownership)
    monkeypatch.setattr(agent_api, "audit_log_repository", audit)
    monkeypatch.setattr(agent_api, "start_task_background", lambda task_id: started.append(task_id))
    # A duplicate must never get as far as reading a profile/résumé either.
    monkeypatch.setattr(
        agent_api, "_build_candidate_profile_snapshot",
        lambda db, user, resume_id, overrides: ({"profile": {}}, []),
    )
    return task_repo, ownership, started, audit


def _body(url="https://careers.example.com/jobs/1/apply"):
    return agent_api.StartTaskRequest(job_url=url)


# ---------------------------------------------------------------------------
# Rejection: nothing is allocated
# ---------------------------------------------------------------------------

def test_duplicate_autonomous_task_is_rejected_without_creating_a_task_or_browser(monkeypatch):
    active = ActiveAutomation(path="autonomous", status="RUNNING", task_id="task_existing")
    task_repo, ownership, started, audit = _install(monkeypatch, active=active)

    with pytest.raises(HTTPException) as exc:
        agent_api.start_task(_body(), user=FakeUser(), db=FakeDb())

    assert exc.value.status_code == 409
    # STEP 6: no task row, no status write, and above all no browser.
    assert task_repo.created == [], "a duplicate request created a task row"
    assert task_repo.statuses == [], "a duplicate request wrote a status"
    assert started == [], "a duplicate request started a browser session"
    # The lock was taken before the check.
    assert ownership.reserved, "the job was never reserved before checking"


def test_active_deterministic_application_blocks_an_autonomous_start(monkeypatch):
    """CASE B."""
    active = ActiveAutomation(path="deterministic", status="processing", application_id="app_1")
    task_repo, _, started, audit = _install(monkeypatch, active=active)

    with pytest.raises(HTTPException) as exc:
        agent_api.start_task(_body(), user=FakeUser(), db=FakeDb())

    detail = exc.value.detail
    assert exc.value.status_code == 409
    assert detail["path"] == "deterministic"
    assert detail["application_id"] == "app_1"
    assert detail["task_id"] is None
    assert task_repo.created == [] and started == []


# ---------------------------------------------------------------------------
# The 409 body the frontend branches on
# ---------------------------------------------------------------------------

def test_conflict_body_is_machine_readable_and_leaks_no_schema(monkeypatch):
    active = ActiveAutomation(path="autonomous", status="WAITING_FOR_HUMAN", task_id="task_existing")
    _install(monkeypatch, active=active)

    with pytest.raises(HTTPException) as exc:
        agent_api.start_task(_body(), user=FakeUser(), db=FakeDb())

    detail = exc.value.detail
    assert detail["reason"] == "active_automation_exists"   # the discriminator
    assert detail["path"] == "autonomous"
    assert detail["status"] == "WAITING_FOR_HUMAN"
    assert detail["task_id"] == "task_existing"             # so the UI can link to it
    assert detail["application_id"] is None
    assert isinstance(detail["message"], str) and detail["message"]
    # Only these keys — no table/column names or other internals.
    assert set(detail) == {"reason", "message", "path", "status", "task_id", "application_id"}


# ---------------------------------------------------------------------------
# Lifetime duplicate: already successfully submitted
# ---------------------------------------------------------------------------

def test_a_previously_submitted_autonomous_task_blocks_a_new_one(monkeypatch):
    """The autonomous side of the lifetime gap."""
    from datetime import datetime, timezone

    when = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    submitted = SubmittedApplication(path="autonomous", submitted_at=when, task_id="task_done")
    task_repo, ownership, started, audit = _install(monkeypatch, submitted=submitted)

    with pytest.raises(HTTPException) as exc:
        agent_api.start_task(_body(), user=FakeUser(), db=FakeDb())

    detail = exc.value.detail
    assert exc.value.status_code == 409
    # A DIFFERENT reason from the concurrency conflict — the UI must be able to
    # say "already applied" vs "something is running".
    assert detail["reason"] == "application_already_submitted"
    assert detail["path"] == "autonomous"
    assert detail["task_id"] == "task_done"
    assert detail["submitted_at"] == when.isoformat()
    # Nothing allocated, no browser.
    assert task_repo.created == [] and started == []


def test_a_previously_submitted_deterministic_application_blocks_an_autonomous_start(monkeypatch):
    """CASE B: deterministic `applied` must block an autonomous re-apply."""
    from datetime import datetime, timezone

    when = datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc)
    submitted = SubmittedApplication(path="deterministic", submitted_at=when, application_id="app_done")
    task_repo, _, started, audit = _install(monkeypatch, submitted=submitted)

    with pytest.raises(HTTPException) as exc:
        agent_api.start_task(_body(), user=FakeUser(), db=FakeDb())

    detail = exc.value.detail
    assert detail["reason"] == "application_already_submitted"
    assert detail["path"] == "deterministic"
    assert detail["application_id"] == "app_done"
    assert detail["task_id"] is None
    assert task_repo.created == [] and started == []


def test_the_active_check_runs_before_the_submitted_check(monkeypatch):
    """Ordering matters for the message the user sees: if something is running
    RIGHT NOW, "already being automated" (with a link to it) is more useful
    than "already submitted"."""
    active = ActiveAutomation(path="autonomous", status="RUNNING", task_id="task_running")
    submitted = SubmittedApplication(path="autonomous", task_id="task_old")
    _install(monkeypatch, active=active, submitted=submitted)

    with pytest.raises(HTTPException) as exc:
        agent_api.start_task(_body(), user=FakeUser(), db=FakeDb())

    assert exc.value.detail["reason"] == "active_automation_exists"
    assert exc.value.detail["task_id"] == "task_running"


def test_both_guards_are_consulted_under_the_same_reservation(monkeypatch):
    """STEP 8: the submitted check must sit inside the same advisory-lock
    window as the active check, so a submission committing concurrently cannot
    slip between them."""
    task_repo, ownership, started, audit = _install(monkeypatch)  # nothing blocking

    agent_api.start_task(_body(), user=FakeUser(), db=FakeDb())

    assert len(ownership.reserved) == 1, "the job was not reserved exactly once"
    assert ownership.lookup_calls == 1
    assert ownership.submitted_lookup_calls == 1, "the submitted check never ran"


# ---------------------------------------------------------------------------
# Happy path still works
# ---------------------------------------------------------------------------

def test_a_first_start_for_a_job_proceeds_normally(monkeypatch):
    task_repo, ownership, started, audit = _install(monkeypatch, active=None)

    task = agent_api.start_task(_body(), user=FakeUser(), db=FakeDb())

    assert len(task_repo.created) == 1
    assert task_repo.statuses == ["ANALYZING_JOB"]
    assert started == [task.task_id]
    assert ownership.reserved == ["https://careers.example.com/jobs/1/apply"]


# ---------------------------------------------------------------------------
# The IntegrityError backstop (the index firing under a lost race)
# ---------------------------------------------------------------------------

def test_integrity_error_from_the_index_becomes_a_clean_409(monkeypatch):
    """If the advisory lock was unavailable and two inserts raced, the partial
    unique index still refuses the second — and the route must translate that
    into the same conflict response rather than a 500."""
    winner = ActiveAutomation(path="autonomous", status="RUNNING", task_id="task_winner")
    # Nothing active at check time (we lose the insert race a moment later),
    # then the post-rollback lookup finds the winner.
    task_repo, ownership, started, audit = _install(
        monkeypatch, raise_integrity=True, lookups=[None, winner],
    )

    db = FakeDb()
    with pytest.raises(HTTPException) as exc:
        agent_api.start_task(_body(), user=FakeUser(), db=db)

    assert exc.value.status_code == 409
    assert exc.value.detail["task_id"] == "task_winner"
    # The poisoned session was rolled back before the follow-up query.
    assert db.rolled_back == 1
    assert ownership.lookup_calls == 2, "the winner was never looked up after rollback"
    # And no browser was started for the loser.
    assert started == []


def test_integrity_error_with_no_discoverable_winner_still_returns_409(monkeypatch):
    """Defensive: the winning row may already have gone terminal by the time we
    look. Still a conflict, never a 500."""
    task_repo, ownership, started, audit = _install(monkeypatch, active=None, raise_integrity=True)

    with pytest.raises(HTTPException) as exc:
        agent_api.start_task(_body(), user=FakeUser(), db=FakeDb())

    assert exc.value.status_code == 409
    assert exc.value.detail["reason"] == "active_automation_exists"
    assert started == []


# ---------------------------------------------------------------------------
# Explicit re-application
# ---------------------------------------------------------------------------

def _ack(path="autonomous", task_id=None, application_id=None):
    return agent_api.ReapplyAcknowledgement(
        path=path, task_id=task_id, application_id=application_id
    )


def _body_with_ack(ack, url="https://careers.example.com/jobs/1/apply"):
    return agent_api.StartTaskRequest(job_url=url, acknowledge_previous_submission=ack)


def test_explicit_reapply_over_an_autonomous_submission_starts_a_new_task(monkeypatch):
    """CASE C. The acknowledgement names the exact prior task, so the lifetime
    guard is deliberately relaxed and a NEW task is created."""
    submitted = SubmittedApplication(path="autonomous", task_id="task_done")
    task_repo, ownership, started, audit = _install(monkeypatch, submitted=submitted)

    task = agent_api.start_task(
        _body_with_ack(_ack(path="autonomous", task_id="task_done")),
        user=FakeUser(), db=FakeDb(),
    )

    assert len(task_repo.created) == 1, "no new task was created"
    assert task_repo.statuses == ["ANALYZING_JOB"]
    assert started == [task.task_id]

    # STEP 6: a deliberate re-application is audited on the EXISTING trail,
    # with metadata only.
    reapply_events = [e for e in audit.events if e["event_type"] == "reapplication_authorized"]
    assert len(reapply_events) == 1, audit.events
    meta = reapply_events[0]["metadata"]
    assert meta["previous_path"] == "autonomous"
    assert meta["previous_task_id"] == "task_done"
    assert meta["new_task_id"] == task.task_id
    assert meta["job_url_hash"]                      # a hash, never the raw URL
    assert reapply_events[0]["actor"] == "user_1"     # the human who chose it
    # Nothing sensitive: no résumé, profile, secret, or browser state.
    blob = str(reapply_events[0])
    for forbidden in ("resume", "password", "otp", "cookie", "session", "profile"):
        assert forbidden not in blob.lower(), forbidden


def test_a_normal_start_records_no_reapplication_audit_event(monkeypatch):
    """The audit event must mean something: it appears ONLY for a deliberate
    override, never on an ordinary first start."""
    task_repo, _, started, audit = _install(monkeypatch, submitted=None)

    agent_api.start_task(_body(), user=FakeUser(), db=FakeDb())

    assert not [e for e in audit.events if e["event_type"] == "reapplication_authorized"]


def test_explicit_reapply_over_a_deterministic_submission_starts_a_new_task(monkeypatch):
    """CASE B: previous DETERMINISTIC submission, re-applied via the autonomous
    path. Works because the new attempt is a new AutonomousTask, which the
    applications table's unique constraint has no say over."""
    submitted = SubmittedApplication(path="deterministic", application_id="app_done")
    task_repo, _, started, audit = _install(monkeypatch, submitted=submitted)

    task = agent_api.start_task(
        _body_with_ack(_ack(path="deterministic", application_id="app_done")),
        user=FakeUser(), db=FakeDb(),
    )

    assert len(task_repo.created) == 1
    assert started == [task.task_id]


def test_reapply_does_NOT_bypass_active_automation(monkeypatch):
    """STEP 3, the central safety property: the override relaxes the LIFETIME
    guard only. An acknowledgement must never get past active ownership."""
    active = ActiveAutomation(path="autonomous", status="RUNNING", task_id="task_running")
    submitted = SubmittedApplication(path="autonomous", task_id="task_done")
    task_repo, _, started, audit = _install(monkeypatch, active=active, submitted=submitted)

    with pytest.raises(HTTPException) as exc:
        agent_api.start_task(
            _body_with_ack(_ack(path="autonomous", task_id="task_done")),
            user=FakeUser(), db=FakeDb(),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["reason"] == "active_automation_exists"
    assert task_repo.created == [] and started == []


@pytest.mark.parametrize("status", ["WAITING_FOR_APPROVAL", "WAITING_FOR_HUMAN", "RESUMING"])
def test_reapply_cannot_bypass_a_paused_but_active_task(monkeypatch, status):
    """A task waiting for approval/OTP is ACTIVE, not submitted — re-apply must
    not be a way around it."""
    active = ActiveAutomation(path="autonomous", status=status, task_id="task_paused")
    submitted = SubmittedApplication(path="autonomous", task_id="task_done")
    task_repo, _, started, audit = _install(monkeypatch, active=active, submitted=submitted)

    with pytest.raises(HTTPException) as exc:
        agent_api.start_task(
            _body_with_ack(_ack(path="autonomous", task_id="task_done")),
            user=FakeUser(), db=FakeDb(),
        )
    assert exc.value.detail["reason"] == "active_automation_exists"
    assert task_repo.created == [] and started == []


@pytest.mark.parametrize("bad_ack,why", [
    (_ack(path="autonomous", task_id="task_SOMETHING_ELSE"), "wrong task id"),
    (_ack(path="deterministic", application_id="task_done"), "wrong path"),
    (_ack(path="autonomous", task_id=None), "no id at all"),
    (_ack(path="autonomous", task_id=""), "empty id"),
])
def test_a_mismatched_acknowledgement_is_refused(monkeypatch, bad_ack, why):
    """An acknowledgement replayed from a different job/automation, or a
    half-filled one, must not authorise anything."""
    submitted = SubmittedApplication(path="autonomous", task_id="task_done")
    task_repo, _, started, audit = _install(monkeypatch, submitted=submitted)

    with pytest.raises(HTTPException) as exc:
        agent_api.start_task(_body_with_ack(bad_ack), user=FakeUser(), db=FakeDb())

    assert exc.value.status_code == 409, why
    assert exc.value.detail["reason"] == "invalid_reapplication_request", why
    assert task_repo.created == [] and started == [], why


def test_a_stale_acknowledgement_stops_working_once_superseded(monkeypatch):
    """STEP 9: "a stale 409 must not accidentally authorize a future start".

    After a re-application completes, the most-recent submission for the job is
    the NEW task — so the acknowledgement kept from the FIRST 409 no longer
    matches and is refused. No token store needed for that expiry."""
    # The job's current submission is now the second task.
    submitted_now = SubmittedApplication(path="autonomous", task_id="task_second")
    task_repo, _, started, audit = _install(monkeypatch, submitted=submitted_now)

    with pytest.raises(HTTPException) as exc:
        agent_api.start_task(
            _body_with_ack(_ack(path="autonomous", task_id="task_first")),  # stale
            user=FakeUser(), db=FakeDb(),
        )

    assert exc.value.detail["reason"] == "invalid_reapplication_request"
    assert task_repo.created == [] and started == []


def test_an_acknowledgement_is_ignored_when_nothing_was_submitted(monkeypatch):
    """A leftover acknowledgement on an otherwise-normal start must not change
    behaviour — the start simply proceeds, because there is nothing to
    override. It must NOT error, or a sticky frontend field would break
    ordinary starts."""
    task_repo, ownership, started, audit = _install(monkeypatch, submitted=None)

    task = agent_api.start_task(
        _body_with_ack(_ack(path="autonomous", task_id="task_long_gone")),
        user=FakeUser(), db=FakeDb(),
    )
    assert len(task_repo.created) == 1
    assert started == [task.task_id]


def test_a_normal_start_still_blocks_after_a_submission(monkeypatch):
    """The default is unchanged: no acknowledgement -> refused."""
    submitted = SubmittedApplication(path="autonomous", task_id="task_done")
    task_repo, _, started, audit = _install(monkeypatch, submitted=submitted)

    with pytest.raises(HTTPException) as exc:
        agent_api.start_task(_body(), user=FakeUser(), db=FakeDb())

    assert exc.value.detail["reason"] == "application_already_submitted"
    assert task_repo.created == [] and started == []


def test_a_non_conforming_acknowledgement_payload_is_rejected_by_validation():
    """Defence in depth for the frontend hazard where a click event (or any
    other stray object) is passed where an acknowledgement belongs: the schema
    itself refuses anything that isn't a well-formed acknowledgement, so such a
    request can never even reach the guard logic."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        agent_api.StartTaskRequest(
            job_url="https://careers.example.com/jobs/1/apply",
            acknowledge_previous_submission={"nativeEvent": {}, "type": "click"},
        )


def test_an_acknowledgement_requires_a_path():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        agent_api.ReapplyAcknowledgement(task_id="task_done")
