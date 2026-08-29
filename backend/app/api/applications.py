"""
Application tracking API — Phase 4 (+ Phase 6 answer-engine wiring).

`POST /applications/start` is the one place `app/` calls into `automation/`
(see ARCHITECTURE.md "Request lifecycle" and `automation/interfaces.py`):
it creates the durable `Application` row, then hands off to a background
task that runs `ATSDetector` -> resolves an `ATSAdapter` via
`automation/ats/registry.py` -> drives `ApplicationFlowManager` -> persists
the `ApplicationRunResult` it gets back. There's no Celery/Redis queue yet
(that's still Phase 4+/not wired up per requirements.txt) so this uses
FastAPI's `BackgroundTasks`, the same pattern `app/api/resumes.py::_run_extraction`
already uses for background resume-text extraction — one process, no new
infrastructure, easy to swap for a real worker later without changing the
route's contract.

Phase 6 adds one more construction step before `ApplicationFlowManager`
runs: an `automation.forms.answer_engine.ApplicationAnswerEngine`, built
from this user's profile (+ this DB session, for its persistent answer
cache, + the optional `job_description` hint from the request body) and
threaded straight through so leftover screening questions FieldMapper
couldn't resolve get answered instead of just left blank.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.config import AUTOMATION_VISION_FALLBACK
from app.core.database import SessionLocal, get_db
from app.models.application import (
    ApplicationApprovalResult,
    ApplicationOverviewResponse,
    ApplicationQuestionResponse,
    ApplicationResponse,
    ApplicationReviewSummary,
    ApplicationStartRequest,
    AuditLogEntryResponse,
    AutomationRunResponse,
    DuplicateCheckResponse,
    QuestionReviewRequest,
    ReportStatusRequest,
    VerificationCodeRequest,
)
from app.models.db_models import Application, User, VALID_APPLICATION_SOURCES
from app.services import (
    answer_cache_repository,
    application_question_repository,
    application_repository,
    audit_log_repository,
    automation_ownership,
    profile_repository,
)
from app.services.event_bus import publish_application_event
from automation.applications import verification_channel
from automation.applications.application_flow_manager import (
    ApplicationFlowManager,
    close_review_session,
    get_live_state,
    submit_open_review_session,
)
from automation.ats.detector import FALLBACK_ATS, detect_ats_for_url
from automation.ats.registry import get_adapter_class
from automation.forms.answer_engine import ApplicationAnswerEngine, is_decoy_field
from automation.forms.vision_fallback import VisionFormAnswerer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/applications", tags=["applications"])

# `ApplicationFlowManager.run()` uses Playwright's *sync* API, which refuses
# to start ("...you are using Playwright Sync API inside the asyncio
# loop...") the instant it detects a running asyncio event loop on its
# calling thread. `_run_application` already runs off the request thread via
# FastAPI's `BackgroundTasks`, but that alone isn't a hard guarantee of "no
# event loop nearby" in every deployment/runner — so Playwright is launched
# on a dedicated plain thread instead, which never has an asyncio loop
# associated with it, period. This is the officially recommended way to run
# Playwright's sync API from inside any asyncio-based app.
#
# Deliberately NOT a shared `ThreadPoolExecutor`: `sync_playwright().start()`
# spins up its own internal asyncio loop + greenlet dispatcher that keeps
# "pumping" on whatever thread called it for as long as that Playwright
# driver stays open — and a `copilot_review`/`needs_review`/`manual_required`
# run leaves its browser (and driver) open ON PURPOSE (see
# `ApplicationFlowManager._finish_browser_session`/`should_keep_browser_open`)
# so a human can look at it, for as long as it takes someone to call
# `close_review_session()`. On a small reusable pool, that permanently pins
# one of the pool's threads into a "loop already running" state; the next
# unrelated run that happens to land on that recycled thread then hits this
# exact error — not a flake, a guaranteed failure once enough review sessions
# pile up (4 of them exhausts a 4-worker pool for good). A fresh,
# never-recycled thread per run means a held-open review session just parks
# its own thread forever instead of poisoning one everyone else shares.
def _run_on_dedicated_thread(fn):
    """Runs `fn()` to completion on a brand-new thread that's used exactly
    once and then discarded, returning its result or re-raising its
    exception on the calling thread. See the module-level comment above for
    why this can't be a shared/reusable thread pool."""
    future: Future = Future()

    def _target() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(fn())
        except BaseException as e:  # noqa: BLE001 - propagate exactly as-is to the caller
            future.set_exception(e)

    threading.Thread(target=_target, name="playwright-run", daemon=True).start()
    return future.result()


def _get_owned_application(db: Session, application_id: str, user: User) -> Application:
    application = application_repository.get_by_id(db, application_id)
    if not application or application.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Application not found.")
    return application


