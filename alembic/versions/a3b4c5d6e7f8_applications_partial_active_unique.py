"""narrow applications' unique constraint to ACTIVE attempts only

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-26 00:00:00.000000

Enables deliberate re-application on the deterministic path WITHOUT destroying
the record of previous successful submissions.

## Why the old constraint had to change

`uq_applications_user_job_url` was a FULL `UNIQUE (user_id, job_url_hash)` —
literally one `Application` row per (user, job), forever. The only way to
attempt a job a second time was `application_repository.retry_application`,
which resets the EXISTING row to `status='pending'` and clears its fields.
For a `failed`/`manual_required`/`needs_review` attempt that is correct (it
never submitted, so there is no history to lose). For an `applied` attempt it
would overwrite `status` and `applied_date` — erasing the fact that the user
ever applied. So a second attempt was unrepresentable.

## What replaces it, and why nothing is weakened

The old constraint was quietly doing two different jobs. They are now split
across the same two layers the autonomous path already uses:

1. **"never two automations on one job at once"** -> `uq_applications_active_job`,
   a PARTIAL unique index over `('pending','processing','copilot_review')` —
   `app/api/applications.py::IN_PROGRESS_STATUSES`. Two concurrent inserts of
   an active attempt still cannot both commit.
2. **"never silently apply twice after success"** -> the route-level lifetime
   check (`automation_ownership.find_submitted_application`), which refuses
   with `409 application_already_submitted` unless the caller explicitly
   acknowledges the exact prior submission.

Historical `applied` rows now sit OUTSIDE the index, so every past attempt is
preserved verbatim alongside any new one. Retryable statuses are outside it
too — those still retry in place, decided by the route exactly as before.

## Data safety

Nothing is created, deleted, rewritten or re-keyed:

* every existing row keeps its `application_id`, `created_at`, `status`,
  `applied_date` and every other column;
* the three tables that reference `applications.application_id`
  (`automation_runs`, `application_questions`, `application_audit_log`) are
  keyed by that id and are untouched, so each attempt keeps its own run
  history, questions and audit trail;
* NO backfill is required, and no duplicate cleanup is possible to need: the
  constraint being dropped guaranteed at most one row per (user, job), so
  there cannot be a pre-existing pair that violates the narrower index.
* `ix_applications_job_url_hash` already exists and serves the
  active/submitted lookups.

Note (see README.md): on a fresh database `app/main.py` bootstraps the schema
via `Base.metadata.create_all`, which creates the new index from
`Application.__table_args__`. `create_all` never ALTERS an existing table,
though, so an existing database needs this migration to drop the old
constraint.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, Sequence[str], None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Must stay identical to `app/api/applications.py::IN_PROGRESS_STATUSES` and
#: to the `postgresql_where` on `Application.__table_args__`'s Index. A test
#: (`test_deterministic_reapply.py`) pins all three together, because if they
#: diverge the database and the application disagree about who owns a job.
_ACTIVE_PREDICATE = "status IN ('pending', 'processing', 'copilot_review')"


def upgrade() -> None:
    """Upgrade schema."""
    # Create the replacement FIRST, so the "one active attempt per job"
    # guarantee is never absent — not even for the instant between the two
    # statements. Both run in one transaction, but ordering it this way means
    # the protective object exists before the old one is removed.
    op.create_index(
        'uq_applications_active_job', 'applications', ['user_id', 'job_url_hash'],
        unique=True, postgresql_where=sa.text(_ACTIVE_PREDICATE),
    )
    op.drop_constraint('uq_applications_user_job_url', 'applications', type_='unique')


def downgrade() -> None:
    """Downgrade schema.

    Only possible while no job has more than one attempt. If a deliberate
    re-application has happened, restoring the full constraint would require
    deleting a real application record, which this migration will NOT do
    silently — it raises instead so an operator can decide.
    """
    connection = op.get_bind()
    duplicates = connection.execute(sa.text(
        "SELECT count(*) FROM (SELECT user_id, job_url_hash FROM applications "
        "GROUP BY user_id, job_url_hash HAVING count(*) > 1) d"
    )).scalar()
    if duplicates:
        raise RuntimeError(
            f"Cannot restore uq_applications_user_job_url: {duplicates} job(s) now have "
            "multiple application attempts. Restoring the full unique constraint would "
            "require deleting real submission history, which this downgrade will not do "
            "automatically. Resolve those rows deliberately first."
        )
    op.create_unique_constraint(
        'uq_applications_user_job_url', 'applications', ['user_id', 'job_url_hash'],
    )
    op.drop_index('uq_applications_active_job', table_name='applications')
