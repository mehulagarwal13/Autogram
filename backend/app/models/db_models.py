from sqlalchemy import Boolean, Column, String, DateTime, Text, Float, Integer, ForeignKey, Index, UniqueConstraint, text
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
    # HITL platform: a human explicitly declined to continue (reject / go-back
    # before submission) — distinct from "failed" (nothing malfunctioned) and
    # from the RETRYABLE set (a cancelled application is not auto-retried).
    "cancelled",
}
# Browser-extension delivery — see Application.source.
VALID_APPLICATION_SOURCES = {"server_automation", "browser_extension"}
# Where a cached screening-question answer originally came from (Phase 6,
# app/services/answer_cache_repository.py) — "cache" isn't a stored value
# here, it's what a lookup hit is *reported as* by the caller.
VALID_ANSWER_SOURCES = {"deterministic", "llm"}

# HITL platform — per-question ledger (see ApplicationQuestion below).
# "needs_user_input" is distinct from "human": it means "nothing answered
# this yet" (surfaced for review), where "human" means an answer a person
# actually typed via the review UI (`ApplicationQuestion.human_answer`).
VALID_QUESTION_SOURCES = {"profile", "answer_memory", "llm", "vision", "needs_user_input", "human"}
VALID_CONFIDENCE_LEVELS = {"HIGH", "MEDIUM", "LOW"}
VALID_QUESTION_REVIEW_STATUSES = {"auto_filled", "pending_review", "approved", "edited", "rejected"}

HIGH_CONFIDENCE_THRESHOLD = 0.85   # mirrors ApplicationFlowManager.AUTO_SUBMIT_CONFIDENCE_THRESHOLD
LOW_CONFIDENCE_THRESHOLD = 0.6     # mirrors ApplicationFlowManager.NEEDS_REVIEW_CONFIDENCE_THRESHOLD


