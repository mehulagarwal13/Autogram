from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, computed_field

from app.services.application_repository import DISPLAY_STATUS_MAP


class ReapplyAcknowledgement(BaseModel):
    """The caller naming the EXACT prior submission it is deliberately
    overriding, in order to apply to the same job again.

    Defined here, and re-exported by `app/api/autonomous_agent.py`, so BOTH
    start routes speak one consent vocabulary rather than two incompatible
    ones. See that module for the full rationale; in short, it is not a
    `reapply: true` flag because a bare flag is exactly what a retrying HTTP
    client or sticky frontend state sets by accident. To fill this in a client
    must have received the `application_already_submitted` 409 and copied the
    specific id out of it, and the server requires it to match the CURRENT
    latest submission — so it self-invalidates once a newer submission exists,
    with no token store.
    """

    path: str                          # "autonomous" | "deterministic"
    task_id: str | None = None         # when path == "autonomous"
    application_id: str | None = None  # when path == "deterministic"


class ApplicationStartRequest(BaseModel):
    """`POST /applications/start` body. `autopilot_enabled` defaults to
    `False` (copilot mode — form gets filled, a human clicks submit) since
    ARCHITECTURE.md requires autopilot to be an explicit opt-in per run, not
    a standing account setting silently applied everywhere."""

    job_url: HttpUrl
    #: Deliberate re-application after a genuine prior submission. Omitted on
    #: every normal start, so the default — refuse an accidental duplicate — is
    #: unchanged. Overrides ONLY the lifetime "already submitted" guard, never
    #: active-automation ownership.
    acknowledge_previous_submission: ReapplyAcknowledgement | None = None
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
    # A light-touch platform hint from the extension's content script (e.g.
    # "greenhouse"), used ONLY when source == "browser_extension" and only
    # in place of the backend's own page-less detect_ats_for_url guess —
    # never trusted blindly: still validated against the real adapter
    # registry before being used for anything (see start_application).
    ats_platform_hint: str | None = None


class ApplicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    application_id: str
    user_id: str
    job_url: str
    company: str | None = None
    position: str | None = None
    ats_platform: str | None = None
    # The pre-flight `ATSDetector` guess, kept separate from `ats_platform`
    # above (which always names the adapter that actually ran, e.g. "custom"
    # for GenericAdapter) — see `Application.detected_ats_platform`. Lets the
    # dashboard show "Detected: smartrecruiters / Resolved: custom" instead
    # of implying a dedicated adapter handled a run GenericAdapter filled.
    detected_ats_platform: str | None = None
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


class FieldMapResponse(BaseModel):
    """`POST /automation/map-fields` result. `action` is whatever
    `ApplicationFlowManager.decide_action()` — the EXACT function the
    server-side Playwright engine calls, not a reimplementation — returns
    for `(overall_confidence, application.ats_platform, application.autopilot_enabled)`.
    The extension acts strictly on this value; it never computes the
    decision itself."""

    fields: list[FieldMapResult]
    overall_confidence: float
    action: str  # "AUTO_SUBMIT" | "NEEDS_REVIEW" | "COPILOT_REVIEW"


class PacingConfig(BaseModel):
    """Mirrors `automation/browser/session.py::HumanPacing` field-for-field —
    read-only here so the extension never invents its own throttle numbers.
    NOTE (documented, not hidden): `HumanPacing` is not yet enforced by the
    server-side Playwright engine either as of this pass — exposing it keeps
    both actuators reading the SAME numbers, it does not retroactively wire
    up enforcement on the Playwright side."""

    per_char_delay_ms_min: int
    per_char_delay_ms_max: int
    per_action_delay_s_min: float
    per_action_delay_s_max: float
    inter_application_delay_s_min: float
    inter_application_delay_s_max: float
    daily_application_cap: int
    working_hours_start: int
    working_hours_end: int


class DecideRequest(BaseModel):
    """`POST /automation/decide` — for a caller (the extension) that already
    combined deterministic client-side matches with `map-fields`' backend
    results into one `overall_confidence` itself (plain arithmetic over
    confidence buckets, not a policy judgment) and now needs the ACTUAL
    submission decision, which must always come from `decide_action()`."""

    application_id: str
    overall_confidence: float


class DecideResponse(BaseModel):
    action: str
    overall_confidence: float


class AutomationConfigResponse(BaseModel):
    """`GET /automation/config` — the one shared "policy brain" surface the
    extension polls before every fill AND again before any auto-submit
    click, never caching it for a whole session."""

    kill_switch_engaged: bool
    pacing: PacingConfig
    auto_submit_confidence_threshold: float
    needs_review_confidence_threshold: float
    public_ats_platforms: list[str]


class ReportStatusRequest(BaseModel):
    """`POST /applications/{id}/report-status` — the extension's own status
    self-reports (waiting on a human for a CAPTCHA, or a final outcome after
    the human clicked submit on the real page). `status` must be one of
    `VALID_APPLICATION_STATUSES`."""

    status: str
    reason: str | None = None
    confidence: float | None = None


class VerificationCodeRequest(BaseModel):
    """`POST /applications/{id}/verification-code` — a one-time passcode the
    human read from their own email/SMS and typed into Autogram.

    HARD RULE, enforced by every layer that touches this value: `code` is
    transient. It is handed straight to
    `automation.applications.verification_channel.deliver`, which holds it in
    process memory until the paused run types it into the live page, and is
    then dropped. It is NEVER written to any table, never logged, never placed
    in `LIVE_RUN_STATE` (which the live endpoint returns to the browser), never
    added to the chat transcript, and never returned by any GET.

    Deliberately NOT reusing `ReportStatusRequest` or the answer routes: those
    persist what they are given, which for a verification code would be exactly
    the leak this whole design exists to prevent. A separate model makes the
    difference impossible to miss at the call site.
    """

    code: str