def _pick_resume_document_id(db: Session, profile_id: str, requested_document_id: str | None) -> str | None:
    """Explicit `resume_document_id` wins if it's a real resume belonging to
    this profile; otherwise falls back to the profile's default resume, same
    selection `automation.interfaces.get_default_resume` would make."""
    if requested_document_id:
        document = profile_repository.get_document(db, requested_document_id)
        if document and document.profile_id == profile_id and document.document_type == "resume":
            return document.document_id
        raise HTTPException(status_code=400, detail="resume_document_id does not refer to one of your resumes.")

    resumes = profile_repository.list_documents(db, profile_id, document_type="resume")
    if not resumes:
        return None
    default = next((r for r in resumes if r.is_default), resumes[0])
    return default.document_id


# Every value `Application.status` can hold (see db_models.VALID_APPLICATION_STATUSES)
# bucketed into exactly what POST /start does about it:
#   - RETRYABLE: nothing actually succeeded — safe to re-run on the same row.
#     `needs_review` (confidence too low to trust) belongs here for the same
#     reason `failed` and `manual_required` (CAPTCHA) do: no successful
#     outcome happened, there's nothing to conflict with.
#   - IN_PROGRESS: a run is either about to start, actively running, or
#     (`copilot_review`) sitting with its browser deliberately left open for
#     a human to submit — see ApplicationFlowManager.should_keep_browser_open.
#     Retrying here could race an in-flight run or blow away a live review
#     session, so it's rejected instead.
#   - COMPLETED: the job was actually applied to — retrying would risk a
#     double-apply, which ARCHITECTURE.md's idempotency rule exists to
#     prevent.
# The three sets are exhaustive and disjoint over VALID_APPLICATION_STATUSES.
RETRYABLE_STATUSES = frozenset({"failed", "manual_required", "needs_review"})
IN_PROGRESS_STATUSES = frozenset({"pending", "processing", "copilot_review"})
COMPLETED_STATUSES = frozenset({"applied"})


