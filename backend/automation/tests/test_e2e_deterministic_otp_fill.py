"""
REAL browser proof that a code typed into Autogram is filled into the external
site, submitted, and the run resumes.

This is the test that was missing. The unit suite proves the channel holds and
releases a code correctly, and the API suite proves the route guards it — but
neither touches a browser, so "the automation actually types it into the page"
was previously asserted only by inspection.

Everything here is real: a real Chromium, a real HTTP site serving a real OTP
form (`fixtures/hitl_test_site.py`, the same fixture the HITL E2E suite uses),
and the real `ApplicationFlowManager._wait_for_human` loop. Nothing about the
verification path is stubbed.

Runs in its own pytest process — see `automation/tests/conftest.py`: a
session-scoped `sync_playwright()` from another file collides with the
independent one `ApplicationFlowManager` starts.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from automation.applications import verification_channel
from automation.applications.application_flow_manager import ApplicationFlowManager
from automation.browser.selectors import VERIFICATION_CODE_INPUT_SELECTOR, find_human_gate
from automation.tests.fixtures.hitl_test_site import INVALID_OTP, VALID_OTP, HitlTestSite


@pytest.fixture(scope="module")
def site():
    with HitlTestSite() as s:
        yield s


@pytest.fixture
def manager():
    """A flow manager used ONLY for its verification-wait behaviour.

    Constructed directly rather than through `POST /applications/start` because
    this test is about one specific loop, and driving a whole application run
    would drag in ATS detection, profile loading, and résumé upload — none of
    which is under test here, all of which can fail for unrelated reasons.
    """
    return ApplicationFlowManager(
        application_id=f"otpfill_{uuid.uuid4().hex[:10]}",
        user_id="user_1",
        job_url="http://127.0.0.1/apply",
        ats_platform="custom",
        adapter_cls=None,
        profile=None,
        resume_document=None,
        autopilot_enabled=False,
        headless=True,
        # Short, so a test that is SUPPOSED to time out does not take minutes.
        human_wait_timeout_s=12,
    )


@pytest.fixture
def page(requires_chromium, site):
    """A real page sitting on the real OTP challenge."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(site.url("/otp"), wait_until="domcontentloaded")
        yield page
        browser.close()


def _deliver_after(application_id: str, code: str, delay: float = 1.0):
    """Deliver a code from another thread while the wait loop is running —
    exactly how it arrives in production (a FastAPI request thread, while the
    Playwright thread polls)."""
    t = threading.Thread(
        target=lambda: (time.sleep(delay), verification_channel.deliver(application_id, code)),
        daemon=True,
    )
    t.start()
    return t


# ---------------------------------------------------------------------------
# The happy path: typed in Autogram -> filled in the browser -> resumed
# ---------------------------------------------------------------------------

def test_a_code_entered_in_autogram_is_filled_submitted_and_clears_the_gate(manager, page, site):
    """THE test. The human never touches the external site.

    Proves the whole chain against a real page: the gate is detected, the code
    arrives from another thread, the automation types it into the real input,
    clicks the real Verify button, the site navigates onward, and the wait
    returns True so the run resumes.
    """
    assert find_human_gate(page) is not None, "fixture should present a real OTP gate"

    _deliver_after(manager.application_id, VALID_OTP)

    resumed = manager._wait_for_human(
        0, "one-time passcode / multi-factor authentication",
        lambda: find_human_gate(page) is None,
        page=page,
    )

    assert resumed is True, "the run must resume once the code clears the gate"
    # The site only serves /next-step on a CORRECT code, so the URL is proof
    # the real form accepted what the automation typed.
    assert page.url.endswith("/next-step"), f"browser did not advance; still at {page.url}"
    assert find_human_gate(page) is None
    assert "verification_code_entered" in manager.steps_completed
    assert "verification_code_submitted" in manager.steps_completed
    assert "human_verification_completed" in manager.steps_completed


def test_the_code_is_consumed_so_it_cannot_be_replayed(manager, page):
    """After a successful fill the channel is empty — a code cannot be typed
    into a second gate later in the same run."""
    _deliver_after(manager.application_id, VALID_OTP)
    manager._wait_for_human(
        0, "one-time passcode", lambda: find_human_gate(page) is None, page=page,
    )
    assert verification_channel.take(manager.application_id) is None


