"""add middle name, postal code, time zone, summary, start date, clearance, referrer, five tri-state booleans

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-08-04 12:00:00.000000

Twelve more columns on `candidate_profiles`, one per question real ATS forms ask
that nothing in the profile could answer:

    middle_name                     legal middle name (Workday/Taleo)
    postal_code                     the ZIP/postal input of the address block
    time_zone                       "Which time zone are you based in?" (remote roles)
    professional_summary            "Tell us about yourself" / profile summary
    earliest_start_date             "What is your earliest start date?" (a date,
                                    where notice_period_days is a duration)
    security_clearance              "Do you hold an active security clearance?"
    referrer_name                   "If you were referred, by whom?" — a person,
                                    not the referral_source website
    age_over_18                     "Are you at least 18 years of age?"
    willing_to_travel               "Are you willing to travel for this role?"
    requires_relocation_assistance  "Do you require relocation assistance?" — a
                                    question about money, NOT willing_to_relocate
    willing_drug_test               "...willing to complete a drug screening?"
    has_drivers_license             "Do you hold a valid driver's license?"

The five booleans are all nullable and stay `NULL` for every existing row on
purpose: `NULL` means "the user was never asked", and back-filling any of them
with `FALSE` would turn "unknown" into an answer nobody gave.

`IF NOT EXISTS` throughout, same as the three revisions before this one.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9e0f1a2b3c4'
down_revision: Union[str, Sequence[str], None] = 'c8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("middle_name", "VARCHAR"),
    ("postal_code", "VARCHAR"),
    ("time_zone", "VARCHAR"),
    ("professional_summary", "TEXT"),
    ("earliest_start_date", "VARCHAR"),
    ("security_clearance", "VARCHAR"),
    ("referrer_name", "VARCHAR"),
    ("age_over_18", "BOOLEAN"),
    ("willing_to_travel", "BOOLEAN"),
    ("requires_relocation_assistance", "BOOLEAN"),
    ("willing_drug_test", "BOOLEAN"),
    ("has_drivers_license", "BOOLEAN"),
)


def upgrade() -> None:
    """Upgrade schema."""
    for name, sql_type in _COLUMNS:
        op.execute(f"ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS {name} {sql_type}")


def downgrade() -> None:
    """Downgrade schema."""
    for name, _sql_type in reversed(_COLUMNS):
        op.drop_column('candidate_profiles', name)
