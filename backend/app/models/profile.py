from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator


# ---------- personal + professional profile ----------

class LanguageEntry(BaseModel):
    """One language and how well the candidate speaks it. The proficiency is the
    whole point: forms ask "are you fluent in X?", and answering that from a bare
    mention of X in a list would be an assumption about degree rather than an
    answer. `proficiency` stays optional so a caller can list a language without
    committing to a level — such an entry just doesn't answer fluency questions.
    """

    language: str
    proficiency: str | None = None  # see VALID_LANGUAGE_PROFICIENCIES


class ProfileUpsertRequest(BaseModel):
    """All fields optional — both create (`POST /profile`) and update
    (`PATCH /profile`) apply only the fields the caller sends."""

    # Personal
    full_name: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    time_zone: str | None = None  # free text in the form's own words — "IST", "GMT+5:30", "US Eastern"
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
    # Questions real ATS forms ask that had no profile field to answer from —
    # see app/models/db_models.py::CandidateProfile. `highest_education_level`
    # is free text in the form's own words ("Bachelor's Degree");
    # `marketing_opt_in` is tri-state, and only an explicit `true` ever ticks a
    # "may we contact you about future roles" box.
    highest_education_level: str | None = None
    willing_to_relocate: bool | None = None
    marketing_opt_in: bool | None = None
    preferred_name: str | None = None
    # A DIFFERENT fact from expected_salary — forms ask both, and the classifier
    # used to answer "current CTC" with the expected number.
    current_salary: float | None = None
    current_salary_currency: str | None = None
    referral_source: str | None = None            # "How did you hear about this job?"
    employment_type_preference: str | None = None  # see VALID_EMPLOYMENT_TYPES
    languages: list[LanguageEntry] | None = None
    willing_background_check: bool | None = None   # tri-state, like marketing_opt_in
    # Third tier — see app/models/db_models.py::CandidateProfile for what each
    # one answers. The five booleans are tri-state on purpose: omit one and it
    # stays `None` ("never asked"), which is not the same as `false`.
    professional_summary: str | None = None
    earliest_start_date: str | None = None   # free-form: "2026-09-01", "September 2026", "Immediately"
    security_clearance: str | None = None
    referrer_name: str | None = None
    age_over_18: bool | None = None
    willing_to_travel: bool | None = None
    requires_relocation_assistance: bool | None = None  # a question about money, NOT willing_to_relocate
    willing_drug_test: bool | None = None
    has_drivers_license: bool | None = None


class ProfileResponse(BaseModel):
    profile_id: str
    user_id: str
    full_name: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    time_zone: str | None = None
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
    highest_education_level: str | None = None
    willing_to_relocate: bool | None = None
    marketing_opt_in: bool | None = None
    preferred_name: str | None = None
    current_salary: float | None = None
    current_salary_currency: str | None = None
    referral_source: str | None = None
    employment_type_preference: str | None = None
    # Echoed back as stored (plain dicts) rather than re-parsed into
    # `LanguageEntry`, matching how `skills` and `sponsorship_countries` behave.
    languages: list[dict] | None = None
    willing_background_check: bool | None = None
    professional_summary: str | None = None
    earliest_start_date: str | None = None
    security_clearance: str | None = None
    referrer_name: str | None = None
    age_over_18: bool | None = None
    willing_to_travel: bool | None = None
    requires_relocation_assistance: bool | None = None
    willing_drug_test: bool | None = None
    has_drivers_license: bool | None = None
    skills: dict | None = None
    # HITL platform — read-only here; only `PUT /profile/automation-settings`
    # (a dedicated endpoint, deliberately NOT part of the generic PATCH
    # /profile payload) may change this, so a routine profile edit can never
    # accidentally flip the account-level autopilot kill switch.
    autopilot_globally_disabled: bool = False
    #: §6.4 — same read-only-except-via-automation-settings rule as above.
    default_trust_level: str = "FULL_MANUAL_REVIEW"
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------- automation settings (HITL platform) ----------

class AutomationSettingsRequest(BaseModel):
    autopilot_globally_disabled: bool
    #: §6.4 trust levels — the level applied to a domain the FIRST time this
    #: user's automation ever sees it. `None` (the default) means "leave it
    #: as-is" — this field is optional so the existing single-purpose caller
    #: (the kill-switch toggle) keeps working unchanged.
    default_trust_level: str | None = None


class AutomationSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    autopilot_globally_disabled: bool
    default_trust_level: str


# ---------- site trust levels (§6.4) ----------

class SiteTrustLevelRequest(BaseModel):
    trust_level: str


class SiteTrustLevelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    domain: str
    trust_level: str
    updated_at: datetime | None = None


# ---------- data retention (§9) ----------

class RetentionPolicyRequest(BaseModel):
    #: `None` (the default) leaves that window unchanged — a caller only
    #: sends the field(s) they're actually updating. No `document_retention_
    #: days` here at all (not just excluded from writes) — that column was
    #: removed (migration `e3f4a5b6c7d8`) once confirmed permanently
    #: unenforceable; see `app/services/retention_service.py`'s docstring.
    screenshot_retention_days: int | None = None
    run_history_retention_days: int | None = None
    hitl_request_retention_days: int | None = None


