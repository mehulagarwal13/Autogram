from types import SimpleNamespace
from unittest.mock import patch
import asyncio
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile

from app.api import profile


def test_draft_rejects_another_users_document_before_parsing():
    with patch.object(profile.repo, "get_by_user_id", return_value=SimpleNamespace(profile_id="mine")), patch.object(profile.repo, "get_document", return_value=SimpleNamespace(profile_id="someone-else")):
        with pytest.raises(HTTPException) as error:
            profile.preview_profile_from_resume("document", SimpleNamespace(user_id="owner"), object())
    assert error.value.status_code == 404


def test_draft_does_not_save_profile():
    candidate = SimpleNamespace(profile_id="mine")
    document = SimpleNamespace(profile_id="mine", document_type="resume")
    with patch.object(profile.repo, "get_by_user_id", return_value=candidate), patch.object(profile.repo, "get_document", return_value=document), patch("app.services.profile_draft.draft_from_document", return_value={"full_name": "Ada"}), patch.object(profile.repo, "update_profile") as save:
        draft = profile.preview_profile_from_resume("document", SimpleNamespace(user_id="owner"), object())
    assert draft.full_name == "Ada"
    save.assert_not_called()


def test_first_upload_creates_profile_after_file_validation():
    user = SimpleNamespace(user_id="new-user")
    upload = UploadFile(filename="resume.pdf", file=BytesIO(b"%PDF-test"))
    with patch.object(profile.repo, "get_by_user_id", return_value=None), patch.object(profile.repo, "create_profile", return_value=SimpleNamespace(profile_id="new-profile")) as create, patch.object(profile, "save_document_file", return_value=("id", "stored.pdf")), patch.object(profile.repo, "create_document", return_value="saved") as document:
        result = asyncio.run(profile.upload_document("resume", upload, user=user, db=object()))
    assert result == "saved"
    assert create.call_args.args[1:] == ("new-user", {})
    assert document.call_args.kwargs["profile_id"] == "new-profile"


def test_invalid_upload_does_not_create_profile():
    upload = UploadFile(filename="resume.pdf", file=BytesIO(b"invalid"))
    with patch.object(profile.repo, "get_by_user_id", return_value=None), patch.object(profile.repo, "create_profile") as create, patch.object(profile, "save_document_file", side_effect=ValueError("Invalid file")):
        with pytest.raises(HTTPException) as error:
            asyncio.run(profile.upload_document("resume", upload, user=SimpleNamespace(user_id="new-user"), db=object()))
    assert error.value.status_code == 400
    create.assert_not_called()
