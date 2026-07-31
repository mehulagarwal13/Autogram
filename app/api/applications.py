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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import SessionLocal, get_db
from app.models.application import ApplicationResponse, ApplicationStartRequest, AutomationRunResponse
from app.models.db_models import Application, User
from app.services import application_repository, profile_repository
from automation.applications.application_flow_manager import ApplicationFlowManager
from automation.ats.detector import detect_ats_for_url
from automation.ats.registry import get_adapter_class
from automation.forms.answer_engine import ApplicationAnswerEngine

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

        if adapter_cls is None:
            application_repository.mark_unsupported_ats(
                db, application, ats_platform=ats_platform, confidence=detection["confidence"]
            )
            logger.info(
                "Application %s: no adapter for '%s' yet — marked needs_review.",
                application_id, ats_platform,
            )
            return

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
            )
        except Exception:
            logger.exception("Application %s: could not build ApplicationAnswerEngine — continuing without it.", application_id)
            answer_engine = None

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
            # watch them.
            headless=False if not application.autopilot_enabled else None,
            answer_engine=answer_engine,
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
