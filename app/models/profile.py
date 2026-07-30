from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ---------- personal + professional profile ----------

class ProfileUpsertRequest(BaseModel):
    """All fields optional — both create (`POST /profile`) and update
    (`PATCH /profile`) apply only the fields the caller sends."""

    # Personal
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    linkedin_url: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None
    website_url: str | None = None

    # Professional
    current_company: str | None = None
    current_role: str | None = None
    years_of_experience: float | None = None
    notice_period_days: int | None = None
    expected_salary: float | None = None
    expected_salary_currency: str | None = None
    work_authorization: str | None = None
    visa_status: str | None = None
    # Phase 8 — compliance screening questions (see app/models/db_models.py's
    # CandidateProfile docstring for why these are separate from the two
    # free-text fields above).
    work_authorized: bool | None = None
    requires_sponsorship: bool | None = None
    visa_type: str | None = None
    sponsorship_countries: list[str] | None = None
    preferred_locations: list[str] | None = None
    remote_preference: str | None = None  # remote / hybrid / onsite / no_preference


class ProfileResponse(BaseModel):
    profile_id: str
    user_id: str
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    linkedin_url: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None
    website_url: str | None = None
    current_company: str | None = None
    current_role: str | None = None
    years_of_experience: float | None = None
    notice_period_days: int | None = None
    expected_salary: float | None = None
    expected_salary_currency: str | None = None
    work_authorization: str | None = None
    visa_status: str | None = None
    work_authorized: bool | None = None
    requires_sponsorship: bool | None = None
    visa_type: str | None = None
    sponsorship_countries: list[str] | None = None
    preferred_locations: list[str] | None = None
    remote_preference: str | None = None
    skills: dict | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------- education ----------

class EducationRequest(BaseModel):
    degree: str | None = None
    university: str | None = None
    field_of_study: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    gpa: str | None = None


class EducationResponse(EducationRequest):
    model_config = ConfigDict(from_attributes=True)

    education_id: str
    profile_id: str
    created_at: datetime | None = None


# ---------- experience ----------

class ExperienceRequest(BaseModel):
    company_name: str | None = None
    job_title: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    description: str | None = None
    skills_used: list[str] | None = None


class ExperienceResponse(ExperienceRequest):
    model_config = ConfigDict(from_attributes=True)

    experience_id: str
    profile_id: str
    created_at: datetime | None = None


# ---------- skills ----------

class SkillsRequest(BaseModel):
    programming_languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    technical_skills: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)


# ---------- documents ----------

class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    document_id: str
    profile_id: str
    document_type: str
    label: str | None = None
    job_type_tag: str | None = None
    original_filename: str
    is_default: bool
    uploaded_at: datetime | None = None


# ---------- demographics (Phase 8, PART 2) ----------
# Deliberately optional on every field, both directions: nothing here is ever
# inferred, so a request only sets what the user actually chose, and a
# response's `None` means "never asked" — not "answered nothing" — which is
# exactly the distinction `automation/forms/answer_engine.py` needs to decide
# whether to ask the user once (see that module's docstring) or reuse what's
# already stored.

class DemographicsRequest(BaseModel):
    gender: str | None = None              # see VALID_GENDER_VALUES
    veteran_status: str | None = None      # see VALID_VETERAN_STATUS_VALUES
    disability_status: str | None = None   # see VALID_DISABILITY_STATUS_VALUES
    race_ethnicity: str | None = None      # free text — categories vary by country/form


class DemographicsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    candidate_id: str
    gender: str | None = None
    veteran_status: str | None = None
    disability_status: str | None = None
    race_ethnicity: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
