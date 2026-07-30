from datetime import datetime

from pydantic import BaseModel, ConfigDict, HttpUrl


class ApplicationStartRequest(BaseModel):
    """`POST /applications/start` body. `autopilot_enabled` defaults to
    `False` (copilot mode — form gets filled, a human clicks submit) since
    ARCHITECTURE.md requires autopilot to be an explicit opt-in per run, not
    a standing account setting silently applied everywhere."""

    job_url: HttpUrl
    autopilot_enabled: bool = False
    company: str | None = None    # optional hint; ATSAdapter/detector don't need it
    position: str | None = None   # optional hint, shown back in list views
    resume_document_id: str | None = None  # override the auto-picked default resume
    # Phase 6: pasted-in job posting text, given straight to
    # ApplicationAnswerEngine as extra context for subjective/novel
    # screening questions ("Why do you want to work here?"). Optional and
    # not persisted anywhere — purely a per-run hint.
    job_description: str | None = None


class ApplicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    application_id: str
    user_id: str
    job_url: str
    company: str | None = None
    position: str | None = None
    ats_platform: str | None = None
    status: str
    autopilot_enabled: bool
    applied_date: datetime | None = None
    resume_used: str | None = None
    confidence_score: float | None = None
    failure_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AutomationRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    application_id: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str
    screenshot_paths: list[str] = []
    trace_path: str | None = None
    error_log: str | None = None
    retry_count: int
