"""
BrowserManager (automation/browser/browser_manager.py) — tests cover the
logic that doesn't require actually launching a browser (retries, file
paths for screenshots/traces/error logs, and error handling when methods are
used out of sequence). Launching real Chromium is exercised manually /
in integration testing once Phase 3/4 adapters exist and CI has
`playwright install chromium` available.
"""

import pytest

from automation.browser.browser_manager import BrowserAutomationError, BrowserManager
from automation.browser.session import SessionStore


class _FakePage:
    """Stands in for a Playwright Page — just records the screenshot call."""

    def __init__(self):
        self.screenshot_calls = []

    def screenshot(self, *, path, full_page):
        self.screenshot_calls.append({"path": path, "full_page": full_page})
        with open(path, "wb") as f:
            f.write(b"fake-png-bytes")


class _FakeTracing:
    def __init__(self):
        self.started = False
        self.stopped_path = None

    def start(self, *, screenshots, snapshots, sources):
        self.started = True

    def stop(self, *, path):
        self.stopped_path = path
        with open(path, "wb") as f:
            f.write(b"fake-trace-zip-bytes")


class _FakeContext:
    def __init__(self):
        self.tracing = _FakeTracing()


def _manager(tmp_path, monkeypatch):
    monkeypatch.setattr("automation.browser.browser_manager.AUTOMATION_LOGS_DIR", str(tmp_path))
    # Session store must be tmp_path-backed too, not just the logs dir: left
    # at its default, BrowserManager builds a SessionStore pointing at the
    # REAL `AUTOMATION_SESSION_DIR`, so `has_saved_session`/`save_session`
    # here read and write actual on-disk session files under this fixture's
    # hardcoded user_id="user-1". Any other test (or any real run) that saved
    # a session for that same (user-1, greenhouse) pair would then make
    # `test_has_saved_session_false_before_any_save` below fail depending on
    # what ran earlier — a leftover file from a previous pytest session is
    # enough to break it, since nothing ever cleaned it up.
    return BrowserManager(
        user_id="user-1",
        ats_platform="greenhouse",
        session_store=SessionStore(base_dir=tmp_path / "sessions"),
    )


# ---------- retries ----------

def test_run_with_retries_succeeds_first_try(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    result = manager.run_with_retries(lambda: 42)
    assert result == 42


def test_run_with_retries_recovers_after_transient_failures(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)  # don't actually wait in tests

    attempts = {"count": 0}

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise TimeoutError("simulated transient failure")
        return "ok"

    result = manager.run_with_retries(flaky, max_attempts=3, retry_on=(TimeoutError,))
    assert result == "ok"
    assert attempts["count"] == 3


def test_run_with_retries_raises_after_exhausting_attempts(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    def always_fails():
        raise TimeoutError("still broken")

    with pytest.raises(BrowserAutomationError):
        manager.run_with_retries(always_fails, max_attempts=2, retry_on=(TimeoutError,))


def test_run_with_retries_does_not_catch_unrelated_errors(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)

    def raises_value_error():
        raise ValueError("not a retryable playwright error")

    with pytest.raises(ValueError):
        manager.run_with_retries(raises_value_error, retry_on=(TimeoutError,))


# ---------- screenshots / traces / error log ----------

def test_screenshot_on_failure_writes_incrementing_filenames(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    page = _FakePage()

    first = manager.screenshot_on_failure(page, "app-123")
    second = manager.screenshot_on_failure(page, "app-123")

    assert first.endswith("screenshot1.png")
    assert second.endswith("screenshot2.png")
    assert len(page.screenshot_calls) == 2


def test_start_and_stop_trace_writes_trace_zip(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    context = _FakeContext()

    manager.start_trace(context)
    assert context.tracing.started is True

    trace_path = manager.stop_trace("app-123", context)
    assert trace_path.endswith("trace.zip")
    assert context.tracing.stopped_path == trace_path


def test_start_trace_without_context_raises(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    with pytest.raises(BrowserAutomationError):
        manager.start_trace()


def test_write_error_log_appends_and_returns_path(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)

    path = manager.write_error_log("app-123", "first failure")
    manager.write_error_log("app-123", "second failure")

    content = open(path, encoding="utf-8").read()
    assert "first failure" in content
    assert "second failure" in content


# ---------- out-of-sequence usage ----------

def test_save_session_without_context_raises(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    with pytest.raises(BrowserAutomationError):
        manager.save_session()


def test_has_saved_session_false_before_any_save(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.has_saved_session() is False


def test_close_without_launch_is_a_no_op(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    manager.close()  # must not raise even though nothing was ever launched
