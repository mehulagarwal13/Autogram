"""
Route-level tests for the §9 data-retention endpoints in
`app/api/profile.py` — `retention_repository`/`retention_service` calls are
mocked (real-DB/real-filesystem behavior is covered by
`automation/tests/test_retention_service.py`); these only check the route's
own validation and wiring, matching the existing `tests/test_applications_
api.py` MagicMock-based convention.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api.profile import get_retention_policy, purge_retention_now, update_retention_policy
from app.models.profile import RetentionPolicyRequest, RetentionPolicyResponse


def _fake_policy(**overrides):
    defaults = dict(screenshot_retention_days=30, run_history_retention_days=90, hitl_request_retention_days=14)
    defaults.update(overrides)
    return MagicMock(spec=["screenshot_retention_days", "run_history_retention_days", "hitl_request_retention_days"], **defaults)


def test_retention_policy_request_and_response_have_no_document_retention_field():
    """§9 deliberately has no `document_retention_days` field anywhere in
    the API surface — there is no per-application generated résumé/cover-
    letter in this codebase to purge on a schedule (see `RetentionPolicy`'s
    own docstring and `retention_service.py`'s module docstring for the
    full reasoning). This checks the actual Pydantic field declarations
    that generate the OpenAPI schema — the thing an API consumer actually
    sees — as a schema-level guard against it being silently added back
    without also adding real enforcement."""
    assert "document_retention_days" not in RetentionPolicyRequest.model_fields
    assert "document_retention_days" not in RetentionPolicyResponse.model_fields


def test_get_retention_policy_returns_the_resolved_policy(monkeypatch):
    import app.api.profile as profile_module

    monkeypatch.setattr(profile_module.retention_repository, "get_policy", lambda db, uid: _fake_policy())

    result = get_retention_policy(user=MagicMock(user_id="user-1"), db=MagicMock())

    assert result.screenshot_retention_days == 30
    assert result.run_history_retention_days == 90
    assert result.hitl_request_retention_days == 14


@pytest.mark.parametrize("field", ["screenshot_retention_days", "run_history_retention_days", "hitl_request_retention_days"])
def test_update_retention_policy_rejects_a_window_below_one_day(field):
    body = RetentionPolicyRequest(**{field: 0})

    with pytest.raises(HTTPException) as exc_info:
        update_retention_policy(body, user=MagicMock(user_id="user-1"), db=MagicMock())
    assert exc_info.value.status_code == 400


def test_update_retention_policy_forwards_only_the_provided_fields(monkeypatch):
    import app.api.profile as profile_module

    calls = []
    monkeypatch.setattr(
        profile_module.retention_repository, "update_policy",
        lambda db, uid, **kwargs: calls.append(kwargs) or _fake_policy(**{k: v for k, v in kwargs.items() if v is not None}),
    )
    body = RetentionPolicyRequest(screenshot_retention_days=5)

    result = update_retention_policy(body, user=MagicMock(user_id="user-1"), db=MagicMock())

    assert calls == [{"screenshot_retention_days": 5, "run_history_retention_days": None, "hitl_request_retention_days": None}]
    assert result.screenshot_retention_days == 5


def test_purge_retention_now_forwards_to_the_service_scoped_to_this_user(monkeypatch):
    import app.api.profile as profile_module

    calls = []
    fake_results = [{"category": "screenshots", "records_purged": 2, "files_deleted": 2, "files_failed": 0, "error": None}]
    monkeypatch.setattr(
        profile_module.retention_service, "run_purge_for_user",
        lambda db, uid: calls.append(uid) or fake_results,
    )

    result = purge_retention_now(user=MagicMock(user_id="user-1"), db=MagicMock())

    assert calls == ["user-1"]
    assert result.results[0].records_purged == 2
