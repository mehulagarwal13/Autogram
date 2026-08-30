"""add site trust levels (§6.4)

Revision ID: b6c7d8e9f0a1
Revises: b4c5d6e7f8a9
Create Date: 2026-08-30 00:00:00.000000

- `candidate_profiles.default_trust_level` — the trust level applied to a
  job posting's domain the first time this user's automation sees it.
- `site_trust_levels` — per-(user, domain) trust-level overrides.

Additive only: one new column with a default, one new table, nothing
altered or backfilled. A user/domain with no row simply uses the documented
default (`FULL_MANUAL_REVIEW`), identical to a brand-new install.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup regardless of
Alembic's history, so this appears either way. This migration exists so
schema changes stay versioned/reproducible for an existing database.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6c7d8e9f0a1'
down_revision: Union[str, Sequence[str], None] = 'b4c5d6e7f8a9'
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


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_site_trust_levels_user_id'), table_name='site_trust_levels')
    op.drop_table('site_trust_levels')
    op.drop_column('candidate_profiles', 'default_trust_level')