@router.post("/start", response_model=ApplicationResponse, status_code=202)
def start_application(
    body: ApplicationStartRequest,
    background_tasks: BackgroundTasks,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Idempotent per (user, job_url) — ARCHITECTURE.md's "never double-apply"
    rule. A normal request (no `acknowledge_previous_submission`) behaves
    exactly as it always has:

    * an ACTIVE attempt on either path -> 409;
    * an already-SUBMITTED job -> 409 `application_already_submitted`;
    * a RETRYABLE_STATUSES attempt never actually submitted, so there is
      nothing to double-apply: it is retried on the SAME row, keeping its
      `application_id` and `AutomationRun` history.

    A job can now hold SEVERAL attempts, because a deliberate re-application
    (`acknowledge_previous_submission`, the same acknowledgement shape
    `POST /agent/tasks` accepts) inserts a new attempt rather than overwriting
    the previous `applied` row. "Never double-apply" is therefore enforced by
    two things rather than one full unique constraint: the partial unique index
    `uq_applications_active_job` for concurrency, and the lifetime check here
    for accidental repeats. Neither guarantee was weakened — see
    `Application.__table_args__`.
    """
    if body.source not in VALID_APPLICATION_SOURCES:
        raise HTTPException(
            status_code=400, detail=f"Invalid source {body.source!r}. Must be one of {sorted(VALID_APPLICATION_SOURCES)}.",
        )

    job_url = str(body.job_url)

    # Cross-path guard. This route has always protected itself against its OWN
    # duplicates via `uq_applications_user_job_url`, but it knew nothing about
    # the autonomous agent — so an autonomous task could already be driving a
    # browser tab on this exact job. The advisory lock makes this check and the
    # create/retry below atomic with respect to a concurrent
    # `POST /agent/tasks` for the same job (a unique index cannot span the two
    # tables). Deliberately narrow: it only reports an ACTIVE automation on the
    # *other* path; everything about this route's own status handling below is
    # unchanged.
    automation_ownership.reserve_job_automation(db, user_id=user.user_id, job_url=job_url)
    active_elsewhere = automation_ownership.find_active_automation(
        db, user_id=user.user_id, job_url=job_url
    )
    if active_elsewhere is not None:
        # ACTIVE ALWAYS WINS, on either path, and a re-application
        # acknowledgement is not even read yet — it can never bypass this.
        if active_elsewhere.is_autonomous:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "active_automation_exists",
                    "message": "This job is already being automated by an autonomous agent task.",
                    "path": active_elsewhere.path,
                    "status": active_elsewhere.status,
                    "task_id": active_elsewhere.task_id,
                    "application_id": None,
                },
            )
        # A deterministic attempt of this user's own is already in progress.
        # Kept as the long-standing plain-string message this route has always
        # returned for that case.
        raise HTTPException(status_code=409, detail="Application is already in progress.")

    # LIFETIME duplicate — now checked for BOTH paths. An autonomous task at
    # COMPLETED creates no `Application` row at all, and a prior `applied`
    # attempt of this route's own is no longer guaranteed to be the row a
    # lookup returns once a job can have several attempts. So the single
    # authority for "has this user already successfully submitted?" is the
    # ownership boundary, for both paths.
    submitted = automation_ownership.find_submitted_application(
        db, user_id=user.user_id, job_url=job_url
    )
    reapplying_over = None
    if submitted is not None:
        if body.acknowledge_previous_submission is None:
            # The normal path: an accidental duplicate, refused exactly as
            # before.
            where = "an autonomous agent application" if submitted.is_autonomous else "an application"
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "application_already_submitted",
                    "message": f"You have already submitted {where} for this job.",
                    "path": submitted.path,
                    "submitted_at": (
                        submitted.submitted_at.isoformat() if submitted.submitted_at else None
                    ),
                    "task_id": submitted.task_id,
                    "application_id": submitted.application_id,
                },
            )
        # A deliberate re-application. Reached only AFTER the active check
        # above, so it relaxes the lifetime guard and nothing else.
        try:
            automation_ownership.validate_reapply_acknowledgement(
                body.acknowledge_previous_submission, submitted
            )
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
        reapplying_over = submitted

    # An attempt that never submitted, and can therefore be resumed IN PLACE.
    # Deliberately NOT `get_by_user_and_url`: that returns the latest attempt
    # whatever its status, which once several attempts exist could hand back an
    # `applied` row — and retrying that would reset its `status`/`applied_date`
    # and erase the record of the submission. This lookup can only ever return
    # `failed`/`manual_required`/`needs_review`.
    existing = application_repository.get_retryable_attempt_for_job(db, user.user_id, job_url)

    profile = profile_repository.get_by_user_id(db, user.user_id)
    if not profile:
        raise HTTPException(
            status_code=400,
            detail="No candidate profile found. Create one with POST /profile before applying.",
        )

    resume_document_id = _pick_resume_document_id(db, profile.profile_id, body.resume_document_id)
    if not resume_document_id:
        raise HTTPException(
            status_code=400,
            detail="No resume on file. Upload one with POST /profile/documents/upload first.",
        )

    # RETRY vs RE-APPLICATION — two different things, kept distinct:
    #
    #   retry            an attempt that never submitted (`failed`,
    #                    `manual_required`, `needs_review`) is resumed IN PLACE
    #                    on the same row, preserving its `application_id` and
    #                    its `AutomationRun` history. Unchanged behaviour.
    #
    #   re-application   the user deliberately acknowledged a prior SUCCESSFUL
    #                    submission. That always inserts a NEW attempt row, so
    #                    the previous `applied` row — its status, `applied_date`,
    #                    runs, questions and audit entries — survives untouched.
    #                    Never a retry, even if some unrelated failed attempt
    #                    also happens to be lying around for this job.
    if existing is None or reapplying_over is not None:
        application = application_repository.create_application(
            db,
            user_id=user.user_id,
            job_url=job_url,
            autopilot_enabled=body.autopilot_enabled,
            company=body.company,
            position=body.position,
            source=body.source,
        )
    else:
        # Retry: same row (same application_id/created_at/job_url_hash), so its
        # AutomationRun history from apply_run_result is kept.
        application = application_repository.retry_application(
            db, existing, company=body.company, position=body.position,
            autopilot_enabled=body.autopilot_enabled, source=body.source,
        )

    if reapplying_over is not None:
        # A deliberate re-application is materially different from a normal
        # start, so it is recorded on the EXISTING append-only audit trail.
        # Metadata only — a job hash and references; nothing sensitive.
        _record_audit_event(
            db, application_id=application.application_id, user_id=user.user_id,
            event_type="reapplication_authorized", actor=user.user_id,
            metadata={
                "job_url_hash": automation_ownership.job_key(job_url),
                "previous_path": reapplying_over.path,
                "previous_task_id": reapplying_over.task_id,
                "previous_application_id": reapplying_over.application_id,
                "new_application_id": application.application_id,
                "new_path": "deterministic",
            },
        )

    if body.source == "server_automation":
        background_tasks.add_task(
            _run_application, application.application_id, resume_document_id, body.job_description,
        )
    else:
        # source == "browser_extension": there is no server-side Playwright
        # run to dispatch — the extension itself fills the form in the
        # user's own tab and reports progress via
        # POST /applications/{id}/report-status.
        #
        # ats_platform matters here specifically because decide_action()
        # gates AUTO_SUBMIT on the platform being in PUBLIC_ATS_PLATFORMS —
        # and the backend's own page-less `detect_ats_for_url` only ever
        # sees `job_url` as pasted, never the live DOM after the content
        # script clicks an Apply/Apply Now button (see
        # extension/content-script.js::detectAtsPlatformHint). A hint from
        # that live page is only ever TRUSTED after being validated against
        # the real adapter registry — never taken on faith — so a bogus or
        # malicious hint can do no more than a wrong URL-detection guess
        # already could.
        try:
            if body.ats_platform_hint and get_adapter_class(body.ats_platform_hint) is not None:
                application.ats_platform = body.ats_platform_hint
            else:
                detection = detect_ats_for_url(job_url)
                application.ats_platform = detection["ats"]
            db.commit()
        except Exception:
            logger.exception("Application %s: pre-flight ATS detection failed — leaving ats_platform unset.", application.application_id)

    return application


@router.get("", response_model=list[ApplicationResponse])
def list_my_applications(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return application_repository.list_for_user(db, user.user_id)


# ------------------------------------------------------------------
# HITL platform — dashboard, review queue, duplicate check
# ------------------------------------------------------------------
# Registered BEFORE /{application_id} so these literal paths are matched
# first — FastAPI resolves routes in declaration order, and a dynamic
# /{application_id} declared first would swallow "overview"/"reviews" as an id.

@router.get("/overview", response_model=ApplicationOverviewResponse)
def get_applications_overview(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Dashboard overview tiles (§9): total/submitted/in-progress/waiting-for-
    human/waiting-for-review/failed/cancelled, for the current user."""
    return application_repository.get_overview_counts(db, user.user_id)


@router.get("/reviews", response_model=list[ApplicationResponse])
def list_applications_needing_review(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The "action needed" queue: every application of this user's still
    waiting on a human (manual_required/needs_review/copilot_review), oldest
    first."""
    return application_repository.list_reviews_for_user(db, user.user_id)


@router.get("/check-duplicate", response_model=DuplicateCheckResponse)
def check_duplicate_application(
    company: str | None = None,
    position: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Soft duplicate warning (§14) using company+position as a secondary
    signal — the URL-based uniqueness constraint on `POST /applications/start`
    remains the hard, authoritative check. The frontend calls this before
    starting a new application and shows a confirm dialog if it comes back
    `possible_duplicate=true`; it never blocks on its own."""
    existing = application_repository.find_possible_duplicate(db, user.user_id, company=company, position=position)
    if existing is None:
        return DuplicateCheckResponse(possible_duplicate=False)
    return DuplicateCheckResponse(
        possible_duplicate=True, existing_application_id=existing.application_id, existing_status=existing.status,
    )


@router.get("/{application_id}", response_model=ApplicationResponse)
def get_application(
    application_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _get_owned_application(db, application_id, user)


@router.get("/{application_id}/runs", response_model=list[AutomationRunResponse])
def list_application_runs(
    application_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Every `ApplicationFlowManager.run()` attempt for this application —
    screenshots/trace/error log for debugging (§14), newest first."""
    application = _get_owned_application(db, application_id, user)
    return application_repository.list_runs(db, application.application_id)


@router.get("/{application_id}/live", response_model=None)
def get_application_live_state(
    application_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Polled by the Live Automation view (§10) every 2-3s while an
    application is in progress. Returns the in-process live state
    (`ApplicationFlowManager.LIVE_RUN_STATE` — current page, last step,
    whether it's waiting on a human) when a run is actually active, or just
    the durable status/confidence from the DB otherwise (nothing running
    right now, or this process didn't run it — see
    `automation/applications/application_flow_manager.py`'s module docstring
    on why this is a best-effort, single-process view)."""
    application = _get_owned_application(db, application_id, user)
    live = get_live_state(application_id)
    return {
        "status": application.status,
        "display_status": application_repository.DISPLAY_STATUS_MAP.get(application.status, application.status.upper()),
        "confidence_score": application.confidence_score,
        "pages_completed": application.pages_completed,
        "live": live,
    }


@router.get("/{application_id}/questions", response_model=list[ApplicationQuestionResponse])
def list_application_questions(
    application_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The per-application screening-question ledger — what the Answer
    Review UI and Application Detail page render."""
    application = _get_owned_application(db, application_id, user)
    return application_question_repository.list_for_application(db, application.application_id)


@router.get("/{application_id}/review-summary", response_model=ApplicationReviewSummary)
def get_application_review_summary(
    application_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The pre-submission review gate's counts (§7)."""
    application = _get_owned_application(db, application_id, user)
    return application_question_repository.summarize_for_application(db, application.application_id)


@router.get("/{application_id}/audit-log", response_model=list[AuditLogEntryResponse])
def list_application_audit_log(
    application_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The append-only decision/approval trail (§15/§16) — who decided what,
    and when. Read-only; there is no update/delete route for this table,
    ever (see `app/services/audit_log_repository.py`)."""
    application = _get_owned_application(db, application_id, user)
    return audit_log_repository.list_for_application(db, application.application_id)


@router.post("/{application_id}/questions/{question_id}/review", response_model=ApplicationQuestionResponse)
def review_application_question(
    application_id: str,
    question_id: str,
    body: QuestionReviewRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """A human approves, edits, or rejects one generated answer (§6). On
    approve/edit, also upserts the (question, final answer) into this user's
    cross-application answer cache — including its embedding, so a
    semantically similar question on a future application can reuse it (§11)
    — so a correction made once is never re-made."""
    application = _get_owned_application(db, application_id, user)
    question = application_question_repository.get(db, question_id)
    if question is None or question.application_id != application.application_id:
        raise HTTPException(status_code=404, detail="Question not found on this application.")

    # Anti-bot decoys are not answerable, and this is the last line of defence:
    # they are no longer recorded as questions and no longer rendered, but rows
    # created before that fix still exist, and this route is reachable directly.
    # Accepting an answer here would write the value into the cross-application
    # answer cache below — auto-filling that decoy on every future application
    # and flagging each one as a bot.
    if is_decoy_field(question.question_text):
        raise HTTPException(
            status_code=409,
            detail=(
                "This field is a hidden anti-bot check, not a question — it is meant to stay "
                "empty. Autogram deliberately leaves it blank; filling it would make the "
                "employer's system treat the application as automated."
            ),
        )

    try:
        question = application_question_repository.apply_review(db, question, action=body.action, answer=body.answer)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if body.action in ("approve", "edit"):
        final_answer = question.human_answer if body.action == "edit" else question.answer
        if final_answer:
            answer_cache_repository.save_answer(
                db, user.user_id, question.question_text,
                answer=final_answer, source="deterministic" if body.action == "approve" else "llm",
                confidence=1.0,
            )
    return question


@router.post("/{application_id}/approve", response_model=ApplicationApprovalResult)
def approve_application(
    application_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Approves final submission for a `copilot_review` application (§7/§8):
    replays a submit click against the exact browser/page the run left open
    (`submit_open_review_session` — the SAME confirmation logic the
    `AUTO_SUBMIT` decision path uses, so the two can never disagree about
    what counts as "submitted"), persists the result, and logs an audit
    entry either way."""
    application = _get_owned_application(db, application_id, user)
    if application.status != "copilot_review":
        raise HTTPException(
            status_code=409,
            detail=f"Application is '{application.status}', not ready for approval (must be copilot_review).",
        )

    outcome = submit_open_review_session(application.application_id)
    if outcome is None:
        raise HTTPException(
            status_code=409,
            detail="No open review session found for this application — it may have already timed out or closed.",
        )
    status, error = outcome
    application.status = status
    application.failure_reason = error if status != "applied" else None
    if status == "applied":
        application.applied_date = datetime.now(timezone.utc)
    application.updated_at = datetime.now(timezone.utc)
    db.commit()

    _record_audit_event(
        db, application_id=application.application_id, user_id=user.user_id,
        event_type="human_approved", actor=user.user_id, metadata={"result_status": status},
    )
    message = {
        "applied": "Submission confirmed.",
        "needs_review": "Submit was clicked but could not be confirmed — please verify on the ATS.",
        "failed": error or "Could not submit — the submit control could not be clicked.",
    }.get(status, "Unknown outcome.")
    return ApplicationApprovalResult(status=status, message=message)


@router.post("/{application_id}/verification-code", response_model=ApplicationApprovalResult)
def submit_verification_code(
    application_id: str,
    body: VerificationCodeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Hand a one-time passcode to a run that is PAUSED on a verification gate.

    This is the deterministic path's answer to "where do I type the OTP?".
    Previously the only way in was the automation's own browser window, which
    assumes the user is sitting in front of it.

    SECRETS — the value reaching this handler is never persisted, never logged,
    and never echoed back. It goes straight to the in-memory
    `verification_channel`, where the paused run picks it up exactly once and
    types it into the live page. The response says only whether it was accepted
    for delivery. Note in particular that this route does NOT write to the chat
    transcript: `chat_repository.record_user_reply` would refuse a secret-typed
    request anyway, and the correct record of this action is the audit event
    below, which carries no value.

    Refuses unless the application is genuinely waiting on a human, so a code
    cannot be parked against a run that is not asking for one — where it would
    sit in memory with nothing to consume it.
    """
    application = _get_owned_application(db, application_id, user)

    if application.status != "manual_required":
        raise HTTPException(
            status_code=409,
            detail=(
                f"This application is not waiting for a verification code "
                f"(status: {application_repository.display_status(application)})."
            ),
        )
    # Only a verification gate takes a code. A CAPTCHA pause also sits in
    # `manual_required`, and typing a code at one would be meaningless — worse,
    # it would imply Autogram was trying to answer the CAPTCHA.
    reason = (application.failure_reason or "").lower()
    if not any(k in reason for k in ("passcode", "multi-factor", "verification", "one-time", "2fa")):
        raise HTTPException(
            status_code=409,
            detail=(
                "This application is waiting on a different kind of human step, not a verification "
                "code. Please complete it in the automation's browser window."
            ),
        )

    if not verification_channel.deliver(application_id, body.code):
        raise HTTPException(status_code=422, detail="Enter the verification code you received.")

    # Metadata only — that a code was supplied, never the code itself.
    _record_audit_event(
        db, application_id=application_id, user_id=user.user_id,
        event_type="verification_code_supplied", actor=user.user_id,
        metadata={"delivered": True},
    )
    _emit(application_id, "HUMAN_ACTION_COMPLETED", request_type="OTP_REQUIRED")
    return ApplicationApprovalResult(
        status=application.status,
        message="Code sent to the automation. It will be entered in the browser shortly.",
    )


@router.post("/{application_id}/reject", response_model=ApplicationResponse)
def reject_application(
    application_id: str,
    reason: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """A human declines to continue — closes any open review session
    (browser) and cancels the application (§7 "Go Back & Edit" / a final
    rejection). Distinct from `failed`: nothing malfunctioned here."""
    application = _get_owned_application(db, application_id, user)
    close_review_session(application.application_id)
    application = application_repository.mark_cancelled(db, application, reason=reason)
    _record_audit_event(
        db, application_id=application.application_id, user_id=user.user_id,
        event_type="human_rejected", actor=user.user_id, metadata={"reason": reason} if reason else None,
    )
    return application


@router.post("/{application_id}/report-status", response_model=ApplicationResponse)
def report_application_status(
    application_id: str,
    body: ReportStatusRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The browser extension's own self-reported progress — there is no
    server-side Playwright run to derive this from (`source ==
    "browser_extension"`, see `start_application`), so the extension calls
    this directly after every state change: `manual_required` while it waits
    on a human for a CAPTCHA, and a final `applied`/`failed`/`needs_review`/
    `copilot_review`/`cancelled` once the human has reviewed and (for v1,
    copilot-only) clicked submit on the real page themselves. Not restricted
    to extension-sourced applications — a server-automation caller could use
    it too, though today only the extension does."""
    application = _get_owned_application(db, application_id, user)
    try:
        application = application_repository.report_status(
            db, application, status=body.status, reason=body.reason, confidence=body.confidence,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _record_audit_event(
        db, application_id=application.application_id, user_id=user.user_id,
        event_type="extension_status_reported", actor=user.user_id,
        metadata={"status": body.status, "reason": body.reason},
    )
    return application


def _record_audit_event(db: Session, **kwargs) -> None:
    """Best-effort — a failed audit write must never block the approve/reject
    action it's describing (the action itself already committed)."""
    try:
        audit_log_repository.record_event(db, **kwargs)
    except Exception:
        logger.exception("Could not record audit log event %r for application %s.", kwargs.get("event_type"), kwargs.get("application_id"))


#: Deterministic-path status -> the live event the UI switches on. Derived from
#: the status the run actually produced rather than guessed at the call site, so
#: the stream can never disagree with what was persisted.
_STATUS_EVENTS = {
    "applied": "APPLICATION_SUBMITTED",
    "copilot_review": "REVIEW_REQUIRED",
    "needs_review": "REVIEW_REQUIRED",
    "manual_required": "HUMAN_ACTION_REQUIRED",
    "failed": "APPLICATION_FAILED",
    "cancelled": "APPLICATION_FAILED",
}


def _emit(application_id: str, event_type: str, **payload) -> None:
    """Publish a live workflow event. Never raises.

    Called from the Playwright worker thread as well as from request handlers,
    and a browser tab that is not listening must never be able to affect a real
    job application — so every failure is swallowed. The durable record is the
    audit log and the status column; this is only the notification that they
    changed.

    `payload` is serialized straight to a browser, so it carries display context
    only — never a verification code, cookie, or token.
    """
    try:
        publish_application_event(application_id, event_type, **payload)
    except Exception:  # noqa: BLE001 - defensive; publish already swallows its own
        logger.debug("Could not publish %s for application %s.", event_type, application_id, exc_info=True)


# ------------------------------------------------------------------
# HITL platform — callbacks invoked FROM the dedicated Playwright thread
# ------------------------------------------------------------------
# Both open their OWN short-lived session rather than reuse `_run_application`'s
# `db` — that session belongs to a different thread (see
# `_run_on_dedicated_thread`'s docstring on why Playwright gets its own
# never-recycled thread), and SQLAlchemy sessions are not meant to be shared
# across threads even when access happens to be sequential rather than
# concurrent.

def _mark_waiting_for_human(application_id: str, reason: str) -> None:
    session = SessionLocal()
    try:
        application_repository.mark_waiting_for_human(session, application_id, reason=reason)
        # Published AFTER the status commits, so a client that reacts by
        # refetching always sees the state the event announced.
        _emit(application_id, "HUMAN_ACTION_REQUIRED", reason=reason)
    except Exception:
        logger.exception("Application %s: could not persist WAITING_FOR_HUMAN status.", application_id)
    finally:
        session.close()


def _recover_crashed_run(application_id: str) -> None:
    """Release the job ownership a crashed `_run_application` would otherwise
    hold forever, on a FRESH session.

    Fresh because the run's own `db` is why we are here: it may be in a
    needs-rollback state, in which case every further statement on it raises
    too — the same reasoning `result_db` above already relies on for the
    result write.

    Only `pending` and `processing` are considered, and each maps to a
    different outcome for a real reason (see
    `automation_recovery.DETERMINISTIC_RECOVERY`): `pending` provably never
    opened a browser, so it is safely `failed`; `processing` may have clicked
    Submit, so it becomes `needs_review` and is never auto-retried.
    `copilot_review` is deliberately NOT touched here — this process is alive,
    so a review session it left open is still genuinely open and approvable.
    """
    from app.services import automation_recovery

    recovery_db = SessionLocal()
    try:
        for from_status in ("processing", "pending"):
            new_status = automation_recovery.try_recover_application(
                recovery_db, application_id=application_id, from_status=from_status,
            )
            if new_status is not None:
                _record_audit_event(
                    recovery_db, application_id=application_id,
                    user_id=_user_id_for_application(recovery_db, application_id),
                    event_type="automation_recovered", actor="system",
                    metadata={
                        "reason": "run_crashed",
                        "prior_status": from_status,
                        "new_status": new_status,
                        "may_have_submitted": from_status == "processing",
                    },
                )
                logger.warning(
                    "Application %s: recovered crashed run %s -> %s.",
                    application_id, from_status, new_status,
                )
                _emit(
                    application_id, _STATUS_EVENTS.get(new_status, "APPLICATION_FAILED"),
                    status=new_status, reason="run_crashed",
                )
                break
    except Exception:
        # Never let recovery bookkeeping mask the original crash, which has
        # already been logged with its traceback above.
        logger.exception("Application %s: could not recover a crashed run.", application_id)
    finally:
        recovery_db.close()


def _user_id_for_application(db: Session, application_id: str) -> str | None:
    application = application_repository.get_by_id(db, application_id)
    return application.user_id if application else None


def _is_kill_switch_engaged(user_id: str) -> bool:
    """Fresh DB read every call — deliberately not cached, so flipping the
    switch takes effect on THIS run's very next page, not just future runs."""
    session = SessionLocal()
    try:
        profile = profile_repository.get_by_user_id(session, user_id)
        return bool(profile and profile.autopilot_globally_disabled)
    finally:
        session.close()


# ------------------------------------------------------------------
# Background task — the actual app.api -> automation handoff
# ------------------------------------------------------------------

def _run_application(application_id: str, resume_document_id: str, job_description: str | None = None) -> None:
    """Runs entirely in the background after `POST /applications/start`
    returns 202. Uses its own DB session (the request session is closed by
    the time this runs) — same pattern as `resumes.py::_run_extraction`."""
    db = SessionLocal()
    try:
        application = application_repository.get_by_id(db, application_id)
        if application is None:
            logger.warning("Application %s vanished before its run started.", application_id)
            return

        application = application_repository.mark_processing(db, application)
        _emit(application_id, "APPLICATION_STARTED", job_url=application.job_url)

        try:
            detection = detect_ats_for_url(application.job_url)
        except Exception:
            logger.exception("ATS detection failed for application %s", application_id)
            application.status = "failed"
            application.failure_reason = "ATS detection failed — see server logs."
            db.commit()
            return

        ats_platform = detection["ats"]
        adapter_cls = get_adapter_class(ats_platform)
        _emit(application_id, "PAGE_ANALYZED", ats_platform=ats_platform)

        if adapter_cls is None and ats_platform != FALLBACK_ATS:
            # A CONFIDENTLY detected platform with no dedicated adapter
            # implemented yet (e.g. oracle_hcm, taleo, icims, smartrecruiters,
            # ashby, bamboohr — see automation/ats/registry.py). This used to
            # be an instant dead end (needs_review, automation never even
            # attempted) on the theory that "clicking around a page already
            # identified as, say, Workday won't produce an adapter that
            # doesn't exist." That reasoning stops applying once GenericAdapter
            # is a real, working fallback: `adapter_cls` is left `None` here on
            # purpose and handed to the flow manager exactly like the
            # FALLBACK_ATS ("custom") case below — its own
            # `_resolve_adapter_from_listing_page` re-detects against the LIVE
            # page and, when no REGISTERED adapter resolves (which it won't,
            # for any of these platforms, since none is registered), falls back
            # to GenericAdapter's same label/name/placeholder-driven fill every
            # real adapter already uses — never a crash.
            #
            # Note this is NOT gated on whether `ats_platform` happens to be a
            # member of `ApplicationFlowManager.PUBLIC_ATS_PLATFORMS` — two of
            # that set's entries ("smartrecruiters", "ashby") have no adapter
            # registered either, so relying on non-membership here would be
            # wrong for exactly those two. What actually keeps this safe is
            # that `_resolve_adapter_from_listing_page`'s
            # `_fall_back_to_generic_adapter` unconditionally reassigns
            # `self.ats_platform` to `GenericAdapter.name` ("custom") the
            # moment it hands off to the generic fallback, regardless of what
            # this variable said going in — so `decide_action` never sees a
            # `PUBLIC_ATS_PLATFORMS` member for a run GenericAdapter actually
            # filled, and AUTO_SUBMIT is never on the table for any of these. A
            # human still reviews (and, in copilot mode, submits) every one of
            # these — this only replaces "automation never even tried" with
            # "automation tried, a human confirms."
            logger.info(
                "Application %s: '%s' confidently detected but no dedicated adapter yet — "
                "attempting the generic fallback instead of an immediate needs_review.",
                application_id, ats_platform,
            )
            adapter_cls = None
        # adapter_cls is None (either FALLBACK_ATS/"custom", or a confidently
        # detected platform with no dedicated adapter — see above): handed to
        # the flow manager anyway ("Apply from Job Link"), which re-detects
        # against the LIVE page and, if this looks like a job-listing page
        # rather than a form, clicks its own Apply/Apply Now/Start
        # Application control and re-detects again before falling back to
        # GenericAdapter — see
        # ApplicationFlowManager._resolve_adapter_from_listing_page.

        profile = profile_repository.get_by_user_id(db, application.user_id)
        resume_document = profile_repository.get_document(db, resume_document_id)
        if profile is None or resume_document is None:
            application.status = "failed"
            application.failure_reason = "Candidate profile or resume disappeared before the run started."
            db.commit()
            return

        application.resume_used = resume_document.document_id
        db.commit()

        # Phase 6: answers screening questions FieldMapper (Phase 5) can't
        # resolve to a known profile field — deterministic facts (notice
        # period, salary, work authorization, years of experience) straight
        # from `profile`, or one batched LLM call per form for genuinely
        # subjective/novel questions. Backed by this user's persistent
        # answer cache (`db`/`user_id`) so a repeated question never costs a
        # second LLM call. Never raises — a failure here would otherwise
        # take down the whole run over what's meant to be a best-effort
        # enhancement, so it's caught and logged instead.
        try:
            answer_engine = ApplicationAnswerEngine(
                profile=profile, job_description=job_description, db=db, user_id=application.user_id,
                application_id=application.application_id,
            )
        except Exception:
            logger.exception("Application %s: could not build ApplicationAnswerEngine — continuing without it.", application_id)
            answer_engine = None

        # The vision fallback reads the fields nothing else could fill off
        # cropped screenshots (see automation/forms/vision_fallback.py). It
        # reuses the answer engine's view of the candidate, so there's nothing
        # to build without one — and, like the engine itself, a failure here
        # only costs the run its last-resort pass, never the run.
        vision_answerer = None
        if answer_engine is not None and AUTOMATION_VISION_FALLBACK:
            try:
                vision_answerer = VisionFormAnswerer(answer_engine)
            except Exception:
                logger.exception(
                    "Application %s: could not build the vision fallback — continuing without it.", application_id,
                )

        manager = ApplicationFlowManager(
            application_id=application.application_id,
            user_id=application.user_id,
            job_url=application.job_url,
            ats_platform=ats_platform,
            adapter_cls=adapter_cls,
            profile=profile,
            resume_document=resume_document,
            autopilot_enabled=application.autopilot_enabled,
            # Copilot mode (autopilot off) is the "a human reviews and
            # submits" path — run visibly and leave the browser open at the
            # end (see ApplicationFlowManager.should_keep_browser_open) so
            # there's actually something to review. Autopilot runs stay
            # headless (AUTOMATION_HEADLESS default) since nothing needs to
            # watch them. A CAPTCHA/human-gate wait also needs a VISIBLE
            # browser for a human to actually act in, so autopilot runs are
            # forced visible too whenever one might occur — cheap, since
            # `_wait_for_human` only fires when a gate is actually detected.
            headless=False,
            answer_engine=answer_engine,
            vision_answerer=vision_answerer,
            on_waiting_for_human=lambda reason: _mark_waiting_for_human(application_id, reason),
            is_kill_switch_engaged=lambda: _is_kill_switch_engaged(application.user_id),
        )
        # See _run_on_dedicated_thread's docstring: Playwright's sync API
        # must run on a thread that was never touched by asyncio AND won't
        # be handed to some other run later if this one leaves its browser
        # open for review, hence the fresh one-off thread rather than a
        # shared pool or calling manager.run() directly here.
        result = _run_on_dedicated_thread(manager.run)
        # `db` has been alive for the ENTIRE automation run above — commonly
        # many minutes, since `answer_engine` (constructed with this same
        # session) keeps using it throughout for cache reads/writes. If
        # anything on it failed and left its transaction in a
        # needs-rollback state at any point during that run (a dropped
        # connection, a transient write failure — observed live: a
        # ~19-minute Amex run's `_fill_opt_in_checkboxes` lazy-load blew up
        # with `PendingRollbackError` from a much earlier, unrelated failed
        # flush), every later operation on `db` — including this one, the
        # single most consequential write of the whole run — would raise
        # too, discarding a real, hard-won result (5 pages filled, résumé
        # uploaded) that never gets a chance to reach the database. A
        # dedicated, guaranteed-fresh session for JUST this write means the
        # result lands regardless of what happened to `db` during the run.
        result_db = SessionLocal()
        try:
            fresh_application = application_repository.get_by_id(result_db, application_id)
            if fresh_application is not None:
                application_repository.apply_run_result(result_db, fresh_application, result)
                # Derived from the status that just COMMITTED, so the stream can
                # never claim an outcome the database disagrees with.
                _emit(
                    application_id,
                    _STATUS_EVENTS.get(fresh_application.status, "APPLICATION_FAILED"),
                    status=fresh_application.status,
                    display_status=application_repository.display_status(fresh_application),
                )
            else:
                logger.warning("Application %s vanished before its result could be recorded.", application_id)
        finally:
            result_db.close()
    except Exception:
        logger.exception("Background application run crashed for %s", application_id)
        db.rollback()
        # This handler used to end here — which meant an unexpected failure
        # anywhere after `mark_processing` left `processing` persisted with no
        # thread behind it, and `find_active_automation` reads status alone, so
        # the job stayed blocked with 409 forever. Reproduced against real
        # Postgres in `automation/tests/test_crash_recovery.py`.
        #
        # Startup reconciliation cannot cover this case: the PROCESS is still
        # alive and serving requests, so there is no restart coming. It has to
        # be resolved here, at the point of failure.
        _recover_crashed_run(application_id)
    finally:
        db.close()