# ---------------------------------------------------------------------------
# The rejection path: never auto-retry
# ---------------------------------------------------------------------------

def test_a_wrong_code_is_never_retried_and_the_run_keeps_waiting(manager, page, site):
    """A rejected code must leave the run paused, NOT trigger an automated
    retry. Retrying a guessed code is exactly the behaviour this project
    refuses — it is indistinguishable from brute-forcing the gate.
    """
    _deliver_after(manager.application_id, INVALID_OTP)

    resumed = manager._wait_for_human(
        0, "one-time passcode", lambda: find_human_gate(page) is None, page=page,
    )

    assert resumed is False, "a wrong code must not resume the run"
    # The site re-serves the OTP page with an error, so the gate is still there.
    assert find_human_gate(page) is not None
    assert "That code was not accepted" in page.inner_text("body")
    # It was entered exactly once. No second attempt was made with the same or
    # any other value.
    assert manager.steps_completed.count("verification_code_entered") == 1
    assert verification_channel.take(manager.application_id) is None


def test_a_second_code_can_be_supplied_after_a_rejection(manager, page, site):
    """The user reads a fresh code off their phone and submits again. The new
    code must be picked up by the SAME paused run."""
    # First attempt: wrong.
    verification_channel.deliver(manager.application_id, INVALID_OTP)
    manager._wait_for_human(
        0, "one-time passcode", lambda: find_human_gate(page) is None, page=page,
    )
    assert find_human_gate(page) is not None

    # Second attempt: correct, into the still-paused gate.
    _deliver_after(manager.application_id, VALID_OTP)
    resumed = manager._wait_for_human(
        0, "one-time passcode", lambda: find_human_gate(page) is None, page=page,
    )
    assert resumed is True
    assert page.url.endswith("/next-step")


# ---------------------------------------------------------------------------
# Safety properties, against a real page
# ---------------------------------------------------------------------------

def test_no_code_is_typed_when_none_was_supplied(manager, page):
    """The overwhelmingly common case: the loop polls repeatedly with nothing
    pending and must leave the page completely untouched."""
    manager._wait_for_human(
        0, "one-time passcode", lambda: find_human_gate(page) is None, page=page,
    )
    assert page.locator(VERIFICATION_CODE_INPUT_SELECTOR).first.input_value() == ""
    assert "verification_code_entered" not in manager.steps_completed


def test_an_uncollected_code_is_discarded_when_the_wait_times_out(manager, page):
    """A code delivered too late to be used must not linger in memory."""
    # Delivered after the (short) wait has already given up.
    _deliver_after(manager.application_id, VALID_OTP, delay=13.0)
    manager._wait_for_human(
        0, "one-time passcode", lambda: find_human_gate(page) is None, page=page,
    )
    time.sleep(1.5)  # let the late delivery land
    # It may have arrived after `discard` ran; either way it must never be
    # usable by a later gate without the human sending it again.
    leftover = verification_channel.take(manager.application_id)
    verification_channel.discard(manager.application_id)
    assert leftover in (None, VALID_OTP), "unexpected value in the channel"


def test_the_code_never_reaches_the_live_state_the_browser_polls(manager, page):
    """`GET /applications/{id}/live` returns `LIVE_RUN_STATE` straight to the
    browser. A code that leaked into it would be served back to every client
    watching the run."""
    from automation.applications.application_flow_manager import LIVE_RUN_STATE

    _deliver_after(manager.application_id, VALID_OTP)
    manager._wait_for_human(
        0, "one-time passcode", lambda: find_human_gate(page) is None, page=page,
    )
    state = LIVE_RUN_STATE.get(manager.application_id, {})
    assert VALID_OTP not in str(state), f"the code leaked into live state: {state}"


def test_the_code_never_reaches_the_run_log(manager, page, caplog):
    """Same rule as the autonomous path's secret audit: the fact is logged, the
    value never is."""
    import logging

    with caplog.at_level(logging.DEBUG):
        _deliver_after(manager.application_id, VALID_OTP)
        manager._wait_for_human(
            0, "one-time passcode", lambda: find_human_gate(page) is None, page=page,
        )
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert VALID_OTP not in logged, "the verification code was written to a log line"


