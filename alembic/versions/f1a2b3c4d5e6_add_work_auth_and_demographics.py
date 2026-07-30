"""add work-authorization/sponsorship columns and candidate_demographics table (Phase 8)

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-07-30 00:00:00.000000

Adds four compliance-screening columns to `candidate_profiles`
(work_authorized, requires_sponsorship, visa_type, sponsorship_countries) and
a new `candidate_demographics` table (gender, veteran_status,
disability_status, race_ethnicity) — see `app/models/db_models.py` for the
full rationale (never inferred/guessed; separate table on purpose).

Note (see README.md / migration 9b1c2f7a3e4d): on a fresh Neon database
`app/main.py` bootstraps the current schema via `Base.metadata.create_all` at
startup regardless of Alembic's history, so these columns/table appear either
way — this migration exists so the schema change stays versioned/reproducible
for every other environment.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('candidate_profiles', sa.Column('work_authorized', sa.Boolean(), nullable=True))
    op.add_column('candidate_profiles', sa.Column('requires_sponsorship', sa.Boolean(), nullable=True))
    op.add_column('candidate_profiles', sa.Column('visa_type', sa.String(), nullable=True))
    op.add_column('candidate_profiles', sa.Column('sponsorship_countries', postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.create_table(
        'candidate_demographics',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('candidate_id', sa.String(), nullable=False),
        sa.Column('gender', sa.String(), nullable=True),
        sa.Column('veteran_status', sa.String(), nullable=True),
        sa.Column('disability_status', sa.String(), nullable=True),
        sa.Column('race_ethnicity', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['candidate_id'], ['candidate_profiles.profile_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_candidate_demographics_candidate_id'), 'candidate_demographics', ['candidate_id'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_candidate_demographics_candidate_id'), table_name='candidate_demographics')
    op.drop_table('candidate_demographics')

    op.drop_column('candidate_profiles', 'sponsorship_countries')
    op.drop_column('candidate_profiles', 'visa_type')
    op.drop_column('candidate_profiles', 'requires_sponsorship')
    op.drop_column('candidate_profiles', 'work_authorized')
