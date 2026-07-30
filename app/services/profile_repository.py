"""
Data access for the master candidate profile system: `CandidateProfile`,
`EducationEntry`, `ExperienceEntry`, `ProfileDocument`.

Encryption boundary lives here (not in the API layer): `phone` and `address`
are encrypted the moment they're written to a `CandidateProfile` row, and
decrypted the moment one is read back out, via `app/core/crypto.py`. Callers
(the API layer) only ever see plaintext — they can't accidentally persist an
unencrypted phone number by bypassing this module.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_field, encrypt_field
from app.models.db_models import CandidateDemographics, CandidateProfile, EducationEntry, ExperienceEntry, ProfileDocument

# Plaintext-facing fields that map onto encrypted columns.
_ENCRYPTED_FIELD_MAP = {"phone": "phone_encrypted", "address": "address_encrypted"}

# Every other CandidateProfile column that's settable directly through the API.
_PLAIN_PROFILE_FIELDS = [
    "full_name", "first_name", "last_name", "email", "location", "city", "state", "country",
    "linkedin_url", "github_url", "portfolio_url", "website_url",
    "current_company", "current_role", "years_of_experience", "notice_period_days",
    "expected_salary", "expected_salary_currency", "work_authorization", "visa_status",
    # Phase 8 — compliance screening questions (see db_models.py's CandidateProfile docstring).
    "work_authorized", "requires_sponsorship", "visa_type", "sponsorship_countries",
    "preferred_locations", "remote_preference",
]


def _apply_profile_fields(profile: CandidateProfile, data: dict) -> None:
    """Writes only the fields present in `data` (partial update semantics),
    routing `phone`/`address` through encryption."""
    for plaintext_key, column_name in _ENCRYPTED_FIELD_MAP.items():
        if plaintext_key in data:
            setattr(profile, column_name, encrypt_field(data[plaintext_key]))
    for field in _PLAIN_PROFILE_FIELDS:
        if field in data:
            setattr(profile, field, data[field])  


def profile_to_dict(profile: CandidateProfile) -> dict:
    """Decrypts phone/address and returns a plain dict ready for a Pydantic response model."""
    result = {
        "profile_id": profile.profile_id,
        "user_id": profile.user_id,
        "phone": decrypt_field(profile.phone_encrypted),
        "address": decrypt_field(profile.address_encrypted),
        "skills": profile.skills,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }
    for field in _PLAIN_PROFILE_FIELDS:
        result[field] = getattr(profile, field)
    return result


# ---------- profile ----------

def get_by_user_id(db: Session, user_id: str) -> CandidateProfile | None:
    return db.query(CandidateProfile).filter(CandidateProfile.user_id == user_id).first()


def create_profile(db: Session, user_id: str, data: dict) -> CandidateProfile:
    profile = CandidateProfile(profile_id=str(uuid.uuid4()), user_id=user_id)
    _apply_profile_fields(profile, data)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def update_profile(db: Session, profile: CandidateProfile, data: dict) -> CandidateProfile:
    _apply_profile_fields(profile, data)
    profile.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(profile)
    return profile


def update_skills(db: Session, profile: CandidateProfile, skills: dict) -> CandidateProfile:
    profile.skills = skills
    profile.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(profile)
    return profile


# ---------- education ----------

def list_education(db: Session, profile_id: str) -> list[EducationEntry]:
    return (
        db.query(EducationEntry)
        .filter(EducationEntry.profile_id == profile_id)
        .order_by(EducationEntry.created_at.desc())
        .all()
    )


def get_education(db: Session, education_id: str) -> EducationEntry | None:
    return db.query(EducationEntry).filter(EducationEntry.education_id == education_id).first()


def add_education(db: Session, profile_id: str, data: dict) -> EducationEntry:
    entry = EducationEntry(education_id=str(uuid.uuid4()), profile_id=profile_id, **data)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def update_education(db: Session, entry: EducationEntry, data: dict) -> EducationEntry:
    for key, value in data.items():
        setattr(entry, key, value)
    db.commit()
    db.refresh(entry)
    return entry


def delete_education(db: Session, entry: EducationEntry) -> None:
    db.delete(entry)
    db.commit()


# ---------- experience ----------

def list_experience(db: Session, profile_id: str) -> list[ExperienceEntry]:
    return (
        db.query(ExperienceEntry)
        .filter(ExperienceEntry.profile_id == profile_id)
        .order_by(ExperienceEntry.created_at.desc())
        .all()
    )


def get_experience(db: Session, experience_id: str) -> ExperienceEntry | None:
    return db.query(ExperienceEntry).filter(ExperienceEntry.experience_id == experience_id).first()


def add_experience(db: Session, profile_id: str, data: dict) -> ExperienceEntry:
    entry = ExperienceEntry(experience_id=str(uuid.uuid4()), profile_id=profile_id, **data)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def update_experience(db: Session, entry: ExperienceEntry, data: dict) -> ExperienceEntry:
    for key, value in data.items():
        setattr(entry, key, value)
    db.commit()
    db.refresh(entry)
    return entry


def delete_experience(db: Session, entry: ExperienceEntry) -> None:
    db.delete(entry)
    db.commit()


# ---------- documents ----------

def list_documents(db: Session, profile_id: str, document_type: str | None = None) -> list[ProfileDocument]:
    query = db.query(ProfileDocument).filter(ProfileDocument.profile_id == profile_id)
    if document_type:
        query = query.filter(ProfileDocument.document_type == document_type)
    return query.order_by(ProfileDocument.uploaded_at.desc()).all()


def get_document(db: Session, document_id: str) -> ProfileDocument | None:
    return db.query(ProfileDocument).filter(ProfileDocument.document_id == document_id).first()


def get_document_by_hash(db: Session, profile_id: str, document_type: str, file_hash: str) -> ProfileDocument | None:
    return (
        db.query(ProfileDocument)
        .filter(
            ProfileDocument.profile_id == profile_id,
            ProfileDocument.document_type == document_type,
            ProfileDocument.file_hash == file_hash,
        )
        .first()
    )


def create_document(
    db: Session,
    profile_id: str,
    document_type: str,
    original_filename: str,
    stored_path: str,
    file_hash: str,
    label: str | None = None,
    job_type_tag: str | None = None,
) -> ProfileDocument:
    document = ProfileDocument(
        document_id=str(uuid.uuid4()),
        profile_id=profile_id,
        document_type=document_type,
        label=label,
        job_type_tag=job_type_tag,
        original_filename=original_filename,
        stored_path=stored_path,
        file_hash=file_hash,
        is_default=False,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def set_default_document(db: Session, document: ProfileDocument) -> ProfileDocument:
    """Unsets any other default of the same type for this profile, then sets this one."""
    (
        db.query(ProfileDocument)
        .filter(
            ProfileDocument.profile_id == document.profile_id,
            ProfileDocument.document_type == document.document_type,
            ProfileDocument.document_id != document.document_id,
        )
        .update({"is_default": False})
    )
    document.is_default = True
    db.commit()
    db.refresh(document)
    return document


def delete_document(db: Session, document: ProfileDocument) -> None:
    db.delete(document)
    db.commit()


# ---------- demographics (Phase 8, PART 2) ----------
# Deliberately its own small CRUD surface, separate from `_apply_profile_fields`
# above — these values are never touched by the generic profile update path,
# only by the dedicated demographics endpoints, so there is exactly one place
# in the whole codebase that ever writes a value here: the user's own explicit
# choice via `PUT /profile/demographics`. Nothing else (not the résumé
# parser, not the LLM answer engine, not any ATS adapter) may write to this
# table — see `app/models/db_models.py::CandidateDemographics` and
# `automation/forms/answer_engine.py` for the enforcement side of that rule.

_DEMOGRAPHICS_FIELDS = ["gender", "veteran_status", "disability_status", "race_ethnicity"]


def get_demographics(db: Session, profile_id: str) -> CandidateDemographics | None:
    return db.query(CandidateDemographics).filter(CandidateDemographics.candidate_id == profile_id).first()


def upsert_demographics(db: Session, profile_id: str, data: dict) -> CandidateDemographics:
    """Partial update semantics, same as `update_profile` — only overwrites
    fields actually present in `data`, so `PUT /profile/demographics` can be
    called once per question as the user answers each one, or all at once."""
    entry = get_demographics(db, profile_id)
    if entry is None:
        entry = CandidateDemographics(id=str(uuid.uuid4()), candidate_id=profile_id)
        db.add(entry)
    for field in _DEMOGRAPHICS_FIELDS:
        if field in data:
            setattr(entry, field, data[field])
    entry.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(entry)
    return entry
