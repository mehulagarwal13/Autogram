from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import profile
from app.core.auth import get_current_user
from app.core.database import get_db


def test_workflow_uses_authenticated_profile_and_default_resume():
    app = FastAPI()
    app.include_router(profile.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="owner")
    db = object()
    app.dependency_overrides[get_db] = lambda: db
    candidate = SimpleNamespace(profile_id="owned-profile", default_trust_level="FULL_MANUAL_REVIEW", autopilot_globally_disabled=True)
    documents = [
        SimpleNamespace(is_default=False, original_filename="variant.pdf"),
        SimpleNamespace(is_default=True, original_filename="master.pdf"),
    ]
    with patch.object(profile.repo, "get_by_user_id", return_value=candidate) as lookup, patch.object(profile.repo, "list_documents", return_value=documents) as listing:
        response = TestClient(app).get("/profile/workflow")
    assert response.status_code == 200
    assert response.json()["resume_name"] == "master.pdf"
    assert response.json()["ready_to_apply"] is True
    lookup.assert_called_once_with(db, "owner")
    listing.assert_called_once_with(db, "owned-profile", document_type="resume")


def test_workflow_without_profile_does_not_query_documents():
    with patch.object(profile.repo, "get_by_user_id", return_value=None), patch.object(profile.repo, "list_documents") as listing:
        result = profile.get_workflow(SimpleNamespace(user_id="new-user"), object())
    assert result["ready_to_apply"] is False
    assert result["resume_name"] is None
    listing.assert_not_called()


def test_profile_without_resume_is_not_ready():
    candidate = SimpleNamespace(profile_id="profile", default_trust_level="FULL_MANUAL_REVIEW", autopilot_globally_disabled=False)
    with patch.object(profile.repo, "get_by_user_id", return_value=candidate), patch.object(profile.repo, "list_documents", return_value=[]):
        result = profile.get_workflow(SimpleNamespace(user_id="owner"), object())
    assert result["profile_ready"] is True
    assert result["ready_to_apply"] is False
