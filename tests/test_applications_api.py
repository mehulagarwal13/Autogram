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

from app.api.applications import (
    _pick_resume_document_id,
    approve_application,
    check_duplicate_application,
    get_applications_overview,
    list_applications_needing_review,
    reject_application,
    report_application_status,
    review_application_question,
    start_application,
)


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
        source="server_automation",
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
    assert retry_calls["kwargs"] == {
        "company": "Acme", "position": "Backend Engineer", "autopilot_enabled": True, "source": "server_automation",
    }
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
        self.source = "server_automation"


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

    def fake_create_application(db, user_id, job_url, *, autopilot_enabled=False, company=None, position=None, source="server_automation"):
        app = _FakeApplication("app-1", user_id, job_url, autopilot_enabled)
        app.source = source
        store[job_url] = app
        return app

    def fake_get_by_user_and_url(db, user_id, job_url):
        return store.get(job_url)

    def fake_get_by_id(db, application_id):
        return next((a for a in store.values() if a.application_id == application_id), None)

    def fake_mark_processing(db, app):
        app.status = "processing"
        return app

    def fake_retry_application(db, app, *, company, position, autopilot_enabled, source="server_automation"):
        app.status = "pending"
        app.failure_reason = None
        app.confidence_score = 0.0
        app.company = company
        app.position = position
        app.autopilot_enabled = autopilot_enabled
        app.source = source
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


# ---------------------------------------------------------------------------
# HITL platform — dashboard, review queue, duplicate check, question review,
# approve/reject. Every collaborator is monkeypatched; no live DB/browser.
# ---------------------------------------------------------------------------

def test_get_applications_overview_delegates_to_the_repository(monkeypatch):
    import app.api.applications as applications_module

    fake_counts = {"total": 3, "submitted": 1, "in_progress": 1, "waiting_for_human": 0, "waiting_for_review": 1, "failed": 0, "cancelled": 0}
    monkeypatch.setattr(applications_module.application_repository, "get_overview_counts", lambda db, uid: fake_counts)

    result = get_applications_overview(user=MagicMock(user_id="user-1"), db=MagicMock())

    assert result == fake_counts


def test_list_applications_needing_review_delegates_to_the_repository(monkeypatch):
    import app.api.applications as applications_module

    fake_apps = [MagicMock(), MagicMock()]
    monkeypatch.setattr(applications_module.application_repository, "list_reviews_for_user", lambda db, uid: fake_apps)

    result = list_applications_needing_review(user=MagicMock(user_id="user-1"), db=MagicMock())

    assert result is fake_apps


def test_check_duplicate_application_reports_no_duplicate_when_none_found(monkeypatch):
    import app.api.applications as applications_module

    monkeypatch.setattr(applications_module.application_repository, "find_possible_duplicate", lambda *a, **kw: None)

    result = check_duplicate_application(company="Acme", position="Engineer", user=MagicMock(user_id="user-1"), db=MagicMock())

    assert result.possible_duplicate is False
    assert result.existing_application_id is None


def test_check_duplicate_application_reports_the_existing_attempt(monkeypatch):
    import app.api.applications as applications_module

    fake_existing = MagicMock(application_id="app-1", status="needs_review")
    monkeypatch.setattr(applications_module.application_repository, "find_possible_duplicate", lambda *a, **kw: fake_existing)

    result = check_duplicate_application(company="Acme", position="Engineer", user=MagicMock(user_id="user-1"), db=MagicMock())

    assert result.possible_duplicate is True
    assert result.existing_application_id == "app-1"
    assert result.existing_status == "needs_review"


def _owned_application(monkeypatch, **overrides):
    """Patches `application_repository.get_by_id` so `_get_owned_application`
    resolves to a fake row owned by `user-1` — the shared setup every
    ownership-checked HITL route below needs."""
    import app.api.applications as applications_module

    defaults = dict(application_id="app-1", user_id="user-1", status="copilot_review")
    defaults.update(overrides)
    fake_application = MagicMock(**defaults)
    monkeypatch.setattr(applications_module.application_repository, "get_by_id", lambda db, aid: fake_application)
    return fake_application


def test_review_question_404s_when_the_question_does_not_belong_to_this_application(monkeypatch):
    import app.api.applications as applications_module
    from app.models.application import QuestionReviewRequest

    _owned_application(monkeypatch)
    other_apps_question = MagicMock(application_id="some-other-app")
    monkeypatch.setattr(applications_module.application_question_repository, "get", lambda db, qid: other_apps_question)

    with pytest.raises(HTTPException) as exc_info:
        review_application_question(
            "app-1", "q-1", QuestionReviewRequest(action="approve"),
            user=MagicMock(user_id="user-1"), db=MagicMock(),
        )
    assert exc_info.value.status_code == 404


