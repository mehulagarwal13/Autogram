from sqlalchemy import Boolean, Column, String, DateTime, Text, Float, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector
from datetime import datetime, timezone

from app.core.database import Base

EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 output size

VALID_MATCH_STATUSES = {"new", "saved", "dismissed"}
VALID_REMOTE_PREFERENCES = {"remote", "hybrid", "onsite", "no_preference"}
VALID_DOCUMENT_TYPES = {"resume", "cover_letter", "certificate", "other"}
# Phase 8 — equal-opportunity/demographic answer options. Deliberately
# includes "decline_to_answer"/"prefer_not_to_say" everywhere real ATS forms
# offer it (nearly always, since EEO-1 collection is voluntary in the US) —
# a candidate choosing not to disclose is a legitimate, storable preference,
# not a missing value that should trigger re-asking every application.
VALID_GENDER_VALUES = {"male", "female", "non_binary", "decline_to_answer", "self_described"}
VALID_VETERAN_STATUS_VALUES = {"veteran", "not_veteran", "decline_to_answer"}
VALID_DISABILITY_STATUS_VALUES = {"has_disability", "no_disability", "decline_to_answer"}
VALID_EMPLOYMENT_TYPES = {"full_time", "part_time", "contract", "internship", "temporary", "no_preference"}
# Language proficiency, coarsest distinction that matters to a form: an ATS asks
# "are you fluent in X?" as a yes/no, and the honest answer depends on which
# band the candidate put themselves in.
VALID_LANGUAGE_PROFICIENCIES = {"native", "fluent", "professional", "conversational", "basic"}
# The bands that answer "yes" to a fluency question. "conversational"/"basic"
# deliberately answer "no": overstating language ability on an application is a
# claim the candidate has to live with in an interview, so the boundary is drawn
# where the candidate themselves drew it, never widened for a better fill rate.
FLUENT_LANGUAGE_PROFICIENCIES = {"native", "fluent", "professional"}
# Mirrors automation.interfaces.ApplicationRunResult.status (Phase 4) plus the
# two states that exist only before/between automation runs ("pending" before
# the first run starts, "processing" while one is in flight).
VALID_APPLICATION_STATUSES = {
    "pending", "processing", "applied", "failed",
    "manual_required", "needs_review", "copilot_review",
}
# Where a cached screening-question answer originally came from (Phase 6,
# app/services/answer_cache_repository.py) — "cache" isn't a stored value
# here, it's what a lookup hit is *reported as* by the caller.
VALID_ANSWER_SOURCES = {"deterministic", "llm"}


class User(Base):
    __tablename__ = "users"

    user_id = Column(String, primary_key=True)
    email = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)  # salted PBKDF2, never plaintext
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ResumeRecord(Base):
    __tablename__ = "resumes"

    resume_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    original_filename = Column(String, nullable=False)
    stored_path = Column(String, nullable=False)
    # Dedup is per-user (same file from two users = two records), so no unique constraint.
    file_hash = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="uploaded")  # uploaded/extracted/extraction_failed/parsed
    extracted_text = Column(Text, nullable=True)
    uploaded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    parsed_data = Column(JSONB, nullable=True)            # structured ParsedResume — queryable JSONB
    confidence_score = Column(Float, nullable=True)
    embedding_vector = Column(Vector(EMBEDDING_DIM), nullable=True)  # pgvector


class JobRecord(Base):
    __tablename__ = "jobs"

    job_id = Column(String, primary_key=True)            # "{source}_{source_id}"
    title = Column(String, nullable=False)
    company = Column(String, nullable=True)
    location = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    salary_min = Column(Float, nullable=True)
    salary_max = Column(Float, nullable=True)
    remote = Column(String, nullable=True)               # "yes"/"no"/None (unknown)
    apply_url = Column(String, nullable=True)
    source = Column(String, nullable=False, default="adzuna")
    dedup_key = Column(String, nullable=True, index=True)  # sha1(title|company) — cross-source dedup
    embedding_vector = Column(Vector(EMBEDDING_DIM), nullable=True)  # searched via HNSW index
    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    min_years_required = Column(Float, nullable=True)


