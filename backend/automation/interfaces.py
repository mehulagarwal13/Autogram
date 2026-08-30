"""
Integration contracts between `automation/` and the rest of this application.

ARCHITECTURE (updated): `automation/` is an internal domain module of the
same FastAPI application as `app/` — not an independently deployable
service. It is expected to import and use real `app/` code directly:
`app.core.database`, `app.core.auth`, `app.core.config`, `app.models.db_models`,
`app.services.*`, `app.ai.*`. There is no dependency-injection boundary here
and no isolation to preserve. See `automation/README.md` for the rationale
and `ARCHITECTURE.md` for the system-level picture.

1. **No circular imports.** `app/api/*` will eventually call INTO
   `automation/` (Phase 4: `app/api/applications.py` calls
   `automation.agents.job_application_agent.JobApplicationAgent.start(...)`).
   That means the dependency graph must stay one-directional:
   `app.api -> automation -> app.services/app.models/app.core/app.ai`.
   Nothing under `automation/` may import `app.api.*` — that would create a
   cycle. Importing `app.services`, `app.models`, `app.core`, `app.ai` is
   fine and expected in any direction from here.
2. **One place to update.** If `app/services/profile_repository.py`'s
   function signatures change, this file (and the handful of call sites that
   use it) is the blast radius — not every adapter file individually.
3. **Keeps browser/ATS logic out of routes and out of the DB layer.**
   Business logic still doesn't belong in `app/api/*` route handlers (use
   `app/services/*`, same as today), and `automation/browser/*` (Playwright)
   still shouldn't run raw SQL/ORM queries itself — it goes through the
   functions below, which call the existing repositories.

## Two layers of contract in this file

**A. Real integration functions (new)** — thin wrappers around actual
`app/` code (`app.services.profile_repository`, `app.services.document_storage`,
`app.ai.llm.router`, `app.services.embedding_service`, `app.core.database`,
`app.core.auth`). Prefer these — and the underlying `app/` modules directly —
for all new Phase 2+ work.

**B. Plain dataclasses/Protocols (kept for compatibility)** — `CandidateProfileView`,
`EducationView`, `ExperienceView`, `ResumeDocumentView`, `ApplicationRunResult`,
`LLMCallable`, `EncryptDecryptPair`, `EmbedCallable`. These were written under
the previous strict-isolation design and are still the type hints used by the
existing Phase 2+ stub signatures (`automation/ats/base.py`,
`automation/browser/session.py`, `automation/forms/answer_engine.py`,
`automation/agents/*.py`, `automation/workers/apply_worker.py`). They are kept
so those files keep importing successfully unchanged. `ApplicationRunResult`
in particular stays the right shape either way — until the Phase 4
`applications` / `automation_runs` tables + a repository exist, it's still
just a plain result value; once they exist, it becomes a thin wrapper around
that repository, same as everything in section A.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Protocol

from sqlalchemy.orm import Session

# =============================================================================
# A. Real integration functions — automation may (and should) use app/ directly
# =============================================================================

# --- database session --------------------------------------------------
from app.core.database import SessionLocal, get_db  # noqa: F401  (get_db re-exported for FastAPI-context callers)

# --- auth / user context -------------------------------------------------
from app.core.auth import get_current_user  # noqa: F401

# --- ORM models -----------------------------------------------------------
from app.models.db_models import (  # noqa: F401
    CandidateDemographics,
    CandidateProfile,
    EducationEntry,
    ExperienceEntry,
    ProfileDocument,
    User,
    FLUENT_LANGUAGE_PROFICIENCIES,
    VALID_DISABILITY_STATUS_VALUES,
    VALID_DOCUMENT_TYPES,
    VALID_EMPLOYMENT_TYPES,
    VALID_GENDER_VALUES,
    VALID_LANGUAGE_PROFICIENCIES,
    VALID_TRUST_LEVELS,
    VALID_VETERAN_STATUS_VALUES,
)

# --- existing repositories / services -------------------------------
from app.services import profile_repository  # noqa: F401
from app.services import answer_cache_repository  # noqa: F401 — Phase 6 screening-question answer cache
from app.services.document_storage import (  # noqa: F401
    compute_file_hash,
    delete_document_file,
    save_document_file,
)

# --- existing AI layer ----------------------------------------------------
from app.ai.llm.router import llm_router, LLMRouterError  # noqa: F401
from app.services.embedding_service import generate_embedding  # noqa: F401


@contextmanager
def automation_db_session() -> Iterator[Session]:
    """Session for automation code that runs outside a FastAPI request (a
    Celery/ARQ worker task, a LangGraph agent step). Mirrors the pattern
    `app/api/resumes.py::_run_extraction` already uses for its background
    task: its own `SessionLocal()`, closed in `finally`. Inside a FastAPI
    route, prefer the `get_db` dependency instead."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_candidate_profile(db: Session, user_id: str) -> CandidateProfile | None:
    """The ORM row itself (not a copy) — automation code reads whatever
    fields it needs directly (`profile.first_name`, `profile.skills`, ...)."""
    return profile_repository.get_by_user_id(db, user_id)


