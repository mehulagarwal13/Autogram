"""
Per-application screening-question ledger — HITL platform.

One row per question ASKED on one application run (see
`app/models/db_models.py::ApplicationQuestion` for why this is a separate
table from the cross-application `answer_cache`). Written by
`automation/forms/answer_engine.py::ApplicationAnswerEngine.answer_batch()` as
it answers each question; read by the Answer Review UI, the Application
Detail page, and updated by a human via
`POST /applications/{id}/questions/{question_id}/review`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.db_models import ApplicationQuestion, confidence_level_for

VALID_REVIEW_ACTIONS = {"approve", "edit", "reject"}


def record_question(
    db: Session,
    application_id: str,
    *,
    question_text: str,
    source: str,
    confidence: float,
    page_number: int | None = None,
    field_type: str | None = None,
    available_options: list[str] | None = None,
    answer: str | None = None,
) -> ApplicationQuestion:
    """Records one answered (or declined) question. `confidence_level` and the
    initial `review_status` are derived here, once, from the same thresholds
    `ApplicationFlowManager.decide_action` gates on — see
    `db_models.confidence_level_for` — so this table can never disagree with
    the flow manager about what counts as trustworthy."""
    level = confidence_level_for(source, confidence)
    entry = ApplicationQuestion(
        question_id=str(uuid.uuid4()),
        application_id=application_id,
        page_number=page_number,
        question_text=question_text,
        field_type=field_type,
        available_options=list(available_options) if available_options else None,
        answer=answer or None,
        source=source,
        confidence=confidence,
        confidence_level=level,
        # A LOW-confidence or undecided answer is never auto-trusted — it
        # waits for a human, same philosophy as the flow manager's own
        # NEEDS_REVIEW gate. Everything else was filled with no human input
        # yet needed, which is its own reviewable state, not "reviewed".
        review_status="pending_review" if level == "LOW" else "auto_filled",
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def get(db: Session, question_id: str) -> ApplicationQuestion | None:
    return db.query(ApplicationQuestion).filter(ApplicationQuestion.question_id == question_id).first()


def list_for_application(db: Session, application_id: str) -> list[ApplicationQuestion]:
    return (
        db.query(ApplicationQuestion)
        .filter(ApplicationQuestion.application_id == application_id)
        .order_by(ApplicationQuestion.page_number, ApplicationQuestion.created_at)
        .all()
    )


def apply_review(
    db: Session,
    question: ApplicationQuestion,
    *,
    action: str,
    answer: str | None = None,
) -> ApplicationQuestion:
    """Applies a human's decision on one question. `approve` accepts the
    existing `answer` as-is; `edit` requires and stores a replacement in
    `human_answer` (the original `answer` is left untouched — this is the
    record of what automation produced vs. what the human actually approved);
    `reject` records that the human declined this answer entirely, leaving the
    field for the pre-submission review to flag as unresolved."""
    if action not in VALID_REVIEW_ACTIONS:
        raise ValueError(f"Invalid review action {action!r}. Must be one of {sorted(VALID_REVIEW_ACTIONS)}.")
    if action == "edit" and not (answer or "").strip():
        raise ValueError("An 'edit' review action requires a non-empty answer.")

    question.review_status = "approved" if action == "approve" else ("edited" if action == "edit" else "rejected")
    if action == "edit":
        question.human_answer = answer.strip()
    question.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(question)
    return question


def summarize_for_application(db: Session, application_id: str) -> dict:
    """The counts the pre-submission review gate (§7) shows: how many
    questions were answered/generated/human-reviewed, plus which ones are
    still missing or risky. Computed on demand from this ledger rather than
    kept as separate counters on `Application`, so there's exactly one source
    of truth and no risk of the two drifting apart."""
    questions = list_for_application(db, application_id)
    missing = [q.question_text for q in questions if not (q.answer or q.human_answer)]
    risky = [
        q.question_text for q in questions
        if q.confidence_level == "LOW" and q.review_status not in ("approved", "edited")
    ]
    return {
        "questions_total": len(questions),
        "questions_answered": sum(1 for q in questions if q.answer or q.human_answer),
        "questions_generated": sum(1 for q in questions if q.source == "llm"),
        "questions_human_reviewed": sum(1 for q in questions if q.review_status in ("approved", "edited", "rejected")),
        "missing_fields": missing,
        "risky_answers": risky,
    }
