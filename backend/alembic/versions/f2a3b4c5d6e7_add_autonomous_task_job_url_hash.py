"""add autonomous_tasks.job_url_hash + partial unique index on active jobs

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-26 00:00:00.000000

Prevents concurrent duplicate automation of the same job.

`POST /agent/tasks` had no duplicate check of any kind, so N calls with the
same URL produced N ACTIVE `AutonomousTask` rows — and under the default
`AUTOMATION_BROWSER_MODE=cdp` that means N browser tabs independently filling
the same application form. (Reproduced against the real database before this
change: three concurrent `RUNNING` tasks on one URL.) The deterministic path
has always protected itself with `uq_applications_user_job_url`; this gives
the autonomous path the equivalent, using the SAME hash function
(`application_repository.compute_job_url_hash`) so the two independent systems
identify a job identically and can therefore recognise each other's work.

Two objects:

* `job_url_hash` — sha256 of the normalized (strip + lowercase) URL.
  Backfilled for existing rows with the same expression Python uses, so the
  index below can be created without a data migration step.
* `uq_autonomous_tasks_active_job` — UNIQUE on (user_id, job_url_hash) but
  **PARTIAL**: only over rows whose `current_status` is not terminal. That is
  what makes "one active automation per job" enforceable at the database level
  while still allowing a retry after COMPLETED/FAILED/CANCELLED. A plain
  unique constraint would have permanently barred a job after its first
  attempt.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup, so both objects
appear either way (the index is declared in `AutonomousTask.__table_args__`).
This migration exists so the change stays versioned/reproducible, and so an
EXISTING database — where `create_all` never alters a table it already sees —
gets the new column and index.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Kept in sync with `AUTONOMOUS_TASK_TERMINAL_STATUSES` in
#: `app/models/db_models.py` and with the `postgresql_where` on
#: `AutonomousTask.__table_args__`'s Index — all three must agree or the
#: constraint and the application's notion of "active" diverge.
_ACTIVE_PREDICATE = "current_status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('autonomous_tasks', sa.Column('job_url_hash', sa.String(), nullable=True))

    # Backfill in PYTHON, using the application's own `compute_job_url_hash`,
    # rather than SQL. `encode(digest(...), 'hex')` would have needed the
    # pgcrypto extension, which this project does not otherwise require — and
    # calling the real function guarantees the backfilled values match what the
    # app will compute, instead of a second implementation that could drift.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT task_id, job_url FROM autonomous_tasks WHERE job_url_hash IS NULL")
    ).fetchall()
    if rows:
        from app.services.application_repository import compute_job_url_hash

        for task_id, job_url in rows:
            connection.execute(
                sa.text("UPDATE autonomous_tasks SET job_url_hash = :h WHERE task_id = :t"),
                {"h": compute_job_url_hash(job_url or ""), "t": task_id},
            )

    # Any pre-existing rows that would violate the new partial unique index
    # are exactly the duplicates this change exists to prevent. They cannot be
    # merged (each may have its own action history), and failing the migration
    # would block deploys — so retire the older ones as CANCELLED, which drops
    # them out of the partial index and leaves the most recent attempt active.
    op.execute(
        f"""
        UPDATE autonomous_tasks SET current_status = 'CANCELLED'
        WHERE task_id IN (
            SELECT task_id FROM (
                SELECT task_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id, job_url_hash ORDER BY created_at DESC
                       ) AS rn
                FROM autonomous_tasks
                WHERE {_ACTIVE_PREDICATE}
            ) ranked
            WHERE ranked.rn > 1
        )
        """
    )

    op.alter_column('autonomous_tasks', 'job_url_hash', nullable=False)
    op.create_index(
        op.f('ix_autonomous_tasks_job_url_hash'), 'autonomous_tasks', ['job_url_hash'], unique=False,
    )
    op.create_index(
        'uq_autonomous_tasks_active_job', 'autonomous_tasks', ['user_id', 'job_url_hash'],
        unique=True, postgresql_where=sa.text(_ACTIVE_PREDICATE),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_autonomous_tasks_active_job', table_name='autonomous_tasks')
    op.drop_index(op.f('ix_autonomous_tasks_job_url_hash'), table_name='autonomous_tasks')
    op.drop_column('autonomous_tasks', 'job_url_hash')
