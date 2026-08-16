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
)
from app.models.db_models import Application, User
from app.services import (
    answer_cache_repository,
    application_question_repository,
    application_repository,
    audit_log_repository,
    profile_repository,
)
from automation.applications.application_flow_manager import (
    ApplicationFlowManager,
    close_review_session,
    get_live_state,
    submit_open_review_session,
)
from automation.ats.detector import FALLBACK_ATS, detect_ats_for_url
from automation.ats.registry import get_adapter_class
from automation.forms.answer_engine import ApplicationAnswerEngine
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
    rule — except a RETRYABLE_STATUSES attempt never actually succeeded, so
    there's nothing to double-apply: it's retried on the same row instead of
    inserting a second one. An IN_PROGRESS_STATUSES or COMPLETED_STATUSES
    attempt is rejected with 409 rather than silently doing nothing, so a
    caller can't mistake "no-op" for "started"."""
    job_url = str(body.job_url)
    existing = application_repository.get_by_user_and_url(db, user.user_id, job_url)

    if existing is not None:
        if existing.status in IN_PROGRESS_STATUSES:
            raise HTTPException(status_code=409, detail="Application is already in progress.")
        if existing.status in COMPLETED_STATUSES:
            raise HTTPException(status_code=409, detail="Application has already been completed.")
        if existing.status not in RETRYABLE_STATUSES:
            # No known status falls outside the three sets above, but if a
            # new one is ever added without updating them, fail safe by
            # leaving the row untouched rather than guessing.
            response.status_code = 200
            return existing

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

    if existing is None:
        application = application_repository.create_application(
            db,
            user_id=user.user_id,
            job_url=job_url,
            autopilot_enabled=body.autopilot_enabled,
            company=body.company,
            position=body.position,
        )
    else:
        # Retry: same row (same application_id/created_at/job_url_hash), so
        # the unique constraint stays satisfied and its AutomationRun
        # history from apply_run_result is kept.
        application = application_repository.retry_application(
            db, existing, company=body.company, position=body.position, autopilot_enabled=body.autopilot_enabled,
        )

    background_tasks.add_task(
        _run_application, application.application_id, resume_document_id, body.job_description,
    )

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


def _record_audit_event(db: Session, **kwargs) -> None:
    """Best-effort — a failed audit write must never block the approve/reject
    action it's describing (the action itself already committed)."""
    try:
        audit_log_repository.record_event(db, **kwargs)
    except Exception:
        logger.exception("Could not record audit log event %r for application %s.", kwargs.get("event_type"), kwargs.get("application_id"))


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
    except Exception:
        logger.exception("Application %s: could not persist WAITING_FOR_HUMAN status.", application_id)
    finally:
        session.close()


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

        if adapter_cls is None and ats_platform != FALLBACK_ATS:
            # A CONFIDENTLY detected platform with no adapter implemented yet
            # (Phase 7 gap) — clicking around a page already identified as,
            # say, Workday won't produce an adapter that doesn't exist.
            application_repository.mark_unsupported_ats(
                db, application, ats_platform=ats_platform, confidence=detection["confidence"]
            )
            logger.info(
                "Application %s: no adapter for '%s' yet — marked needs_review.",
                application_id, ats_platform,
            )
            return
        # adapter_cls is None AND ats_platform == FALLBACK_ATS ("custom"):
        # pre-flight detection found nothing recognizable at all — handed to
        # the flow manager anyway ("Apply from Job Link"), which re-detects
        # against the LIVE page and, if this looks like a job-listing page
        # rather than a form, clicks its own Apply/Apply Now/Start
        # Application control and re-detects again before giving up — see
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
        application_repository.apply_run_result(db, application, result)
    except Exception:
        logger.exception("Background application run crashed for %s", application_id)
        db.rollback()
    finally:
        db.close()
