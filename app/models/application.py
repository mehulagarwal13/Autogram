from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, computed_field

from app.services.application_repository import DISPLAY_STATUS_MAP


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
    # Browser-extension delivery — see db_models.py::Application.source.
    # "server_automation" (default) dispatches the existing server-side
    # Playwright run; "browser_extension" only creates/returns the tracking
    # row (same idempotency/duplicate-check either way) — the extension
    # itself does the filling in the user's own tab and reports progress via
    # POST /applications/{id}/report-status.
    source: str = "server_automation"


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
    pages_completed: int | None = None
    source: str = "server_automation"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @computed_field
    @property
    def display_status(self) -> str:
        """The HITL vocabulary the dashboard/frontend show (READY,
        WAITING_FOR_HUMAN, READY_TO_SUBMIT, SUBMITTED, ...) — a pure
        presentation-layer mapping over `status`, see
        `app/services/application_repository.py::DISPLAY_STATUS_MAP`. The
        underlying `status` value (and everything built on it — `decide_action`,
        retry logic) is unchanged and still present on this response."""
        return DISPLAY_STATUS_MAP.get(self.status, self.status.upper())


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
    log_lines: list[dict] = []


# ---------- HITL platform: question ledger, review, overview, duplicate check ----------

class ApplicationQuestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    question_id: str
    application_id: str
    page_number: int | None = None
    question_text: str
    field_type: str | None = None
    available_options: list[str] | None = None
    answer: str | None = None
    source: str
    confidence: float
    confidence_level: str
    review_status: str
    human_answer: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime | None = None


class QuestionReviewRequest(BaseModel):
    """`POST /applications/{id}/questions/{question_id}/review` body. `answer`
    is required for `action="edit"` and ignored otherwise — approving accepts
    the automation-produced answer as-is, rejecting simply declines it."""

    action: str  # "approve" | "edit" | "reject"
    answer: str | None = None


class ApplicationReviewSummary(BaseModel):
    """The pre-submission review gate's counts (§7): what got answered, what's
    still missing, and what's risky enough that a human should look twice."""

    questions_total: int
    questions_answered: int
    questions_generated: int
    questions_human_reviewed: int
    missing_fields: list[str] = []
    risky_answers: list[str] = []


class ApplicationOverviewResponse(BaseModel):
    """Dashboard overview tiles (§9)."""

    total: int
    submitted: int
    in_progress: int
    waiting_for_human: int
    waiting_for_review: int
    failed: int
    cancelled: int


class DuplicateCheckResponse(BaseModel):
    """`GET /applications/check-duplicate` — a soft warning, not a hard block
    (the URL-based uniqueness constraint is the authoritative check). `None`
    fields mean no likely duplicate was found."""

    possible_duplicate: bool
    existing_application_id: str | None = None
    existing_status: str | None = None


class ApplicationApprovalResult(BaseModel):
    """`POST /applications/{id}/approve` result — whether the submit actually
    went through and was confirmed, mirroring the same confirmation logic
    `ApplicationFlowManager`'s own `AUTO_SUBMIT` branch uses."""

    status: str  # "applied" | "needs_review" | "failed"
    message: str


class AuditLogEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    log_id: str
    application_id: str
    event_type: str
    actor: str
    event_metadata: dict | None = Field(default=None, alias="event_metadata")
    created_at: datetime | None = None


# ---------- Browser extension: field mapping + self-reported status ----------

class FieldQuery(BaseModel):
    """One field the extension's content script found on the page and
    couldn't confidently fill itself — everything it knows about it, sent as
    plain data (no DOM handle) to `POST /automation/map-fields`."""

    question_text: str
    field_type: str | None = None  # "text"/"textarea"/"select"/"radio"/"checkbox"/"date"/"number"
    options: list[str] | None = None  # the field's real, visible choices, if any


class FieldMapRequest(BaseModel):
    application_id: str
    job_description: str | None = None
    page_number: int | None = None
    fields: list[FieldQuery]


class FieldMapResult(BaseModel):
    """One field's answer — mirrors `ApplicationQuestionResponse`'s
    confidence semantics exactly (same HIGH/MEDIUM/LOW buckets, same
    thresholds `decide_action` uses) since both paths write to the same
    `application_questions` ledger via the same `ApplicationAnswerEngine`."""

    question_text: str
    answer: str
    confidence: float
    confidence_level: str
    source: str


class ReportStatusRequest(BaseModel):
    """`POST /applications/{id}/report-status` — the extension's own status
    self-reports (waiting on a human for a CAPTCHA, or a final outcome after
    the human clicked submit on the real page). `status` must be one of
    `VALID_APPLICATION_STATUSES`."""

    status: str
    reason: str | None = None
    confidence: float | None = None
