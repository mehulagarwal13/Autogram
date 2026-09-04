"""
API surface for the general-purpose autonomous browser agent — completely
separate from `app/api/applications.py` / `app/api/automation.py`, which
back the deterministic, per-ATS-adapter path. See `AUTONOMOUS_AGENT.md` for
how the two coexist.

Endpoints:
    POST   /agent/tasks                        start a new task
    GET    /agent/tasks                         list this user's tasks
    GET    /agent/tasks/{task_id}                task status/state
    POST   /agent/tasks/{task_id}/resume         resume after a non-answer intervention (e.g. "I logged in")
    POST   /agent/tasks/{task_id}/answer         supply an answer to a pending question, then resume
    POST   /agent/tasks/{task_id}/approve        explicit consent to submit; resumes and lets the loop click submit
    POST   /agent/tasks/{task_id}/cancel         stop the task and release the browser

Every route enforces the same ownership check every other per-resource
endpoint in this codebase uses (`task.user_id != user.user_id -> 404`, same
pattern as `app/api/automation.py::map_fields`).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.application import ReapplyAcknowledgement  # one shared consent vocabulary for both start routes
from app.models.db_models import (
    AUTONOMOUS_TASK_TERMINAL_STATUSES,
    SECRET_HUMAN_REQUEST_TYPES,
    ResumeRecord,
    User,
)
from app.services import audit_log_repository
from app.services import chat_repository
from app.services import automation_ownership
from app.services import autonomous_task_repository as task_repo
from app.services import human_interaction_repository as human_interaction_repo
from app.services import profile_repository, resume_repository
from app.services.document_storage import (
    MAX_FILE_SIZE_MB,
    save_task_upload_file,
    stage_stored_file_for_agent,
)
from app.services.event_bus import publish_task_event
from automation.agents.autonomous.runner import (
    request_cancel,
    signal_resume,
    start_task_background,
)


def _resolve_active_request(db: Session, task_id: str, *, cancelled: bool = False) -> None:
    """Keeps `HumanInteractionRequest` (see `app/api/human_interaction.py`)
    in sync when a client resumes/answers/approves/cancels via these older,
    per-intervention-type routes instead of the newer, unified
    `/human-requests/{id}/respond` route — both paths are supported, so
    whichever one a client used, the addressable request record doesn't get
    left dangling in PENDING forever."""
    req = human_interaction_repo.get_active_for_task(db, task_id)
    if req is None:
        return
    if cancelled:
        human_interaction_repo.mark_cancelled(db, req)
    else:
        human_interaction_repo.mark_resolved(db, req)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["autonomous-agent"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class StartTaskRequest(BaseModel):
    job_url: str
    resume_id: str | None = None  # defaults to the user's default resume if omitted
    objective_override: str | None = None  # rarely needed; defaults to a standard objective sentence
    # Optional per-task overrides layered on top of the stored profile —
    # never persisted back to the profile itself.
    profile_overrides: dict | None = None
    #: Deliberate re-application. Omitted on every normal start, so the default
    #: behaviour — reject an accidental duplicate — is unchanged. Overrides ONLY
    #: the lifetime "already submitted" guard, never active-automation
    #: ownership.
    acknowledge_previous_submission: ReapplyAcknowledgement | None = None


class AnswerRequest(BaseModel):
    question: str
    answer: str


class TaskDocumentResponse(BaseModel):
    """Public document metadata.

    ``AutonomousTask.uploaded_documents`` is also the executor's local-path
    allowlist.  Returning those absolute server paths to a browser is both
    unnecessary and an infrastructure leak, so this narrow response model
    intentionally drops ``file_path`` and ``file_hash``.
    """

    model_config = ConfigDict(extra="ignore")

    document_id: str | None = None
    label: str
    original_filename: str
    document_type: str | None = None
    source: str | None = None


class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: str
    user_id: str
    job_url: str
    original_objective: str
    current_status: str
    current_browser_state: dict | None = None
    action_history: list = []
    application_progress: dict = {}
    human_intervention: dict | None = None
    confirmed_answers: dict = {}
    uploaded_documents: list[TaskDocumentResponse] = Field(default_factory=list)
    final_result: dict | None = None
    error: str | None = None
    auto_submit_approved: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_owned_task(db: Session, task_id: str, user: User):
    task = task_repo.get_by_id(db, task_id)
    if task is None or task.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Autonomous task not found.")
    return task


#: Profile-row bookkeeping that has no business in the agent's snapshot. These
#: are also the two `datetime` values `profile_to_dict` returns, so dropping
#: them fixes the JSONB serialization failure — but they would be dropped
#: regardless of type: the snapshot exists to answer job-application questions,
#: and no form ever asks when a row in our database was written. Leaving them in
#: as ISO strings would put two dates into the decision prompt that the model
#: could only ever misuse (e.g. as an availability date).
_PROFILE_SNAPSHOT_EXCLUDED_FIELDS = frozenset({"created_at", "updated_at"})


def _json_safe_profile(profile_dict: dict | None) -> dict | None:
    """Make a `profile_to_dict` result safe to store in a JSONB column.

    Drops `_PROFILE_SNAPSHOT_EXCLUDED_FIELDS`, then coerces anything still not
    JSON-encodable — `datetime`/`date` to ISO 8601, anything else to `str` with
    a warning. The catch-all is deliberate but noisy: a new profile column of an
    unencodable type must never again turn task creation into a 500, yet it
    should not pass silently either, because the agent would then be reasoning
    about a value nobody designed for it.
    """
    if profile_dict is None:
        return None
    safe: dict = {}
    for key, value in profile_dict.items():
        if key in _PROFILE_SNAPSHOT_EXCLUDED_FIELDS:
            continue
        if isinstance(value, datetime):
            safe[key] = value.isoformat()
        elif isinstance(value, date):
            safe[key] = value.isoformat()
        else:
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                logger.warning(
                    "Autonomous task snapshot: profile field %r is a %s, which is not JSON "
                    "encodable — stored as a string. Add it to the snapshot's handling "
                    "explicitly if the agent is meant to use it.",
                    key, type(value).__name__,
                )
                safe[key] = str(value)
            else:
                safe[key] = value
    return safe


def _build_candidate_profile_snapshot(
    db: Session, user: User, resume_id: str | None, overrides: dict | None
) -> tuple[dict, list[dict]]:
    """One point-in-time snapshot taken at task start — see
    `AutonomousTask.candidate_profile`'s docstring for why this is a copy,
    not a live reference.

    Returns `(candidate_profile, uploadable_documents)` — the second element
    goes to `AutonomousTask.uploaded_documents` and doubles as the
    `upload_file` allowlist (see `_build_uploadable_documents`).

    What the agent therefore gets, and what it deliberately does NOT:

    * profile columns (name/contact/location, work authorization, visa,
      salary, notice period, clearance, languages, highest education level,
      ...) via `profile_to_dict`;
    * `resume_text` plus `parsed_resume`, which is where **education and work
      history** reach the agent (`ParsedResume.education[]` /
      `.experience[]`) — the `education`/`experience` child TABLES are not
      snapshotted separately;
    * NOT `CandidateDemographics` (race/gender/veteran/disability). Those are
      protected-class answers the agent must never fill on the candidate's
      behalf, so they are withheld and `executor.py`'s sensitive-field gate
      turns any such field into a human pause. The deterministic per-ATS path
      DOES read them (`automation/forms/answer_engine.py`); this asymmetry is
      intentional, not an oversight.
    * NOT `answer_cache` or `application_questions` — answers approved on
      other applications are not reused here, so a repeated free-text
      question is asked again rather than auto-answered from another context.
    """
    profile = profile_repository.get_by_user_id(db, user.user_id)
    profile_dict = profile_repository.profile_to_dict(profile) if profile is not None else None

    resume_record: ResumeRecord | None = None
    if resume_id:
        resume_record = resume_repository.get_by_id(db, resume_id)
        if resume_record is None or resume_record.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="Resume not found.")
    else:
        candidates = resume_repository.list_for_user(db, user.user_id)
        resume_record = candidates[0] if candidates else None

    snapshot = {
        # `profile_to_dict` is documented as "ready for a Pydantic response
        # model", where a `datetime` is correct and gets serialized by Pydantic.
        # This snapshot goes somewhere else entirely — straight into the
        # `candidate_profile` JSONB column — and psycopg2's JSON encoder raises
        # `TypeError: Object of type datetime is not JSON serializable` on one,
        # which surfaced as a hard 500 from `POST /agent/tasks` for every user
        # who actually had a profile row. Sanitized here, at the JSONB boundary,
        # rather than in `profile_to_dict`: its other four callers all build
        # `ProfileResponse` from it and want real datetimes.
        "profile": _json_safe_profile(profile_dict),
        "resume_text": (resume_record.extracted_text if resume_record else None) or "",
        # Already JSONB in the database, so already plain JSON types.
        "parsed_resume": (resume_record.parsed_data if resume_record else None),
        "resume_id": resume_record.resume_id if resume_record else None,
    }
    if overrides:
        # Shallow-merge overrides into the profile view only — never mutates
        # the stored profile row itself.
        merged_profile = dict(snapshot["profile"] or {})
        merged_profile.update(overrides)
        snapshot["profile"] = merged_profile
    return snapshot, _build_uploadable_documents(resume_record)


def _build_uploadable_documents(resume_record: ResumeRecord | None) -> list[dict]:
    """The documents an `upload_file` action may attach, written to
    `AutonomousTask.uploaded_documents` at task creation and enforced as an
    allowlist by `ActionExecutor` (see its `allowed_upload_paths`).

    Why this exists: `uploaded_documents` used to be initialised to `[]` and
    populated by nothing, while the decision prompt told the model its
    `upload_file` `file_path` "must be one of the uploaded_documents you were
    given". With an always-empty list the agent could never attach a résumé —
    so it stalled (or asked a human) at the file input that essentially every
    real job application has. The controlled browser fixture has no file
    input, which is why the gap survived end-to-end testing.

    Deliberately conservative:

    - Existing local files are used directly. Object-storage locators are
      materialized through the storage abstraction into a runner-local cache,
      because Playwright's `set_input_files` requires a filesystem path.
    - Nothing else on disk is ever added, because this list IS the
      exfiltration allowlist.
    """
    if resume_record is None or not resume_record.stored_path:
        return []
    path = Path(str(resume_record.stored_path))
    try:
        if path.is_file():
            upload_path = str(path.resolve())
        else:
            upload_path = stage_stored_file_for_agent(
                resume_record.resume_id,
                "resume",
                resume_record.original_filename,
                str(resume_record.stored_path),
            )
    except Exception:  # noqa: BLE001 - unavailable storage must become an honest pause
        logger.warning(
            "Autonomous task: resume %s could not be materialized for browser upload.",
            resume_record.resume_id,
            exc_info=True,
        )
        return []
    return [{
        "label": "resume",
        "document_type": "resume",
        "original_filename": resume_record.original_filename,
        "file_path": upload_path,
        "source": "stored_resume",
    }]


def _duplicate_automation_error(active: automation_ownership.ActiveAutomation) -> HTTPException:
    """409 for "something else is already automating this job".

    The body is structured so the frontend can tell the cases apart without
    parsing prose or knowing anything about our schema:

        {"detail": {"reason": "active_automation_exists",
                    "path": "autonomous" | "deterministic",
                    "status": "<that automation's status>",
                    "task_id": "..." | null,
                    "application_id": "..." | null}}

    `reason` is the machine-readable discriminator; `path` + the id let the UI
    link straight to the run that already owns the job. Only ids the caller
    already owns are returned — no internal schema detail.
    """
    if active.is_autonomous:
        message = "This job is already being automated by an autonomous agent task."
    else:
        message = "This job already has an application in progress."
    return HTTPException(
        status_code=409,
        detail={
            "reason": "active_automation_exists",
            "message": message,
            "path": active.path,
            "status": active.status,
            "task_id": active.task_id,
            "application_id": active.application_id,
        },
    )


def _already_submitted_error(submitted: automation_ownership.SubmittedApplication) -> HTTPException:
    """409 for "you have already successfully applied to this job".

    A DIFFERENT `reason` from `active_automation_exists` on purpose: the two
    mean different things to the user and warrant different UI. "Something is
    running right now" is transient and offers "open the run in progress";
    "already submitted" is permanent and offers nothing to resume. Collapsing
    them into one reason would make the frontend unable to say which happened.
    """
    where = "an autonomous agent task" if submitted.is_autonomous else "an application"
    return HTTPException(
        status_code=409,
        detail={
            "reason": "application_already_submitted",
            "message": f"You have already submitted {where} for this job.",
            "path": submitted.path,
            "submitted_at": submitted.submitted_at.isoformat() if submitted.submitted_at else None,
            "task_id": submitted.task_id,
            "application_id": submitted.application_id,
        },
    )


def _validate_reapply_acknowledgement(
    acknowledgement: ReapplyAcknowledgement,
    submitted: automation_ownership.SubmittedApplication,
) -> None:
    """Thin HTTP wrapper over the shared
    `automation_ownership.validate_reapply_acknowledgement`, so both start
    routes apply identical matching rules and only differ in how they render
    the refusal."""
    try:
        automation_ownership.validate_reapply_acknowledgement(acknowledgement, submitted)
    except automation_ownership.ReapplyAcknowledgementError:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "invalid_reapplication_request",
                "message": (
                    "This re-application request doesn't match the application currently on "
                    "file for this job. Reload and try again."
                ),
                "path": submitted.path,
                "task_id": submitted.task_id,
                "application_id": submitted.application_id,
            },
        ) from None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/tasks", response_model=TaskResponse)
def start_task(body: StartTaskRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # --- Duplicate/concurrent automation guard -----------------------------
    # FIRST, before a task row exists and before `start_task_background` can
    # open a browser tab. Two automations independently filling the same
    # application form is the failure being prevented, so nothing may be
    # allocated until we know this request owns the job.
    #
    # `reserve_job_automation` takes a transaction-scoped Postgres advisory
    # lock on (user, job), so concurrent start requests — including one from
    # the deterministic path, which no single unique index could cover —
    # serialize through the check below instead of interleaving with it.
    automation_ownership.reserve_job_automation(db, user_id=user.user_id, job_url=body.job_url)
    active = automation_ownership.find_active_automation(db, user_id=user.user_id, job_url=body.job_url)
    if active is not None:
        raise _duplicate_automation_error(active)

    # Separately: has this job ALREADY been successfully submitted, on either
    # path? Distinct from the check above — that one is about concurrency
    # ("who is driving a browser right now") and deliberately lets a
    # FAILED/CANCELLED attempt be retried. This one is about lifetime: a job
    # confirmed submitted must not be silently applied to a second time.
    # Both checks sit under the same advisory lock, so a submission committing
    # concurrently cannot slip between them.
    submitted = automation_ownership.find_submitted_application(
        db, user_id=user.user_id, job_url=body.job_url
    )
    reapplying_over = None
    if submitted is not None:
        if body.acknowledge_previous_submission is None:
            # The normal path: no deliberate acknowledgement, so this is an
            # accidental duplicate and is refused exactly as before.
            raise _already_submitted_error(submitted)
        # A deliberate re-application. Note this branch is reached only AFTER
        # the active-automation check above, so an override can never bypass
        # ownership — it relaxes the lifetime guard and nothing else.
        _validate_reapply_acknowledgement(body.acknowledge_previous_submission, submitted)
        reapplying_over = submitted

    candidate_profile, uploadable_documents = _build_candidate_profile_snapshot(
        db, user, body.resume_id, body.profile_overrides
    )

    objective = body.objective_override or (
        "Complete the job application at the given URL as accurately as possible, "
        "using only information from the candidate's resume, verified profile, and "
        "answers they confirm during this task. Stop for human input whenever "
        "required, and stop at final submission for explicit approval."
    )

    try:
        task = task_repo.create_task(
            db, user_id=user.user_id, job_url=body.job_url,
            original_objective=objective, candidate_profile=candidate_profile,
            uploaded_documents=uploadable_documents,
        )
    except IntegrityError:
        # Backstop for `uq_autonomous_tasks_active_job`. The advisory lock
        # above should already have serialized concurrent starts, so reaching
        # here means either the lock was unavailable (see
        # `reserve_job_automation`) or a client bypassed this route. Either
        # way the DB refused to create a second ACTIVE task for this job,
        # which is the guarantee that matters — report it the same way.
        #
        # Roll back first: the failed INSERT poisons the session, so the
        # lookup below would otherwise raise PendingRollbackError.
        db.rollback()
        existing = automation_ownership.find_active_automation(
            db, user_id=user.user_id, job_url=body.job_url
        )
        if existing is not None:
            raise _duplicate_automation_error(existing) from None
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "active_automation_exists",
                "message": "This job is already being automated.",
                "path": "autonomous", "status": None,
                "task_id": None, "application_id": None,
            },
        ) from None

    if reapplying_over is not None:
        # A deliberate re-application is materially different from a normal
        # start, so it gets its own audit event on the EXISTING append-only
        # trail (no new logging system). Metadata only — a job hash, the prior
        # reference, and the new task id; nothing sensitive.
        audit_log_repository.record_event(
            db, user_id=user.user_id, autonomous_task_id=task.task_id,
            event_type="reapplication_authorized", actor=user.user_id,
            metadata={
                "job_url_hash": automation_ownership.job_key(body.job_url),
                "previous_path": reapplying_over.path,
                "previous_task_id": reapplying_over.task_id,
                "previous_application_id": reapplying_over.application_id,
                "new_task_id": task.task_id,
                "new_path": "autonomous",
            },
        )

    task_repo.set_status(db, task, "ANALYZING_JOB")
    # Only NOW is a browser allowed to exist for this job.
    start_task_background(task.task_id)
    return task


@router.get("/tasks", response_model=list[TaskResponse])
def list_tasks(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return task_repo.list_for_user(db, user.user_id)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _get_owned_task(db, task_id, user)


@router.post("/tasks/{task_id}/documents", response_model=TaskResponse)
async def attach_task_document(
    task_id: str,
    file: UploadFile = File(...),
    document_type: str = Form("other"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stage a user-supplied file for the currently paused task.

    This fills the UI/API gap behind ``FILE_UPLOAD_REQUIRED``.  The upload is
    accepted only while the task is genuinely waiting for the human, is
    validated by extension *and* magic bytes, and becomes one more path in
    this task's executor allowlist. The same request then claims and resumes
    the paused task, avoiding a fragile two-request "upload, then continue"
    sequence in the browser client.
    """
    task = _get_owned_task(db, task_id, user)
    if task.current_status != "WAITING_FOR_HUMAN":
        raise HTTPException(
            status_code=409,
            detail=f"Documents can only be attached while the task is waiting for you (status: {task.current_status}).",
        )
    _reject_if_active_request_is_secret(task)

    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size_mb:.1f} MB). Max {MAX_FILE_SIZE_MB} MB.",
        )
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    safe_name = Path(file.filename or "document").name
    try:
        document_id, local_path = save_task_upload_file(
            task_id, document_type, safe_name, content
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    entry = {
        "document_id": document_id,
        "label": document_type.replace("_", " "),
        "document_type": document_type,
        "original_filename": safe_name,
        "file_path": local_path,
        "source": "task_upload",
    }
    task.uploaded_documents = [*(task.uploaded_documents or []), entry]
    db.commit()
    db.refresh(task)

    request_type = (task.human_intervention or {}).get("request_type")
    try:
        chat_repository.record_user_reply(
            db,
            user_id=user.user_id,
            autonomous_task_id=task_id,
            content=f'Attached "{safe_name}" for this application.',
            request_type=request_type,
        )
    except Exception:
        logger.exception("Task %s: could not add document attachment to transcript.", task_id)
    try:
        audit_log_repository.record_event(
            db,
            user_id=user.user_id,
            autonomous_task_id=task_id,
            event_type="document_attached",
            actor=user.user_id,
            metadata={
                "document_id": document_id,
                "document_type": document_type,
                "original_filename": safe_name,
            },
        )
    except Exception:
        logger.exception("Task %s: could not audit document attachment.", task_id)
    publish_task_event(
        task_id,
        "DOCUMENT_ATTACHED",
        document_id=document_id,
        document_type=document_type,
    )
    if not task_repo.try_claim_for_resume(db, task, from_status="WAITING_FOR_HUMAN"):
        # Another tab may have resumed it between upload and claim. The file is
        # safely attached either way; return the authoritative state instead
        # of turning a successful upload into a misleading client error.
        return task_repo.get_by_id(db, task_id)
    _resolve_active_request(db, task_id)
    publish_task_event(
        task_id,
        "HUMAN_ACTION_COMPLETED",
        request_type=request_type or "FILE_UPLOAD_REQUIRED",
        action="DOCUMENT_ATTACHED",
    )
    if not signal_resume(task_id):
        start_task_background(task_id)
    return task_repo.get_by_id(db, task_id)


def _reject_if_active_request_is_secret(task) -> None:
    """A pending OTP/MFA request cannot be waved past by the generic
    "I've handled it" (`/resume`) or free-text (`/answer`) routes — those
    never carry a verification code, so silently letting them resume would
    either do nothing useful (the loop just re-observes and re-pauses on the
    same field) or, worse for `/answer` specifically, invite a user to paste
    their verification code into a free-text answer box, which WOULD get
    permanently written to `confirmed_answers` and returned by every future
    `GET /agent/tasks/{id}` — exactly the leak this whole system exists to
    prevent. Callers must use `POST /human-requests/{request_id}/respond`
    with `OTP_SUBMITTED`/`MFA_SUBMITTED` instead."""
    request_type = (task.human_intervention or {}).get("request_type")
    if request_type in SECRET_HUMAN_REQUEST_TYPES:
        raise HTTPException(
            status_code=409,
            detail=f"This task requires a verification code ({request_type}). "
                   "Use POST /human-requests/{request_id}/respond instead of this route.",
        )


@router.post("/tasks/{task_id}/resume", response_model=TaskResponse)
def resume_task(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Resume after a non-answer intervention — e.g. the human logged in,
    solved a CAPTCHA, or otherwise cleared the blocker by hand in the open
    browser tab. Per spec, the loop re-observes the page from scratch on the
    very next iteration; it never assumes what changed."""
    task = _get_owned_task(db, task_id, user)
    if task.current_status != "WAITING_FOR_HUMAN":
        raise HTTPException(status_code=409, detail=f"Task is not waiting for human input (status: {task.current_status}).")
    _reject_if_active_request_is_secret(task)
    # Read BEFORE the claim: `try_claim_for_resume` clears `human_intervention`
    # as part of the same conditional UPDATE, so afterwards there is nothing
    # left to say which blocker the human just cleared.
    requested_type = (task.human_intervention or {}).get("request_type")
    # Atomic claim — see `human_interaction.py::respond_to_human_request`'s
    # "guard #2" comment: this is the SAME chokepoint, so a concurrent
    # `/human-requests/{id}/respond` (or a duplicate `/resume`) can never
    # resume this task twice.
    if not task_repo.try_claim_for_resume(db, task, from_status="WAITING_FOR_HUMAN"):
        raise HTTPException(status_code=409, detail=f"Task is not waiting for human input (status: {task.current_status}).")
    _resolve_active_request(db, task_id)
    # The human's turn for a non-answer blocker — a solved CAPTCHA, a completed
    # sign-in, anything they cleared by hand in the open tab. This route is the
    # only place those are recorded: `/human-requests/{id}/respond` never sees
    # them (its actions are the two secret ones, USER_APPROVED, and
    # USER_PROVIDED_VALUE). Best-effort: the resume has already been claimed
    # atomically above, and a transcript write must not undo it.
    blocker = requested_type or "the blocker"
    try:
        chat_repository.record_user_reply(
            db, user_id=user.user_id, autonomous_task_id=task_id,
            content="I've handled it — please continue.",
            request_type=requested_type,
        )
    except Exception:
        logger.exception("Task %s: could not write the resume to the chat transcript.", task_id)
    publish_task_event(task_id, "HUMAN_ACTION_COMPLETED", request_type=blocker, action="USER_CONFIRMED")
    if not signal_resume(task_id):
        start_task_background(task_id)  # process restarted since pause — see runner.py docstring
    return task_repo.get_by_id(db, task_id)


@router.post("/tasks/{task_id}/answer", response_model=TaskResponse)
def answer_question(task_id: str, body: AnswerRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Records a human-supplied answer, scoped to THIS task only (never
    written into the global profile/answer cache — see
    `autonomous_task_repository.record_confirmed_answer`), then resumes."""
    task = _get_owned_task(db, task_id, user)
    if task.current_status != "WAITING_FOR_HUMAN":
        raise HTTPException(status_code=409, detail=f"Task is not waiting for human input (status: {task.current_status}).")
    _reject_if_active_request_is_secret(task)
    if not task_repo.try_claim_for_resume(db, task, from_status="WAITING_FOR_HUMAN"):
        raise HTTPException(status_code=409, detail=f"Task is not waiting for human input (status: {task.current_status}).")
    task_repo.record_confirmed_answer(db, task, body.question, body.answer)
    _resolve_active_request(db, task_id)
    if not signal_resume(task_id):
        start_task_background(task_id)
    return task_repo.get_by_id(db, task_id)


@router.post("/tasks/{task_id}/approve", response_model=TaskResponse)
def approve_submission(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The ONLY route that ever sets `auto_submit_approved`. Compliance:
    auto-submit stays off until this explicit, per-task, human-initiated
    call — see `AUTONOMOUS_AGENT.md`."""
    task = _get_owned_task(db, task_id, user)
    if task.current_status != "WAITING_FOR_APPROVAL":
        raise HTTPException(status_code=409, detail=f"Task is not waiting for approval (status: {task.current_status}).")
    if not task_repo.try_claim_for_resume(db, task, from_status="WAITING_FOR_APPROVAL"):
        raise HTTPException(status_code=409, detail=f"Task is not waiting for approval (status: {task.current_status}).")
    task_repo.approve_submission(db, task)
    publish_task_event(task_id, "HUMAN_ACTION_COMPLETED", action="approved")
    if not signal_resume(task_id):
        start_task_background(task_id)
    return task_repo.get_by_id(db, task_id)


@router.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
def cancel_task(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = _get_owned_task(db, task_id, user)
    if task.current_status in AUTONOMOUS_TASK_TERMINAL_STATUSES:
        return task
    _resolve_active_request(db, task_id, cancelled=True)
    # Signal any live loop so it stops promptly and releases its browser, and
    # persist the cancellation UNCONDITIONALLY. Previously this only persisted
    # when there was no live handle, on the assumption that a live loop would
    # do it — but a loop blocked waiting for a human never reached that code,
    # so cancelling a paused task left it stuck in WAITING_FOR_HUMAN forever
    # (see `loop.py::_wait_for_resume`'s docstring). Writing it here means the
    # user's cancellation is reflected immediately no matter what state the
    # loop is in; `cancel_task` is idempotent and the loop's own call is then
    # a no-op.
    request_cancel(task_id)
    task_repo.cancel_task(db, task)
    audit_log_repository.record_event(
        db, user_id=user.user_id, autonomous_task_id=task_id, event_type="automation_cancelled",
        actor=user.user_id, metadata={},
    )
    return task_repo.get_by_id(db, task_id)
