"""add HITL platform tables (application_questions, application_audit_log) + columns

Revision ID: a2b3c4d5e6f7
Revises: d9e0f1a2b3c4
Create Date: 2026-08-15 00:00:00.000000

Adds the schema for the human-in-the-loop application platform:

- `application_questions` — per-application screening-question ledger (source,
  confidence, HIGH/MEDIUM/LOW bucket, human review status). Read by the Answer
  Review UI, the Application Detail page, and the pre-submission review
  summary. Written by `automation/forms/answer_engine.py::ApplicationAnswerEngine`.
- `application_audit_log` — append-only decision/approval trail (autopilot run
  started, human approved/rejected, kill switch triggered), separate from
  `automation_runs` (execution mechanics) and `application_questions`
  (individual answers). No update/delete route is ever added for this table.
- `candidate_profiles.autopilot_globally_disabled` — account-level kill switch.
- `applications.pages_completed` — from the last run's `ApplicationRunResult`.
- `automation_runs.log_lines` — this run's structured progress log (§18).

Left alone deliberately, same convention as `a1b2c3d4e5f6`: the new
`answer_cache.embedding_vector` column and its HNSW index. Those are owned
solely by `app/core/pgvector_setup.py::ensure_vector_schema()`, not Alembic.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup regardless of
Alembic's history, so these tables/columns appear either way. This migration
exists so schema changes stay versioned/reproducible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, Sequence[str], None] = 'd9e0f1a2b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'candidate_profiles',
        sa.Column('autopilot_globally_disabled', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column('applications', sa.Column('pages_completed', sa.Integer(), nullable=True))
    op.add_column('automation_runs', sa.Column('log_lines', postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.create_table(
        'application_questions',
        sa.Column('question_id', sa.String(), nullable=False),
        sa.Column('application_id', sa.String(), nullable=False),
        sa.Column('page_number', sa.Integer(), nullable=True),
        sa.Column('question_text', sa.Text(), nullable=False),
        sa.Column('field_type', sa.String(), nullable=True),
        sa.Column('available_options', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('answer', sa.Text(), nullable=True),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('confidence_level', sa.String(), nullable=False),
        sa.Column('review_status', sa.String(), nullable=False),
        sa.Column('human_answer', sa.Text(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['application_id'], ['applications.application_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('question_id'),
    )
    op.create_index(
        op.f('ix_application_questions_application_id'), 'application_questions', ['application_id'], unique=False,
    )

    op.create_table(
        'application_audit_log',
        sa.Column('log_id', sa.String(), nullable=False),
        sa.Column('application_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('event_type', sa.String(), nullable=False),
        sa.Column('actor', sa.String(), nullable=False),
        sa.Column('event_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['application_id'], ['applications.application_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('log_id'),
    )
    op.create_index(
        op.f('ix_application_audit_log_application_id'), 'application_audit_log', ['application_id'], unique=False,
    )
    op.create_index(
        op.f('ix_application_audit_log_user_id'), 'application_audit_log', ['user_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_application_audit_log_user_id'), table_name='application_audit_log')
    op.drop_index(op.f('ix_application_audit_log_application_id'), table_name='application_audit_log')
    op.drop_table('application_audit_log')

    op.drop_index(op.f('ix_application_questions_application_id'), table_name='application_questions')
    op.drop_table('application_questions')

    op.drop_column('automation_runs', 'log_lines')
    op.drop_column('applications', 'pages_completed')
    op.drop_column('candidate_profiles', 'autopilot_globally_disabled')
