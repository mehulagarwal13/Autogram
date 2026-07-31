"""
Tests for app/api/applications.py's pure-logic helper, `_pick_resume_document_id`
(the explicit-override vs. auto-picked-default resume selection used by
`POST /applications/start`). `profile_repository` calls are mocked — no live
DB needed, matching this repo's existing test conventions (see
tests/test_profile_repository_helpers.py).
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException, Response

from app.api.applications import _pick_resume_document_id, start_application


def test_pick_resume_document_id_uses_explicit_override_when_it_belongs_to_the_profile():
    fake_doc = MagicMock(document_id="doc-1", profile_id="profile-1", document_type="resume")
    with patch("app.api.applications.profile_repository.get_document", return_value=fake_doc):
        result = _pick_resume_document_id(db=MagicMock(), profile_id="profile-1", requested_document_id="doc-1")
    assert result == "doc-1"


def test_pick_resume_document_id_rejects_an_override_belonging_to_another_profile():
    fake_doc = MagicMock(document_id="doc-1", profile_id="someone-elses-profile", document_type="resume")
    with patch("app.api.applications.profile_repository.get_document", return_value=fake_doc):
        with pytest.raises(HTTPException) as exc_info:
            _pick_resume_document_id(db=MagicMock(), profile_id="profile-1", requested_document_id="doc-1")
    assert exc_info.value.status_code == 400


def test_pick_resume_document_id_rejects_a_non_resume_document_override():
    fake_doc = MagicMock(document_id="doc-1", profile_id="profile-1", document_type="cover_letter")
    with patch("app.api.applications.profile_repository.get_document", return_value=fake_doc):
        with pytest.raises(HTTPException):
            _pick_resume_document_id(db=MagicMock(), profile_id="profile-1", requested_document_id="doc-1")


def test_pick_resume_document_id_falls_back_to_the_default_resume():
    default_doc = MagicMock(document_id="doc-default", is_default=True)
    other_doc = MagicMock(document_id="doc-other", is_default=False)
    with patch("app.api.applications.profile_repository.list_documents", return_value=[other_doc, default_doc]):
        result = _pick_resume_document_id(db=MagicMock(), profile_id="profile-1", requested_document_id=None)
    assert result == "doc-default"


def test_pick_resume_document_id_falls_back_to_the_first_resume_when_none_is_flagged_default():
    only_doc = MagicMock(document_id="doc-only", is_default=False)
    with patch("app.api.applications.profile_repository.list_documents", return_value=[only_doc]):
        result = _pick_resume_document_id(db=MagicMock(), profile_id="profile-1", requested_document_id=None)
    assert result == "doc-only"


def test_pick_resume_document_id_returns_none_when_the_profile_has_no_resumes():
    with patch("app.api.applications.profile_repository.list_documents", return_value=[]):
        result = _pick_resume_document_id(db=MagicMock(), profile_id="profile-1", requested_document_id=None)
    assert result is None


# ---------------------------------------------------------------------------
# start_application: RETRYABLE_STATUSES (failed/manual_required/needs_review)
# retry on the same row; IN_PROGRESS_STATUSES/COMPLETED_STATUSES are rejected
# with 409 instead of silently no-op'ing.
# ---------------------------------------------------------------------------

def _fake_start_body(**overrides):
    defaults = dict(
        job_url="https://boards.greenhouse.io/acme/jobs/123",
        autopilot_enabled=True,
        company=None,
        position=None,
        resume_document_id=None,
        job_description=None,
    )
    defaults.update(overrides)
    return MagicMock(**defaults)


@pytest.mark.parametrize("retryable_status", ["failed", "manual_required", "needs_review"])
def test_start_application_retries_a_retryable_application_on_the_same_row(monkeypatch, retryable_status):
    import app.api.applications as applications_module

    fake_existing = MagicMock(application_id="app-1", status=retryable_status)
    fake_profile = MagicMock(profile_id="profile-1")
    fake_resume = MagicMock(document_id="doc-1", is_default=True)
    fake_retried = MagicMock(application_id="app-1", status="pending")

    monkeypatch.setattr(applications_module.application_repository, "get_by_user_and_url", lambda db, uid, url: fake_existing)
    monkeypatch.setattr(applications_module.profile_repository, "get_by_user_id", lambda db, uid: fake_profile)
    monkeypatch.setattr(applications_module.profile_repository, "list_documents", lambda db, pid, document_type=None: [fake_resume])
    retry_calls = {}

    def _fake_retry_application(db, app, **kw):
        retry_calls["app"] = app
        retry_calls["kwargs"] = kw
        return fake_retried

    create_calls = []
    monkeypatch.setattr(applications_module.application_repository, "retry_application", _fake_retry_application)
    monkeypatch.setattr(
        applications_module.application_repository, "create_application",
        lambda *a, **kw: create_calls.append((a, kw)),
    )

    background_tasks = BackgroundTasks()
    response = Response(status_code=599)
    body = _fake_start_body(company="Acme", position="Backend Engineer")

    result = start_application(body, background_tasks, response, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert result is fake_retried
    assert retry_calls["app"] is fake_existing
    assert retry_calls["kwargs"] == {"company": "Acme", "position": "Backend Engineer", "autopilot_enabled": True}
    assert not create_calls  # a retry must never insert a second row for this job
    assert response.status_code == 599  # left at 202 default, not forced to 200/409
    assert len(background_tasks.tasks) == 1
    assert background_tasks.tasks[0].args[0] == "app-1"


@pytest.mark.parametrize("in_progress_status", ["pending", "processing", "copilot_review"])
def test_start_application_rejects_an_in_progress_application_with_409(monkeypatch, in_progress_status):
    import app.api.applications as applications_module

    fake_existing = MagicMock(application_id="app-1", status=in_progress_status)
    monkeypatch.setattr(applications_module.application_repository, "get_by_user_and_url", lambda db, uid, url: fake_existing)

    background_tasks = BackgroundTasks()
    body = _fake_start_body()

    with pytest.raises(HTTPException) as exc_info:
        start_application(body, background_tasks, Response(), user=MagicMock(user_id="user-1"), db=MagicMock())

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Application is already in progress."
    assert not background_tasks.tasks  # nothing new started


def test_start_application_rejects_a_completed_application_with_409(monkeypatch):
    import app.api.applications as applications_module

    fake_existing = MagicMock(application_id="app-1", status="applied")
    monkeypatch.setattr(applications_module.application_repository, "get_by_user_and_url", lambda db, uid, url: fake_existing)

    background_tasks = BackgroundTasks()
    body = _fake_start_body()

    with pytest.raises(HTTPException) as exc_info:
        start_application(body, background_tasks, Response(), user=MagicMock(user_id="user-1"), db=MagicMock())

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Application has already been completed."
    assert not background_tasks.tasks  # nothing new started


class _FakeApplication:
    """Stands in for the `Application` DB row — just the fields
    `start_application`/`_run_application` actually read or write."""

    def __init__(self, application_id, user_id, job_url, autopilot_enabled):
        self.application_id = application_id
        self.user_id = user_id
        self.job_url = job_url
        self.autopilot_enabled = autopilot_enabled
        self.status = "pending"
        self.failure_reason = None
        self.resume_used = None
        self.ats_platform = None
        self.confidence_score = None
        self.applied_date = None
        self.company = None
        self.position = None


def _run_enqueued_background_task(background_tasks):
    """Runs the one task `start_application` just enqueued, synchronously —
    stands in for Starlette actually dispatching it after the response is
    sent (see `starlette.background.BackgroundTask.__call__`)."""
    task = background_tasks.tasks[0]
    task.func(*task.args, **task.kwargs)


def test_start_application_retries_after_a_failure_and_stops_retrying_once_applied(monkeypatch):
    """End-to-end (collaborators mocked, no real DB/browser/LLM): apply for a
    job, let the run fail, hit /applications/start again for the SAME job —
    it must retry on the same row and actually re-run ApplicationFlowManager,
    not just re-return the failed row. Once a retry succeeds, calling start
    again must go back to being a no-op (idempotent), not retry forever."""
    import app.api.applications as applications_module

    store: dict[str, _FakeApplication] = {}

    def fake_create_application(db, user_id, job_url, *, autopilot_enabled=False, company=None, position=None):
        app = _FakeApplication("app-1", user_id, job_url, autopilot_enabled)
        store[job_url] = app
        return app

    def fake_get_by_user_and_url(db, user_id, job_url):
        return store.get(job_url)

    def fake_get_by_id(db, application_id):
        return next((a for a in store.values() if a.application_id == application_id), None)

    def fake_mark_processing(db, app):
        app.status = "processing"
        return app

    def fake_retry_application(db, app, *, company, position, autopilot_enabled):
        app.status = "pending"
        app.failure_reason = None
        app.confidence_score = 0.0
        app.company = company
        app.position = position
        app.autopilot_enabled = autopilot_enabled
        return app

    def fake_apply_run_result(db, app, result):
        app.status = result.status
        app.confidence_score = result.confidence
        app.failure_reason = result.error_log if result.status == "failed" else None
        if result.status == "applied":
            app.applied_date = "now"
        return app

    monkeypatch.setattr(applications_module.application_repository, "create_application", fake_create_application)
    monkeypatch.setattr(applications_module.application_repository, "get_by_user_and_url", fake_get_by_user_and_url)
    monkeypatch.setattr(applications_module.application_repository, "get_by_id", fake_get_by_id)
    monkeypatch.setattr(applications_module.application_repository, "mark_processing", fake_mark_processing)
    monkeypatch.setattr(applications_module.application_repository, "retry_application", fake_retry_application)
    monkeypatch.setattr(applications_module.application_repository, "apply_run_result", fake_apply_run_result)

    monkeypatch.setattr(applications_module, "SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(applications_module, "detect_ats_for_url", lambda url: {"ats": "greenhouse", "confidence": 0.9})
    monkeypatch.setattr(applications_module, "get_adapter_class", lambda ats: MagicMock())
    monkeypatch.setattr(applications_module.profile_repository, "get_by_user_id", lambda db, uid: MagicMock(profile_id="profile-1"))
    monkeypatch.setattr(applications_module.profile_repository, "get_document", lambda db, doc_id: MagicMock(document_id=doc_id))
    monkeypatch.setattr(applications_module.profile_repository, "list_documents", lambda db, pid, document_type=None: [MagicMock(document_id="doc-1", is_default=True)])
    monkeypatch.setattr(applications_module, "ApplicationAnswerEngine", lambda **kw: MagicMock())
    monkeypatch.setattr(applications_module, "_run_on_dedicated_thread", lambda fn: fn())

    run_attempts = {"count": 0}

    class _FakeFlowManager:
        def __init__(self, **kwargs):
            pass

        def run(self):
            run_attempts["count"] += 1
            if run_attempts["count"] == 1:
                return MagicMock(status="failed", ats_platform="greenhouse", confidence=0.0, screenshot_paths=[], trace_path=None, error_log="boom")
            return MagicMock(status="applied", ats_platform="greenhouse", confidence=0.95, screenshot_paths=[], trace_path=None, error_log=None)

    monkeypatch.setattr(applications_module, "ApplicationFlowManager", _FakeFlowManager)

    body = _fake_start_body(job_url="https://boards.greenhouse.io/acme/jobs/999")
    user = MagicMock(user_id="user-1")

    # 1st call: brand new application, and its run fails.
    bg1, response1 = BackgroundTasks(), Response(status_code=599)
    application = start_application(body, bg1, response1, user=user, db=MagicMock())
    _run_enqueued_background_task(bg1)
    assert application.status == "failed"
    assert run_attempts["count"] == 1

    # 2nd call, same job_url: must retry the SAME row (not create a second
    # one) and actually run the flow manager again — this time it succeeds.
    bg2, response2 = BackgroundTasks(), Response(status_code=599)
    retried = start_application(body, bg2, response2, user=user, db=MagicMock())
    assert retried is application
    assert retried.application_id == "app-1"
    assert len(store) == 1  # still exactly one row for this job
    _run_enqueued_background_task(bg2)
    assert retried.status == "applied"
    assert run_attempts["count"] == 2

    # 3rd call, now that it's applied: must be rejected outright, not retried.
    bg3 = BackgroundTasks()
    with pytest.raises(HTTPException) as exc_info:
        start_application(body, bg3, Response(), user=user, db=MagicMock())
    assert exc_info.value.status_code == 409
    assert not bg3.tasks
    assert run_attempts["count"] == 2  # no third run triggered


# ---------------------------------------------------------------------------
# Phase 6: _run_application builds an ApplicationAnswerEngine and threads
# `job_description` through to it, then passes that engine into
# ApplicationFlowManager. Every collaborator is mocked/monkeypatched — this
# only verifies the wiring in app/api/applications.py itself, not any real
# ATS/browser/LLM behavior (covered elsewhere).
# ---------------------------------------------------------------------------

def test_run_application_builds_an_answer_engine_with_the_job_description_and_passes_it_to_the_flow_manager(monkeypatch):
    import app.api.applications as applications_module

    fake_application = MagicMock(application_id="app-1", user_id="user-1", autopilot_enabled=False)
    fake_profile = MagicMock()
    fake_resume_document = MagicMock(document_id="doc-1")
    fake_db = MagicMock()

    monkeypatch.setattr(applications_module, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(applications_module.application_repository, "get_by_id", lambda db, aid: fake_application)
    monkeypatch.setattr(applications_module.application_repository, "mark_processing", lambda db, app: fake_application)
    monkeypatch.setattr(applications_module.application_repository, "apply_run_result", lambda db, app, result: None)
    monkeypatch.setattr(applications_module, "detect_ats_for_url", lambda url: {"ats": "greenhouse", "confidence": 0.9})
    monkeypatch.setattr(applications_module, "get_adapter_class", lambda ats: MagicMock())
    monkeypatch.setattr(applications_module.profile_repository, "get_by_user_id", lambda db, uid: fake_profile)
    monkeypatch.setattr(applications_module.profile_repository, "get_document", lambda db, doc_id: fake_resume_document)

    captured = {}

    class _FakeEngine:
        def __init__(self, **kwargs):
            captured["engine_kwargs"] = kwargs
            captured["engine_instance"] = self

    monkeypatch.setattr(applications_module, "ApplicationAnswerEngine", _FakeEngine)

    class _FakeManager:
        def __init__(self, **kwargs):
            captured["manager_kwargs"] = kwargs

        def run(self):
            return MagicMock()

    monkeypatch.setattr(applications_module, "ApplicationFlowManager", _FakeManager)
    # Runs `manager.run` synchronously instead of on a real dedicated thread —
    # these tests only check wiring, not threading behavior.
    monkeypatch.setattr(applications_module, "_run_on_dedicated_thread", lambda fn: fn())

    applications_module._run_application("app-1", "doc-1", "We are hiring a backend engineer.")

    assert captured["engine_kwargs"]["profile"] is fake_profile
    assert captured["engine_kwargs"]["job_description"] == "We are hiring a backend engineer."
    assert captured["engine_kwargs"]["db"] is fake_db
    assert captured["engine_kwargs"]["user_id"] == "user-1"
    assert captured["manager_kwargs"]["answer_engine"] is captured["engine_instance"]


def test_run_application_still_runs_when_building_the_answer_engine_fails(monkeypatch):
    """A broken ApplicationAnswerEngine construction is a best-effort
    enhancement failing, not a reason to fail the whole application run —
    ApplicationFlowManager must still get called, just with answer_engine=None."""
    import app.api.applications as applications_module

    fake_application = MagicMock(application_id="app-1", user_id="user-1", autopilot_enabled=False)
    fake_resume_document = MagicMock(document_id="doc-1")
    fake_db = MagicMock()

    monkeypatch.setattr(applications_module, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(applications_module.application_repository, "get_by_id", lambda db, aid: fake_application)
    monkeypatch.setattr(applications_module.application_repository, "mark_processing", lambda db, app: fake_application)
    monkeypatch.setattr(applications_module.application_repository, "apply_run_result", lambda db, app, result: None)
    monkeypatch.setattr(applications_module, "detect_ats_for_url", lambda url: {"ats": "greenhouse", "confidence": 0.9})
    monkeypatch.setattr(applications_module, "get_adapter_class", lambda ats: MagicMock())
    monkeypatch.setattr(applications_module.profile_repository, "get_by_user_id", lambda db, uid: MagicMock())
    monkeypatch.setattr(applications_module.profile_repository, "get_document", lambda db, doc_id: fake_resume_document)

    def _broken_engine(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(applications_module, "ApplicationAnswerEngine", _broken_engine)

    captured = {}

    class _FakeManager:
        def __init__(self, **kwargs):
            captured["manager_kwargs"] = kwargs

        def run(self):
            return MagicMock()

    monkeypatch.setattr(applications_module, "ApplicationFlowManager", _FakeManager)
    # Runs `manager.run` synchronously instead of on a real dedicated thread —
    # these tests only check wiring, not threading behavior.
    monkeypatch.setattr(applications_module, "_run_on_dedicated_thread", lambda fn: fn())

    applications_module._run_application("app-1", "doc-1")  # must not raise

    assert captured["manager_kwargs"]["answer_engine"] is None
