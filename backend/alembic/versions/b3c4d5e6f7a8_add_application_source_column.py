"""add applications.source column (browser-extension delivery)

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-08-16 00:00:00.000000

Adds `applications.source` (`"server_automation"` default / `"browser_extension"`)
— which "engine" is driving an application: the existing server-side Playwright
automation, or the new MV3 browser extension (`extension/`), which runs inside
the user's own already-logged-in Chrome tab instead. See
`app/models/db_models.py::Application.source` / `VALID_APPLICATION_SOURCES`.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup regardless of
Alembic's history, so this column appears either way. This migration exists
so schema changes stay versioned/reproducible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, Sequence[str], None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'applications',
        sa.Column('source', sa.String(), nullable=False, server_default='server_automation'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('applications', 'source')
