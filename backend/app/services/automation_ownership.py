"""
The one coordination boundary between Autogram's two independent automation
paths: *who currently owns the automation of a given job*.

Background. There are two automation systems and they are deliberately
architecturally separate (see `AUTONOMOUS_AGENT.md`): the deterministic
per-ATS path (`Application` / `AutomationRun`, entered via
`POST /applications/start`) and the autonomous agent
(`AutonomousTask`, entered via `POST /agent/tasks`). They share no tables and
no execution logic, and this module does not change that. It answers exactly
one question, reading only status columns:

    "Is another active automation already operating on this same job?"

Nothing here starts, stops, resumes, or inspects the internals of either
path. It is a lookup plus a lock, not a scheduler.

Why it is needed. Without it, `POST /agent/tasks` had no duplicate check at
all, so N calls with the same URL produced N active tasks — and under the
default `AUTOMATION_BROWSER_MODE=cdp`, N browser tabs independently filling
the same application form. Verified against the real database before the fix:
three concurrent `RUNNING` tasks on one URL. The deterministic path already
protected itself via `uq_applications_user_job_url`, but neither path knew
about the other.

## How a job is identified

`application_repository.compute_job_url_hash` — the function the
deterministic path has always used for `Application.job_url_hash`. It is
`sha256(url.strip().lower())`: trim and case-fold, and deliberately nothing
else. No trailing-slash collapsing, no fragment stripping, no query-parameter
removal, because on real career sites the query string routinely *is* the
posting identity (`?gh_jid=`, `?jobId=`, Workday's path/params) and merging
two genuinely different postings is far worse than failing to merge two
spellings of one. Both paths now call this same function, which is what makes
cross-path recognition work at all.

## Two layers of protection, and why both exist

1. **Database constraints** do the real work and are race-proof on their own:
   `uq_applications_active_job` (deterministic) and
   `uq_autonomous_tasks_active_job` (autonomous). BOTH are now PARTIAL unique
   indexes covering only the statuses where an attempt is actively being
   automated, so two simultaneous same-path inserts cannot both commit, while
   finished attempts drop out of the index and a later attempt is still
   possible. (The deterministic one began life as a FULL
   `UniqueConstraint(user_id, job_url_hash)`; that made a deliberate
   re-application impossible to represent without overwriting the historical
   `applied` row, so it was narrowed — with its "never silently apply twice"
   half taken over, unweakened, by `find_submitted_application` below.)
2. **`reserve_job_automation`** — a Postgres transaction-scoped advisory lock
   plus a read of both tables. A unique index cannot span two tables, so the
   *cross-path* case (deterministic starting while autonomous runs, or vice
   versa) is the one place a plain check-then-act could interleave. The
   advisory lock closes that window. It is a built-in Postgres primitive, not
   new infrastructure, and it releases automatically when the transaction
   ends, so it cannot leak.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import nullslast, text
from sqlalchemy.orm import Session

from app.models.db_models import (
    AUTONOMOUS_TASK_ACTIVE_STATUSES,
    Application,
    AutonomousTask,
)
from app.services.application_repository import compute_job_url_hash

logger = logging.getLogger(__name__)

#: `Application.status` values where the deterministic path still owns the
#: job. Mirrors `app/api/applications.py`'s own `IN_PROGRESS_STATUSES` —
#: intentionally NOT the retryable ones (`failed`, `manual_required`,
#: `needs_review`), because that route already treats those as "retry on the
#: same row", and `copilot_review` IS included there since a browser is being
#: held open for the human.
#:
#: Imported lazily inside the function rather than at module scope: importing
#: `app.api.applications` from a service would invert this codebase's
#: api -> services dependency direction (see `automation/interfaces.py`).
def _deterministic_active_statuses() -> frozenset[str]:
    from app.api.applications import IN_PROGRESS_STATUSES

    return IN_PROGRESS_STATUSES


#: `Application.status` values meaning the application was genuinely SUBMITTED
#: — `app/api/applications.py`'s own `COMPLETED_STATUSES`, reused rather than
#: re-listed. Reached only through
#: `application_flow_manager.submit_and_confirm` -> `wait_for_submission_confirmation`,
#: i.e. positive confirmation was observed, and `applied_date` is stamped at
#: the same time. Lazily imported for the same api -> services direction
#: reason as `_deterministic_active_statuses`.
def _deterministic_submitted_statuses() -> frozenset[str]:
    from app.api.applications import COMPLETED_STATUSES

    return COMPLETED_STATUSES


#: The autonomous path's equivalent. `COMPLETED` has exactly ONE call site
#: (`loop.py`'s `TASK_COMPLETED` branch) and it is gated on
#: `_page_shows_confirmation` — a post-submit confirmation page must actually
#: have been observed, or the claim is downgraded to `WAITING_FOR_APPROVAL`.
#: So `COMPLETED` is a genuine submission signal here, NOT merely "the browser
#: flow ended", "saved as a draft", or "the user stopped": those end up
#: WAITING_FOR_APPROVAL / CANCELLED / FAILED instead, none of which are in
#: this set.
AUTONOMOUS_SUBMITTED_STATUSES = frozenset({"COMPLETED"})


@dataclass(frozen=True)
class SubmittedApplication:
    """A job this user has ALREADY successfully applied to, on either path.

    Deliberately a separate concept from `ActiveAutomation`: that one is about
    *concurrency* ("who is driving a browser right now"), this one is about
    *lifetime* ("this was already submitted"). Conflating them — e.g. by adding
    COMPLETED to the active partial unique index — would wrongly give
    COMPLETED the same retry semantics as FAILED/CANCELLED."""

    path: str                          # "autonomous" | "deterministic"
    submitted_at: object | None = None  # datetime; best-effort, see below
    task_id: str | None = None
    application_id: str | None = None

    @property
    def is_autonomous(self) -> bool:
        return self.path == "autonomous"


@dataclass(frozen=True)
class ActiveAutomation:
    """Who owns this job right now. `path` is which system holds it, so a
    caller can render an accurate message and a link to the right place."""

    path: str                      # "autonomous" | "deterministic"
    status: str
    task_id: str | None = None      # set when path == "autonomous"
    application_id: str | None = None  # set when path == "deterministic"

    @property
    def is_autonomous(self) -> bool:
        return self.path == "autonomous"


def job_key(job_url: str) -> str:
    """The logical identity of a job for one user — see the module docstring
    on why this reuses the deterministic path's hash verbatim."""
    return compute_job_url_hash(job_url)


