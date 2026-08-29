"""add answer_cache table (screening-question answer cache, Phase 6)

Revision ID: e5f6a7b8c9d0
Revises: c3d4e5f6a7b8
Create Date: 2026-07-29 00:00:00.000000

Adds: answer_cache (see ARCHITECTURE.md §2 Database Schema). One row per
(user, normalized question) — `automation/forms/answer_engine.py`'s
`ApplicationAnswerEngine` (Phase 6) reads/writes this via
`app/services/answer_cache_repository.py` so a repeated screening question
(deterministic or LLM-answered) never costs a second lookup/LLM call.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup regardless of
Alembic's history, so this table appears either way. This migration exists
so schema changes stay versioned/reproducible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'answer_cache',
        sa.Column('cache_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('question_hash', sa.String(), nullable=False),
        sa.Column('question_text', sa.Text(), nullable=False),
        sa.Column('answer', sa.Text(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('cache_id'),
        sa.UniqueConstraint('user_id', 'question_hash', name='uq_answer_cache_user_question'),
    )
    op.create_index(op.f('ix_answer_cache_user_id'), 'answer_cache', ['user_id'], unique=False)
    op.create_index(op.f('ix_answer_cache_question_hash'), 'answer_cache', ['question_hash'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_answer_cache_question_hash'), table_name='answer_cache')
    op.drop_index(op.f('ix_answer_cache_user_id'), table_name='answer_cache')
    op.drop_table('answer_cache')
