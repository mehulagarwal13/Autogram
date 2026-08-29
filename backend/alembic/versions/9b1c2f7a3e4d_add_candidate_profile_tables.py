"""add candidate profile tables (master profile system, Phase 1)

Revision ID: 9b1c2f7a3e4d
Revises: 4475eb486d90
Create Date: 2026-07-26 00:00:00.000000

Adds: candidate_profiles, education_entries, experience_entries, profile_documents.

Note (see README.md): on a fresh Neon database `app/main.py` bootstraps the
current schema via `Base.metadata.create_all` at startup regardless of
Alembic's history, so these tables appear either way. This migration exists
so schema changes stay versioned/reproducible per the project's engineering
rules; if `alembic upgrade head` complains about an out-of-sync history, use
`alembic stamp head` after confirming the tables already exist, or follow the
README's "generate a new baseline" instructions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '9b1c2f7a3e4d'
# Was '4475eb486d90'. Re-pointed at the revision that creates `users`: every
# table below declares a foreign key to `users.user_id`, and nothing in the
# history had ever created it — see a1b2c3d4e5f6's docstring.
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'candidate_profiles',
        sa.Column('profile_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('full_name', sa.String(), nullable=True),
        sa.Column('first_name', sa.String(), nullable=True),
        sa.Column('last_name', sa.String(), nullable=True),
        sa.Column('email', sa.String(), nullable=True),
        sa.Column('phone_encrypted', sa.String(), nullable=True),
        sa.Column('location', sa.String(), nullable=True),
        sa.Column('address_encrypted', sa.Text(), nullable=True),
        sa.Column('city', sa.String(), nullable=True),
        sa.Column('state', sa.String(), nullable=True),
        sa.Column('country', sa.String(), nullable=True),
        sa.Column('linkedin_url', sa.String(), nullable=True),
        sa.Column('github_url', sa.String(), nullable=True),
        sa.Column('portfolio_url', sa.String(), nullable=True),
        sa.Column('website_url', sa.String(), nullable=True),
        sa.Column('current_company', sa.String(), nullable=True),
        sa.Column('current_role', sa.String(), nullable=True),
        sa.Column('years_of_experience', sa.Float(), nullable=True),
        sa.Column('notice_period_days', sa.Integer(), nullable=True),
        sa.Column('expected_salary', sa.Float(), nullable=True),
        sa.Column('expected_salary_currency', sa.String(), nullable=True),
        sa.Column('work_authorization', sa.String(), nullable=True),
        sa.Column('visa_status', sa.String(), nullable=True),
        sa.Column('preferred_locations', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('remote_preference', sa.String(), nullable=True),
        sa.Column('skills', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('profile_id'),
    )
    op.create_index(op.f('ix_candidate_profiles_user_id'), 'candidate_profiles', ['user_id'], unique=True)

    op.create_table(
        'education_entries',
        sa.Column('education_id', sa.String(), nullable=False),
        sa.Column('profile_id', sa.String(), nullable=False),
        sa.Column('degree', sa.String(), nullable=True),
        sa.Column('university', sa.String(), nullable=True),
        sa.Column('field_of_study', sa.String(), nullable=True),
        sa.Column('start_date', sa.String(), nullable=True),
        sa.Column('end_date', sa.String(), nullable=True),
        sa.Column('gpa', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['profile_id'], ['candidate_profiles.profile_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('education_id'),
    )
    op.create_index(op.f('ix_education_entries_profile_id'), 'education_entries', ['profile_id'], unique=False)

    op.create_table(
        'experience_entries',
        sa.Column('experience_id', sa.String(), nullable=False),
        sa.Column('profile_id', sa.String(), nullable=False),
        sa.Column('company_name', sa.String(), nullable=True),
        sa.Column('job_title', sa.String(), nullable=True),
        sa.Column('start_date', sa.String(), nullable=True),
        sa.Column('end_date', sa.String(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('skills_used', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['profile_id'], ['candidate_profiles.profile_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('experience_id'),
    )
    op.create_index(op.f('ix_experience_entries_profile_id'), 'experience_entries', ['profile_id'], unique=False)

    op.create_table(
        'profile_documents',
        sa.Column('document_id', sa.String(), nullable=False),
        sa.Column('profile_id', sa.String(), nullable=False),
        sa.Column('document_type', sa.String(), nullable=False),
        sa.Column('label', sa.String(), nullable=True),
        sa.Column('job_type_tag', sa.String(), nullable=True),
        sa.Column('original_filename', sa.String(), nullable=False),
        sa.Column('stored_path', sa.String(), nullable=False),
        sa.Column('file_hash', sa.String(), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['profile_id'], ['candidate_profiles.profile_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('document_id'),
    )
    op.create_index(op.f('ix_profile_documents_profile_id'), 'profile_documents', ['profile_id'], unique=False)
    op.create_index(op.f('ix_profile_documents_file_hash'), 'profile_documents', ['file_hash'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_profile_documents_file_hash'), table_name='profile_documents')
    op.drop_index(op.f('ix_profile_documents_profile_id'), table_name='profile_documents')
    op.drop_table('profile_documents')

    op.drop_index(op.f('ix_experience_entries_profile_id'), table_name='experience_entries')
    op.drop_table('experience_entries')

    op.drop_index(op.f('ix_education_entries_profile_id'), table_name='education_entries')
    op.drop_table('education_entries')

    op.drop_index(op.f('ix_candidate_profiles_user_id'), table_name='candidate_profiles')
    op.drop_table('candidate_profiles')
