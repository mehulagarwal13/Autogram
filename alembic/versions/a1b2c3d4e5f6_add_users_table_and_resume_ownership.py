"""add users table and per-user resume ownership

Revision ID: a1b2c3d4e5f6
Revises: 4475eb486d90
Create Date: 2026-08-03 00:00:00.000000

Closes the one real hole in this project's migration history: NOTHING ever
created `users`. Every later migration that references it — candidate_profiles,
profile_documents, applications, automation_runs, answer_cache — declares a
foreign key to `users.user_id`, and `9b1c2f7a3e4d` was the first to try, so a
clean database failed there with:

    psycopg2.errors.UndefinedTable: relation "users" does not exist

It went unnoticed because `app/main.py` calls `Base.metadata.create_all()` at
startup, which creates whatever tables are missing straight from the models. On
any database that had been booted once, `users` already existed and Alembic's
gap was invisible. It only surfaces when migrations run first, on a database
that has never seen the app — which is exactly the reproducible path the
migrations are supposed to guarantee.

Also fixes `resumes`, created by the base revision `fbfb4ab1cf61` in its
pre-auth shape and never brought forward:

- `user_id` was missing entirely. `app/core/pgvector_setup.py::ensure_vector_schema`
  adds it at runtime, but as a bare nullable VARCHAR with no foreign key — so a
  migrated-only database has no referential integrity on resume ownership and
  no cascade when a user is deleted.
- `parsed_data` was TEXT; the model is JSONB.
- `confidence_score` was VARCHAR; the model is FLOAT.

Left alone deliberately: the `embedding_vector` columns and the HNSW index.
Those stay owned by `ensure_vector_schema()`, which is where the pgvector setup
already lives and which runs after the extension is guaranteed enabled — see
`app/core/pgvector_setup.py`'s docstring for the required call ordering.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '4475eb486d90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    """`ensure_vector_schema()` may already have added `resumes.user_id` at
    runtime on a database that booted the app before migrating. Checking keeps
    this migration correct in both directions instead of exploding on the
    duplicate."""
    inspector = sa.inspect(op.get_bind())
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'users',
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('password_hash', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('user_id'),
    )
    # unique=True on the model's `email` column, so the index carries it.
    op.create_index('ix_users_email', 'users', ['email'], unique=True)

    # --- resumes: bring the pre-auth table up to the model ------------------
    if not _has_column('resumes', 'user_id'):
        op.add_column('resumes', sa.Column('user_id', sa.String(), nullable=True))
    op.create_index('ix_resumes_user_id', 'resumes', ['user_id'], unique=False)

    # NOT NULL last, and as its own step: on a database that somehow holds
    # pre-auth resume rows this fails loudly with a clear constraint error
    # rather than inventing an owner for someone's uploaded resume.
    op.alter_column('resumes', 'user_id', existing_type=sa.String(), nullable=False)
    op.create_foreign_key(
        'fk_resumes_user_id_users', 'resumes', 'users',
        ['user_id'], ['user_id'], ondelete='CASCADE',
    )

    op.alter_column(
        'resumes', 'parsed_data',
        existing_type=sa.Text(),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=True,
        postgresql_using='parsed_data::jsonb',
    )
    op.alter_column(
        'resumes', 'confidence_score',
        existing_type=sa.String(),
        type_=sa.Float(),
        existing_nullable=True,
        postgresql_using='confidence_score::double precision',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'resumes', 'confidence_score',
        existing_type=sa.Float(),
        type_=sa.String(),
        existing_nullable=True,
    )
    op.alter_column(
        'resumes', 'parsed_data',
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.drop_constraint('fk_resumes_user_id_users', 'resumes', type_='foreignkey')
    op.drop_index('ix_resumes_user_id', table_name='resumes')
    op.drop_column('resumes', 'user_id')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_table('users')