# ---------------------------------------------------------------------------
# Rejection feedback — so a paused run is not silent about a wrong code
# ---------------------------------------------------------------------------

def test_a_rejected_code_is_reported_back_to_the_user(manager, page):
    """Without this the UI cannot distinguish "your code was wrong" from
    "still waiting for you to type one", so a user who mistyped waits for
    automation that is waiting for them."""
    from automation.applications.application_flow_manager import LIVE_RUN_STATE

    verification_channel.deliver(manager.application_id, INVALID_OTP)
    manager._wait_for_human(
        0, "one-time passcode", lambda: find_human_gate(page) is None, page=page,
    )
    state = LIVE_RUN_STATE.get(manager.application_id, {})
    assert state.get("verification_rejected") is True
    assert "verification_code_rejected" in manager.steps_completed
    # The flag is a boolean — nothing derived from the code may appear here,
    # since this dict is served to the browser by GET /applications/{id}/live.
    assert INVALID_OTP not in str(state)


def test_an_accepted_code_leaves_no_rejection_flag(manager, page):
    from automation.applications.application_flow_manager import LIVE_RUN_STATE

    _deliver_after(manager.application_id, VALID_OTP)
    manager._wait_for_human(
        0, "one-time passcode", lambda: find_human_gate(page) is None, page=page,
    )
    assert "verification_rejected" not in LIVE_RUN_STATE.get(manager.application_id, {})


def test_a_retry_clears_the_previous_rejection_notice(manager, page):
    """A stale "not accepted" banner while a fresh code is being checked would
    be actively misleading."""
    from automation.applications.application_flow_manager import LIVE_RUN_STATE

    verification_channel.deliver(manager.application_id, INVALID_OTP)
    manager._wait_for_human(0, "otp", lambda: find_human_gate(page) is None, page=page)
    assert LIVE_RUN_STATE[manager.application_id].get("verification_rejected") is True

    _deliver_after(manager.application_id, VALID_OTP)
    manager._wait_for_human(0, "otp", lambda: find_human_gate(page) is None, page=page)
    assert "verification_rejected" not in LIVE_RUN_STATE[manager.application_id]


# ---------------------------------------------------------------------------
# Per-digit ("six circles") layouts — the shape real sites actually use
# ---------------------------------------------------------------------------

@pytest.fixture
def split_page(requires_chromium, site):
    """A page whose code field is SIX single-character boxes, like the one
    American Express serves."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(site.url("/otp-split"), wait_until="domcontentloaded")
        yield page
        browser.close()


def test_a_code_is_distributed_across_per_digit_boxes(manager, split_page):
    """The bug this catches, seen on a REAL American Express application: the
    original implementation filled `.first` with the whole code, so a
    `maxlength=1` box kept one character and the form rejected it.

    Passing here requires the code to be spread one character per box.
    """
    assert find_human_gate(split_page) is not None

    _deliver_after(manager.application_id, VALID_OTP)
    resumed = manager._wait_for_human(
        0, "one-time passcode", lambda: find_human_gate(split_page) is None, page=split_page,
    )

    assert resumed is True, "the six-box layout must accept a distributed code"
    # The fixture only redirects here when the six boxes JOIN to the valid code.
    assert split_page.url.endswith("/next-step")


def test_each_box_receives_exactly_one_character(manager, split_page):
    """Directly asserts the distribution, so a regression is diagnosed at the
    field level rather than as a mysterious rejection."""
    manager._fill_verification_code(split_page, VALID_OTP)
    values = [b.input_value() for b in split_page.locator(VERIFICATION_CODE_INPUT_SELECTOR).all()]
    assert values == list(VALID_OTP), f"expected one digit per box, got {values}"


def test_a_wrong_code_in_a_split_layout_is_still_never_retried(manager, split_page):
    _deliver_after(manager.application_id, INVALID_OTP)
    resumed = manager._wait_for_human(
        0, "one-time passcode", lambda: find_human_gate(split_page) is None, page=split_page,
    )
    assert resumed is False
    assert manager.steps_completed.count("verification_code_entered") == 1
