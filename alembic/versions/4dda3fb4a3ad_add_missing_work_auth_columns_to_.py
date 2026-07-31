"""add missing work-auth columns to candidate_profiles

Revision ID: 4dda3fb4a3ad
Revises: f1a2b3c4d5e6
Create Date: 2026-07-30 21:55:33.172076

f1a2b3c4d5e6 was already stamped as applied on some databases (it created
`candidate_demographics` successfully) before its `add_column` calls for
these four columns were added to that file, so those calls never ran there.
Using `IF NOT EXISTS` here makes this safe to run regardless of which
partial state a given database is in.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4dda3fb4a3ad'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS work_authorized BOOLEAN")
    op.execute("ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS requires_sponsorship BOOLEAN")
    op.execute("ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS visa_type VARCHAR")
    op.execute("ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS sponsorship_countries JSONB")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('candidate_profiles', 'sponsorship_countries')
    op.drop_column('candidate_profiles', 'visa_type')
    op.drop_column('candidate_profiles', 'requires_sponsorship')
    op.drop_column('candidate_profiles', 'work_authorized')
