"""
Tests for app/api/applications.py's pure-logic helper, `_pick_resume_document_id`
(the explicit-override vs. auto-picked-default resume selection used by
`POST /applications/start`). `profile_repository` calls are mocked — no live
DB needed, matching this repo's existing test conventions (see
tests/test_profile_repository_helpers.py).
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.applications import _pick_resume_document_id


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
# Phase 6: _run_application builds an ApplicationAnswerEngine and threads
# `job_description` through to it, then passes that engine into
# ApplicationFlowManager. Every collaborator is mocked/monkeypatched — this
# only verifies the wiring in app/api/applications.py itself, not any real
# ATS/browser/LLM behavior (covered elsewhere).
# ---------------------------------------------------------------------------

class _ImmediateFuture:
    """Stands in for the Future concurrent.futures.ThreadPoolExecutor.submit()
    returns — runs `fn` synchronously so this test needs no real thread."""

    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


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
    monkeypatch.setattr(applications_module._PLAYWRIGHT_EXECUTOR, "submit", lambda fn: _ImmediateFuture(fn()))

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
    monkeypatch.setattr(applications_module._PLAYWRIGHT_EXECUTOR, "submit", lambda fn: _ImmediateFuture(fn()))

    applications_module._run_application("app-1", "doc-1")  # must not raise

    assert captured["manager_kwargs"]["answer_engine"] is None