def _advisory_lock_key(user_id: str, job_url_hash: str) -> int:
    """A stable signed-64-bit key for `pg_advisory_xact_lock`, derived from
    (user, job). Postgres advisory locks take a bigint, so the sha256 is
    folded down; a collision would only ever mean two unrelated jobs briefly
    serialize their *start* requests, which is harmless."""
    digest = hashlib.sha256(f"{user_id}:{job_url_hash}".encode()).digest()
    unsigned = int.from_bytes(digest[:8], "big", signed=False)
    return unsigned - (1 << 63)  # fold into the signed range Postgres expects


def reserve_job_automation(db: Session, *, user_id: str, job_url: str) -> str:
    """Takes the per-(user, job) advisory lock for the CURRENT transaction and
    returns the job key.

    Call this at the top of a start handler, before checking for an existing
    active automation. Every concurrent start request for the same logical job
    then serializes here, so the "look, then insert" sequence that follows
    cannot interleave with another request's — which is what makes the
    cross-path check (which no single unique index can cover) safe.

    Released automatically by Postgres when the transaction commits or rolls
    back; there is nothing to unlock by hand and nothing to leak.

    Degrades to a no-op (with a warning) if the backend has no advisory locks.
    That is not the deployment this project uses — it is Postgres/Neon only —
    but a caller must never crash because a lock hint was unavailable; the
    unique constraints still hold in that case, which covers same-path
    duplicates, the common case.
    """
    key = job_key(job_url)
    try:
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _advisory_lock_key(user_id, key)})
    except Exception:  # noqa: BLE001 - a missing lock primitive must not fail the request
        logger.warning(
            "Advisory lock unavailable; relying on unique constraints alone for "
            "duplicate-automation protection.", exc_info=True,
        )
    return key


def find_active_automation(
    db: Session, *, user_id: str, job_url: str, exclude_task_id: str | None = None,
) -> ActiveAutomation | None:
    """The active automation for this (user, job), from EITHER path, or None.

    Checks the autonomous path first purely because it is the one that opens a
    tab immediately on start. `exclude_task_id` lets a caller ignore a row it
    just created itself.
    """
    key = job_key(job_url)

    autonomous_q = (
        db.query(AutonomousTask)
        .filter(
            AutonomousTask.user_id == user_id,
            AutonomousTask.job_url_hash == key,
            AutonomousTask.current_status.in_(tuple(AUTONOMOUS_TASK_ACTIVE_STATUSES)),
        )
    )
    if exclude_task_id is not None:
        autonomous_q = autonomous_q.filter(AutonomousTask.task_id != exclude_task_id)
    task = autonomous_q.order_by(AutonomousTask.created_at.desc()).first()
    if task is not None:
        return ActiveAutomation(
            path="autonomous", status=task.current_status, task_id=task.task_id,
        )

    application = (
        db.query(Application)
        .filter(
            Application.user_id == user_id,
            Application.job_url_hash == key,
            Application.status.in_(tuple(_deterministic_active_statuses())),
        )
        .order_by(Application.created_at.desc())
        .first()
    )
    if application is not None:
        return ActiveAutomation(
            path="deterministic", status=application.status,
            application_id=application.application_id,
        )

    return None


