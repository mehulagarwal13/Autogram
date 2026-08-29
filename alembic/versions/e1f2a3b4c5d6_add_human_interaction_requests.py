"""add human_interaction_requests table + autonomous_task audit support

Revision ID: e1f2a3b4c5d6
Revises: d1e2f3a4b5c6
Create Date: 2026-08-25 00:00:00.000000

Adds the durable, individually-addressable Human-in-the-Loop request record
(`human_interaction_requests`) the autonomous agent
(`automation/agents/autonomous/loop.py`) creates whenever it pauses for a
human — OTP, MFA, CAPTCHA, login, an ambiguous question, or any other
blocker (see `AUTONOMOUS_AGENT.md`'s "Human-in-the-loop" section). Addressed
by `app/api/human_interaction.py`. Deliberately has NO column that could
hold a secret (OTP/MFA code) — see that table's model docstring.

Also relaxes `application_audit_log.application_id` to nullable and adds
`autonomous_task_id`, so the existing compliance audit trail
(`app/services/audit_log_repository.py`) can be shared between the
deterministic per-ATS path (`application_id`) and the autonomous agent
(`autonomous_task_id`) rather than standing up a second, copy-pasted audit
table for the latter.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup regardless of
Alembic's history, so this table/columns appear either way. This migration
exists so schema changes stay versioned/reproducible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, Sequence[str], None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'human_interaction_requests',
        sa.Column('request_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('task_id', sa.String(), nullable=False),
        sa.Column('request_type', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='PENDING'),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('safe_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('responded_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['task_id'], ['autonomous_tasks.task_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('request_id'),
    )
    op.create_index(
        op.f('ix_human_interaction_requests_user_id'), 'human_interaction_requests', ['user_id'], unique=False,
    )
    op.create_index(
        op.f('ix_human_interaction_requests_task_id'), 'human_interaction_requests', ['task_id'], unique=False,
    )

    op.alter_column('application_audit_log', 'application_id', existing_type=sa.String(), nullable=True)
    op.add_column('application_audit_log', sa.Column('autonomous_task_id', sa.String(), nullable=True))
    op.create_foreign_key(
        'application_audit_log_autonomous_task_id_fkey', 'application_audit_log', 'autonomous_tasks',
        ['autonomous_task_id'], ['task_id'], ondelete='CASCADE',
    )
    op.create_index(
        op.f('ix_application_audit_log_autonomous_task_id'), 'application_audit_log', ['autonomous_task_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_application_audit_log_autonomous_task_id'), table_name='application_audit_log')
    op.drop_constraint('application_audit_log_autonomous_task_id_fkey', 'application_audit_log', type_='foreignkey')
    op.drop_column('application_audit_log', 'autonomous_task_id')
    op.alter_column('application_audit_log', 'application_id', existing_type=sa.String(), nullable=False)

    op.drop_index(op.f('ix_human_interaction_requests_task_id'), table_name='human_interaction_requests')
    op.drop_index(op.f('ix_human_interaction_requests_user_id'), table_name='human_interaction_requests')
    op.drop_table('human_interaction_requests')