def test_review_question_approve_caches_the_answer_for_future_applications(monkeypatch):
    import app.api.applications as applications_module
    from app.models.application import QuestionReviewRequest

    application = _owned_application(monkeypatch)
    fake_question = MagicMock(application_id="app-1", question_text="Notice period?", answer="30 days", human_answer=None)
    monkeypatch.setattr(applications_module.application_question_repository, "get", lambda db, qid: fake_question)
    monkeypatch.setattr(applications_module.application_question_repository, "apply_review", lambda db, q, **kw: fake_question)
    save_calls = []
    monkeypatch.setattr(
        applications_module.answer_cache_repository, "save_answer",
        lambda db, uid, text, **kw: save_calls.append((uid, text, kw)),
    )

    result = review_application_question(
        "app-1", "q-1", QuestionReviewRequest(action="approve"),
        user=MagicMock(user_id="user-1"), db=MagicMock(),
    )

    assert result is fake_question
    assert len(save_calls) == 1
    assert save_calls[0][0] == "user-1"
    assert save_calls[0][1] == "Notice period?"
    assert save_calls[0][2]["answer"] == "30 days"


def test_review_question_reject_never_touches_the_answer_cache(monkeypatch):
    import app.api.applications as applications_module
    from app.models.application import QuestionReviewRequest

    _owned_application(monkeypatch)
    fake_question = MagicMock(application_id="app-1", answer=None, human_answer=None)
    monkeypatch.setattr(applications_module.application_question_repository, "get", lambda db, qid: fake_question)
    monkeypatch.setattr(applications_module.application_question_repository, "apply_review", lambda db, q, **kw: fake_question)
    save_calls = []
    monkeypatch.setattr(
        applications_module.answer_cache_repository, "save_answer",
        lambda db, uid, text, **kw: save_calls.append((uid, text, kw)),
    )

    review_application_question(
        "app-1", "q-1", QuestionReviewRequest(action="reject"),
        user=MagicMock(user_id="user-1"), db=MagicMock(),
    )

    assert save_calls == []


def test_approve_application_requires_copilot_review_status(monkeypatch):
    _owned_application(monkeypatch, status="processing")

    with pytest.raises(HTTPException) as exc_info:
        approve_application("app-1", user=MagicMock(user_id="user-1"), db=MagicMock())
    assert exc_info.value.status_code == 409


def test_approve_application_404s_when_no_review_session_is_open(monkeypatch):
    import app.api.applications as applications_module

    _owned_application(monkeypatch, status="copilot_review")
    monkeypatch.setattr(applications_module, "submit_open_review_session", lambda aid: None)

    with pytest.raises(HTTPException) as exc_info:
        approve_application("app-1", user=MagicMock(user_id="user-1"), db=MagicMock())
    assert exc_info.value.status_code == 409


def test_approve_application_persists_a_confirmed_submission(monkeypatch):
    import app.api.applications as applications_module

    application = _owned_application(monkeypatch, status="copilot_review")
    monkeypatch.setattr(applications_module, "submit_open_review_session", lambda aid: ("applied", None))
    monkeypatch.setattr(applications_module, "_record_audit_event", lambda *a, **kw: None)

    result = approve_application("app-1", user=MagicMock(user_id="user-1"), db=MagicMock())

    assert result.status == "applied"
    assert application.status == "applied"
    assert application.applied_date is not None
    assert application.failure_reason is None


def test_approve_application_persists_an_unconfirmed_submission_as_needs_review(monkeypatch):
    import app.api.applications as applications_module

    application = _owned_application(monkeypatch, status="copilot_review")
    monkeypatch.setattr(
        applications_module, "submit_open_review_session",
        lambda aid: ("needs_review", "Submit was clicked but no confirmation could be detected."),
    )
    monkeypatch.setattr(applications_module, "_record_audit_event", lambda *a, **kw: None)

    result = approve_application("app-1", user=MagicMock(user_id="user-1"), db=MagicMock())

    assert result.status == "needs_review"
    assert application.status == "needs_review"
    assert application.failure_reason is not None