def get_candidate_profile_dict(db: Session, user_id: str) -> dict | None:
    """Decrypted, plain-dict view of the profile (phone/address decrypted via
    `app.core.crypto`) — same shape `app/api/profile.py` returns from
    `GET /profile`. Convenient when automation code wants to serialize/log a
    profile snapshot rather than hold a live ORM row."""
    profile = profile_repository.get_by_user_id(db, user_id)
    if profile is None:
        return None
    return profile_repository.profile_to_dict(profile)


def list_education(db: Session, profile_id: str) -> list[EducationEntry]:
    return profile_repository.list_education(db, profile_id)


def list_experience(db: Session, profile_id: str) -> list[ExperienceEntry]:
    return profile_repository.list_experience(db, profile_id)


def get_default_resume(db: Session, profile_id: str) -> ProfileDocument | None:
    """The document flagged `is_default=True` for `document_type="resume"`,
    or the most recently uploaded resume if none is flagged yet."""
    documents = profile_repository.list_documents(db, profile_id, document_type="resume")
    if not documents:
        return None
    return next((d for d in documents if d.is_default), documents[0])


def get_resume_for_job(db: Session, profile_id: str, job_type_tag: str | None) -> ProfileDocument | None:
    """Prefers a resume tagged for this job type (see `ProfileDocument.job_type_tag`,
    set via `POST /profile/documents/upload`); falls back to the default resume."""
    if job_type_tag:
        tagged = [
            d for d in profile_repository.list_documents(db, profile_id, document_type="resume")
            if d.job_type_tag == job_type_tag
        ]
        if tagged:
            return tagged[0]
    return get_default_resume(db, profile_id)


def list_documents(db: Session, profile_id: str, document_type: str | None = None) -> list[ProfileDocument]:
    return profile_repository.list_documents(db, profile_id, document_type=document_type)


def get_candidate_demographics(db: Session, profile_id: str) -> CandidateDemographics | None:
    """The stored equal-opportunity/EEO row for this candidate, or `None` if
    they've never been asked. `automation/forms/answer_engine.py` is the ONLY
    caller that should ever read this via `automation/` — and it never
    writes to it; the only write path in the whole codebase is the user's
    own explicit `PUT /profile/demographics` (see `app/api/profile.py`)."""
    return profile_repository.get_demographics(db, profile_id)


def generate_answer(*, task: str, prompt: str, system: str | None = None, **overrides) -> str:
    """Routes through the existing `LLMRouter` (`app/ai/llm/router.py`) —
    same retry/backoff/provider-selection logic every other part of the app
    uses. `task` must be a registered route in `app/ai/llm/registry.py`
    (Phase 6 will add an answer-generation task alongside the existing
    `resume_parse` / `job_fit_analysis` / `tailoring` routes). Raises
    `LLMRouterError` on exhausted retries — callers (Phase 6
    `ApplicationAnswerEngine`) decide whether that's a hard failure or a
    `NEEDS_REVIEW` outcome."""
    return llm_router.run(task=task, prompt=prompt, system=system, **overrides)


def embed_text(text: str) -> list[float]:
    """Same `sentence-transformers` model/vector space as resume/job
    embeddings (`app/services/embedding_service.py`) — reused so a resume
    picked by `automation/agents/profile_agent.py` (Phase 6) is comparable to
    the job-matching embeddings already in `jobs.embedding_vector` / pgvector."""
    return generate_embedding(text)


# =============================================================================
# B. Plain views / callback Protocols — kept so existing Phase 2+ stub
#    signatures keep importing unchanged (see module docstring, section B).
# =============================================================================

