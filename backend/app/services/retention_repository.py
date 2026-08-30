"""
§9 data retention — CRUD for `RetentionPolicy`. A missing row means "use the
global defaults" (the same values `RetentionPolicy`'s columns default to),
so a user who never touches this setting is indistinguishable, at the DB
level, from one who explicitly confirmed the defaults. See
`app/services/retention_service.py` for what actually consumes this.

No `document_retention_days` here — see `RetentionPolicy`'s own docstring
(`app/models/db_models.py`) for why that column was removed rather than
carried as a permanently-unenforceable setting.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.db_models import RetentionPolicy

DEFAULT_SCREENSHOT_RETENTION_DAYS = 30
DEFAULT_RUN_HISTORY_RETENTION_DAYS = 90
DEFAULT_HITL_REQUEST_RETENTION_DAYS = 14


class EffectivePolicy:
    """A resolved policy — either the user's own row, or the global
    defaults for a user who's never customized anything. Duck-types the
    three fields `retention_service.py` reads, so callers never need to
    branch on whether a row actually exists."""

    def __init__(
        self, *, screenshot_retention_days: int, run_history_retention_days: int,
        hitl_request_retention_days: int,
    ) -> None:
        self.screenshot_retention_days = screenshot_retention_days
        self.run_history_retention_days = run_history_retention_days
        self.hitl_request_retention_days = hitl_request_retention_days


_GLOBAL_DEFAULT = EffectivePolicy(
    screenshot_retention_days=DEFAULT_SCREENSHOT_RETENTION_DAYS,
    run_history_retention_days=DEFAULT_RUN_HISTORY_RETENTION_DAYS,
    hitl_request_retention_days=DEFAULT_HITL_REQUEST_RETENTION_DAYS,
)


def get_default_policy() -> EffectivePolicy:
    """The global defaults, with no DB round-trip — for a caller (the
    scheduled all-users purge) that already knows a given user has no row
    and just needs the fallback."""
    return _GLOBAL_DEFAULT


def get_policy(db: Session, user_id: str) -> EffectivePolicy:
    row = db.query(RetentionPolicy).filter(RetentionPolicy.user_id == user_id).first()
    if row is None:
        return _GLOBAL_DEFAULT
    return EffectivePolicy(
        screenshot_retention_days=row.screenshot_retention_days,
        run_history_retention_days=row.run_history_retention_days,
        hitl_request_retention_days=row.hitl_request_retention_days,
    )


def get_all_policies(db: Session) -> dict[str, EffectivePolicy]:
    """Every user who has a real policy row, keyed by `user_id` — used by the
    scheduled job, which needs one pass across every user rather than one
    query per user. A user with no row is simply absent from this dict;
    callers combine it with `get_policy`'s default for anyone missing."""
    rows = db.query(RetentionPolicy).all()
    return {
        row.user_id: EffectivePolicy(
            screenshot_retention_days=row.screenshot_retention_days,
            run_history_retention_days=row.run_history_retention_days,
            hitl_request_retention_days=row.hitl_request_retention_days,
        )
        for row in rows
    }


def update_policy(
    db: Session, user_id: str, *,
    screenshot_retention_days: int | None = None,
    run_history_retention_days: int | None = None,
    hitl_request_retention_days: int | None = None,
) -> RetentionPolicy:
    """Upserts this user's policy row. Only the fields explicitly passed are
    changed — a field left `None` leaves that column untouched on an
    existing row, or falls back to the global default on a brand-new one."""
    row = db.query(RetentionPolicy).filter(RetentionPolicy.user_id == user_id).first()
    if row is None:
        row = RetentionPolicy(user_id=user_id)
        db.add(row)
        db.flush()

    if screenshot_retention_days is not None:
        row.screenshot_retention_days = screenshot_retention_days
    if run_history_retention_days is not None:
        row.run_history_retention_days = run_history_retention_days
    if hitl_request_retention_days is not None:
        row.hitl_request_retention_days = hitl_request_retention_days

    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row
