"""
Route-level tests for the §6.4 trust-level endpoints in `app/api/profile.py`
— `trust_level_repository` calls are mocked (its own real-DB behavior is
covered by `automation/tests/test_trust_level_repository.py`); these only
check the route's own validation and wiring, matching the existing
`tests/test_applications_api.py` convention.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api.profile import (
    delete_site_trust_level,
    list_site_trust_levels,
    set_site_trust_level,
    update_automation_settings,
)
from app.models.profile import AutomationSettingsRequest, SiteTrustLevelRequest


def _owned_profile(monkeypatch, **overrides):
    import app.api.profile as profile_module

    defaults = dict(user_id="user-1", autopilot_globally_disabled=False, default_trust_level="FULL_MANUAL_REVIEW")
    defaults.update(overrides)
    fake_profile = MagicMock(**defaults)
    monkeypatch.setattr(profile_module.repo, "get_by_user_id", lambda db, uid: fake_profile)
    return fake_profile


def test_update_automation_settings_rejects_an_invalid_trust_level(monkeypatch):
    _owned_profile(monkeypatch)
    body = AutomationSettingsRequest(autopilot_globally_disabled=False, default_trust_level="not-a-real-level")

    with pytest.raises(HTTPException) as exc_info:
        update_automation_settings(body, user=MagicMock(user_id="user-1"), db=MagicMock())
    assert exc_info.value.status_code == 400


def test_update_automation_settings_passes_a_valid_trust_level_through(monkeypatch):
    import app.api.profile as profile_module

    _owned_profile(monkeypatch)
    calls = []
    monkeypatch.setattr(
        profile_module.repo, "update_automation_settings",
        lambda db, profile, *, autopilot_globally_disabled, default_trust_level=None: calls.append(
            (autopilot_globally_disabled, default_trust_level)
        ),
    )
    body = AutomationSettingsRequest(autopilot_globally_disabled=True, default_trust_level="DRAFT_ONLY")

    update_automation_settings(body, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert calls == [(True, "DRAFT_ONLY")]


def test_update_automation_settings_leaves_trust_level_untouched_when_omitted(monkeypatch):
    import app.api.profile as profile_module

    _owned_profile(monkeypatch)
    calls = []
    monkeypatch.setattr(
        profile_module.repo, "update_automation_settings",
        lambda db, profile, *, autopilot_globally_disabled, default_trust_level=None: calls.append(
            (autopilot_globally_disabled, default_trust_level)
        ),
    )
    body = AutomationSettingsRequest(autopilot_globally_disabled=True)

    update_automation_settings(body, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert calls == [(True, None)]


def test_set_site_trust_level_returns_400_on_an_invalid_value(monkeypatch):
    import app.api.profile as profile_module

    monkeypatch.setattr(
        profile_module.trust_level_repository, "set_trust_level",
        lambda db, uid, domain, level: (_ for _ in ()).throw(ValueError(f"Unknown trust level: {level!r}")),
    )
    body = SiteTrustLevelRequest(trust_level="not-a-real-level")

    with pytest.raises(HTTPException) as exc_info:
        set_site_trust_level("boards.greenhouse.io", body, user=MagicMock(user_id="user-1"), db=MagicMock())
    assert exc_info.value.status_code == 400


def test_set_site_trust_level_forwards_to_the_repository(monkeypatch):
    import app.api.profile as profile_module

    calls = []
    fake_row = MagicMock(domain="boards.greenhouse.io", trust_level="TRUSTED_AUTO_SUBMIT")
    monkeypatch.setattr(
        profile_module.trust_level_repository, "set_trust_level",
        lambda db, uid, domain, level: calls.append((uid, domain, level)) or fake_row,
    )
    body = SiteTrustLevelRequest(trust_level="TRUSTED_AUTO_SUBMIT")

    result = set_site_trust_level("boards.greenhouse.io", body, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert calls == [("user-1", "boards.greenhouse.io", "TRUSTED_AUTO_SUBMIT")]
    assert result is fake_row


def test_list_site_trust_levels_forwards_to_the_repository(monkeypatch):
    import app.api.profile as profile_module

    monkeypatch.setattr(profile_module.trust_level_repository, "list_trust_levels", lambda db, uid: ["row-for", uid])

    result = list_site_trust_levels(user=MagicMock(user_id="user-1"), db=MagicMock())

    assert result == ["row-for", "user-1"]


def test_delete_site_trust_level_returns_204(monkeypatch):
    import app.api.profile as profile_module

    monkeypatch.setattr(profile_module.trust_level_repository, "delete_trust_level", lambda db, uid, domain: True)

    response = delete_site_trust_level("boards.greenhouse.io", user=MagicMock(user_id="user-1"), db=MagicMock())

    assert response.status_code == 204
