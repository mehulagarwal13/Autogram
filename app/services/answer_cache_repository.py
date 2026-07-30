"""
Answer cache — Phase 6 (see ARCHITECTURE.md, automation/forms/answer_engine.py).

Screening-question answers (deterministic or LLM-generated) are cached per
user, keyed by a hash of the *normalized* question text, so the same
question asked again on a later application (a large share of real
screening questions repeat near-verbatim across postings on the same ATS
family) costs nothing the second time — no re-deriving, no second LLM call.

This is deliberately an EXACT-match cache (normalized whitespace/case/
punctuation only) — there's no semantic/embedding similarity search yet.
Two differently-worded questions that mean the same thing will not hit each
other; each gets its own row. A pgvector-backed near-duplicate cache
(question -> embedding -> nearest cached question above a similarity floor,
reusing `app/services/embedding_service.py` the same way job matching
does) is a natural follow-up once this exact-match version has real usage
data to justify the extra complexity — see ARCHITECTURE.md §4 Roadmap.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.db_models import AnswerCacheEntry

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]")


def normalize_question(question: str) -> str:
    """Case/whitespace/punctuation-insensitive — "What's your notice
    period?" and "whats your notice period" normalize (and therefore hash)
    identically. Same "cheap, explainable, no fuzzy matching" philosophy as
    `automation/forms/field_mapper.py`."""
    text = _PUNCTUATION.sub("", question.strip().lower())
    return _WHITESPACE.sub(" ", text).strip()


def compute_question_hash(question: str) -> str:
    return hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()


def get_cached_answer(db: Session, user_id: str, question: str) -> AnswerCacheEntry | None:
    question_hash = compute_question_hash(question)
    return (
        db.query(AnswerCacheEntry)
        .filter(AnswerCacheEntry.user_id == user_id, AnswerCacheEntry.question_hash == question_hash)
        .first()
    )


def save_answer(
    db: Session,
    user_id: str,
    question: str,
    *,
    answer: str,
    source: str,
    confidence: float,
) -> AnswerCacheEntry:
    """Upsert, not insert-only — a later run answering the same question
    with fresher profile data (or a corrected LLM answer) should overwrite
    the stale cached one instead of piling up duplicate rows for the same
    (user, question)."""
    question_hash = compute_question_hash(question)
    existing = (
        db.query(AnswerCacheEntry)
        .filter(AnswerCacheEntry.user_id == user_id, AnswerCacheEntry.question_hash == question_hash)
        .first()
    )
    now = datetime.now(timezone.utc)
    if existing:
        existing.answer = answer
        existing.source = source
        existing.confidence = confidence
        existing.question_text = question
        existing.updated_at = now
        db.commit()
        db.refresh(existing)
        return existing

    entry = AnswerCacheEntry(
        cache_id=str(uuid.uuid4()),
        user_id=user_id,
        question_hash=question_hash,
        question_text=question,
        answer=answer,
        source=source,
        confidence=confidence,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
