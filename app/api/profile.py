"""
Master candidate profile API — Phase 1.

The user creates their profile once (`POST /profile`), fills in education,
experience, skills, and documents, and every later ATS adapter (Phase 4+)
reads from this same data instead of asking the user again per application.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.db_models import (
    CandidateProfile,
    EducationEntry,
    ExperienceEntry,
    ProfileDocument,
    User,
    VALID_DISABILITY_STATUS_VALUES,
    VALID_DOCUMENT_TYPES,
    VALID_GENDER_VALUES,
    VALID_REMOTE_PREFERENCES,
    VALID_VETERAN_STATUS_VALUES,
)
from app.models.profile import (
    DemographicsRequest,
    DemographicsResponse,
    DocumentResponse,
    EducationRequest,
    EducationResponse,
    ExperienceRequest,
    ExperienceResponse,
    ProfileResponse,
    ProfileUpsertRequest,
    SkillsRequest,
)
from app.services import profile_repository as repo
from app.services.document_storage import (
    MAX_FILE_SIZE_MB,
    compute_file_hash,
    delete_document_file,
    save_document_file,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/profile", tags=["profile"])


def _get_owned_profile(db: Session, user: User) -> CandidateProfile:
    profile = repo.get_by_user_id(db, user.user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="No profile found. Create one with POST /profile.")
    return profile


def _get_owned_education(db: Session, education_id: str, profile: CandidateProfile) -> EducationEntry:
    entry = repo.get_education(db, education_id)
    if not entry or entry.profile_id != profile.profile_id:
        raise HTTPException(status_code=404, detail="Education entry not found.")
    return entry


def _get_owned_experience(db: Session, experience_id: str, profile: CandidateProfile) -> ExperienceEntry:
    entry = repo.get_experience(db, experience_id)
    if not entry or entry.profile_id != profile.profile_id:
        raise HTTPException(status_code=404, detail="Experience entry not found.")
    return entry


def _get_owned_document(db: Session, document_id: str, profile: CandidateProfile) -> ProfileDocument:
    document = repo.get_document(db, document_id)
    if not document or document.profile_id != profile.profile_id:
        raise HTTPException(status_code=404, detail="Document not found.")
    return document


def _validate_remote_preference(value: str | None) -> None:
    if value is not None and value not in VALID_REMOTE_PREFERENCES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid remote_preference '{value}'. Must be one of {sorted(VALID_REMOTE_PREFERENCES)}.",
        )


def _validate_choice(value: str | None, valid: set[str], field_name: str) -> None:
    if value is not None and value not in valid:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name} '{value}'. Must be one of {sorted(valid)}.")


# ---------- profile ----------

@router.post("", response_model=ProfileResponse, status_code=201)
def create_profile(
    body: ProfileUpsertRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if repo.get_by_user_id(db, user.user_id):
        raise HTTPException(status_code=409, detail="Profile already exists. Use PATCH /profile to update it.")

    data = body.model_dump(exclude_unset=True)
    _validate_remote_preference(data.get("remote_preference"))
    profile = repo.create_profile(db, user.user_id, data)
    return ProfileResponse(**repo.profile_to_dict(profile))


@router.get("", response_model=ProfileResponse)
def get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = _get_owned_profile(db, user)
    return ProfileResponse(**repo.profile_to_dict(profile))


@router.patch("", response_model=ProfileResponse)
def update_profile(
    body: ProfileUpsertRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_owned_profile(db, user)
    data = body.model_dump(exclude_unset=True)
    _validate_remote_preference(data.get("remote_preference"))
    profile = repo.update_profile(db, profile, data)
    return ProfileResponse(**repo.profile_to_dict(profile))


# ---------- education ----------

@router.post("/education", response_model=EducationResponse, status_code=201)
def add_education(
    body: EducationRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_owned_profile(db, user)
    entry = repo.add_education(db, profile.profile_id, body.model_dump(exclude_unset=True))
    return entry


@router.get("/education", response_model=list[EducationResponse])
def list_education(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = _get_owned_profile(db, user)
    return repo.list_education(db, profile.profile_id)


@router.patch("/education/{education_id}", response_model=EducationResponse)
def update_education(
    education_id: str,
    body: EducationRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_owned_profile(db, user)
    entry = _get_owned_education(db, education_id, profile)
    return repo.update_education(db, entry, body.model_dump(exclude_unset=True))


@router.delete("/education/{education_id}", status_code=204)
def delete_education(
    education_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_owned_profile(db, user)
    entry = _get_owned_education(db, education_id, profile)
    repo.delete_education(db, entry)


# ---------- experience ----------

@router.post("/experience", response_model=ExperienceResponse, status_code=201)
def add_experience(
    body: ExperienceRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_owned_profile(db, user)
    entry = repo.add_experience(db, profile.profile_id, body.model_dump(exclude_unset=True))
    return entry


@router.get("/experience", response_model=list[ExperienceResponse])
def list_experience(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = _get_owned_profile(db, user)
    return repo.list_experience(db, profile.profile_id)


@router.patch("/experience/{experience_id}", response_model=ExperienceResponse)
def update_experience(
    experience_id: str,
    body: ExperienceRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_owned_profile(db, user)
    entry = _get_owned_experience(db, experience_id, profile)
    return repo.update_experience(db, entry, body.model_dump(exclude_unset=True))


@router.delete("/experience/{experience_id}", status_code=204)
def delete_experience(
    experience_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_owned_profile(db, user)
    entry = _get_owned_experience(db, experience_id, profile)
    repo.delete_experience(db, entry)


# ---------- skills ----------

@router.put("/skills", response_model=ProfileResponse)
def set_skills(
    body: SkillsRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replaces the entire skills object — the client sends the full set each time."""
    profile = _get_owned_profile(db, user)
    profile = repo.update_skills(db, profile, body.model_dump())
    return ProfileResponse(**repo.profile_to_dict(profile))


