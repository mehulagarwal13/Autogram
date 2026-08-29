"""
Unit tests for `app/services/human_interaction_repository.py`. Only
`db.add`/`db.commit`/`db.refresh` are ever called by this module (no
`db.query`), so a trivial no-op fake session is enough — no real Postgres,
same convention as `automation/tests/test_autonomous_loop.py`.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import human_interaction_repository as repo


class FakeSession:
    def add(self, obj):
        pass

    def commit(self):
        pass

    def refresh(self, obj):
        pass


def test_create_request_rejects_an_unknown_request_type():
    with pytest.raises(ValueError):
        repo.create_request(
            FakeSession(), user_id="u1", task_id="t1", request_type="NOT_A_REAL_TYPE", message="hi",
        )


def test_create_request_sets_a_default_expiry():
    req = repo.create_request(FakeSession(), user_id="u1", task_id="t1", request_type="OTP_REQUIRED", message="Enter the code")
    assert req.status == "PENDING"
    assert req.request_id.startswith("hreq_")
    assert req.expires_at is not None
    assert req.expires_at > datetime.now(timezone.utc)


def test_create_request_can_disable_expiry():
    req = repo.create_request(
        FakeSession(), user_id="u1", task_id="t1", request_type="USER_CONFIRMATION_REQUIRED",
        message="Confirm this action.", expires_in_minutes=None,
    )
    assert req.expires_at is None
    assert repo.is_expired(req) is False


def test_is_expired_true_once_past_expires_at():
    req = repo.create_request(FakeSession(), user_id="u1", task_id="t1", request_type="OTP_REQUIRED", message="x")
    assert repo.is_expired(req) is False
    future = datetime.now(timezone.utc) + timedelta(minutes=repo.DEFAULT_EXPIRY_MINUTES + 1)
    assert repo.is_expired(req, now=future) is True


def test_status_transitions():
    req = repo.create_request(FakeSession(), user_id="u1", task_id="t1", request_type="LOGIN_REQUIRED", message="x")
    db = FakeSession()

    repo.mark_responded(db, req)
    assert req.status == "RESPONDED"
    assert req.responded_at is not None

    repo.mark_resuming(db, req)
    assert req.status == "RESUMING"

    repo.mark_resolved(db, req)
    assert req.status == "RESOLVED"
    assert req.resolved_at is not None


def test_mark_resuming_never_clobbers_a_terminal_status():
    """Regression (found by the real-browser E2E run,
    `test_04_correct_otp_resumes_and_continues`): the automation loop is woken
    by `deliver_secret` and can drive a request all the way to RESOLVED before
    the `/respond` request thread executes its next statement. A late,
    unconditional `mark_resuming` would then drag a fully-consumed request
    back to a non-terminal status and leave it stuck there forever."""
    for terminal in ("RESOLVED", "EXPIRED", "CANCELLED", "FAILED"):
        req = repo.create_request(
            FakeSession(), user_id="u1", task_id="t1", request_type="OTP_REQUIRED", message="x",
        )
        req.status = terminal
        result = repo.mark_resuming(FakeSession(), req)
        assert result.status == terminal, f"mark_resuming clobbered {terminal}"

    # ...but it still works for the legitimate intermediate case.
    req = repo.create_request(
        FakeSession(), user_id="u1", task_id="t1", request_type="OTP_REQUIRED", message="x",
    )
    repo.mark_responded(FakeSession(), req)
    repo.mark_resuming(FakeSession(), req)
    assert req.status == "RESUMING"


def test_mark_expired_and_cancelled_and_failed_set_resolved_at():
    for terminal_fn, expected_status in [
        (repo.mark_expired, "EXPIRED"),
        (repo.mark_cancelled, "CANCELLED"),
        (repo.mark_failed, "FAILED"),
    ]:
        req = repo.create_request(FakeSession(), user_id="u1", task_id="t1", request_type="CAPTCHA_REQUIRED", message="x")
        terminal_fn(FakeSession(), req)
        assert req.status == expected_status
        assert req.resolved_at is not None
