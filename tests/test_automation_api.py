"""
Tests for app/api/automation.py::map_fields — the browser extension's one new
endpoint, a thin wrapper around `ApplicationAnswerEngine` (mocked here; its
own behavior is covered by automation/tests/*answer_engine*). These tests
only verify the wiring: ownership, request/response shape, and that results
come back in the same order as the request.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api.automation import map_fields
from app.models.application import FieldMapRequest, FieldQuery


def _fake_answer_result(question, answer, source, confidence):
    return MagicMock(question=question, answer=answer, source=source, confidence=confidence)


def test_map_fields_404s_when_the_application_does_not_belong_to_this_user(monkeypatch):
    import app.api.automation as automation_module

    monkeypatch.setattr(
        automation_module.application_repository, "get_by_id",
        lambda db, aid: MagicMock(application_id="app-1", user_id="someone-else"),
    )
    body = FieldMapRequest(application_id="app-1", fields=[FieldQuery(question_text="Notice period?")])

    with pytest.raises(HTTPException) as exc_info:
        map_fields(body, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert exc_info.value.status_code == 404


def test_map_fields_400s_when_the_user_has_no_profile(monkeypatch):
    import app.api.automation as automation_module

    monkeypatch.setattr(
        automation_module.application_repository, "get_by_id",
        lambda db, aid: MagicMock(application_id="app-1", user_id="user-1"),
    )
    monkeypatch.setattr(automation_module.profile_repository, "get_by_user_id", lambda db, uid: None)
    body = FieldMapRequest(application_id="app-1", fields=[FieldQuery(question_text="Notice period?")])

    with pytest.raises(HTTPException) as exc_info:
        map_fields(body, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert exc_info.value.status_code == 400


def test_map_fields_returns_one_result_per_field_in_order(monkeypatch):
    import app.api.automation as automation_module

    monkeypatch.setattr(
        automation_module.application_repository, "get_by_id",
        lambda db, aid: MagicMock(application_id="app-1", user_id="user-1"),
    )
    monkeypatch.setattr(automation_module.profile_repository, "get_by_user_id", lambda db, uid: MagicMock())

    captured = {}

    class _FakeEngine:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs
            self.current_page_number = None

        def answer_batch(self, questions):
            captured["questions"] = list(questions)
            return [
                _fake_answer_result("What's your notice period?", "2 weeks", "deterministic", 0.95),
                _fake_answer_result("Why do you want this role?", "", "llm", 0.0),
            ]

    monkeypatch.setattr(automation_module, "ApplicationAnswerEngine", _FakeEngine)

    body = FieldMapRequest(
        application_id="app-1",
        job_description="Backend role",
        page_number=2,
        fields=[
            FieldQuery(question_text="What's your notice period?", field_type="text"),
            FieldQuery(question_text="Why do you want this role?", field_type="textarea"),
        ],
    )

    results = map_fields(body, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert [r.question_text for r in results] == ["What's your notice period?", "Why do you want this role?"]
    assert results[0].answer == "2 weeks"
    assert results[0].source == "profile"  # "deterministic" -> "profile" via QUESTION_SOURCE_MAP
    assert results[0].confidence_level == "HIGH"
    assert results[1].answer == ""
    assert results[1].confidence_level == "LOW"
    assert captured["kwargs"]["job_description"] == "Backend role"
    assert captured["kwargs"]["application_id"] == "app-1"
