"""
Escalating a CAPABILITY gap to a human, against a real browser.

Autogram already pauses for security gates (CAPTCHA, OTP, login). It did not
pause when it simply could not complete a page — a required field that
exhausted its retries, or a repeating "Add Experience"/"Add Skill" section it
cannot drive. It ended the run with `manual_required` instead, abandoning a
browser that was sitting on exactly the page a human could have finished in
seconds.

These tests pin the new behaviour AND the invariant that matters more: a page
that stays blocked must never be clicked past.
"""

from __future__ import annotations

import uuid

import pytest

from automation.applications.application_flow_manager import ApplicationFlowManager
from automation.browser.selectors import find_unfilled_required_fields
from automation.tests.fixtures.hitl_test_site import HitlTestSite


@pytest.fixture(scope="module")
def site():
    with HitlTestSite() as s:
        yield s


@pytest.fixture
def manager():
    """`reasons` collects everything the run reported to a human.

    Captured through `on_waiting_for_human` — the real production callback,
    which is what persists `failure_reason` — rather than by reading
    `LIVE_RUN_STATE` afterwards. That dict is transient: a wait that clears
    correctly REMOVES the reason, so asserting on it after the fact tested
    whether the page had cleared, not what the user was told.
    """
    reasons: list[str] = []
    m = ApplicationFlowManager(
        on_waiting_for_human=reasons.append,
        application_id=f"gap_{uuid.uuid4().hex[:8]}", user_id="u1",
        job_url="http://127.0.0.1/apply", ats_platform="custom", adapter_cls=None,
        profile=None, resume_document=None, autopilot_enabled=False, headless=True,
        human_wait_timeout_s=12,
    )
    m.reported_reasons = reasons
    return m


@pytest.fixture
def blocked_page(requires_chromium, site):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(site.url("/blocked"), wait_until="domcontentloaded")
        yield page
        browser.close()


class _Nav:
    """Stands in for a NavigationOutcome that reported failure."""

    def __init__(self, reason="the Next button did nothing", errors=None, click_failed=False):
        self.advanced = False
        self.reason = reason
        self.validation_errors = errors or []
        self.click_failed = click_failed


def test_the_page_really_is_blocked_to_begin_with(blocked_page):
    """Guards the fixture itself: if this stopped being blocked, every test
    below would pass vacuously."""
    assert find_unfilled_required_fields(blocked_page), "fixture should have an unfilled required field"


def test_a_human_filling_the_required_field_unblocks_the_page(manager, blocked_page):
    """The point of the change: the run pauses, the human fills the field in
    the open browser, and the wait reports the page as advanceable."""
    # Scheduled in the PAGE, not in a Python thread: Playwright's sync API is
    # thread-affine, and filling from another thread raises `greenlet.error`.
    # This also models reality more closely — a human typing into the open
    # browser while the run polls.
    blocked_page.evaluate(
        "setTimeout(() => { document.querySelector('#req').value = '3 years'; }, 1500)"
    )

    unblocked = manager._wait_for_human_to_unblock_page(blocked_page, 0, _Nav())

    assert unblocked is True
    assert not find_unfilled_required_fields(blocked_page)
    assert "waiting_for_human" in manager.steps_completed


def test_an_unresolved_block_times_out_and_never_advances(manager, blocked_page):
    """The safety invariant. If nobody fixes it, the run must NOT decide to
    click Next anyway — that would submit a page the form itself rejects."""
    unblocked = manager._wait_for_human_to_unblock_page(blocked_page, 0, _Nav())

    assert unblocked is False
    assert find_unfilled_required_fields(blocked_page), "still blocked, as expected"
    # Still on the same page — nothing was clicked past.
    assert blocked_page.url.rstrip("/").endswith("/blocked")


def test_the_reason_names_what_is_missing_so_the_human_knows_what_to_do(manager, blocked_page):
    """A pause that says only "could not continue" makes the user hunt. The
    live reason must name the field."""
    manager._wait_for_human_to_unblock_page(blocked_page, 0, _Nav())
    reason = " ".join(manager.reported_reasons)
    assert "page 1" in reason
    assert "years" in reason.lower() or "still empty" in reason.lower()


def test_an_unsupported_widget_is_explained_rather_than_reported_as_empty(manager, blocked_page):
    """When no required field is detectably empty but the control will not
    click, the cause is usually a widget the adapter cannot drive — Amex's
    repeating "Add Experience" sections. Saying so beats a blank reason."""
    blocked_page.fill("#req", "filled")  # no missing field left to blame
    manager._wait_for_human_to_unblock_page(
        blocked_page, 0, _Nav(click_failed=True),
    )
    reason = " ".join(manager.reported_reasons)
    assert "Add Experience" in reason or "repeating sections" in reason


def test_validation_errors_are_passed_through_verbatim(manager, blocked_page):
    manager._wait_for_human_to_unblock_page(
        blocked_page, 0, _Nav(errors=["Postal code is not valid for India"]),
    )
    reason = " ".join(manager.reported_reasons)
    assert "Postal code is not valid for India" in reason
