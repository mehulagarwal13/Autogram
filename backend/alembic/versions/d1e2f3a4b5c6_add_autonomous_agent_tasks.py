"""add autonomous_tasks table (general-purpose LLM agent platform)

Revision ID: d1e2f3a4b5c6
Revises: c4d5e6f7a8b9
Create Date: 2026-08-23 00:00:00.000000

Adds `autonomous_tasks` — persistence for the new general-purpose,
non-per-ATS autonomous browser agent (`automation/agents/autonomous/`, see
`AUTONOMOUS_AGENT.md`). Completely independent of `applications` /
`automation_runs` / `application_questions`, which continue to back the
existing deterministic `ApplicationFlowManager` path — the two systems
coexist without a foreign key between them.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup regardless of
Alembic's history, so this table appears either way. This migration exists
so schema changes stay versioned/reproducible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'autonomous_tasks',
        sa.Column('task_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('job_url', sa.String(), nullable=False),
        sa.Column('original_objective', sa.Text(), nullable=False),
        sa.Column('candidate_profile', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('job_information', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('current_status', sa.String(), nullable=False, server_default='CREATED'),
        sa.Column('current_browser_state', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('action_history', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('application_progress', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('human_intervention', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('confirmed_answers', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('uploaded_documents', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('final_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('auto_submit_approved', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('task_id'),
    )
    op.create_index(
        op.f('ix_autonomous_tasks_user_id'), 'autonomous_tasks', ['user_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_autonomous_tasks_user_id'), table_name='autonomous_tasks')
    op.drop_table('autonomous_tasks')
