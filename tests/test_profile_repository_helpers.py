"""
Pure-logic tests for the profile repository's encryption boundary:
`_apply_profile_fields` and `profile_to_dict` operate on an in-memory
CandidateProfile instance (no DB session needed — SQLAlchemy model
instances are plain Python objects until added to a session).
"""

from app.core.crypto import decrypt_field
from app.models.db_models import CandidateProfile
from app.services.profile_repository import _apply_profile_fields, profile_to_dict


def _blank_profile() -> CandidateProfile:
    return CandidateProfile(profile_id="p1", user_id="u1")


def test_apply_profile_fields_encrypts_phone_and_address():
    profile = _blank_profile()
    _apply_profile_fields(profile, {"phone": "+1-555-0100", "address": "1 Infinite Loop"})

    # Never stored in plaintext on the model instance.
    assert profile.phone_encrypted != "+1-555-0100"
    assert profile.address_encrypted != "1 Infinite Loop"
    assert decrypt_field(profile.phone_encrypted) == "+1-555-0100"
    assert decrypt_field(profile.address_encrypted) == "1 Infinite Loop"


def test_apply_profile_fields_partial_update_only_touches_given_keys():
    profile = _blank_profile()
    _apply_profile_fields(profile, {"full_name": "Ada Lovelace"})

    assert profile.full_name == "Ada Lovelace"
    assert profile.phone_encrypted is None  # untouched


def test_profile_to_dict_decrypts_for_the_api_response():
    profile = _blank_profile()
    _apply_profile_fields(profile, {"phone": "0123456789", "full_name": "Grace Hopper"})

    result = profile_to_dict(profile)

    assert result["phone"] == "0123456789"  # plaintext for the owning user
    assert result["full_name"] == "Grace Hopper"
    assert result["profile_id"] == "p1"
    assert result["user_id"] == "u1"


def test_profile_to_dict_handles_missing_phone_and_address():
    profile = _blank_profile()

    result = profile_to_dict(profile)

    assert result["phone"] is None
    assert result["address"] is None
