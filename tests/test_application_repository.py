"""
Tests for application_repository.py: `compute_job_url_hash`'s normalization
(idempotency — the `uq_applications_user_job_url` DB constraint depends on
this being stable) and the `ApplicationRunResult` -> `Application`/
`AutomationRun` field mapping. DB-touching calls (`db.add`/`commit`/`refresh`)
are exercised against a real in-memory `Application` instance with a
`MagicMock` session — the same "no live Postgres needed" approach
test_profile_repository_helpers.py uses for `_apply_profile_fields`.
"""

from dataclasses import dataclass, field
from unittest.mock import MagicMock

from app.models.db_models import Application
from app.services.application_repository import (
    apply_run_result,
    compute_job_url_hash,
    mark_unsupported_ats,
)


def _blank_application(**overrides) -> Application:
    defaults = dict(
        application_id="app-1",
        user_id="user-1",
        job_url="https://boards.greenhouse.io/acme/jobs/1",
        job_url_hash=compute_job_url_hash("https://boards.greenhouse.io/acme/jobs/1"),
        status="pending",
        autopilot_enabled=False,
    )
    defaults.update(overrides)
    return Application(**defaults)


# ---------- compute_job_url_hash ----------

def test_compute_job_url_hash_is_deterministic():
    url = "https://boards.greenhouse.io/acme/jobs/123"
    assert compute_job_url_hash(url) == compute_job_url_hash(url)


def test_compute_job_url_hash_ignores_case_and_surrounding_whitespace():
    padded_mixed_case = "  HTTPS://Boards.Greenhouse.IO/Acme/Jobs/1  "
    canonical = "https://boards.greenhouse.io/acme/jobs/1"
    assert compute_job_url_hash(padded_mixed_case) == compute_job_url_hash(canonical)


def test_compute_job_url_hash_differs_for_different_urls():
    a = compute_job_url_hash("https://jobs.lever.co/acme/1")
    b = compute_job_url_hash("https://jobs.lever.co/acme/2")
    assert a != b


# ---------- apply_run_result ----------

@dataclass
class _FakeRunResult:
    """Duck-types automation.interfaces.ApplicationRunResult without
    depending on automation/ from a tests/ (app-only) test file."""

    application_id: str
    status: str
    ats_platform: str
    confidence: float
    screenshot_paths: list = field(default_factory=list)
    trace_path: str | None = None
    error_log: str | None = None


def test_apply_run_result_marks_applied_and_sets_applied_date():
    application = _blank_application(status="processing")
    db = MagicMock()
    result = _FakeRunResult(application_id="app-1", status="applied", ats_platform="greenhouse", confidence=0.95)

    apply_run_result(db, application, result)

    assert application.status == "applied"
    assert application.ats_platform == "greenhouse"
    assert application.confidence_score == 0.95
    assert application.applied_date is not None
    assert application.failure_reason is None

    db.add.assert_called_once()
    added_run = db.add.call_args[0][0]
    assert added_run.application_id == "app-1"
    assert added_run.status == "applied"
    db.commit.assert_called()


def test_apply_run_result_records_failure_reason_on_failure():
    application = _blank_application(status="processing")
    db = MagicMock()
    result = _FakeRunResult(
        application_id="app-1",
        status="failed",
        ats_platform="greenhouse",
        confidence=0.0,
        error_log="BrowserAutomationError: could not launch browser",
    )

    apply_run_result(db, application, result)

    assert application.status == "failed"
    assert application.applied_date is None
    assert application.failure_reason == "BrowserAutomationError: could not launch browser"


def test_apply_run_result_does_not_set_a_failure_reason_for_non_failed_status():
    application = _blank_application(status="processing")
    db = MagicMock()
    result = _FakeRunResult(application_id="app-1", status="needs_review", ats_platform="workday", confidence=0.4)

    apply_run_result(db, application, result)

    assert application.status == "needs_review"
    assert application.failure_reason is None
    assert application.applied_date is None


# ---------- mark_unsupported_ats ----------

def test_mark_unsupported_ats_sets_needs_review_with_a_clear_reason():
    application = _blank_application()
    db = MagicMock()

    mark_unsupported_ats(db, application, ats_platform="workday", confidence=0.98)

    assert application.status == "needs_review"
    assert application.ats_platform == "workday"
    assert application.confidence_score == 0.98
    assert "workday" in application.failure_reason
    db.commit.assert_called_once()