def confidence_level_for(source: str, confidence: float) -> str:
    """The HIGH/MEDIUM/LOW bucket a question-answer review UI shows, derived
    from the same two thresholds `ApplicationFlowManager.decide_action` already
    gates auto-submit/review on — so a question this labels HIGH is exactly one
    the flow manager itself would trust, not a second, independently-tuned
    notion of confidence."""
    if source in ("profile", "answer_memory") or confidence >= HIGH_CONFIDENCE_THRESHOLD:
        return "HIGH"
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        return "LOW"
    return "MEDIUM"


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

    # --- third tier of form-answer fields -----------------------------------
    # Same rule as the two tiers above: one column per question real ATS forms
    # ask that nothing already in this table could answer, and NOTHING here is
    # ever inferred — every one is set only by the user through
    # `POST`/`PATCH /profile`.
    #
    # Contact/identity block, all three asked on ordinary application forms and
    # all three previously unfillable:
    #
    # `middle_name` — Workday and Taleo forms ask for a legal middle name right
    # next to first/last. `full_name` can't answer it (splitting a full name on
    # whitespace to guess which part is the middle name is exactly the kind of
    # guess this codebase doesn't make).
    #
    # `postal_code` — the ZIP/postal field of the address block. Deliberately
    # NOT folded into the Fernet-encrypted `address_encrypted`: it is asked as
    # its own input, so it has to be readable as its own value, and it sits with
    # `city`/`state`/`country` (also plaintext) rather than with the street
    # address it would take to actually locate someone.
    #
    # `time_zone` — "Which time zone are you based in?", asked by nearly every
    # remote posting. Free text in the form's own vocabulary ("IST", "GMT+5:30",
    # "US Eastern") for the same reason `highest_education_level` is: the value's
    # only job is to be matchable against the options a given form offers.
    # Named `time_zone`, not `timezone`, so it can't be misread as the
    # `datetime.timezone` this module imports.
    middle_name = Column(String, nullable=True)
    postal_code = Column(String, nullable=True)
    time_zone = Column(String, nullable=True)

    # The candidate's own summary/headline ("Tell us about yourself", "Profile
    # summary"). Text, not String: it's a paragraph. Written by the user, which
    # is the point — a summary field otherwise gets composed from scratch by the
    # LLM on every single application, differently each time.
    professional_summary = Column(Text, nullable=True)

    # "What is your earliest start date?" — a DIFFERENT shape of the same fact
    # as `notice_period_days`, and forms ask for it as a date, not a duration.
    # Free-form string like the education/experience dates for the same reason:
    # candidates give "2026-09-01", "September 2026", or "Immediately", and
    # coercing that into a `Date` would reject two of the three.
    earliest_start_date = Column(String, nullable=True)

    # "Do you hold an active security clearance? If so, which level?" — asked on
    # every US defense/government-adjacent posting. Free text ("Active Secret",
    # "None") and never inferred: claiming a clearance the candidate doesn't
    # hold is a false statement on a federal application.
    security_clearance = Column(String, nullable=True)

    # "If you were referred, who referred you?" — a SEPARATE field from
    # `referral_source` on Greenhouse's referral block, and answering it with
    # the source ("LinkedIn") puts a website where a person's name goes.
    referrer_name = Column(String, nullable=True)

    # Tri-state booleans, all five for the same reason `marketing_opt_in` is:
    # `None` means the user was never asked, and none of these is ever answered
    # from silence. Each one is a question a live posting asked that had no
    # column to answer from.
    age_over_18 = Column(Boolean, nullable=True)            # "Are you at least 18 years of age?"
    willing_to_travel = Column(Boolean, nullable=True)      # "Are you willing to travel for this role?"
    # "Do you require relocation assistance?" — NOT the same question as
    # `willing_to_relocate`, and the distinction is money: a candidate can be
    # happy to move and still need the employer to pay for it, or be happy to
    # move at their own expense. `automation/forms/question_classifier.py`
    # already refused to answer this one from `willing_to_relocate` (see its
    # WILLING_TO_RELOCATE note) and so left it blank; this is where the real
    # answer lives.
    requires_relocation_assistance = Column(Boolean, nullable=True)
    willing_drug_test = Column(Boolean, nullable=True)      # "Are you willing to complete a pre-employment drug screening?"
    has_drivers_license = Column(Boolean, nullable=True)    # "Do you hold a valid driver's license?"

    # Skills — structured JSONB rather than a separate table: read/written as
    # one unit (`PUT /profile/skills`), never filtered/joined on independently.
    # Shape: {programming_languages, frameworks, tools, certifications,
    #         technical_skills, soft_skills} — each a list[str].
    skills = Column(JSONB, nullable=True)

    # HITL platform — account-level kill switch (PHASE2_ARCHITECTURE.md
    # Initiative 3): a belt-and-suspenders backstop that hard-stops every
    # autopilot run for this user regardless of any per-application
    # `autopilot_enabled` flag. Checked fresh from the DB at the top of every
    # page in ApplicationFlowManager's loop, not just at dispatch time, and
    # fails CLOSED (DB unreachable == treated as engaged) — see
    # `app/api/applications.py::_is_kill_switch_engaged`.
    autopilot_globally_disabled = Column(Boolean, nullable=False, default=False)

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
        #
        # This was a FULL `UniqueConstraint(user_id, job_url_hash)`, i.e. one
        # row per (user, job) forever. That made a deliberate re-application
        # impossible to represent: the only way to attempt a job twice was to
        # reset the existing row via `retry_application`, which overwrites
        # `status='applied'` and `applied_date` — destroying the record that
        # the first application ever happened.
        #
        # It is now PARTIAL, covering only the statuses where an attempt is
        # actively being automated. The original constraint was really doing
        # two jobs at once; those are now split across the same two layers the
        # autonomous path already uses, with NEITHER guarantee weakened:
        #
        #   1. "never two automations on one job at once" -> this index. Two
        #      concurrent inserts still cannot both commit.
        #   2. "never silently apply twice after success" -> the route-level
        #      lifetime check (`automation_ownership.find_submitted_application`
        #      in `POST /applications/start`), which refuses unless the caller
        #      explicitly acknowledges the exact prior submission.
        #
        # Historical `applied` rows sit OUTSIDE the index, so every past
        # attempt is preserved verbatim alongside the new one — which is the
        # entire point. Retryable statuses (`failed`/`manual_required`/
        # `needs_review`) are outside it too; those still retry in place via
        # `retry_application`, decided by the route, exactly as before.
        Index(
            "uq_applications_active_job",
            "user_id", "job_url_hash",
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'processing', 'copilot_review')"
            ),
        ),
    )

    application_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    job_url = Column(Text, nullable=False)
    job_url_hash = Column(String, nullable=False, index=True)  # sha256(job_url) — see application_repository
    company = Column(String, nullable=True)
    position = Column(String, nullable=True)
    ats_platform = Column(String, nullable=True)  # the adapter that actually ran — e.g. "custom" for GenericAdapter
    # The pre-flight `ATSDetector` guess, kept separate from `ats_platform`
    # above so "detected smartrecruiters, GenericAdapter actually filled it"
    # is visible instead of silently collapsed into one field. `None` when
    # detection never ran differently from what was resolved (or for rows
    # written before this column existed).
    detected_ats_platform = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending")  # see VALID_APPLICATION_STATUSES
    autopilot_enabled = Column(Boolean, nullable=False, default=False)
    applied_date = Column(DateTime, nullable=True)
    resume_used = Column(String, ForeignKey("profile_documents.document_id", ondelete="SET NULL"), nullable=True)
    confidence_score = Column(Float, nullable=True)
    failure_reason = Column(Text, nullable=True)
    # HITL platform — how many pages of a multi-page application were
    # actually processed by the last run, straight from
    # `ApplicationRunResult.pages_completed`. Powers the pre-submission
    # review summary (§7) without re-deriving it from automation_runs/logs.
    pages_completed = Column(Integer, nullable=True)
    # Browser-extension delivery — which "engine" is driving this application:
    # the server-side Playwright automation (`automation/`, the default) or
    # the MV3 browser extension (`extension/`), which runs inside the user's
    # own already-logged-in Chrome tab instead. See VALID_APPLICATION_SOURCES.
    # Determines whether `POST /applications/start` dispatches a server-side
    # Playwright run at all (see `app/api/applications.py::start_application`)
    # and which endpoints ever write to this row afterward —
    # `POST /applications/{id}/approve` (server automation's copilot replay)
    # vs `POST /applications/{id}/report-status` (the extension self-reporting).
    source = Column(String, nullable=False, default="server_automation")
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
    # HITL platform (§18 Observability) — this run's structured progress log,
    # list[{"timestamp": iso8601 str, "message": str}], captured by a
    # per-run logging handler scoped to this application_id (see
    # `automation/applications/application_flow_manager.py`). Surfaced
    # verbatim by `GET /applications/{id}/runs` for the dashboard's activity
    # log — a human-readable trail distinct from the Playwright trace/screenshots.
    log_lines = Column(JSONB, nullable=True)


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
    # HITL platform — enables semantic (near-duplicate) answer-memory lookup:
    # a normalized-text miss falls back to cosine similarity over this vector
    # (via the same local sentence-transformers model job/resume matching
    # already uses — no extra API cost) before ever reaching the LLM. See
    # `app/services/answer_cache_repository.py::find_similar_answer`. This was
    # the exact pgvector follow-up this module's docstring already called out
    # as "a natural follow-up once this exact-match version has real usage
    # data" — nullable so existing rows keep working until they're re-saved.
    embedding_vector = Column(Vector(EMBEDDING_DIM), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# --- HITL platform: per-question ledger + audit log -------------------------

class ApplicationQuestion(Base):
    """One row per screening question ASKED on one application run — the
    ledger the Answer Review UI, the Application Detail page, and the
    pre-submission review summary (§7) all read from.

    Distinct from `AnswerCacheEntry`: the cache is a per-user, cross-application
    memory of "what did we answer this question with last time"; this table is
    a per-APPLICATION record of "what was asked on THIS application, from
    where, at what confidence, and what a human did about it" — the two serve
    different questions and neither can stand in for the other. Written by
    `automation/forms/answer_engine.py::ApplicationAnswerEngine.answer_batch()`
    as it answers each question (see that module for the `source`/confidence
    semantics), and updated by a human via
    `POST /applications/{id}/questions/{question_id}/review`.
    """

    __tablename__ = "application_questions"

    question_id = Column(String, primary_key=True)
    application_id = Column(String, ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=False, index=True)
    page_number = Column(Integer, nullable=True)
    question_text = Column(Text, nullable=False)
    field_type = Column(String, nullable=True)  # text/textarea/select/radio/checkbox/date/number/file
    available_options = Column(JSONB, nullable=True)  # list[str] — echoed verbatim from the DOM, see answer_engine.Question
    answer = Column(Text, nullable=True)
    source = Column(String, nullable=False)  # see VALID_QUESTION_SOURCES
    confidence = Column(Float, nullable=False)
    confidence_level = Column(String, nullable=False)  # see VALID_CONFIDENCE_LEVELS / confidence_level_for()
    review_status = Column(String, nullable=False, default="auto_filled")  # see VALID_QUESTION_REVIEW_STATUSES
    human_answer = Column(Text, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ApplicationAuditLog(Base):
    """Append-only record of decisions/approvals for one application — DISTINCT
    from `AutomationRun` (which tracks execution mechanics: screenshots, trace,
    error log) and from `ApplicationQuestion` (which tracks individual answers).
    This is the compliance-facing trail of "who decided what, and when":
    autopilot run started, human approved/rejected, kill switch triggered.

    HARD RULE: no route ever updates or deletes a row here (see
    `app/services/audit_log_repository.py` — it exposes only `record_event`,
    never an update/delete). It is the record of "did the system submit
    something without explicit permission," and a mutable audit log defeats
    the entire point of keeping one.

    `application_id` / `autonomous_task_id` are mutually exclusive-ish (exactly
    one is expected to be set per row, enforced in `audit_log_repository`, not
    at the DB level) — this one table is shared between the deterministic
    per-ATS path (`applications.application_id`) and the general-purpose
    autonomous agent (`autonomous_tasks.task_id`), rather than standing up a
    second copy-pasted audit table for the latter."""

    __tablename__ = "application_audit_log"

    log_id = Column(String, primary_key=True)
    application_id = Column(String, ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=True, index=True)
    autonomous_task_id = Column(String, ForeignKey("autonomous_tasks.task_id", ondelete="CASCADE"), nullable=True, index=True)
    user_id = Column(String, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String, nullable=False)  # e.g. autopilot_run_started, human_approved, human_rejected, kill_switch_triggered
    actor = Column(String, nullable=False)  # "system" or a user_id
    event_metadata = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# --- Autonomous agent platform (general-purpose observe/decide/act loop) ---
# Distinct from the `Application` / `AutomationRun` / `ApplicationQuestion`
# tables above, which back the deterministic, per-ATS-adapter
# `ApplicationFlowManager` path. `AutonomousTask` is the persistence for the
# NEW general-purpose LLM-driven agent
# (`automation/agents/autonomous/loop.py::AutonomousAgentLoop`) that has no
# per-ATS branching at all. The two systems are intentionally independent —
# see `AUTONOMOUS_AGENT.md` for how they coexist. This table is one row per
# autonomous task attempt (roughly analogous to one `Application` +
# `AutomationRun` combined, since here there is exactly one continuous run
# per task rather than several discrete attempts).
VALID_AUTONOMOUS_TASK_STATUSES = {
    "CREATED", "ANALYZING_JOB", "RUNNING", "WAITING_FOR_HUMAN",
    "WAITING_FOR_APPROVAL", "RESUMING", "COMPLETED", "FAILED", "CANCELLED",
}
# Statuses where the loop is not actively driving the browser right now and a
# human (or the API) can act on the task next.
AUTONOMOUS_TASK_PAUSED_STATUSES = frozenset({"WAITING_FOR_HUMAN", "WAITING_FOR_APPROVAL"})
AUTONOMOUS_TASK_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
#: Statuses where this task still owns the job: a browser tab either is, or is
#: about to be, driving that application. Exactly the complement of the
#: terminal set, derived rather than re-listed so a new status can never be
#: silently omitted from the duplicate-automation guard.
#: Used by `uq_autonomous_tasks_active_job` (below) and
#: `app/services/automation_ownership.py`.
AUTONOMOUS_TASK_ACTIVE_STATUSES = frozenset(
    VALID_AUTONOMOUS_TASK_STATUSES - AUTONOMOUS_TASK_TERMINAL_STATUSES
)


class AutonomousTask(Base):
    """One row per autonomous-agent job-application task. Every field the
    task spec requires is a top-level column so the status/resume/answer/
    approve endpoints (`app/api/autonomous_agent.py`) can each touch exactly
    what they need without deserializing one big opaque blob:

    - `candidate_profile` / `job_information`: point-in-time snapshots taken
      at task start (`ANALYZING_JOB`) — NOT live references to
      `CandidateProfile`/`JobRecord`, so a task already in flight is
      unaffected by the user editing their profile mid-run.
    - `current_browser_state`: the last `PageState` the observer produced
      (see `automation/agents/autonomous/observer.py`) — what the status API
      and the resume flow show/re-derive from.
    - `action_history`: append-only list of every action the executor actually
      dispatched (`{action_type, params, result, timestamp}`) — the audit
      trail the "never invent, never silently submit" guarantees rely on.
    - `human_intervention`: the CURRENT pending intervention request while
      `current_status == "WAITING_FOR_HUMAN"`
      (`{type, reason, message, information_required}`); cleared on resume.
    - `confirmed_answers`: task-scoped Q&A the human supplied via
      `POST .../answer` — used as agent context on every subsequent decision
      step, never written back into the global profile/answer cache (default
      posture — see `AUTONOMOUS_AGENT.md`'s no-invention section).
    - `final_result`: evidence payload for `APPLICATION_READY_FOR_SUBMISSION`
      / `TASK_COMPLETED` / `TASK_FAILED` decisions.
    """

    __tablename__ = "autonomous_tasks"
    __table_args__ = (
        # At most ONE ACTIVE autonomous task per (user, job) — the
        # autonomous-path counterpart to `uq_applications_user_job_url`, and
        # the reason two simultaneous `POST /agent/tasks` calls cannot both
        # win: whichever INSERT commits second raises IntegrityError.
        #
        # PARTIAL (a `WHERE` clause), which is the whole point: once a task
        # reaches COMPLETED/FAILED/CANCELLED it drops out of the index, so a
        # retry after a failure or cancellation inserts cleanly. A plain
        # unique constraint would have permanently barred the job after the
        # first attempt.
        Index(
            "uq_autonomous_tasks_active_job",
            "user_id", "job_url_hash",
            unique=True,
            postgresql_where=text(
                "current_status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')"
            ),
        ),
    )

    task_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    job_url = Column(String, nullable=False)
    #: sha256 of the normalized job URL, computed by the EXISTING
    #: `app/services/application_repository.py::compute_job_url_hash` — the
    #: same function (and therefore the same normalization: strip + lowercase,
    #: nothing more) the deterministic path already uses for
    #: `Application.job_url_hash`. Sharing one implementation is what lets the
    #: two independent paths recognise each other's jobs; normalizing
    #: differently in each would make cross-path detection silently miss.
    job_url_hash = Column(String, nullable=False, index=True)
    original_objective = Column(Text, nullable=False)

    candidate_profile = Column(JSONB, nullable=True)
    job_information = Column(JSONB, nullable=True)

    current_status = Column(String, nullable=False, default="CREATED")  # see VALID_AUTONOMOUS_TASK_STATUSES
    current_browser_state = Column(JSONB, nullable=True)
    action_history = Column(JSONB, nullable=False, default=list)
    application_progress = Column(JSONB, nullable=False, default=dict)
    human_intervention = Column(JSONB, nullable=True)
    confirmed_answers = Column(JSONB, nullable=False, default=dict)
    uploaded_documents = Column(JSONB, nullable=False, default=list)
    final_result = Column(JSONB, nullable=True)
    error = Column(Text, nullable=True)

    # Off by default (compliance: no auto-submit without explicit per-task
    # approval — see POST .../approve). Never flipped anywhere except that
    # one endpoint.
    auto_submit_approved = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# --- Human-in-the-loop interaction requests (OTP / MFA / CAPTCHA / login / ---
# --- ambiguous-question / confirmation pauses raised by the autonomous     ---
# --- agent — see `automation/agents/autonomous/loop.py` and               ---
# --- AUTONOMOUS_AGENT.md's "Human-in-the-loop" section).
#
# `AutonomousTask.human_intervention` (above) stays as-is: a denormalized
# "what's pending right now" snapshot the existing status-polling endpoints
# already read (`GET /agent/tasks/{id}`, the `/resume` and `/answer` routes).
# This table is the durable, individually-addressable record of EVERY pause
# a task ever had — one row per pause, never overwritten — so it can carry
# its own id, status, and expiry independent of the task's coarser status,
# and so `app/api/human_interaction.py` has something to address by id.
VALID_HUMAN_REQUEST_TYPES = frozenset({
    "OTP_REQUIRED", "LOGIN_REQUIRED", "MFA_REQUIRED", "CAPTCHA_REQUIRED",
    "USER_CONFIRMATION_REQUIRED", "ANSWER_REQUIRED", "MANUAL_ACTION_REQUIRED",
    "UNKNOWN_BLOCKER",
})
#: Request types whose response carries a transient secret (a verification
#: code) that must NEVER be persisted to this table, `AutonomousTask`, logs,
#: or any API response — see `automation/agents/autonomous/runner.py`'s
#: `deliver_secret`, the only place such a value is ever held, and only
#: in-process, and only until the automation loop consumes it once.
SECRET_HUMAN_REQUEST_TYPES = frozenset({"OTP_REQUIRED", "MFA_REQUIRED"})

VALID_HUMAN_REQUEST_STATUSES = frozenset({
    "PENDING", "RESPONDED", "RESUMING", "RESOLVED", "EXPIRED", "CANCELLED", "FAILED",
})
HUMAN_REQUEST_TERMINAL_STATUSES = frozenset({"RESOLVED", "EXPIRED", "CANCELLED", "FAILED"})


class HumanInteractionRequest(Base):
    """One row per human-in-the-loop pause the autonomous agent ever raised
    for a task. Deliberately has NO column that could hold a secret
    (OTP/MFA code, password, session token) — `safe_metadata` is for
    non-secret context only (masked destination, which field/button was
    detected, whether the site exposes a resend action), enforced by
    convention in `app/services/human_interaction_repository.py` and never
    populated with a raw code anywhere in this codebase."""

    __tablename__ = "human_interaction_requests"

    request_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = Column(String, ForeignKey("autonomous_tasks.task_id", ondelete="CASCADE"), nullable=False, index=True)

    request_type = Column(String, nullable=False)  # see VALID_HUMAN_REQUEST_TYPES
    status = Column(String, nullable=False, default="PENDING")  # see VALID_HUMAN_REQUEST_STATUSES

    title = Column(String, nullable=True)
    message = Column(Text, nullable=False)
    safe_metadata = Column(JSONB, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=True)
    responded_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)


# --- Chat transcript (HITL conversation surface) ----------------------------

#: Who produced a chat message.
#:   agent  — Autogram itself: a question, a status note, a pause explanation.
#:   user   — the human's reply typed into the chat panel.
#:   system — workflow milestones rendered inline ("Application submitted").
VALID_CHAT_ROLES = frozenset({"agent", "user", "system"})


class ChatMessage(Base):
    """The user-visible conversation for ONE automation attempt.

    Why this exists as its own table rather than being derived from
    `HumanInteractionRequest` or `ApplicationAuditLog`:

    * `HumanInteractionRequest` is the *state machine* for a pause — one row
      per blocker, with a status that drives resume/expiry. It deliberately
      holds no conversational history, and a chat needs the turns BETWEEN
      pauses too ("filling page 2 of 3", the user's free-text answer).
    * `ApplicationAuditLog` is append-only compliance evidence, written for
      auditors and never for display. Rendering it as chat would leak internal
      event vocabulary into the UI and, worse, tempt someone to start writing
      user-facing prose into the compliance trail.

    Both are kept as they are; this is the presentation-layer transcript and
    references the pause it belongs to via `human_request_id` when there is
    one, so the frontend can render an answerable prompt inline instead of a
    separate modal.

    SECRETS: `content` is user-visible prose and is persisted, so an OTP/MFA
    code must NEVER be written here. The response routes for
    `SECRET_HUMAN_REQUEST_TYPES` record only that a code was submitted — see
    `chat_repository.record_secret_submission`, which is the only sanctioned
    way to log that turn. This mirrors the rule `HumanInteractionRequest`
    already follows for `safe_metadata`.

    Shared between both automation paths by the same convention
    `ApplicationAuditLog` uses: exactly one of `application_id` /
    `autonomous_task_id` is set per row, enforced in the repository rather
    than at the DB level, so the autonomous agent does not need a second
    copy-pasted transcript table.
    """

    __tablename__ = "chat_messages"

    message_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    application_id = Column(String, ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=True, index=True)
    autonomous_task_id = Column(String, ForeignKey("autonomous_tasks.task_id", ondelete="CASCADE"), nullable=True, index=True)

    role = Column(String, nullable=False)  # see VALID_CHAT_ROLES
    content = Column(Text, nullable=False)

    #: Set when this message IS a human-in-the-loop prompt, so the UI can render
    #: the right control (OTP field, "CAPTCHA completed" button, free-text box)
    #: and know whether it is still answerable.
    human_request_id = Column(
        String, ForeignKey("human_interaction_requests.request_id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    #: Non-secret display context only — same rule as
    #: `HumanInteractionRequest.safe_metadata`.
    safe_metadata = Column(JSONB, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


# --- §9 data retention --------------------------------------------------------

class RetentionPolicy(Base):
    """One row per user who has customized their retention windows — a
    missing row means "use the global defaults" (the same values these
    columns default to), so a user who never touches this setting is
    indistinguishable, at the DB level, from one who explicitly confirmed
    the defaults. See `app/services/retention_repository.py`.

    There is deliberately no `document_retention_days` column — a résumé is
    always a reference to the user's own permanent document library
    (`ProfileDocument`), never a per-application generated file, so there is
    nothing to purge on a schedule (see `retention_service.py`'s module
    docstring for the full reasoning, and
    `automation/tests/test_retention_service.py::
    test_retention_purge_never_touches_profile_documents` for the invariant
    guard). Add it back — with real enforcement, not just the column — if
    per-application document generation ever exists.
    """

    __tablename__ = "retention_policies"

    user_id = Column(String, ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)

    screenshot_retention_days = Column(Integer, nullable=False, default=30)
    run_history_retention_days = Column(Integer, nullable=False, default=90)
    hitl_request_retention_days = Column(Integer, nullable=False, default=14)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class RetentionPurgeLog(Base):
    """Append-only record of what the retention purge job actually did, one
    row per (run, category) — e.g. a nightly run writes up to four rows
    (screenshots / run_history / hitl_requests / purge_log itself). This is
    itself subject to a retention rule (`PURGE_LOG_RETENTION_DAYS` in
    `retention_service.py`), which is why it has no FK to any user: these
    rows describe a SYSTEM-wide job execution across every user in one pass,
    not one user's own history (a per-user manual purge, triggered via
    `POST /profile/retention-policy/purge-now`, still writes here the same
    way, scoped to that one user's counts)."""

    __tablename__ = "retention_purge_log"

    purge_id = Column(String, primary_key=True)
    run_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    category = Column(String, nullable=False)  # "screenshots" | "run_history" | "hitl_requests" | "purge_log"
    records_purged = Column(Integer, nullable=False, default=0)
    files_deleted = Column(Integer, nullable=False, default=0)
    files_failed = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)
