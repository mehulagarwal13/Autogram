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

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.application import FieldMapRequest, FieldMapResult
from app.models.db_models import User, confidence_level_for
from app.services import application_repository, profile_repository
from automation.forms.answer_engine import ApplicationAnswerEngine, Question, QUESTION_SOURCE_MAP

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/automation", tags=["automation"])


@router.post("/map-fields", response_model=list[FieldMapResult])
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
    same way every other per-application endpoint enforces it."""
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

    return [
        FieldMapResult(
            question_text=result.question,
            answer=result.answer,
            confidence=result.confidence,
            confidence_level=confidence_level_for(QUESTION_SOURCE_MAP.get(result.source, "llm"), result.confidence),
            source=QUESTION_SOURCE_MAP.get(result.source, "llm"),
        )
        for result in results
    ]