# ---------- documents ----------

@router.post("/documents/upload", response_model=DocumentResponse, status_code=201)
async def upload_document(
    document_type: str,
    file: UploadFile = File(...),
    label: str | None = None,
    job_type_tag: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Uploads a resume version, cover letter, certificate, or other document.
    `document_type` must be one of: resume, cover_letter, certificate, other.
    Use `job_type_tag` (e.g. "backend", "data-science") so the right resume
    version can later be auto-selected per job."""
    profile = _get_owned_profile(db, user)

    if document_type not in VALID_DOCUMENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid document_type. Must be one of {sorted(VALID_DOCUMENT_TYPES)}.")

    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(status_code=413, detail=f"File too large ({size_mb:.1f} MB). Max {MAX_FILE_SIZE_MB} MB.")
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    file_hash = compute_file_hash(content)
    existing = repo.get_document_by_hash(db, profile.profile_id, document_type, file_hash)
    if existing:
        return existing

    try:
        _, stored_path = save_document_file(document_type, file.filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return repo.create_document(
        db=db,
        profile_id=profile.profile_id,
        document_type=document_type,
        original_filename=file.filename,
        stored_path=stored_path,
        file_hash=file_hash,
        label=label,
        job_type_tag=job_type_tag,
    )


@router.get("/documents", response_model=list[DocumentResponse])
def list_documents(
    document_type: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_owned_profile(db, user)
    if document_type and document_type not in VALID_DOCUMENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid document_type. Must be one of {sorted(VALID_DOCUMENT_TYPES)}.")
    return repo.list_documents(db, profile.profile_id, document_type=document_type)


@router.patch("/documents/{document_id}/set-default", response_model=DocumentResponse)
def set_default_document(
    document_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_owned_profile(db, user)
    document = _get_owned_document(db, document_id, profile)
    return repo.set_default_document(db, document)


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(
    document_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_owned_profile(db, user)
    document = _get_owned_document(db, document_id, profile)
    stored_path = document.stored_path
    repo.delete_document(db, document)
    delete_document_file(stored_path)


# ---------- demographics (Phase 8, PART 2) ----------
# Deliberately its own resource, not folded into GET/PATCH /profile — see
# app/models/db_models.py::CandidateDemographics for the rationale. A `None`
# field on GET means "never asked" (the automation layer should prompt the
# user once and then PUT the answer), not "no opinion" — never inferred or
# defaulted here or anywhere else.

@router.get("/demographics", response_model=DemographicsResponse)
def get_demographics(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = _get_owned_profile(db, user)
    entry = repo.get_demographics(db, profile.profile_id)
    if entry is None:
        # Every field defaults to None — "never asked" — rather than 404,
        # so a caller can always GET this endpoint to check what (if
        # anything) is already on file before deciding whether to prompt.
        return DemographicsResponse(candidate_id=profile.profile_id)
    return entry


@router.put("/demographics", response_model=DemographicsResponse)
def put_demographics(
    body: DemographicsRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The ONLY write path for demographic data anywhere in this codebase —
    always a direct, explicit user choice via this endpoint. Partial:
    fields omitted from the request body are left exactly as they were."""
    profile = _get_owned_profile(db, user)
    data = body.model_dump(exclude_unset=True)
    _validate_choice(data.get("gender"), VALID_GENDER_VALUES, "gender")
    _validate_choice(data.get("veteran_status"), VALID_VETERAN_STATUS_VALUES, "veteran_status")
    _validate_choice(data.get("disability_status"), VALID_DISABILITY_STATUS_VALUES, "disability_status")
    return repo.upsert_demographics(db, profile.profile_id, data)
