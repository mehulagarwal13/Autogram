"""
Browser-extension support — a thin, plain-JSON wrapper around the SAME
`ApplicationAnswerEngine` (`automation/forms/answer_engine.py`) the
server-side Playwright automation uses. This is the one new backend
endpoint the extension needs: everything else (profile, applications,
question review, audit log) is the existing API, unchanged.

`ApplicationAnswerEngine` has no Playwright coupling at all — `Question`/
`AnswerResult` are plain dataclasses, `answer_batch()` only needs a
`CandidateProfile` (a data object) and strings — so wrapping it here is
"call the same class with different data," not a second answer-generation
engine. Every answer is still persisted to `application_questions` (via
`answer_batch`'s own `_persist_question` call, unchanged) and cached in
`answer_cache` for reuse across BOTH delivery paths, so a question answered
once through the web/server-automation flow is already known here, and
vice versa.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.application import (
    AutomationConfigResponse,
    DecideRequest,
    DecideResponse,
    FieldMapRequest,
    FieldMapResponse,
    FieldMapResult,
    PacingConfig,
)
from app.models.db_models import User, confidence_level_for
from app.services import application_repository, profile_repository, trust_level_repository
from automation.applications.application_flow_manager import (
    AUTO_SUBMIT_CONFIDENCE_THRESHOLD,
    NEEDS_REVIEW_CONFIDENCE_THRESHOLD,
    PUBLIC_ATS_PLATFORMS,
    decide_action,
)
from automation.browser.session import DEFAULT_PACING
from automation.forms.answer_engine import ApplicationAnswerEngine, Question, QUESTION_SOURCE_MAP

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/automation", tags=["automation"])


@router.post("/map-fields", response_model=FieldMapResponse)
def map_fields(
    body: FieldMapRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The browser extension's content script sends every field it found on
    the page as plain data (label, type, options — no DOM handle, since this
    call happens from a background service worker, not inside the page);
    this answers each one exactly the way the server-side flow does:
    profile match -> answer memory (exact or semantic) -> one batched LLM
    call for whatever's left. Ownership of `application_id` is enforced the
    same way every other per-application endpoint enforces it.

    `action` comes from `decide_action()` — the SAME function the
    server-side Playwright engine's `_run_on_page` calls — never a
    reimplementation, so the two actuators can never disagree about whether
    a given form should auto-submit. Same for the §6.4 trust level it's
    given: resolved fresh from the DB, never cached across this request."""
    application = application_repository.get_by_id(db, body.application_id)
    if application is None or application.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Application not found.")

    profile = profile_repository.get_by_user_id(db, user.user_id)
    if profile is None:
        raise HTTPException(status_code=400, detail="No candidate profile found. Create one with POST /profile first.")

    engine = ApplicationAnswerEngine(
        profile=profile, job_description=body.job_description, db=db, user_id=user.user_id,
        application_id=application.application_id,
    )
    if body.page_number is not None:
        engine.current_page_number = body.page_number

    questions = [Question(f.question_text, tuple(f.options or ())) for f in body.fields]
    results = engine.answer_batch(questions)

    mapped = [
        FieldMapResult(
            question_text=result.question,
            answer=result.answer,
            confidence=result.confidence,
            confidence_level=confidence_level_for(QUESTION_SOURCE_MAP.get(result.source, "llm"), result.confidence),
            source=QUESTION_SOURCE_MAP.get(result.source, "llm"),
        )
        for result in results
    ]

    # Same definition `ApplicationFlowManager._aggregate_confidence` uses:
    # the fraction of fields that actually came back USABLE (HIGH/MEDIUM —
    # i.e. something a form would actually get filled with), not a raw
    # average of confidence scores. An empty batch is trivially "nothing to
    # decide on" — 0.0, same as the Playwright engine's own empty case.
    usable = sum(1 for f in mapped if f.confidence_level in ("HIGH", "MEDIUM"))
    overall_confidence = round(usable / len(mapped), 4) if mapped else 0.0
    trust_level = trust_level_repository.resolve_trust_level(db, user.user_id, application.job_url)
    action = decide_action(overall_confidence, application.ats_platform or "custom", application.autopilot_enabled, trust_level)

    return FieldMapResponse(fields=mapped, overall_confidence=overall_confidence, action=action)


@router.post("/decide", response_model=DecideResponse)
def decide(
    body: DecideRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Wraps `decide_action()` directly for a caller that has ALREADY
    computed `overall_confidence` itself — e.g. the extension, after
    combining client-side deterministic profile matches (no LLM involved)
    with `POST /automation/map-fields`' results for whatever was left. The
    combining is plain arithmetic (a fraction of usable fields); the actual
    submission decision is not — that always comes from this one function,
    never reimplemented client-side.

    §6.4: resolves this job's trust level the same way the Playwright engine
    does (`app/api/applications.py::_resolve_trust_level_for`) — the
    extension is a second, parallel automation surface, and its own
    auto-submit gate must honor the same per-site trust the user set,
    fresh from the DB every call, same as the kill switch below it."""
    application = application_repository.get_by_id(db, body.application_id)
    if application is None or application.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Application not found.")

    trust_level = trust_level_repository.resolve_trust_level(db, user.user_id, application.job_url)
    action = decide_action(body.overall_confidence, application.ats_platform or "custom", application.autopilot_enabled, trust_level)
    return DecideResponse(action=action, overall_confidence=body.overall_confidence)


@router.get("/config", response_model=AutomationConfigResponse)
def get_automation_config(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The extension's "policy brain" surface — polled before every fill AND
    again before any auto-submit click (never cached for a session): the
    account-level kill switch (fail closed on a DB error, same contract the
    Playwright engine's own check uses — see
    `app/api/applications.py::_is_kill_switch_engaged`) plus the shared
    pacing/confidence-threshold numbers, so the extension never invents its
    own throttle limits or auto-submit bar."""
    try:
        profile = profile_repository.get_by_user_id(db, user.user_id)
        kill_switch_engaged = bool(profile and profile.autopilot_globally_disabled)
    except Exception:
        logger.exception("User %s: kill switch check failed — failing closed (treating as engaged).", user.user_id)
        kill_switch_engaged = True

    return AutomationConfigResponse(
        kill_switch_engaged=kill_switch_engaged,
        pacing=PacingConfig(**asdict(DEFAULT_PACING)),
        auto_submit_confidence_threshold=AUTO_SUBMIT_CONFIDENCE_THRESHOLD,
        needs_review_confidence_threshold=NEEDS_REVIEW_CONFIDENCE_THRESHOLD,
        public_ats_platforms=sorted(PUBLIC_ATS_PLATFORMS),
    )
