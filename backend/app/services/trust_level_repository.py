"""
§6.4 trust levels — resolves and persists per-(user, domain) automation
trust, consumed by `automation/applications/application_flow_manager.py::
decide_action`. See `db_models.SiteTrustLevel`/`VALID_TRUST_LEVELS` for the
data model and what each level means.

New users and newly-seen domains always resolve to `FULL_MANUAL_REVIEW`
(the safe default) unless the user has explicitly set something else —
nothing here ever upgrades a domain's trust on its own.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models.db_models import CandidateProfile, SiteTrustLevel, VALID_TRUST_LEVELS

DEFAULT_TRUST_LEVEL = "FULL_MANUAL_REVIEW"


def domain_for_url(job_url: str) -> str | None:
    """The hostname a trust level is keyed on — `None` for an unparseable
    URL (fails closed: `resolve_trust_level` treats that as "no override
    possible", falling back to the account default)."""
    try:
        host = urlparse(job_url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def resolve_trust_level(db: Session, user_id: str, job_url: str) -> str:
    """The trust level `decide_action` should use for this job posting:
    an explicit per-domain override if one exists, else this user's own
    `default_trust_level`, else the global safe default — in that order."""
    domain = domain_for_url(job_url)
    if domain:
        override = (
            db.query(SiteTrustLevel)
            .filter(SiteTrustLevel.user_id == user_id, SiteTrustLevel.domain == domain)
            .first()
        )
        if override is not None:
            return override.trust_level

    profile = db.query(CandidateProfile).filter(CandidateProfile.user_id == user_id).first()
    if profile is not None and profile.default_trust_level in VALID_TRUST_LEVELS:
        return profile.default_trust_level
    return DEFAULT_TRUST_LEVEL


def list_trust_levels(db: Session, user_id: str) -> list[SiteTrustLevel]:
    return (
        db.query(SiteTrustLevel)
        .filter(SiteTrustLevel.user_id == user_id)
        .order_by(SiteTrustLevel.domain.asc())
        .all()
    )


def set_trust_level(db: Session, user_id: str, domain: str, trust_level: str) -> SiteTrustLevel:
    if trust_level not in VALID_TRUST_LEVELS:
        raise ValueError(f"Unknown trust level: {trust_level!r}. Must be one of {sorted(VALID_TRUST_LEVELS)}.")
    domain = domain.strip().lower()
    row = (
        db.query(SiteTrustLevel)
        .filter(SiteTrustLevel.user_id == user_id, SiteTrustLevel.domain == domain)
        .first()
    )
    if row is None:
        row = SiteTrustLevel(trust_id=f"trust_{uuid.uuid4().hex[:12]}", user_id=user_id, domain=domain, trust_level=trust_level)
        db.add(row)
    else:
        row.trust_level = trust_level
    db.commit()
    db.refresh(row)
    return row


def delete_trust_level(db: Session, user_id: str, domain: str) -> bool:
    """Removes an override, reverting that domain to the account default.
    Returns `False` if there was nothing to remove."""
    domain = domain.strip().lower()
    deleted = (
        db.query(SiteTrustLevel)
        .filter(SiteTrustLevel.user_id == user_id, SiteTrustLevel.domain == domain)
        .delete(synchronize_session=False)
    )
    db.commit()
    return bool(deleted)


def set_default_trust_level(db: Session, profile: CandidateProfile, trust_level: str) -> CandidateProfile:
    if trust_level not in VALID_TRUST_LEVELS:
        raise ValueError(f"Unknown trust level: {trust_level!r}. Must be one of {sorted(VALID_TRUST_LEVELS)}.")
    profile.default_trust_level = trust_level
    db.commit()
    db.refresh(profile)
    return profile