def test_reject_application_closes_the_review_session_and_cancels(monkeypatch):
    import app.api.applications as applications_module

    application = _owned_application(monkeypatch, status="copilot_review")
    close_calls = []
    monkeypatch.setattr(applications_module, "close_review_session", lambda aid: close_calls.append(aid))
    monkeypatch.setattr(
        applications_module.application_repository, "mark_cancelled",
        lambda db, app, reason=None: (setattr(app, "status", "cancelled"), setattr(app, "failure_reason", reason), app)[-1],
    )
    monkeypatch.setattr(applications_module, "_record_audit_event", lambda *a, **kw: None)

    result = reject_application("app-1", reason="Changed my mind", user=MagicMock(user_id="user-1"), db=MagicMock())

    assert close_calls == ["app-1"]
    assert result.status == "cancelled"
    assert result.failure_reason == "Changed my mind"


# ---------------------------------------------------------------------------
# Browser extension: source="browser_extension" skips the server-side
# Playwright dispatch entirely; report-status is how it self-reports progress
# since there is no server-side run to derive that from.
# ---------------------------------------------------------------------------

def test_start_application_skips_background_dispatch_for_browser_extension_source(monkeypatch):
    import app.api.applications as applications_module

    fake_profile = MagicMock(profile_id="profile-1")
    fake_resume = MagicMock(document_id="doc-1", is_default=True)
    fake_application = MagicMock(application_id="app-1", source="browser_extension")

    monkeypatch.setattr(applications_module.application_repository, "get_by_user_and_url", lambda db, uid, url: None)
    monkeypatch.setattr(applications_module.profile_repository, "get_by_user_id", lambda db, uid: fake_profile)
    monkeypatch.setattr(applications_module.profile_repository, "list_documents", lambda db, pid, document_type=None: [fake_resume])
    create_calls = []
    monkeypatch.setattr(
        applications_module.application_repository, "create_application",
        lambda *a, **kw: (create_calls.append(kw), fake_application)[-1],
    )

    background_tasks = BackgroundTasks()
    body = _fake_start_body(source="browser_extension")

    result = start_application(body, background_tasks, Response(), user=MagicMock(user_id="user-1"), db=MagicMock())

    assert result is fake_application
    assert create_calls[0]["source"] == "browser_extension"
    assert not background_tasks.tasks  # no server-side Playwright run dispatched


def test_start_application_rejects_an_invalid_source(monkeypatch):
    import app.api.applications as applications_module

    monkeypatch.setattr(applications_module.application_repository, "get_by_user_and_url", lambda db, uid, url: None)
    background_tasks = BackgroundTasks()
    body = _fake_start_body(source="not-a-real-source")

    with pytest.raises(HTTPException) as exc_info:
        start_application(body, background_tasks, Response(), user=MagicMock(user_id="user-1"), db=MagicMock())

    assert exc_info.value.status_code == 400
    assert not background_tasks.tasks


def test_report_application_status_updates_the_row_and_records_an_audit_event(monkeypatch):
    import app.api.applications as applications_module

    application = _owned_application(monkeypatch, source="browser_extension")
    audit_calls = []
    monkeypatch.setattr(applications_module, "_record_audit_event", lambda *a, **kw: audit_calls.append(kw))

    def fake_report_status(db, app, *, status, reason=None, confidence=None):
        app.status = status
        app.failure_reason = reason
        app.confidence_score = confidence
        return app

    monkeypatch.setattr(applications_module.application_repository, "report_status", fake_report_status)

    from app.models.application import ReportStatusRequest
    body = ReportStatusRequest(status="manual_required", reason="CAPTCHA on the page", confidence=0.0)

    result = report_application_status("app-1", body, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert result.status == "manual_required"
    assert result.failure_reason == "CAPTCHA on the page"
    assert audit_calls[0]["event_type"] == "extension_status_reported"


def test_report_application_status_rejects_an_invalid_status(monkeypatch):
    import app.api.applications as applications_module

    _owned_application(monkeypatch)

    def fake_report_status(db, app, *, status, reason=None, confidence=None):
        raise ValueError(f"Invalid status {status!r}.")

    monkeypatch.setattr(applications_module.application_repository, "report_status", fake_report_status)

    from app.models.application import ReportStatusRequest
    body = ReportStatusRequest(status="not-a-real-status")

    with pytest.raises(HTTPException) as exc_info:
        report_application_status("app-1", body, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert exc_info.value.status_code == 400


def test_report_application_status_404s_for_an_unowned_application(monkeypatch):
    import app.api.applications as applications_module

    monkeypatch.setattr(applications_module.application_repository, "get_by_id", lambda db, aid: None)

    from app.models.application import ReportStatusRequest
    body = ReportStatusRequest(status="applied")

    with pytest.raises(HTTPException) as exc_info:
        report_application_status("app-1", body, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert exc_info.value.status_code == 404
