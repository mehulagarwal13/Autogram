"""drop retention_policies.document_retention_days (confirmed permanently inert)

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-08-30 00:00:00.000000

`document_retention_days` was added in `d2e3f4a5b6c7` for a category of data
that turned out not to exist: there is no per-application generated résumé
or cover letter anywhere in this codebase. `Application.resume_used` is a
plain FK into the user's own PERMANENT `ProfileDocument` library — the same
file is reused across every application, never regenerated per-posting —
and resume tailoring/cover-letter generation was removed at the owner's
request (`app/services/tailoring_service.py` is a one-line stub). Purging a
`ProfileDocument` on this column's schedule would delete a file the user
may still want to reuse for a FUTURE application, which is the opposite of a
retention cleanup, so nothing ever enforced it — it was visible via
`GET /profile/retention-policy` but silently rejected by the PUT body
(`RetentionPolicyRequest` never accepted it), which is misleading to any API
consumer. Removing it now rather than carrying a permanently-fake setting;
see `app/services/retention_service.py`'s module docstring for the full
reasoning and `automation/tests/test_retention_service.py`'s
`test_retention_purge_never_touches_profile_documents` for the regression
guard that replaces the old (now-impossible-to-write) test against this
column.

Reversible: the down-migration re-adds the exact column `d2e3f4a5b6c7`
defined (nullable Integer, no default) — genuinely trivial to restore if
per-application document generation ever comes back, since nothing ever
depended on this column having a value.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3f4a5b6c7d8'
down_revision: Union[str, Sequence[str], None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('retention_policies', 'document_retention_days')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('retention_policies', sa.Column('document_retention_days', sa.Integer(), nullable=True))
