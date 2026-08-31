"""
Answer cache — Phase 6 (see ARCHITECTURE.md, automation/forms/answer_engine.py).

Screening-question answers (deterministic or LLM-generated) are cached per
user, keyed by a hash of the *normalized* question text, so the same
question asked again on a later application (a large share of real
screening questions repeat near-verbatim across postings on the same ATS
family) costs nothing the second time — no re-deriving, no second LLM call.

Originally an EXACT-match-only cache (normalized whitespace/case/punctuation).
HITL platform update: `find_similar_answer` now also checks a SEMANTIC
near-duplicate match (question -> embedding -> nearest cached question above a
similarity floor, via `app/services/embedding_service.py` — the same local,
free embedding model job/resume matching already uses) when the exact-hash
lookup misses, so two differently-worded questions that mean the same thing
("What's your notice period?" / "How soon can you start a new role?") can
still hit the cache instead of costing a second LLM call. This is exactly the
follow-up this module's docstring used to call out as a "natural" future step
— see ARCHITECTURE.md §4 Roadmap / §2 Database Schema.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.db_models import AnswerCacheEntry
from app.services.embedding_service import generate_embedding

logger = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]")

# Defense-in-depth (spec §12 / non-negotiable #9: never store an OTP or a
# password). The REAL protection is architectural — a verification code only
# ever lives in `TaskHandle.pending_secret` (in-process, cleared on read) and
# never reaches this cache at all. This is the belt-and-suspenders backstop
# for the residual case: a screening QUESTION whose own text happens to look
# like a secret prompt (e.g. a misclassified "Enter the code we sent you")
# must never be written here even if some future caller mistakenly tried.
# Deliberately self-contained rather than importing
# `automation/agents/autonomous/observer.py`'s page-text patterns: this
# module guards a cached question string, not page text, and `app/services`
# does not depend on `automation/` (the dependency runs the other way).
_SECRET_PROMPT_RE = re.compile(
    r"one[-\s]?time\s*(password|code|pass)|verification\s*code|security\s*code|"
    r"authenticat(?:or|ion)\s*code|two[-\s]?factor|2fa\b|\botp\b|\bmfa\b|"
    r"captcha|i'?m\s*not\s*a\s*robot|"
    r"\bpassword\b|\bpasscode\b",
    re.IGNORECASE,
)


def _looks_like_secret_prompt(question: str) -> bool:
    return bool(_SECRET_PROMPT_RE.search(question or ""))

# Cosine similarity floor for treating two DIFFERENTLY-worded questions as
# "the same question" — high enough that only genuine paraphrases hit (not
# just two questions in the same topic area), since a wrong hit here means
# reusing a possibly-inapplicable answer without asking anyone.
SEMANTIC_SIMILARITY_THRESHOLD = 0.87


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
) -> AnswerCacheEntry | None:
    """Upsert, not insert-only — a later run answering the same question
    with fresher profile data (or a corrected LLM answer) should overwrite
    the stale cached one instead of piling up duplicate rows for the same
    (user, question).

    Also computes and stores this question's embedding, so a later,
    differently-worded question can find it via `find_similar_answer`. A
    failed embedding call degrades to exact-match-only for this row rather
    than blocking the save — losing the semantic hit is a minor cost saving
    missed, not a correctness problem.

    Returns `None`, writing nothing, when `question` reads as an OTP/MFA/
    CAPTCHA/password prompt (see `_looks_like_secret_prompt`) — see spec
    non-negotiable #9. Callers that don't already handle a secret specially
    (this cache is never in the OTP code path itself) still get this backstop
    for free."""
    if _looks_like_secret_prompt(question):
        logger.warning(
            "answer_cache_repository.save_answer: refused to cache a question that reads as a "
            "secret/verification prompt for user %s.", user_id,
        )
        return None

    question_hash = compute_question_hash(question)
    try:
        embedding = generate_embedding(question)
    except Exception:  # noqa: BLE001 - never let embedding generation block caching a real answer
        embedding = None

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
        existing.embedding_vector = embedding
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
        embedding_vector=embedding,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def find_similar_answer(db: Session, user_id: str, question: str) -> AnswerCacheEntry | None:
    """Semantic near-duplicate lookup — checked by
    `automation/forms/answer_engine.py::ApplicationAnswerEngine._cache_lookup`
    AFTER an exact-hash miss and BEFORE falling through to the LLM (spec's
    answer pipeline steps 2/3: "check answer memory" / "semantic matching").

    Returns the closest cached answer for this user above
    `SEMANTIC_SIMILARITY_THRESHOLD`, or `None` if nothing clears the bar (or
    the user has no cached answers with an embedding yet — e.g. rows saved
    before this feature existed). A failed embedding/query degrades to "no
    semantic hit" rather than raising, matching `get_cached_answer`'s own
    best-effort contract."""
    try:
        query_vector = generate_embedding(question)
    except Exception:  # noqa: BLE001
        return None

    distance = AnswerCacheEntry.embedding_vector.cosine_distance(query_vector)
    try:
        row = (
            db.query(AnswerCacheEntry, (1 - distance).label("similarity"))
            .filter(AnswerCacheEntry.user_id == user_id, AnswerCacheEntry.embedding_vector.isnot(None))
            .order_by(distance)
            .first()
        )
    except Exception:  # noqa: BLE001 - a broken vector query must never block answering
        return None
    if row is None:
        return None
    entry, similarity = row
    if similarity < SEMANTIC_SIMILARITY_THRESHOLD:
        return None
    return entry
