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

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_field, encrypt_field
from app.models.db_models import CandidateDemographics, CandidateProfile, EducationEntry, ExperienceEntry, ProfileDocument
from automation.forms.field_mapper import FieldMapper

logger = logging.getLogger(__name__)

# Plaintext-facing fields that map onto encrypted columns.
_ENCRYPTED_FIELD_MAP = {"phone": "phone_encrypted", "address": "address_encrypted"}

# Every other CandidateProfile column that's settable directly through the API.
_PLAIN_PROFILE_FIELDS = [
    "full_name", "first_name", "middle_name", "last_name", "email", "location",
    "city", "state", "postal_code", "country", "time_zone",
    "linkedin_url", "github_url", "portfolio_url", "website_url",
    "current_company", "current_role", "years_of_experience", "notice_period_days",
    "expected_salary", "expected_salary_currency", "work_authorization", "visa_status",
    # Phase 8 — compliance screening questions (see db_models.py's CandidateProfile docstring).
    "work_authorized", "requires_sponsorship", "visa_type", "sponsorship_countries",
    "preferred_locations", "remote_preference",
    # Form-answer fields (see db_models.py::CandidateProfile) — ordinary profile
    # data, settable through the normal POST/PATCH /profile path. Note
    # `marketing_opt_in` lives here rather than with the demographics below
    # because it is a preference the user states about themselves, not
    # protected-class data; it shares only the tri-state/never-inferred rule.
    "highest_education_level", "willing_to_relocate", "marketing_opt_in",
    "preferred_name", "current_salary", "current_salary_currency", "referral_source",
    "employment_type_preference", "languages", "willing_background_check",
    # Third tier (see db_models.py::CandidateProfile). `postal_code` is
    # deliberately in the plaintext list above rather than routed through
    # `_ENCRYPTED_FIELD_MAP` with `address`: a postal code is asked as its own
    # input and has to be readable as its own value, and on its own it doesn't
    # locate anyone the way a street address does.
    "professional_summary", "earliest_start_date", "security_clearance", "referrer_name",
    "age_over_18", "willing_to_travel", "requires_relocation_assistance",
    "willing_drug_test", "has_drivers_license",
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
        "autopilot_globally_disabled": profile.autopilot_globally_disabled,
        "default_trust_level": profile.default_trust_level,
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


#: Confidence floor for accepting a write-back — the same tier
#: `FieldMapper.map_field` returns for a `<label>` match. A human-answered
#: screening question is prose, not a raw field name/placeholder attribute,
#: so only the label-confidence tier (or better) is ever reachable here.
_WRITE_BACK_MIN_CONFIDENCE = 0.75

#: Spec §13's own example list (address, phone, country, state, city,
#: postal code, LinkedIn, portfolio) — deliberately an EXPLICIT allowlist,
#: not "whatever `FieldMapper.map_field` can resolve": `FIELD_SYNONYMS` also
#: maps to compliance/screening-adjacent facts (visa_status,
#: requires_sponsorship, willing_background_check, security_clearance,
#: current_salary, ...) that this codebase treats elsewhere as
#: never-silently-inferred (see `CandidateProfile`'s tri-state screening
#: fields and Profile.jsx's "never asked" default) — those must stay
#: per-application, human-reviewed facts, never silently promoted to a
#: permanent profile value from a single answered question.
_WRITE_BACK_SAFE_ATTRIBUTES = frozenset({
    "full_name", "preferred_name", "first_name", "middle_name", "last_name",
    "email", "phone", "location", "city", "state", "postal_code", "country",
    "address", "time_zone", "linkedin_url", "github_url", "portfolio_url",
    "website_url", "current_company", "current_role",
})


def write_back_stable_answer(db: Session, user_id: str, question_text: str, value: str) -> bool:
    """Spec §13: when a human answers a screening question that turns out to
    be a plain contact/professional-link fact (phone, city, LinkedIn, ...)
    rather than a one-off application-specific question, save it to the
    candidate's profile too — so it is never re-asked on the next
    application.

    Reuses `FieldMapper.map_field`, the same classifier already used to
    resolve an on-page field's label to a `CandidateProfile` attribute for
    autofill — same mapping, opposite direction — but only ever ACTS on a
    result in `_WRITE_BACK_SAFE_ATTRIBUTES` (see that constant's docstring
    for why the full `FIELD_SYNONYMS` range is not trusted here).

    Best-effort: returns `False` on no match / no profile / any failure,
    never raises — a missed write-back is a convenience lost, not a
    correctness problem, and must never break the HITL response that
    triggered it."""
    if not value or not value.strip():
        return False
    match = FieldMapper.map_field(label=question_text)
    if match is None:
        return False
    attribute, confidence = match
    if confidence < _WRITE_BACK_MIN_CONFIDENCE:
        return False
    if attribute not in _WRITE_BACK_SAFE_ATTRIBUTES:
        return False
    try:
        profile = get_by_user_id(db, user_id)
        if profile is None:
            return False
        update_profile(db, profile, {attribute: value.strip()})
        return True
    except Exception:  # noqa: BLE001 — a missed write-back must never break the caller's own response
        logger.exception("write_back_stable_answer: failed to write %r for user %s", attribute, user_id)
        return False


def update_automation_settings(
    db: Session, profile: CandidateProfile, *,
    autopilot_globally_disabled: bool, default_trust_level: str | None = None,
) -> CandidateProfile:
    """The account-level autopilot kill switch (PHASE2_ARCHITECTURE.md
    Initiative 3) plus, optionally, the §6.4 default trust level for
    newly-seen sites — deliberately its own function, not folded into
    `update_profile`/`_apply_profile_fields`, so neither can be flipped as a
    side effect of an unrelated `PATCH /profile` call. The only write path is
    `PUT /profile/automation-settings`.

    `default_trust_level=None` (the default) leaves the stored value
    unchanged — the request model makes this field optional for exactly that
    reason, so the kill-switch-only caller this endpoint originally served
    keeps working without having to also resend a trust level it doesn't
    know about. Caller (the API route) is responsible for validating the
    value against `VALID_TRUST_LEVELS` before calling this."""
    profile.autopilot_globally_disabled = autopilot_globally_disabled
    if default_trust_level is not None:
        profile.default_trust_level = default_trust_level
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
    # `experience_id` breaks ties: a bulk insert writes every row inside one
    # transaction, so several rows can share a `created_at` and `desc()` alone
    # would order them arbitrarily (differently between two identical calls).
    return (
        db.query(ExperienceEntry)
        .filter(ExperienceEntry.profile_id == profile_id)
        .order_by(ExperienceEntry.created_at.desc(), ExperienceEntry.experience_id)
        .all()
    )


def get_experience(db: Session, experience_id: str) -> ExperienceEntry | None:
    return db.query(ExperienceEntry).filter(ExperienceEntry.experience_id == experience_id).first()


def add_experiences(db: Session, profile_id: str, items: list[dict]) -> list[ExperienceEntry]:
    """Creates every entry in `items` for `profile_id` in ONE transaction.

    All-or-nothing: the rows are staged, flushed together so the DB reports
    any constraint violation while we can still act on it, and committed once.
    If anything raises — a bad column in `items`, a FK violation because the
    profile vanished under us, a lost connection mid-commit — the session is
    rolled back and the error propagates, leaving *no* partial batch behind.
    That guarantee is why the API layer must not commit around this call.

    Returns the created entries in the order they were given.
    """
    entries: list[ExperienceEntry] = []
    try:
        for data in items:
            entry = ExperienceEntry(experience_id=str(uuid.uuid4()), profile_id=profile_id, **data)
            db.add(entry)
            entries.append(entry)
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
    for entry in entries:
        db.refresh(entry)  # expire_on_commit=True — reload server-side defaults (created_at)
    return entries


def add_experience(db: Session, profile_id: str, data: dict) -> ExperienceEntry:
    """Single-entry create. Kept as-is for existing callers; delegates to
    `add_experiences` so the one-row and many-row paths can't drift apart."""
    return add_experiences(db, profile_id, [data])[0]


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

_DEMOGRAPHICS_FIELDS = [
    "gender", "veteran_status", "disability_status", "race_ethnicity",
    "pronouns", "ethnicities",
]


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
