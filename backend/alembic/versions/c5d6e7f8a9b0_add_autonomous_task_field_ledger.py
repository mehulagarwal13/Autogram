"""add autonomous_tasks.field_attempt_ledger column

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-08-30 00:00:00.000000

Adds `autonomous_tasks.field_attempt_ledger` — the persisted, cross-resume/
process-restart counterpart of `TaskHandle.unverified_streak`
(`automation/agents/autonomous/loop.py`, in-process only). Keyed by
`observer.field_identity` (stable across re-observations of the same logical
field, unlike `PageElement.ref`, which is DOM-order-based and can shift):
`{field_identity: {"status": "attempted"|"verified"|"failed", "attempts":
int, "last_action_type": str}}`. Spec §16: once a field's status reaches
"failed", the loop refuses to dispatch another action at it for the rest of
this task, even across a resume.

Additive only: a new column with a default, no existing column altered, no
backfill needed — a task created before this migration simply starts with an
empty ledger, identical to a brand-new task's initial state.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup regardless of
Alembic's history, so this column appears either way. This migration exists
so schema changes stay versioned/reproducible for an existing database.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, Sequence[str], None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'autonomous_tasks',
        sa.Column(
            'field_attempt_ledger',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('autonomous_tasks', 'field_attempt_ledger')
