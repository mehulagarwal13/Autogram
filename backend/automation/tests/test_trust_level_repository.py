"""
`app/services/trust_level_repository.py` — §6.4 trust levels. Needs real
Postgres (real unique-constraint/upsert behavior, not something worth
faking); skips cleanly without one, same convention
`test_duplicate_automation_guard.py` uses.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.core.database import SessionLocal, engine
from app.models.db_models import CandidateProfile, SiteTrustLevel, User
from app.services import trust_level_repository as repo


def _db_available() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


db_required = pytest.mark.skipif(not _db_available(), reason="No reachable Postgres.")


@pytest.fixture
def user():
    db = SessionLocal()
    uid = f"trust_{uuid.uuid4().hex[:10]}"
    db.add(User(user_id=uid, email=f"{uid}@example.com", password_hash="x"))
    db.commit()
    db.add(CandidateProfile(profile_id=f"profile_{uid}", user_id=uid, first_name="A", last_name="B", email=f"{uid}@example.com"))
    db.commit()
    yield db, uid
    db.rollback()
    db.query(SiteTrustLevel).filter(SiteTrustLevel.user_id == uid).delete()
    db.query(CandidateProfile).filter(CandidateProfile.user_id == uid).delete()
    db.query(User).filter(User.user_id == uid).delete()
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# domain_for_url
# ---------------------------------------------------------------------------

def test_domain_for_url_extracts_the_hostname():
    assert repo.domain_for_url("https://boards.greenhouse.io/acme/jobs/123?ref=x") == "boards.greenhouse.io"


def test_domain_for_url_lowercases():
    assert repo.domain_for_url("https://Boards.Greenhouse.IO/acme/jobs/123") == "boards.greenhouse.io"


def test_domain_for_url_returns_none_for_garbage():
    assert repo.domain_for_url("not a url at all") is None


# ---------------------------------------------------------------------------
# resolve_trust_level
# ---------------------------------------------------------------------------

@db_required
def test_resolve_trust_level_defaults_to_full_manual_review_for_a_brand_new_user(user):
    db, uid = user
    assert repo.resolve_trust_level(db, uid, "https://boards.greenhouse.io/acme/jobs/1") == "FULL_MANUAL_REVIEW"


@db_required
def test_resolve_trust_level_uses_the_account_default_when_no_override_exists(user):
    db, uid = user
    profile = db.query(CandidateProfile).filter(CandidateProfile.user_id == uid).first()
    repo.set_default_trust_level(db, profile, "DRAFT_ONLY")

    assert repo.resolve_trust_level(db, uid, "https://boards.greenhouse.io/acme/jobs/1") == "DRAFT_ONLY"


@db_required
def test_a_per_domain_override_wins_over_the_account_default(user):
    db, uid = user
    profile = db.query(CandidateProfile).filter(CandidateProfile.user_id == uid).first()
    repo.set_default_trust_level(db, profile, "DRAFT_ONLY")
    repo.set_trust_level(db, uid, "boards.greenhouse.io", "TRUSTED_AUTO_SUBMIT")

    assert repo.resolve_trust_level(db, uid, "https://boards.greenhouse.io/acme/jobs/1") == "TRUSTED_AUTO_SUBMIT"
    # A different domain is unaffected by the override.
    assert repo.resolve_trust_level(db, uid, "https://jobs.lever.co/acme/1") == "DRAFT_ONLY"


@db_required
def test_set_trust_level_upserts_rather_than_duplicating(user):
    db, uid = user
    repo.set_trust_level(db, uid, "boards.greenhouse.io", "TRUSTED_AUTO_SUBMIT")
    repo.set_trust_level(db, uid, "boards.greenhouse.io", "DRAFT_ONLY")

    rows = repo.list_trust_levels(db, uid)
    assert len(rows) == 1
    assert rows[0].trust_level == "DRAFT_ONLY"


@db_required
def test_set_trust_level_rejects_an_unrecognized_value(user):
    db, uid = user
    with pytest.raises(ValueError):
        repo.set_trust_level(db, uid, "boards.greenhouse.io", "not-a-real-level")


@db_required
def test_delete_trust_level_reverts_to_the_account_default(user):
    db, uid = user
    repo.set_trust_level(db, uid, "boards.greenhouse.io", "TRUSTED_AUTO_SUBMIT")
    assert repo.resolve_trust_level(db, uid, "https://boards.greenhouse.io/acme/1") == "TRUSTED_AUTO_SUBMIT"

    removed = repo.delete_trust_level(db, uid, "boards.greenhouse.io")

    assert removed is True
    assert repo.resolve_trust_level(db, uid, "https://boards.greenhouse.io/acme/1") == "FULL_MANUAL_REVIEW"


@db_required
def test_delete_trust_level_is_a_harmless_no_op_when_nothing_exists(user):
    db, uid = user
    assert repo.delete_trust_level(db, uid, "boards.greenhouse.io") is False


@db_required
def test_trust_levels_are_scoped_per_user(user):
    db, uid = user
    other_uid = f"trust_other_{uuid.uuid4().hex[:8]}"
    db.add(User(user_id=other_uid, email=f"{other_uid}@example.com", password_hash="x"))
    db.commit()
    try:
        repo.set_trust_level(db, uid, "boards.greenhouse.io", "TRUSTED_AUTO_SUBMIT")
        assert repo.resolve_trust_level(db, other_uid, "https://boards.greenhouse.io/acme/1") == "FULL_MANUAL_REVIEW"
    finally:
        db.query(User).filter(User.user_id == other_uid).delete()
        db.commit()