class MatchResult(Base):
    __tablename__ = "match_results"

    match_id = Column(String, primary_key=True)
    resume_id = Column(
        String,
        ForeignKey("resumes.resume_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    job_id = Column(
        String,
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vector_similarity = Column(Float, nullable=False)
    skill_overlap_ratio = Column(Float, nullable=False)
    blended_score = Column(Float, nullable=False)         # numeric — sorts correctly
    matched_skills = Column(JSONB, nullable=False)
    missing_skills = Column(JSONB, nullable=False)
    explanation = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="new")  # new/saved/dismissed
    generated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    ats_score = Column(Float, nullable=True)
    ats_found_keywords = Column(JSONB, nullable=True)
    ats_missing_keywords = Column(JSONB, nullable=True)
    ats_format_score = Column(Float, nullable=True)


# --- Master candidate profile system (auto-apply platform, Phase 1) --------

class CandidateProfile(Base):
    """One row per user — the master profile every ATS adapter fills forms from.

    `phone_encrypted` / `address_encrypted` are Fernet-encrypted at the
    application layer (see `app/core/crypto.py`) before ever reaching
    SQLAlchemy; this column just stores the resulting ciphertext.
    """

    __tablename__ = "candidate_profiles"

    profile_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    # Personal information
    full_name = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone_encrypted = Column(String, nullable=True)
    location = Column(String, nullable=True)
    address_encrypted = Column(Text, nullable=True)
    city = Column(String, nullable=True)
    state = Column(String, nullable=True)
    country = Column(String, nullable=True)
    linkedin_url = Column(String, nullable=True)
    github_url = Column(String, nullable=True)
    portfolio_url = Column(String, nullable=True)
    website_url = Column(String, nullable=True)

    # Professional information
    current_company = Column(String, nullable=True)
    current_role = Column(String, nullable=True)
    years_of_experience = Column(Float, nullable=True)
    notice_period_days = Column(Integer, nullable=True)
    expected_salary = Column(Float, nullable=True)
    expected_salary_currency = Column(String, nullable=True)
    work_authorization = Column(String, nullable=True)
    visa_status = Column(String, nullable=True)

    # Phase 8 — compliance screening questions. Kept alongside the two
    # free-text columns above (`work_authorization`/`visa_status`), never
    # replacing them: those two are still useful as free-text/echo-back
    # context (see `answer_engine._format_work_authorization`), but real ATS
    # forms ask two DIFFERENT yes/no questions that a single free-text field
    # can't answer safely — "are you currently authorized to work in
    # <country>" and "will you now or in future require sponsorship" are not
    # the same fact and a candidate can be True/False on either
    # independently (e.g. authorized today via OPT, but will need H-1B
    # sponsorship later). Guessing one from the other is exactly the kind of
    # mistake this system must never make on a compliance-sensitive
    # question, so both are explicit, separately-set booleans.
    work_authorized = Column(Boolean, nullable=True)          # authorized to work RIGHT NOW, for the country the question means
    requires_sponsorship = Column(Boolean, nullable=True)     # will need employer sponsorship now or in the future
    visa_type = Column(String, nullable=True)                 # e.g. "H1B", "F1 OPT", "None" — free text, never inferred
    sponsorship_countries = Column(JSONB, nullable=True)      # list[str] — which countries `work_authorized` applies to, e.g. ["USA"]

    preferred_locations = Column(JSONB, nullable=True)  # list[str]
    remote_preference = Column(String, nullable=True)   # see VALID_REMOTE_PREFERENCES

    # Three facts real ATS forms ask for constantly that had nowhere to live in
    # this table, so each one was left blank (or, for education level,
    # re-derived by the LLM from the résumé on every single application).
    # All observed unanswered on one live Lever posting.
    #
    # `highest_education_level` is deliberately FREE TEXT in the form's own
    # vocabulary ("Bachelor's Degree", "Master's Degree") rather than a closed
    # enum: every ATS words its own education dropdown differently, and the
    # value's only job is to be matchable against the options a given form
    # actually offers. When it's empty the LLM+résumé path still answers the
    # question exactly as it does today — this column only makes the common
    # case deterministic and free.
    highest_education_level = Column(String, nullable=True)
    willing_to_relocate = Column(Boolean, nullable=True)

    # "Preferred name" / "What should we call you?" — a distinct field on most
    # Greenhouse and Lever forms, sitting right next to legal first/last name.
    # `format_preferred_name` falls back to `first_name` when this is empty,
    # which is not a guess: a form asking what to call someone is correctly
    # answered with their first name when they haven't said otherwise.
    preferred_name = Column(String, nullable=True)
    # Current compensation. A SEPARATE fact from `expected_salary`, for the same
    # reason `work_authorized` is separate from `requires_sponsorship`: forms ask
    # both, they are different numbers, and the classifier used to route "current
    # CTC" to the expected-salary formatter — so the candidate's expected number
    # was typed into a field asking what they earn today. A wrong answer, not a
    # blank one, which is the worse of the two failures.
    current_salary = Column(Float, nullable=True)
    current_salary_currency = Column(String, nullable=True)
    # "How did you hear about this job?" — asked on nearly every form, and
    # unanswerable from anything else in this table, so it was either left blank
    # or composed by the LLM out of nothing.
    referral_source = Column(String, nullable=True)
    employment_type_preference = Column(String, nullable=True)  # see VALID_EMPLOYMENT_TYPES
    # list[{"language": str, "proficiency": str}] — see VALID_LANGUAGE_PROFICIENCIES.
    # Structured rather than a flat list of names because the question forms
    # actually ask is "are you fluent in English?", and answering that from a
    # bare mention of English in a list would be an inference about degree. A
    # live Lever posting asked exactly this as a required radio group (Yes / No /
    # Limited Working Proficiency) and it was left blank.
    languages = Column(JSONB, nullable=True)
    # "Are you willing to complete a background check?" Tri-state for the same
    # reason `marketing_opt_in` is: `None` means never asked, and agreeing to a
    # background check on someone's behalf because they were silent is not a
    # thing this system does.
    willing_background_check = Column(Boolean, nullable=True)
    # "Yes, <company> may contact me about future roles" — the marketing/
    # talent-pool opt-in nearly every form ends with. Tri-state on purpose and
    # for the same reason the demographic columns are: `None` means the user
    # was never asked, and an un-asked consent is NEVER inferred as consent.
    # `automation/ats/base.py` ticks one of these boxes only for an explicit
    # `True`; `False` and `None` both leave it exactly as the page rendered it.
    marketing_opt_in = Column(Boolean, nullable=True)

    # Skills — structured JSONB rather than a separate table: read/written as
    # one unit (`PUT /profile/skills`), never filtered/joined on independently.
    # Shape: {programming_languages, frameworks, tools, certifications,
    #         technical_skills, soft_skills} — each a list[str].
    skills = Column(JSONB, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class EducationEntry(Base):
    __tablename__ = "education_entries"

    education_id = Column(String, primary_key=True)
    profile_id = Column(String, ForeignKey("candidate_profiles.profile_id", ondelete="CASCADE"), nullable=False, index=True)
    degree = Column(String, nullable=True)
    university = Column(String, nullable=True)
    field_of_study = Column(String, nullable=True)
    start_date = Column(String, nullable=True)  # free-form ("2018" / "2018-08") — resumes rarely give full dates
    end_date = Column(String, nullable=True)
    gpa = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ExperienceEntry(Base):
    __tablename__ = "experience_entries"

    experience_id = Column(String, primary_key=True)
    profile_id = Column(String, ForeignKey("candidate_profiles.profile_id", ondelete="CASCADE"), nullable=False, index=True)
    company_name = Column(String, nullable=True)
    job_title = Column(String, nullable=True)
    start_date = Column(String, nullable=True)
    end_date = Column(String, nullable=True)  # None/"" == current role
    description = Column(Text, nullable=True)
    skills_used = Column(JSONB, nullable=True)  # list[str]
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ProfileDocument(Base):
    """Resume versions, cover letters, certificates, and other uploaded
    documents. `job_type_tag` + `is_default` let `ApplicationFlowManager`
    (Phase 4) auto-pick the right resume for a given job without asking."""

    __tablename__ = "profile_documents"

    document_id = Column(String, primary_key=True)
    profile_id = Column(String, ForeignKey("candidate_profiles.profile_id", ondelete="CASCADE"), nullable=False, index=True)
    document_type = Column(String, nullable=False)  # see VALID_DOCUMENT_TYPES
    label = Column(String, nullable=True)            # e.g. "Backend-focused resume"
    job_type_tag = Column(String, nullable=True)      # e.g. "backend", "data-science"
    original_filename = Column(String, nullable=False)
    stored_path = Column(String, nullable=False)
    file_hash = Column(String, nullable=False, index=True)
    is_default = Column(Boolean, nullable=False, default=False)
    uploaded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class CandidateDemographics(Base):
    """One row per user — voluntary equal-opportunity/EEO answers (gender,
    veteran status, disability status, race/ethnicity).

    Deliberately a SEPARATE table from `CandidateProfile`, not more columns
    on it (see PART 2 of the request that created this): these are not
    professional/contact facts an ATS *needs* to route an application, and
    mixing them into the same row that every fill pass reads/writes raises
    the risk one of them accidentally gets treated like an ordinary
    deterministic field. They also have a fundamentally different consent
    model — a candidate can refuse to answer any one of them individually
    (`decline_to_answer`/`self_described`), and separating the table makes
    "the user was never asked" (no row / a `None` column) and "the user was
    asked and declined" (an explicit `decline_to_answer` value) distinguishable.

    HARD RULE, enforced in `automation/forms/answer_engine.py`, never here:
    these values are NEVER inferred, guessed, or generated by the LLM. A
    demographic screening question on an ATS form is answered ONLY from a
    value already stored in this table; if there isn't one yet, the
    question is left unanswered and the run is routed to human review so a
    person is asked once, and the answer saved here for reuse on every
    future application (see the module docstring of `answer_engine.py`)."""

    __tablename__ = "candidate_demographics"

    id = Column(String, primary_key=True)
    candidate_id = Column(String, ForeignKey("candidate_profiles.profile_id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    gender = Column(String, nullable=True)              # see VALID_GENDER_VALUES
    veteran_status = Column(String, nullable=True)       # see VALID_VETERAN_STATUS_VALUES
    disability_status = Column(String, nullable=True)    # see VALID_DISABILITY_STATUS_VALUES
    race_ethnicity = Column(String, nullable=True)       # free text — EEO race/ethnicity categories vary by country/form

    # Pronouns. Identity data, so it belongs here under the never-inferred rule
    # above rather than on CandidateProfile — and it is the single field where
    # letting an LLM answer would be worst: the only thing a model could infer
    # pronouns FROM is the candidate's name, which is exactly how you misgender
    # someone on a job application.
    #
    # Free text, NOT a closed enum: one live Lever form offers nine pronoun sets
    # plus "Use name only" and "Custom", and a fixed set would force anyone
    # outside it into "self_described". Store it the way the form words it
    # ("he/him", "she/her", "they/them") and it matches directly.
    pronouns = Column(String, nullable=True)
    # list[str] — for the "select all that apply" ethnicity checkbox groups
    # (Lever renders eight of them). `race_ethnicity` above stays the answer to
    # a single-choice race/ethnicity question; this is the multi-choice form of
    # the same fact, and `automation/forms/answer_engine.py` falls back to
    # `[race_ethnicity]` when only that one is set.
    ethnicities = Column(JSONB, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# --- Application tracking (auto-apply platform, Phase 4) -------------------

class Application(Base):
    """One row per (user, job) apply attempt — the durable record `app/`
    keeps once `automation/` hands back an `ApplicationRunResult` (see
    `automation/interfaces.py` and `app/services/application_repository.py`).
    `automation/` itself never writes to this table; only `app/` does."""

    __tablename__ = "applications"
    __table_args__ = (
        # Idempotency (ARCHITECTURE.md §"Compliance & Risk"): never double-apply
        # to the same job for the same user.
        UniqueConstraint("user_id", "job_url_hash", name="uq_applications_user_job_url"),
    )

    application_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    job_url = Column(Text, nullable=False)
    job_url_hash = Column(String, nullable=False, index=True)  # sha256(job_url) — see application_repository
    company = Column(String, nullable=True)
    position = Column(String, nullable=True)
    ats_platform = Column(String, nullable=True)  # filled in once ATSDetector runs
    status = Column(String, nullable=False, default="pending")  # see VALID_APPLICATION_STATUSES
    autopilot_enabled = Column(Boolean, nullable=False, default=False)
    applied_date = Column(DateTime, nullable=True)
    resume_used = Column(String, ForeignKey("profile_documents.document_id", ondelete="SET NULL"), nullable=True)
    confidence_score = Column(Float, nullable=True)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class AutomationRun(Base):
    """One row per actual `ApplicationFlowManager.run()` attempt (§14 Logging
    and Debugging) — an `Application` can have more than one if a run is
    retried. Stores exactly what `ApplicationRunResult` carries back from
    `automation/`, plus timing."""

    __tablename__ = "automation_runs"

    run_id = Column(String, primary_key=True)
    application_id = Column(String, ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=False, index=True)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, nullable=False)  # snapshot of ApplicationRunResult.status for this attempt
    screenshot_paths = Column(JSONB, nullable=True)  # list[str]
    trace_path = Column(String, nullable=True)
    error_log = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)


# --- Screening-question answer cache (Phase 6) ------------------------------

class AnswerCacheEntry(Base):
    """One cached answer to one screening question, per user —
    `automation/forms/answer_engine.py::ApplicationAnswerEngine` (Phase 6)
    reads/writes this via `app/services/answer_cache_repository.py` so a
    question asked again on a later application (extremely common — the same
    ATS family tends to reuse near-identical screening questions across
    postings) costs nothing the second time, whether it was originally
    answered deterministically from the candidate's profile or by an LLM
    call. Keyed by a hash of the *normalized* question text (see the
    repository module) — this is an exact-match cache, not yet a semantic
    one; see that module's docstring for the pgvector follow-up this could
    become."""

    __tablename__ = "answer_cache"
    __table_args__ = (
        UniqueConstraint("user_id", "question_hash", name="uq_answer_cache_user_question"),
    )

    cache_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    question_hash = Column(String, nullable=False, index=True)
    question_text = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    source = Column(String, nullable=False)  # see VALID_ANSWER_SOURCES
    confidence = Column(Float, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
