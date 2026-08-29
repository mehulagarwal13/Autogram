"""add applications + automation_runs tables (auto-apply tracking, Phase 4)

Revision ID: c3d4e5f6a7b8
Revises: 9b1c2f7a3e4d
Create Date: 2026-07-27 00:00:00.000000

Adds: applications, automation_runs (see ARCHITECTURE.md §2 Database Schema).
`applications` is the durable per-(user, job) apply record `app/api/applications.py`
creates and updates; `automation_runs` holds one row per actual
`ApplicationFlowManager.run()` attempt (screenshots/trace/error log), so a
retried application keeps its full history instead of overwriting it.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup regardless of
Alembic's history, so these tables appear either way. This migration exists
so schema changes stay versioned/reproducible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = '9b1c2f7a3e4d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'applications',
        sa.Column('application_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('job_url', sa.Text(), nullable=False),
        sa.Column('job_url_hash', sa.String(), nullable=False),
        sa.Column('company', sa.String(), nullable=True),
        sa.Column('position', sa.String(), nullable=True),
        sa.Column('ats_platform', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('autopilot_enabled', sa.Boolean(), nullable=False),
        sa.Column('applied_date', sa.DateTime(), nullable=True),
        sa.Column('resume_used', sa.String(), nullable=True),
        sa.Column('confidence_score', sa.Float(), nullable=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['resume_used'], ['profile_documents.document_id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('application_id'),
        sa.UniqueConstraint('user_id', 'job_url_hash', name='uq_applications_user_job_url'),
    )
    op.create_index(op.f('ix_applications_user_id'), 'applications', ['user_id'], unique=False)
    op.create_index(op.f('ix_applications_job_url_hash'), 'applications', ['job_url_hash'], unique=False)

    op.create_table(
        'automation_runs',
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('application_id', sa.String(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('screenshot_paths', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('trace_path', sa.String(), nullable=True),
        sa.Column('error_log', sa.Text(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['application_id'], ['applications.application_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('run_id'),
    )
    op.create_index(op.f('ix_automation_runs_application_id'), 'automation_runs', ['application_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_automation_runs_application_id'), table_name='automation_runs')
    op.drop_table('automation_runs')

    op.drop_index(op.f('ix_applications_job_url_hash'), table_name='applications')
    op.drop_index(op.f('ix_applications_user_id'), table_name='applications')
    op.drop_table('applications')
