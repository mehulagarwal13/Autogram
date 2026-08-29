"""add profile + demographic fields real ATS forms ask for

Revision ID: b7c8d9e0f1a2
Revises: 4dda3fb4a3ad
Create Date: 2026-08-03 21:20:00.000000

Five columns for five questions a live Lever posting left blank because the
profile had nowhere to store the answer:

    candidate_profiles.highest_education_level  "What is your highest level of education?"
    candidate_profiles.willing_to_relocate      "Are you willing to relocate?"
    candidate_profiles.marketing_opt_in         "Yes, <company> can contact me about future roles"
    candidate_demographics.pronouns             "Pronouns" (checkbox group)
    candidate_demographics.ethnicities          "I identify my ethnicity as" (select all that apply)

`IF NOT EXISTS` throughout, same as 4dda3fb4a3ad, so this is safe to run
against a database where `create_all` at app startup already produced some of
them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = '4dda3fb4a3ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS highest_education_level VARCHAR")
    op.execute("ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS willing_to_relocate BOOLEAN")
    op.execute("ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS marketing_opt_in BOOLEAN")
    op.execute("ALTER TABLE candidate_demographics ADD COLUMN IF NOT EXISTS pronouns VARCHAR")
    op.execute("ALTER TABLE candidate_demographics ADD COLUMN IF NOT EXISTS ethnicities JSONB")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('candidate_demographics', 'ethnicities')
    op.drop_column('candidate_demographics', 'pronouns')
    op.drop_column('candidate_profiles', 'marketing_opt_in')
    op.drop_column('candidate_profiles', 'willing_to_relocate')
    op.drop_column('candidate_profiles', 'highest_education_level')
