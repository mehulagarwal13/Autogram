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

from app.api.automation import decide, get_automation_config, map_fields
from app.models.application import DecideRequest, FieldMapRequest, FieldQuery


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
    monkeypatch.setattr(automation_module.trust_level_repository, "resolve_trust_level", lambda db, uid, url: "FULL_MANUAL_REVIEW")

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

    response = map_fields(body, user=MagicMock(user_id="user-1"), db=MagicMock())
    results = response.fields

    assert [r.question_text for r in results] == ["What's your notice period?", "Why do you want this role?"]
    assert results[0].answer == "2 weeks"
    assert results[0].source == "profile"  # "deterministic" -> "profile" via QUESTION_SOURCE_MAP
    assert results[0].confidence_level == "HIGH"
    assert results[1].answer == ""
    assert results[1].confidence_level == "LOW"
    assert captured["kwargs"]["job_description"] == "Backend role"
    assert captured["kwargs"]["application_id"] == "app-1"
    # One field HIGH (usable), one LOW (not) -> overall_confidence is the
    # usable fraction, same definition ApplicationFlowManager._aggregate_confidence
    # uses — and `action` comes straight from decide_action(), not reimplemented.
    assert response.overall_confidence == 0.5
    assert response.action in ("AUTO_SUBMIT", "NEEDS_REVIEW", "COPILOT_REVIEW")


def test_decide_404s_when_the_application_does_not_belong_to_this_user(monkeypatch):
    import app.api.automation as automation_module

    monkeypatch.setattr(
        automation_module.application_repository, "get_by_id",
        lambda db, aid: MagicMock(application_id="app-1", user_id="someone-else"),
    )
    body = DecideRequest(application_id="app-1", overall_confidence=0.9)

    with pytest.raises(HTTPException) as exc_info:
        decide(body, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert exc_info.value.status_code == 404


def test_decide_calls_the_real_decide_action_with_this_applications_platform_and_autopilot_flag(monkeypatch):
    import app.api.automation as automation_module

    application = MagicMock(application_id="app-1", user_id="user-1", ats_platform="greenhouse", autopilot_enabled=True)
    monkeypatch.setattr(automation_module.application_repository, "get_by_id", lambda db, aid: application)
    # §6.4: AUTO_SUBMIT additionally requires this job's site to be trusted —
    # real trust-level resolution is covered by test_trust_level_repository.py;
    # this test only needs decide() to actually consult and pass it through.
    monkeypatch.setattr(automation_module.trust_level_repository, "resolve_trust_level", lambda db, uid, url: "TRUSTED_AUTO_SUBMIT")

    body = DecideRequest(application_id="app-1", overall_confidence=0.95)
    response = decide(body, user=MagicMock(user_id="user-1"), db=MagicMock())

    # confidence 0.95 + public ATS (greenhouse) + autopilot on + trusted site
    # -> AUTO_SUBMIT, straight from the real decide_action(), never
    # reimplemented here.
    assert response.action == "AUTO_SUBMIT"
    assert response.overall_confidence == 0.95


def test_decide_never_auto_submits_on_an_untrusted_site_even_with_autopilot_on(monkeypatch):
    """The new §6.4 condition, isolated: same setup as the test above except
    the site's trust level, which alone is enough to block AUTO_SUBMIT."""
    import app.api.automation as automation_module

    application = MagicMock(application_id="app-1", user_id="user-1", ats_platform="greenhouse", autopilot_enabled=True)
    monkeypatch.setattr(automation_module.application_repository, "get_by_id", lambda db, aid: application)
    monkeypatch.setattr(automation_module.trust_level_repository, "resolve_trust_level", lambda db, uid, url: "FULL_MANUAL_REVIEW")

    body = DecideRequest(application_id="app-1", overall_confidence=0.95)
    response = decide(body, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert response.action != "AUTO_SUBMIT"


def test_decide_never_auto_submits_without_autopilot_opted_in(monkeypatch):
    import app.api.automation as automation_module

    application = MagicMock(application_id="app-1", user_id="user-1", ats_platform="greenhouse", autopilot_enabled=False)
    monkeypatch.setattr(automation_module.application_repository, "get_by_id", lambda db, aid: application)
    monkeypatch.setattr(automation_module.trust_level_repository, "resolve_trust_level", lambda db, uid, url: "TRUSTED_AUTO_SUBMIT")

    body = DecideRequest(application_id="app-1", overall_confidence=0.95)
    response = decide(body, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert response.action != "AUTO_SUBMIT"


def test_get_config_reports_kill_switch_disengaged_and_real_pacing_values(monkeypatch):
    import app.api.automation as automation_module
    from automation.browser.session import DEFAULT_PACING

    monkeypatch.setattr(
        automation_module.profile_repository, "get_by_user_id",
        lambda db, uid: MagicMock(autopilot_globally_disabled=False),
    )

    config = get_automation_config(user=MagicMock(user_id="user-1"), db=MagicMock())

    assert config.kill_switch_engaged is False
    assert config.pacing.daily_application_cap == DEFAULT_PACING.daily_application_cap
    assert config.pacing.per_char_delay_ms_min == DEFAULT_PACING.per_char_delay_ms_min
    assert config.auto_submit_confidence_threshold == 0.85
    assert config.needs_review_confidence_threshold == 0.6
    assert "greenhouse" in config.public_ats_platforms


def test_get_config_reports_kill_switch_engaged(monkeypatch):
    import app.api.automation as automation_module

    monkeypatch.setattr(
        automation_module.profile_repository, "get_by_user_id",
        lambda db, uid: MagicMock(autopilot_globally_disabled=True),
    )

    config = get_automation_config(user=MagicMock(user_id="user-1"), db=MagicMock())

    assert config.kill_switch_engaged is True


def test_get_config_fails_closed_when_the_kill_switch_check_itself_errors(monkeypatch):
    import app.api.automation as automation_module

    def _broken(db, uid):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(automation_module.profile_repository, "get_by_user_id", _broken)

    config = get_automation_config(user=MagicMock(user_id="user-1"), db=MagicMock())

    assert config.kill_switch_engaged is True