class RetentionPolicyResponse(BaseModel):
    screenshot_retention_days: int
    run_history_retention_days: int
    hitl_request_retention_days: int


class RetentionPurgeResult(BaseModel):
    category: str
    records_purged: int
    files_deleted: int
    files_failed: int
    error: str | None = None


class RetentionPurgeNowResponse(BaseModel):
    results: list[RetentionPurgeResult]


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

# How many entries one `POST /profile/experience` may create. A resume import
# or a hand-filled profile is a handful of jobs; anything past this is a client
# bug or an abusive payload, and the whole batch goes in one transaction, so an
# unbounded list is an unbounded lock.
MAX_EXPERIENCE_BATCH = 50

_EXPERIENCE_TEXT_FIELDS = ("company_name", "job_title", "start_date", "end_date", "description")


class ExperienceBase(BaseModel):
    """The experience columns plus the normalisation every read and write
    shares. Deliberately *not* used as a request body itself — see
    `ExperienceCreate` (POST) and `ExperienceRequest` (PATCH).

    No length caps live here on purpose: `ExperienceResponse` inherits these
    fields, and a cap invented in the schema layer would make a legitimately
    long stored `description` unserialisable on read.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    company_name: str | None = None
    job_title: str | None = None
    start_date: str | None = None
    end_date: str | None = None  # None/"" == current role (see db_models.ExperienceEntry)
    description: str | None = None
    skills_used: list[str] | None = None

    @field_validator(*_EXPERIENCE_TEXT_FIELDS, mode="after")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        """`""` and `"   "` are absence, not content. Collapsing them to None
        keeps one representation in the DB, and for `end_date` "" and None
        already mean the same thing (current role)."""
        return value or None

    @field_validator("skills_used", mode="after")
    @classmethod
    def _clean_skills_used(cls, value: list[str] | None) -> list[str] | None:
        """Drops blanks and case-insensitive duplicates, keeping first-seen
        order and the caller's original casing."""
        if value is None:
            return None
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in value:
            skill = raw.strip()
            if not skill or skill.casefold() in seen:
                continue
            seen.add(skill.casefold())
            cleaned.append(skill)
        return cleaned


class ExperienceRequest(ExperienceBase):
    """`PATCH /profile/experience/{id}` — partial update. Every field is
    optional and only the fields actually sent are applied, so `{}` is a
    no-op rather than an error."""


class ExperienceCreate(ExperienceBase):
    """One entry in a `POST /profile/experience` body.

    Same fields as `ExperienceRequest`; the difference is that a *create* has
    to say something. A partial update may legitimately send nothing, but an
    empty create would persist a row that answers no question any form asks.
    """

    @model_validator(mode="after")
    def _reject_empty_entry(self) -> "ExperienceCreate":
        if not any(getattr(self, field) for field in (*_EXPERIENCE_TEXT_FIELDS, "skills_used")):
            raise ValueError(
                "An experience entry must set at least one field "
                f"({', '.join((*_EXPERIENCE_TEXT_FIELDS, 'skills_used'))})."
            )
        return self


class ExperienceBatchCreate(RootModel[list[ExperienceCreate]]):
    """A JSON *array* body for `POST /profile/experience` — create many
    experiences in one request/one transaction.

    Exists as a named model rather than a bare `list[ExperienceCreate]` so the
    batch-wide rules (size bounds, duplicate detection) are enforced by
    Pydantic — i.e. reported as a normal 422 alongside the per-entry errors,
    with a `loc` pointing at the offending index — instead of as a hand-rolled
    check in the route.
    """

    root: list[ExperienceCreate] = Field(
        ...,
        min_length=1,
        max_length=MAX_EXPERIENCE_BATCH,
        description="One or more experience entries to create atomically.",
    )

    @model_validator(mode="after")
    def _reject_duplicate_entries(self) -> "ExperienceBatchCreate":
        """The same company + title + start date twice in one payload is a
        double-submit or a bad merge on the client, never a real second job."""
        seen: set[tuple] = set()
        for index, entry in enumerate(self.root):
            key = (
                (entry.company_name or "").casefold(),
                (entry.job_title or "").casefold(),
                (entry.start_date or "").casefold(),
            )
            if key in seen:
                raise ValueError(
                    f"Duplicate experience entry at index {index}: "
                    "company_name + job_title + start_date already appears earlier in this request."
                )
            seen.add(key)
        return self

    def __iter__(self):
        return iter(self.root)

    def __len__(self) -> int:
        return len(self.root)


class ExperienceResponse(ExperienceBase):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)

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
    # Free text, unvalidated, both of them — deliberately. Pronoun sets are
    # open-ended (one live form offers nine plus "Custom") and ethnicity
    # categories differ by country, so a closed enum here would reject
    # legitimate answers. Store them worded the way forms word them
    # ("they/them", "Asian") and they match the page's own options directly.
    pronouns: str | None = None
    ethnicities: list[str] | None = None   # "select all that apply" ethnicity groups


class DemographicsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    candidate_id: str
    gender: str | None = None
    veteran_status: str | None = None
    disability_status: str | None = None
    race_ethnicity: str | None = None
    pronouns: str | None = None
    ethnicities: list[str] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
