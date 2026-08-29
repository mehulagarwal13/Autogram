"""add chat_messages (HITL conversation transcript)

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-08-26 12:00:00.000000

Backs `app/services/chat_repository.py` — the user-visible conversation for one
automation attempt.

Additive only: a brand-new table, no existing table altered, no data migrated,
nothing backfilled. Applications and tasks that predate it simply have an empty
transcript, which renders as an empty chat panel rather than an error.

Shared between both automation paths by the same convention
`application_audit_log` already uses: `application_id` XOR `autonomous_task_id`,
enforced in the repository rather than by a DB constraint, so the autonomous
agent needs no second copy-pasted transcript table.

Note (see README.md): on a fresh database `app/main.py` bootstraps the schema
via `Base.metadata.create_all`, which creates this table from the model. This
migration is what an EXISTING database needs, since `create_all` will create a
missing table but never reconcile one that has drifted.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, Sequence[str], None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'chat_messages',
        sa.Column('message_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('application_id', sa.String(), nullable=True),
        sa.Column('autonomous_task_id', sa.String(), nullable=True),
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('human_request_id', sa.String(), nullable=True),
        sa.Column('safe_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('message_id'),
        # CASCADE on the owning rows: a transcript has no meaning once the
        # application/task it describes is gone. SET NULL on the human request,
        # because the CONVERSATION should survive an expired/cleaned-up pause —
        # losing "Autogram asked you for an OTP" would leave an unexplained gap
        # in the user's own history.
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['application_id'], ['applications.application_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['autonomous_task_id'], ['autonomous_tasks.task_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['human_request_id'], ['human_interaction_requests.request_id'], ondelete='SET NULL',
        ),
    )
    op.create_index(op.f('ix_chat_messages_user_id'), 'chat_messages', ['user_id'])
    op.create_index(op.f('ix_chat_messages_application_id'), 'chat_messages', ['application_id'])
    op.create_index(op.f('ix_chat_messages_autonomous_task_id'), 'chat_messages', ['autonomous_task_id'])
    op.create_index(op.f('ix_chat_messages_human_request_id'), 'chat_messages', ['human_request_id'])
    # Every read is "this attempt's messages, oldest first", so the ordering
    # column is indexed too.
    op.create_index(op.f('ix_chat_messages_created_at'), 'chat_messages', ['created_at'])


def downgrade() -> None:
    """Downgrade schema.

    Safe to reverse: dropping this table loses only the rendered conversation.
    Every fact it describes is still held by the records it was built ALONGSIDE
    rather than replacing — `human_interaction_requests` (the pauses),
    `application_audit_log` (the compliance trail), and the status columns
    themselves. That separation was the reason for a distinct table.
    """
    op.drop_index(op.f('ix_chat_messages_created_at'), table_name='chat_messages')
    op.drop_index(op.f('ix_chat_messages_human_request_id'), table_name='chat_messages')
    op.drop_index(op.f('ix_chat_messages_autonomous_task_id'), table_name='chat_messages')
    op.drop_index(op.f('ix_chat_messages_application_id'), table_name='chat_messages')
    op.drop_index(op.f('ix_chat_messages_user_id'), table_name='chat_messages')
    op.drop_table('chat_messages')
