"""add applications.detected_ats_platform column

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-08-19 00:00:00.000000

Adds `applications.detected_ats_platform` — the pre-flight `ATSDetector`
guess (e.g. "smartrecruiters"), kept separate from the existing
`ats_platform` column, which now always names the adapter that actually ran
(e.g. "custom" for GenericAdapter). Without this split, a run resolved by
GenericAdapter for a confidently-detected-but-unregistered platform (see
`automation/ats/registry.py`) had no way to be distinguished, in stored data,
from a run a dedicated adapter actually performed. See
`automation.interfaces.ApplicationRunResult.detected_ats_platform` and
`ApplicationFlowManager._fall_back_to_generic_adapter`.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup regardless of
Alembic's history, so this column appears either way. This migration exists
so schema changes stay versioned/reproducible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('applications', sa.Column('detected_ats_platform', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('applications', 'detected_ats_platform')
