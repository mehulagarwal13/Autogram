"""add retention_policies and retention_purge_log tables

Revision ID: f4a5b6c7d8e9
Revises: b4c5d6e7f8a9
Create Date: 2026-08-30 00:00:00.000000

§9 data retention — per-user retention windows (missing row = use the
global defaults, which are these same column defaults) plus an append-only
log of what the scheduled purge job actually did each run.

Additive only: two new tables, nothing altered or backfilled. A user who
never customizes retention is indistinguishable, at the DB level, from one
who explicitly confirmed the defaults.

Deliberately no `document_retention_days` column: there is no per-
application generated résumé or cover letter anywhere in this codebase —
`Application.resume_used` is a plain FK into the user's own PERMANENT
`ProfileDocument` library, reused across every application, never
regenerated per-posting — so there is nothing to purge on that schedule.
Auto-deleting a document the user may still want to reuse for a FUTURE
application would be actively harmful, not a retention cleanup. See
`app/services/retention_service.py`'s module docstring and
`automation/tests/test_retention_service.py::
test_retention_purge_never_touches_profile_documents` for the regression
guard that locks this in as an invariant.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup regardless of
Alembic's history, so this appears either way. This migration exists so
schema changes stay versioned/reproducible for an existing database.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a5b6c7d8e9'
down_revision: Union[str, Sequence[str], None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'retention_policies',
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('screenshot_retention_days', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('run_history_retention_days', sa.Integer(), nullable=False, server_default='90'),
        sa.Column('hitl_request_retention_days', sa.Integer(), nullable=False, server_default='14'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('user_id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
    )

    op.create_table(
        'retention_purge_log',
        sa.Column('purge_id', sa.String(), nullable=False),
        sa.Column('run_at', sa.DateTime(), nullable=True),
        sa.Column('category', sa.String(), nullable=False),
        sa.Column('records_purged', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('files_deleted', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('files_failed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('purge_id'),
    )
    op.create_index(op.f('ix_retention_purge_log_run_at'), 'retention_purge_log', ['run_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_retention_purge_log_run_at'), table_name='retention_purge_log')
    op.drop_table('retention_purge_log')
    op.drop_table('retention_policies')