@dataclass(frozen=True)
class EducationView:
    degree: str | None = None
    university: str | None = None
    field_of_study: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    gpa: str | None = None


@dataclass(frozen=True)
class ExperienceView:
    company_name: str | None = None
    job_title: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    description: str | None = None
    skills_used: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResumeDocumentView:
    """A plain-data snapshot of a `ProfileDocument` row. New code can just use
    the real `ProfileDocument` ORM object (see `get_default_resume` /
    `get_resume_for_job` above) instead of building one of these."""

    document_id: str
    document_type: str
    label: str | None
    job_type_tag: str | None
    stored_path: str
    original_filename: str


@dataclass(frozen=True)
class CandidateProfileView:
    """A plain-data snapshot of a `CandidateProfile` (+ children). New code
    can just use the real `CandidateProfile` ORM object (see
    `get_candidate_profile` above) or `get_candidate_profile_dict` instead of
    building one of these."""

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
    sponsorship_countries: list[str] = field(default_factory=list)
    preferred_locations: list[str] = field(default_factory=list)
    remote_preference: str | None = None
    skills: dict = field(default_factory=dict)
    education: list[EducationView] = field(default_factory=list)
    experience: list[ExperienceView] = field(default_factory=list)


@dataclass
class ApplicationRunResult:
    """What one apply run produces. Until the Phase 4 `applications` /
    `automation_runs` tables + a repository exist, `app/api/applications.py`
    persists this by hand; afterwards this becomes a thin wrapper around e.g.
    `app.services.application_repository.save_run(result)` — same pattern as
    section A above."""

    application_id: str
    status: str  # "applied" | "failed" | "manual_required" | "needs_review" | "copilot_review"
    # The platform whose adapter actually ran this application — safety-
    # critical: `ApplicationFlowManager.decide_action` gates AUTO_SUBMIT on
    # THIS value being a member of `PUBLIC_ATS_PLATFORMS`, so it must always
    # name the adapter that did the work (e.g. "custom" for GenericAdapter),
    # never the pre-flight platform guess when the two diverge.
    ats_platform: str
    confidence: float
    screenshot_paths: list[str] = field(default_factory=list)
    trace_path: str | None = None
    error_log: str | None = None
    # HITL platform: how many pages of a multi-page application this run
    # actually processed — see `Application.pages_completed`.
    pages_completed: int | None = None
    # HITL platform (§18 Observability): this run's structured progress log,
    # captured by a per-run logging handler — see `AutomationRun.log_lines`.
    log_lines: list[dict] = field(default_factory=list)
    # "Apply from Job Link": best-effort (title, company) read off the job
    # posting page itself — see
    # `automation/browser/selectors.py::find_job_posting_title_and_company`.
    # `app/services/application_repository.py::apply_run_result` fills
    # `Application.company`/`.position` from these ONLY when the caller
    # didn't already supply one, never overwriting an explicit hint.
    detected_company: str | None = None
    detected_position: str | None = None
    # Observability only — never read by `decide_action` or any safety check.
    # The pre-flight `ATSDetector` guess (e.g. "smartrecruiters"), kept
    # separate from `ats_platform` above so a dashboard/audit log can show
    # "Detected: smartrecruiters / Resolved: custom" instead of silently
    # reporting a GenericAdapter run as though a dedicated adapter performed
    # it. `None` for callers that never set it (e.g. hand-built results in
    # older tests) — treat that as "same as ats_platform", not "unknown".
    detected_ats_platform: str | None = None


class LLMCallable(Protocol):
    """Legacy injection-style signature, still referenced by
    `automation/forms/answer_engine.py` and `automation/agents/answer_agent.py`.
    New code should call `generate_answer()` (section A) directly instead of
    requiring a caller to inject one of these."""

    def __call__(self, *, system_prompt: str, user_prompt: str) -> str: ...


class EncryptDecryptPair(Protocol):
    """Legacy injection-style signature, still referenced by
    `automation/browser/session.py`. New code should import
    `app.core.crypto.encrypt_field` / `decrypt_field` directly instead of
    requiring a caller to inject this."""

    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, ciphertext: str) -> str: ...


EmbedCallable = Callable[[str], list[float]]
"""Legacy injection-style signature, still referenced by
`automation/agents/profile_agent.py`. New code should call `embed_text()`
(section A) directly instead of requiring a caller to inject one of these."""
