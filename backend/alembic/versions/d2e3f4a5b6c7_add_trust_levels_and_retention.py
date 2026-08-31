"""add site trust levels and data-retention policy/log tables

Revision ID: d2e3f4a5b6c7
Revises: c5d6e7f8a9b0
Create Date: 2026-08-30 00:00:00.000000

Two independent features landing in one migration since they were built in
the same pass (§6.4 trust levels, §9 data retention):

- `candidate_profiles.default_trust_level` — the trust level applied to a
  job posting's domain the first time this user's automation sees it.
- `site_trust_levels` — per-(user, domain) trust-level overrides.
- `retention_policies` — per-user retention windows (missing row = use the
  global defaults, which are these same column defaults).
- `retention_purge_log` — append-only record of what the purge job did.

Additive only: one new column with a default, three new tables, nothing
altered or backfilled. A user/domain with no row simply uses the documented
defaults, identical to a brand-new install.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup regardless of
Alembic's history, so this appears either way. This migration exists so
schema changes stay versioned/reproducible for an existing database.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, Sequence[str], None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'candidate_profiles',
        sa.Column('default_trust_level', sa.String(), nullable=False, server_default='FULL_MANUAL_REVIEW'),
    )

    op.create_table(
        'site_trust_levels',
        sa.Column('trust_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('domain', sa.String(), nullable=False),
        sa.Column('trust_level', sa.String(), nullable=False, server_default='FULL_MANUAL_REVIEW'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('trust_id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.UniqueConstraint('user_id', 'domain', name='uq_site_trust_levels_user_domain'),
    )
    op.create_index(op.f('ix_site_trust_levels_user_id'), 'site_trust_levels', ['user_id'])

    op.create_table(
        'retention_policies',
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('screenshot_retention_days', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('run_history_retention_days', sa.Integer(), nullable=False, server_default='90'),
        sa.Column('hitl_request_retention_days', sa.Integer(), nullable=False, server_default='14'),
        sa.Column('document_retention_days', sa.Integer(), nullable=True),
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
    op.drop_index(op.f('ix_site_trust_levels_user_id'), table_name='site_trust_levels')
    op.drop_table('site_trust_levels')
    op.drop_column('candidate_profiles', 'default_trust_level')
