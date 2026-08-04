"""add preferred name, current salary, referral source, employment type, languages, background check

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-08-03 22:10:00.000000

Seven more columns on `candidate_profiles`, one per question real ATS forms ask
that nothing in the profile could answer:

    preferred_name             "Preferred name" / "What should we call you?"
    current_salary             "What is your current CTC / salary?"
    current_salary_currency     ^ its unit
    referral_source            "How did you hear about this job?"
    employment_type_preference "What type of employment are you seeking?"
    languages                  "Are you fluent in English?"  (list of
                               {language, proficiency} — the degree is the answer)
    willing_background_check   "Are you willing to complete a background check?"

`current_salary` also fixes a wrong-ANSWER bug rather than a blank one:
`question_classifier` routed "current CTC" into the expected-salary category, so
the candidate's expected number was typed into a field asking what they earn
today.

`IF NOT EXISTS` throughout, same as the two revisions before this one.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8d9e0f1a2b3'
down_revision: Union[str, Sequence[str], None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("preferred_name", "VARCHAR"),
    ("current_salary", "DOUBLE PRECISION"),
    ("current_salary_currency", "VARCHAR"),
    ("referral_source", "VARCHAR"),
    ("employment_type_preference", "VARCHAR"),
    ("languages", "JSONB"),
    ("willing_background_check", "BOOLEAN"),
)


def upgrade() -> None:
    """Upgrade schema."""
    for name, sql_type in _COLUMNS:
        op.execute(f"ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS {name} {sql_type}")


def downgrade() -> None:
    """Downgrade schema."""
    for name, _sql_type in reversed(_COLUMNS):
        op.drop_column('candidate_profiles', name)