def find_submitted_application(
    db: Session, *, user_id: str, job_url: str,
) -> SubmittedApplication | None:
    """Has this user ALREADY successfully submitted an application for this
    logical job, on either path? `None` if not.

    This is the lifetime-duplicate counterpart to `find_active_automation`, and
    the reason it is needed: the concurrency guard deliberately excludes
    terminal statuses so a FAILED/CANCELLED attempt can be retried — which also
    let a *successful* one through. Verified against the real database before
    this was added: an autonomous task at `COMPLETED` was invisible to
    `POST /applications/start`, and an application at `applied` was invisible to
    `POST /agent/tasks`, so either could silently drive a second full
    application for a job already submitted.

    **No new marker is written anywhere for this.** It reads the two signals
    each path already records only on genuine, confirmation-verified
    submission (`AutonomousTask.current_status == 'COMPLETED'`, gated on an
    observed confirmation page; `Application.status == 'applied'`, stamped with
    `applied_date` by `submit_and_confirm`). That is deliberate: a brand-new
    "submitted" flag would be one more thing that could be written at the wrong
    moment, whereas these two are already load-bearing and already correct.
    """
    key = job_key(job_url)

    task = (
        db.query(AutonomousTask)
        .filter(
            AutonomousTask.user_id == user_id,
            AutonomousTask.job_url_hash == key,
            AutonomousTask.current_status.in_(tuple(AUTONOMOUS_SUBMITTED_STATUSES)),
        )
        .order_by(AutonomousTask.updated_at.desc())
        .first()
    )
    if task is not None:
        return SubmittedApplication(
            path="autonomous",
            # No dedicated completed_at column exists; `updated_at` is when the
            # COMPLETED transition was written, so it is the honest best
            # available approximation rather than a fabricated precision.
            submitted_at=task.updated_at,
            task_id=task.task_id,
        )

    application = (
        db.query(Application)
        .filter(
            Application.user_id == user_id,
            Application.job_url_hash == key,
            Application.status.in_(tuple(_deterministic_submitted_statuses())),
        )
        # By `applied_date`, not `created_at`: with several successful attempts
        # for one job, "the latest submission" means the one most recently
        # SUBMITTED. `created_at` would order by when each attempt row was
        # opened, which can differ. `nullslast` is belt-and-braces — an
        # `applied` row always has `applied_date` (stamped in the same write by
        # `apply_run_result`) — so a hand-edited row can never sort first and
        # shadow a real submission.
        .order_by(nullslast(Application.applied_date.desc()), Application.created_at.desc())
        .first()
    )
    if application is not None:
        return SubmittedApplication(
            path="deterministic",
            submitted_at=application.applied_date,
            application_id=application.application_id,
        )

    return None


class ReapplyAcknowledgementError(Exception):
    """The caller's re-application acknowledgement does not name the submission
    that is actually on file for this (user, job) right now.

    Raised by `validate_reapply_acknowledgement`; each start route converts it
    into its own `409 invalid_reapplication_request` response. A plain
    exception rather than an `HTTPException` so this service layer stays free
    of HTTP concerns, matching the api -> services direction the rest of the
    module respects.
    """


def validate_reapply_acknowledgement(acknowledgement, submitted: SubmittedApplication) -> None:
    """Accept a deliberate re-application ONLY if `acknowledgement` names the
    submission we just found for THIS user and THIS job. Raises
    `ReapplyAcknowledgementError` otherwise.

    Shared by both start routes so "what counts as a valid acknowledgement"
    has exactly one definition. What each check rules out:

    * wrong `path` / wrong id -> an acknowledgement replayed from a different
      job, or naming the other automation path, cannot authorise this one;
    * a superseded id -> `find_submitted_application` returns the LATEST
      submission, so once a newer attempt succeeds, an acknowledgement naming
      the older one stops matching. That is what stops a stale 409 from
      authorising a future start, without needing a token table.

    There is deliberately nothing to compare against a *user*: `submitted` was
    itself produced by a `user_id`-scoped query, so another user's id can never
    appear here and therefore can never match.
    """
    expected_id = submitted.task_id if submitted.is_autonomous else submitted.application_id
    provided_id = (
        acknowledgement.task_id if acknowledgement.path == "autonomous"
        else acknowledgement.application_id
    )
    if acknowledgement.path != submitted.path or not provided_id or provided_id != expected_id:
        raise ReapplyAcknowledgementError(
            "the acknowledgement does not match the current submission for this job"
        )
