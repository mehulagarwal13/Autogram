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
    delete,
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
    detected_ats_platform: str | None = None


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


def test_apply_run_result_records_the_reason_for_manual_required_too():
    # manual_required's reason ("this required field is missing from your
    # profile") matters just as much as failed's — must surface on the
    # Application row itself, not just the per-run AutomationRun record.
    application = _blank_application(status="processing")
    db = MagicMock()
    result = _FakeRunResult(
        application_id="app-1",
        status="manual_required",
        ats_platform="lever",
        confidence=0.5,
        error_log="Required field(s) could not be filled: linkedin_url.",
    )

    apply_run_result(db, application, result)

    assert application.status == "manual_required"
    assert application.failure_reason == "Required field(s) could not be filled: linkedin_url."


def test_apply_run_result_does_not_set_a_failure_reason_for_non_failed_status():
    application = _blank_application(status="processing")
    db = MagicMock()
    result = _FakeRunResult(application_id="app-1", status="needs_review", ats_platform="workday", confidence=0.4)

    apply_run_result(db, application, result)

    assert application.status == "needs_review"
    assert application.failure_reason is None
    assert application.applied_date is None


# ---------- detected_ats_platform vs. ats_platform ----------

def test_apply_run_result_records_the_detected_platform_separately_from_the_resolved_one():
    """The regression this exists for: a run confidently detected as
    "smartrecruiters" but actually filled by GenericAdapter reports
    `ats_platform="custom"` (see ApplicationFlowManager._fall_back_to_generic_adapter)
    and `detected_ats_platform="smartrecruiters"` — both must land on the
    `Application` row, distinctly, so nothing ever displays this as though a
    dedicated SmartRecruiters adapter ran it."""
    application = _blank_application(status="processing")
    db = MagicMock()
    result = _FakeRunResult(
        application_id="app-1", status="copilot_review", ats_platform="custom",
        confidence=1.0, detected_ats_platform="smartrecruiters",
    )

    apply_run_result(db, application, result)

    assert application.ats_platform == "custom"
    assert application.detected_ats_platform == "smartrecruiters"


def test_apply_run_result_falls_back_to_ats_platform_when_detected_platform_is_unset():
    """Older/hand-built results (or a run where detection and resolution
    genuinely agreed) never set `detected_ats_platform` — `apply_run_result`
    must not leave the column spuriously blank for those; it should read the
    same as `ats_platform`."""
    application = _blank_application(status="processing")
    db = MagicMock()
    result = _FakeRunResult(application_id="app-1", status="applied", ats_platform="greenhouse", confidence=0.95)

    apply_run_result(db, application, result)

    assert application.detected_ats_platform == "greenhouse"


# ---------- delete ----------

def test_delete_removes_the_row_and_commits():
    """Cascading child rows (AutomationRun/ApplicationQuestion/
    ApplicationAuditLog/ChatMessage) are the database's job (`ondelete=
    "CASCADE"` on each FK) — this only needs to prove the application row
    itself is handed to `db.delete` and the transaction is committed."""
    application = _blank_application(status="failed")
    db = MagicMock()

    delete(db, application)

    db.delete.assert_called_once_with(application)
    db.commit.assert_called_once()


def test_delete_removes_the_on_disk_log_directory(monkeypatch, tmp_path):
    import app.services.application_repository as repo_module

    monkeypatch.setattr(repo_module, "AUTOMATION_LOGS_DIR", str(tmp_path))
    application = _blank_application(status="failed", application_id="app-with-logs")
    run_dir = tmp_path / "app-with-logs"
    run_dir.mkdir()
    (run_dir / "screenshot1.png").write_bytes(b"fake")
    (run_dir / "trace.zip").write_bytes(b"fake")

    delete(MagicMock(), application)

    assert not run_dir.exists()


def test_delete_does_not_raise_when_there_is_no_log_directory(monkeypatch, tmp_path):
    """The common case — most applications never hit a code path that writes
    a screenshot/trace at all."""
    import app.services.application_repository as repo_module

    monkeypatch.setattr(repo_module, "AUTOMATION_LOGS_DIR", str(tmp_path))
    application = _blank_application(status="failed", application_id="app-with-no-logs")

    delete(MagicMock(), application)  # must not raise
