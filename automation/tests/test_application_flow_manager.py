"""
ApplicationFlowManager (automation/applications/application_flow_manager.py)
— Phase 4.

`decide_action()` and `_aggregate_confidence()` are pure functions and are
tested directly with no browser involved. The orchestration logic (CAPTCHA
short-circuit, the multi-page cycle and its safety backstop, and the final
decision -> status mapping) is tested end-to-end against a real (but headless,
network-free) Playwright browser, using `data:` URLs the same way
test_detector.py does, and a small in-test fake ATSAdapter so the test doesn't
depend on any specific ATS's real markup.

The multi-page tests near the bottom of this file drive a `data:` URL form
whose inline JS turns the page for real — heading changes, old fields go away,
new ones appear, the button relabels itself to "Submit" at the end. That is
what makes them worth having: a form that behaves like a real one is the only
kind that can catch the failure this loop was rebuilt for, where a click that
never advanced anything was scored as a completed step.

These end-to-end tests use the `requires_chromium` fixture from conftest.py
purely to trigger its "skip if Chromium isn't installed" logic —
ApplicationFlowManager launches its own BrowserManager/browser internally.
They deliberately do NOT use the session-scoped `browser` fixture: it keeps
its own `sync_playwright()` context open for the whole test session once
anything requests it, which collides ("...you are using Playwright Sync API
inside the asyncio loop...") with the separate `sync_playwright()` instance
ApplicationFlowManager's own BrowserManager starts — see `requires_chromium`'s
docstring.

Each test also builds its own `BrowserManager` with a `tmp_path`-backed
`SessionStore` (via `_isolated_browser_manager`) rather than letting
ApplicationFlowManager construct one with the real default session
directory: `_finish_browser_session` saves a session after every real run
regardless of outcome, and multiple tests below share the same
(`user_id="user-1"`, `ats_platform="greenhouse"`) pair test_browser_manager.py
also uses — without this, a real run here would leave a real, encrypted
session file on disk that makes unrelated BrowserManager tests
(e.g. "no session exists yet") fail depending on what ran earlier in the
same pytest session.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, ProfileDocument
from automation.ats.base import ATSAdapter, FieldFillResult
from automation.browser.browser_manager import BrowserManager
from automation.browser.session import SessionStore
from automation.applications.application_flow_manager import (
    _OPEN_REVIEW_SESSIONS,
    ApplicationFlowManager,
    AUTO_SUBMIT_CONFIDENCE_THRESHOLD,
    NEEDS_REVIEW_CONFIDENCE_THRESHOLD,
    PUBLIC_ATS_PLATFORMS,
    REVIEW_STATUSES,
    close_review_session,
    decide_action,
    list_open_review_sessions,
    should_keep_browser_open,
)


# ---------------------------------------------------------------------------
# Pure logic: decide_action() / _aggregate_confidence() — no browser needed
# ---------------------------------------------------------------------------

def test_decide_action_auto_submits_when_all_conditions_met():
    assert "greenhouse" in PUBLIC_ATS_PLATFORMS
    action = decide_action(confidence=0.95, ats_platform="greenhouse", autopilot_enabled=True)
    assert action == "AUTO_SUBMIT"


def test_decide_action_requires_autopilot_opt_in():
    action = decide_action(confidence=0.95, ats_platform="greenhouse", autopilot_enabled=False)
    assert action == "COPILOT_REVIEW"


def test_decide_action_requires_a_public_ats():
    assert "workday" not in PUBLIC_ATS_PLATFORMS
    action = decide_action(confidence=0.95, ats_platform="workday", autopilot_enabled=True)
    assert action == "COPILOT_REVIEW"


def test_decide_action_needs_review_below_the_low_threshold():
    action = decide_action(
        confidence=NEEDS_REVIEW_CONFIDENCE_THRESHOLD - 0.01,
        ats_platform="greenhouse",
        autopilot_enabled=True,
    )
    assert action == "NEEDS_REVIEW"


# ---------------------------------------------------------------------------
# Pure logic: should_keep_browser_open() — the manual-review handoff decision
# ---------------------------------------------------------------------------

def test_keeps_the_browser_open_for_every_review_status_when_visible():
    for status in REVIEW_STATUSES:
        assert should_keep_browser_open(headless=False, status=status) is True


def test_never_keeps_a_headless_browser_open_regardless_of_status():
    # No point leaving an invisible browser running — and this is what keeps
    # every existing (headless) test/run behaving exactly as before.
    for status in REVIEW_STATUSES | {"applied", "failed"}:
        assert should_keep_browser_open(headless=True, status=status) is False


def test_closes_a_visible_browser_for_non_review_outcomes():
    assert should_keep_browser_open(headless=False, status="applied") is False
    assert should_keep_browser_open(headless=False, status="failed") is False


def test_application_flow_manager_passes_headless_through_to_its_own_browser_manager():
    # No injected browser_manager — ApplicationFlowManager builds its own,
    # and `headless` must reach it. Constructing this doesn't launch a
    # browser (BrowserManager.__init__ just stores the setting), so this
    # needs no real Chromium at all.
    manager = ApplicationFlowManager(
        application_id="app-1",
        user_id="user-1",
        job_url="data:text/html,<html></html>",
        ats_platform="greenhouse",
        adapter_cls=object,  # never instantiated in this test
        profile=None,
        resume_document=None,
        headless=False,
    )
    assert manager.browser_manager.headless is False


# ---------------------------------------------------------------------------
# Regression: a browser "kept open" must actually stay referenced somewhere.
#
# `should_keep_browser_open` returning True only means `run()` skips calling
# `.close()` — on its own that's NOT enough to guarantee the browser stays
# usable. `run()` executes inside a FastAPI BackgroundTasks call; once it
# returns, every local variable (the ApplicationFlowManager, its
# BrowserManager, the underlying Playwright driver connection) goes out of
# scope. Without something holding a real Python reference, that's eligible
# for garbage collection immediately — which is exactly the kind of silent
# failure that could look like "the browser closed anyway" even though
# `.close()` was correctly never called. `_OPEN_REVIEW_SESSIONS` is that
# reference; these tests use an injected mock BrowserManager (no real
# Chromium needed) to verify `_finish_browser_session` actually populates it.
# ---------------------------------------------------------------------------

def _fake_flow_manager(application_id: str, fake_browser_manager) -> ApplicationFlowManager:
    return ApplicationFlowManager(
        application_id=application_id,
        user_id="user-1",
        job_url="data:text/html,<html></html>",
        ats_platform="greenhouse",
        adapter_cls=object,  # never instantiated in these tests
        profile=None,
        resume_document=None,
        browser_manager=fake_browser_manager,
    )


def test_finish_browser_session_registers_the_browser_when_kept_open():
    fake_browser_manager = MagicMock()
    fake_browser_manager.headless = False
    manager = _fake_flow_manager("app-registry-open", fake_browser_manager)

    try:
        manager._finish_browser_session(context=MagicMock(), status="copilot_review")

        assert "app-registry-open" in list_open_review_sessions()
        fake_browser_manager.close.assert_not_called()
    finally:
        _OPEN_REVIEW_SESSIONS.pop("app-registry-open", None)


def test_finish_browser_session_closes_and_does_not_register_for_non_review_status():
    fake_browser_manager = MagicMock()
    fake_browser_manager.headless = False
    manager = _fake_flow_manager("app-registry-closed", fake_browser_manager)

    manager._finish_browser_session(context=MagicMock(), status="applied")

    fake_browser_manager.close.assert_called_once()
    assert "app-registry-closed" not in list_open_review_sessions()


def test_close_review_session_closes_and_forgets_a_registered_browser():
    fake_browser_manager = MagicMock()
    _OPEN_REVIEW_SESSIONS["app-registry-manual-close"] = fake_browser_manager

    try:
        assert close_review_session("app-registry-manual-close") is True
        fake_browser_manager.close.assert_called_once()
        assert "app-registry-manual-close" not in list_open_review_sessions()
    finally:
        _OPEN_REVIEW_SESSIONS.pop("app-registry-manual-close", None)


def test_close_review_session_returns_false_when_nothing_is_open():
    assert close_review_session("no-such-application-id") is False


def test_decide_action_copilot_review_in_the_middle_band():
    mid_confidence = (NEEDS_REVIEW_CONFIDENCE_THRESHOLD + AUTO_SUBMIT_CONFIDENCE_THRESHOLD) / 2
    action = decide_action(confidence=mid_confidence, ats_platform="greenhouse", autopilot_enabled=True)
    assert action == "COPILOT_REVIEW"


def test_aggregate_confidence_is_zero_for_no_tracked_fields():
    assert ApplicationFlowManager._aggregate_confidence([]) == 0.0


def test_aggregate_confidence_is_the_filled_fraction():
    results = [
        FieldFillResult("a", "a", "x", 0.9, True),
        FieldFillResult("b", "b", "y", 0.9, True),
        FieldFillResult("c", "c", None, 0.0, False),
        FieldFillResult("d", "d", None, 0.0, False),
    ]
    assert ApplicationFlowManager._aggregate_confidence(results) == 0.5


# ---------------------------------------------------------------------------
# Fixtures shared by the end-to-end tests
# ---------------------------------------------------------------------------

def _profile() -> CandidateProfile:
    profile = CandidateProfile(
        profile_id="profile-1",
        user_id="user-1",
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
    )
    profile.phone_encrypted = encrypt_field("+1-555-0100")
    return profile


def _resume_document(path) -> ProfileDocument:
    return ProfileDocument(
        document_id="doc-1",
        profile_id="profile-1",
        document_type="resume",
        original_filename="resume.pdf",
        stored_path=str(path),
        file_hash="abc123",
        is_default=True,
    )


# What a real ATS does that a `return True` doesn't: it CHANGES THE PAGE.
# Greenhouse and Lever both replace the form with a success banner (and usually
# navigate) once a submit is accepted, and `wait_for_submission_confirmation`
# exists precisely to look for that. A fake `submit_application` that returned
# `True` while leaving the DOM and URL byte-identical was therefore simulating
# something no ATS does — a submit with zero observable effect — and the run
# correctly came out `needs_review` ("clicked, but cannot prove it landed").
# The heading below matches `SUBMISSION_CONFIRMATION_TEXT_PATTERNS`' "thank you
# for applying", so the submit path here exercises the real detection code
# rather than a page that never changes.
_FAKE_SUBMIT_CONFIRMATION_HTML = (
    "<html><body><h1>Thank you for applying!</h1>"
    "<p>We have received your application and will be in touch.</p></body></html>"
)


def _make_fake_adapter_cls(
    fill_results,
    *,
    upload_ok=True,
    submit_ok=True,
    submit_confirmation_html=_FAKE_SUBMIT_CONFIRMATION_HTML,
):
    """A minimal ATSAdapter that touches `self.page` only to simulate a
    submit's page change (see `_FAKE_SUBMIT_CONFIRMATION_HTML`) — used to
    isolate ApplicationFlowManager's own orchestration logic (loop, decision,
    checkpoints) from any real ATS's markup, which is already covered by
    test_greenhouse_adapter.py / test_lever_adapter.py. `calls` records what
    the flow manager invoked, for assertions.

    Pass `submit_confirmation_html=None` to model an ATS that accepts the click
    but shows nothing recognizable afterwards — the unconfirmed-submission case,
    which is a deliberate scenario rather than the default."""

    calls = {"upload_resume": 0, "fill_personal_information": 0, "answer_questions": 0, "submit_application": 0}

    class _FakeAdapter(ATSAdapter):
        name = "fake"

        def detect(self) -> float:
            return 1.0

        def upload_resume(self) -> bool:
            calls["upload_resume"] += 1
            return upload_ok

        def fill_personal_information(self):
            calls["fill_personal_information"] += 1
            return fill_results

        def answer_questions(self):
            calls["answer_questions"] += 1
            return []

        def submit_application(self) -> bool:
            calls["submit_application"] += 1
            if not submit_ok:
                # A submit that didn't go through leaves the form on screen —
                # so, correctly, no page change here either.
                return False
            if submit_confirmation_html is not None:
                self.page.set_content(submit_confirmation_html)
            return True

    _FakeAdapter.calls = calls
    return _FakeAdapter


_HIGH_CONFIDENCE_RESULTS = [
    FieldFillResult("first_name", "first_name", "Ada", 0.95, True),
    FieldFillResult("email", "email", "ada@example.com", 0.95, True),
]

def _isolated_browser_manager(tmp_path, ats_platform="greenhouse") -> BrowserManager:
    """A real `BrowserManager` (so these tests still exercise a genuine
    Playwright launch/navigate/close cycle) backed by a `tmp_path`-local
    `SessionStore` instead of the real default session directory — see this
    module's docstring for why that isolation matters here specifically.

    `browser_mode="launch"` pins these tests to a throwaway headless browser
    rather than the production default (`cdp`, which attaches to the developer's
    own running Chrome). Two reasons, both about determinism: attaching would
    make the suite's outcome depend on whether Chrome happens to be running with
    a debug port on this machine, and it would open real tabs — including a
    deliberately-left-open one for every `copilot_review` test — in the browser
    the developer is using. Attach/fallback selection itself is covered by
    `test_browser_attach.py`, which doesn't need a browser to do it."""
    return BrowserManager(
        user_id="user-1",
        ats_platform=ats_platform,
        browser_mode="launch",
        session_store=SessionStore(base_dir=tmp_path / "sessions"),
    )


_NO_CAPTCHA_NO_NEXT_HTML = "data:text/html,<html><body><p>Single-step application form.</p></body></html>"
# Explicit size: an unstyled, empty <div> renders at zero height (no content,
# no default height), which `page_has_captcha` now (correctly) treats as a
# dormant/invisible widget rather than a real challenge — see
# `automation/browser/selectors.py::page_has_captcha`'s docstring. Sized here
# to represent one actually being presented (a real reCAPTCHA checkbox widget
# renders at roughly this size), matching this test's actual intent.
_CAPTCHA_HTML = (
    "data:text/html,<html><body>"
    "<div class='g-recaptcha' style='width:304px;height:78px'></div>"
    "<p>Verify you're human.</p></body></html>"
)
_ALWAYS_NEXT_HTML = "data:text/html,<html><body><button>Next</button></body></html>"


# ---------------------------------------------------------------------------
# End-to-end orchestration tests (real headless browser, data: URLs)
# ---------------------------------------------------------------------------

def test_run_auto_submits_on_high_confidence_public_ats(requires_chromium, tmp_path):
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-auto-1",
        user_id="user-1",
        job_url=_NO_CAPTCHA_NO_NEXT_HTML,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
    )

    result = manager.run()

    assert result.status == "applied"
    assert result.ats_platform == "greenhouse"
    assert result.confidence == 1.0
    assert result.trace_path is not None  # confirms the full pipeline, including tracing, ran for real
    assert adapter_cls.calls["submit_application"] == 1
    assert "decision_auto_submit" in manager.steps_completed
    assert "navigated" in manager.steps_completed
    assert "resume_uploaded" in manager.steps_completed
    assert "step_0_filled" in manager.steps_completed
    assert "step_0_advanced" not in manager.steps_completed  # no Next control on this page


def test_run_holds_for_copilot_review_when_autopilot_is_off(requires_chromium, tmp_path):
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-copilot-1",
        user_id="user-1",
        job_url=_NO_CAPTCHA_NO_NEXT_HTML,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=False,
    )

    result = manager.run()

    assert result.status == "copilot_review"
    assert adapter_cls.calls["submit_application"] == 0
    assert "decision_copilot_review" in manager.steps_completed


def test_run_stops_before_filling_anything_when_captcha_is_present(requires_chromium, tmp_path):
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-captcha-1",
        user_id="user-1",
        job_url=_CAPTCHA_HTML,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
    )

    result = manager.run()

    assert result.status == "manual_required"
    assert result.confidence == 0.0
    assert adapter_cls.calls["fill_personal_information"] == 0
    assert adapter_cls.calls["upload_resume"] == 0
    assert manager.steps_completed == ["navigated", "captcha_detected"]


def test_run_stops_immediately_when_clicking_next_does_not_advance(requires_chromium, tmp_path):
    """A "Next" button that never leads anywhere — a broken selector, or a form
    silently rejecting the page — must cost ONE attempt, not a full loop.

    This is the regression that made long applications unworkable. The old loop
    couldn't tell a successful Next from a rejected one, so it refilled the same
    page up to its cap and then declared the page it had never left to be the
    final step, running the whole auto-submit decision against page 1 of N.
    Navigation is verified now (see `page_navigator.advance_to_next_page`), so
    a page that doesn't move ends the run and says so."""
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-cap-1",
        user_id="user-1",
        job_url=_ALWAYS_NEXT_HTML,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
    )

    result = manager.run()

    # One page processed, one fill pass. Not MAX_PAGES of them.
    assert adapter_cls.calls["fill_personal_information"] == 1
    assert "step_0_filled" in manager.steps_completed
    assert "step_0_advanced" not in manager.steps_completed
    assert "navigation_blocked" in manager.steps_completed

    # And it is handed to a human rather than scored as a finished application.
    assert result.status == "manual_required"
    assert adapter_cls.calls["submit_application"] == 0


def test_page_cap_is_a_backstop_not_an_assumed_form_length(requires_chromium, tmp_path):
    """The cap exists only so a mis-detected control can't loop forever. It is
    deliberately far above any real application's length — nothing in the loop
    derives from it, and a form ends when the adapter says so."""
    assert ApplicationFlowManager.MAX_PAGES >= 20
    assert ApplicationFlowManager.MAX_STEPS == ApplicationFlowManager.MAX_PAGES


def test_run_reports_failure_status_when_navigation_fails(requires_chromium, monkeypatch, tmp_path):
    # Skip BrowserManager's own retry/backoff (already covered by
    # test_browser_manager.py) so this test fails fast instead of waiting
    # through real retry sleeps.
    monkeypatch.setattr(BrowserManager, "run_with_retries", lambda self, action, **kwargs: action())

    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-fail-1",
        user_id="user-1",
        job_url="not-a-real-url",
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=False,
    )

    result = manager.run()

    assert result.status == "failed"
    assert result.confidence == 0.0
    assert adapter_cls.calls["fill_personal_information"] == 0


_REQUIRED_FIELD_MISSING_HTML = (
    "data:text/html,<html><body><input type='text' name='linkedin_url' required></body></html>"
)
_REQUIRED_FIELD_PREFILLED_HTML = (
    "data:text/html,<html><body>"
    "<input type='text' name='full_name' value='Ada Lovelace' required>"
    "</body></html>"
)


def test_run_marks_manual_required_when_a_required_field_has_no_available_value(requires_chromium, tmp_path):
    # The fake adapter never touches this input (it's not one of the profile
    # fields it fills) — simulating a field the candidate's profile simply
    # has no value for. `run()` must catch this via the fresh post-fill DOM
    # scan (`find_unfilled_required_fields`), not just a low confidence score.
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-required-missing-1",
        user_id="user-1",
        job_url=_REQUIRED_FIELD_MISSING_HTML,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
    )

    result = manager.run()

    assert result.status == "manual_required"
    # `error_log` is a path (§14 — screenshots/trace/error-log are always
    # persisted as file paths, never inline text, same as every other status
    # that writes one); the actual reason lives in the file it points to.
    assert result.error_log is not None
    assert "linkedin_url" in Path(result.error_log).read_text(encoding="utf-8")
    assert adapter_cls.calls["submit_application"] == 0  # never submits with a required field missing
    assert "manual_required_missing_fields" in manager.steps_completed


def test_run_does_not_flag_manual_required_when_the_required_field_already_has_a_value(requires_chromium, tmp_path):
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-required-filled-1",
        user_id="user-1",
        job_url=_REQUIRED_FIELD_PREFILLED_HTML,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
    )

    result = manager.run()

    assert result.status != "manual_required"


_CONFIRMED_SUBMISSION_HTML = (
    "data:text/html,<html><body><h1>Thank you for applying!</h1></body></html>"
)
_UNCONFIRMED_SUBMISSION_HTML = (
    "data:text/html,<html><body><p>Nothing here confirms anything.</p></body></html>"
)
_VERIFICATION_ERROR_HTML = (
    "data:text/html,<html><body>"
    "<div class='application-error'>There was an error verifying your application. Please try again.</div>"
    "</body></html>"
)


def test_run_reports_applied_only_when_submission_is_confirmed(requires_chromium, tmp_path):
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-confirmed-1",
        user_id="user-1",
        job_url=_CONFIRMED_SUBMISSION_HTML,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
    )

    result = manager.run()

    assert result.status == "applied"
    assert adapter_cls.calls["submit_application"] == 1
    assert "submission_confirmed" in manager.steps_completed


def test_run_does_not_claim_applied_when_submission_cannot_be_confirmed(requires_chromium, tmp_path):
    """The regression that matters most: a submit click landing is NOT proof
    the ATS accepted the application. Claiming `applied` here would put a
    false record in front of the candidate AND let idempotency block a retry.

    `submit_confirmation_html=None` is the whole point of this test: the click
    lands and the adapter reports success, but the page shows nothing that
    proves the ATS accepted it."""
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS, submit_confirmation_html=None)
    manager = ApplicationFlowManager(
        application_id="app-unconfirmed-1",
        user_id="user-1",
        job_url=_UNCONFIRMED_SUBMISSION_HTML,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
    )

    result = manager.run()

    assert result.status != "applied"
    assert result.status == "needs_review"
    assert adapter_cls.calls["submit_application"] == 1  # it did try
    assert "submit_clicked" in manager.steps_completed
    assert "submission_unconfirmed" in manager.steps_completed
    assert result.error_log is not None
    logged = Path(result.error_log).read_text(encoding="utf-8")
    assert "no confirmation" in logged
    assert "double-apply" in logged  # warns a human before they retry


def test_run_blocks_submission_while_a_validation_error_is_visible(requires_chromium, tmp_path):
    """The real Lever banner from a live run. Both specs gate submission on
    zero visible validation errors — this must never be submitted over."""
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-validation-1",
        user_id="user-1",
        job_url=_VERIFICATION_ERROR_HTML,
        ats_platform="lever",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path, ats_platform="lever"),
        autopilot_enabled=True,
    )

    result = manager.run()

    assert result.status == "needs_review"
    assert adapter_cls.calls["submit_application"] == 0  # never submitted over an error
    assert "needs_review_validation_errors" in manager.steps_completed
    assert "error verifying your application" in Path(result.error_log).read_text(encoding="utf-8")


def test_run_stops_on_a_login_wall_before_filling_anything(requires_chromium, tmp_path):
    """A visible password field means an account wall — a human-only gate,
    never transacted with automatically (no account creation, no third-party
    passwords)."""
    login_wall = (
        "data:text/html,<html><body><form>"
        "<input type='password' name='pw'><button>Sign in</button>"
        "</form></body></html>"
    )
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-loginwall-1",
        user_id="user-1",
        job_url=login_wall,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
    )

    result = manager.run()

    assert result.status == "manual_required"
    assert adapter_cls.calls["fill_personal_information"] == 0
    assert adapter_cls.calls["upload_resume"] == 0
    assert "human_gate_detected" in manager.steps_completed
    assert "login" in Path(result.error_log).read_text(encoding="utf-8")


def test_run_passes_the_answer_engine_through_to_the_adapter_it_constructs(requires_chromium, tmp_path):
    """Phase 6: ApplicationFlowManager never calls `answer_engine` itself —
    it's a pure pass-through into whatever adapter `run()` constructs. Not
    passing one (the default, `None`) is covered by every other end-to-end
    test above, all of which construct `ApplicationFlowManager` without it."""
    seen_engines = []

    class _RecordingAdapter(ATSAdapter):
        name = "fake"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen_engines.append(self.answer_engine)

        def detect(self) -> float:
            return 1.0

        def upload_resume(self) -> bool:
            return True

        def fill_personal_information(self):
            return []

        def answer_questions(self):
            return []

        def submit_application(self) -> bool:
            return True

    sentinel_engine = object()
    manager = ApplicationFlowManager(
        application_id="app-answer-engine-1",
        user_id="user-1",
        job_url=_NO_CAPTCHA_NO_NEXT_HTML,
        ats_platform="greenhouse",
        adapter_cls=_RecordingAdapter,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
        answer_engine=sentinel_engine,
    )

    manager.run()

    assert seen_engines == [sentinel_engine]


# ---------------------------------------------------------------------------
# Vision fallback + résumé re-check (see automation/forms/vision_fallback.py and
# ATSAdapter.ensure_resume_attached). These exercise the flow manager's own
# bookkeeping directly rather than through a real run: what's worth pinning down
# here is how the two passes' outcomes are folded into the confidence score and
# the missing-required decision, which is pure logic.
# ---------------------------------------------------------------------------

def _bare_manager(tmp_path, **kwargs) -> ApplicationFlowManager:
    """A manager with a mocked BrowserManager — never launches a browser. For
    the helper methods below, which take the page/adapter they act on as
    arguments."""
    return ApplicationFlowManager(
        application_id="app-helpers-1",
        user_id="user-1",
        job_url="about:blank",
        ats_platform="greenhouse",
        adapter_cls=_make_fake_adapter_cls([]),
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=MagicMock(),
        **kwargs,
    )


class _VisionAdapterStub:
    """Just enough adapter for the flow manager's two post-fill helpers."""

    def __init__(self, *, vision_outcome=None, attached=None):
        from automation.ats.base import VisionPassOutcome

        self.vision_outcome = vision_outcome or VisionPassOutcome()
        self.attached = attached
        self.vision_calls = 0

    def fill_unfilled_fields_with_vision(self, answerer, *, debug_dir=None):
        self.vision_calls += 1
        return self.vision_outcome

    def ensure_resume_attached(self):
        return self.attached


def test_vision_pass_is_skipped_entirely_when_no_answerer_was_injected(tmp_path):
    adapter = _VisionAdapterStub()
    manager = _bare_manager(tmp_path)

    assert manager._run_vision_pass(adapter, []) == set()
    assert adapter.vision_calls == 0


def test_vision_results_replace_an_earlier_passs_failure_instead_of_double_counting(tmp_path):
    """A field the answer engine reported unfilled and the vision pass then
    filled must count ONCE, as filled — otherwise a fully filled form scores
    50% on that field and never reaches auto-submit."""
    from automation.ats.base import VisionPassOutcome

    question = "If yes to the above question, what role?"
    all_results = [
        FieldFillResult("first_name", "first_name", "Ada", 0.95, True),
        FieldFillResult(question, "answer_engine:llm", None, 0.0, False),
    ]
    adapter = _VisionAdapterStub(vision_outcome=VisionPassOutcome(
        results=[FieldFillResult(question, "vision", "N/A", 0.95, True)],
    ))
    manager = _bare_manager(tmp_path, vision_answerer=MagicMock())

    manager._run_vision_pass(adapter, all_results)

    assert len(all_results) == 2
    assert all_results[1].filled is True
    assert all_results[1].value_used == "N/A"
    assert all_results[1].profile_path == "vision"
    assert ApplicationFlowManager._aggregate_confidence(all_results) == 1.0


def test_a_vision_field_no_earlier_pass_saw_is_appended(tmp_path):
    from automation.ats.base import VisionPassOutcome

    all_results = [FieldFillResult("first_name", "first_name", "Ada", 0.95, True)]
    adapter = _VisionAdapterStub(vision_outcome=VisionPassOutcome(
        results=[FieldFillResult("An unlabeled box", "vision", "N/A", 0.9, True)],
    ))
    manager = _bare_manager(tmp_path, vision_answerer=MagicMock())

    manager._run_vision_pass(adapter, all_results)

    assert [r.field_key for r in all_results] == ["first_name", "An unlabeled box"]


def test_vision_pass_returns_the_fields_it_confirmed_already_answered(tmp_path):
    from automation.ats.base import VisionPassOutcome

    adapter = _VisionAdapterStub(vision_outcome=VisionPassOutcome(
        confirmed_already_filled=["candidate-location", "country"],
    ))
    manager = _bare_manager(tmp_path, vision_answerer=MagicMock())

    assert manager._run_vision_pass(adapter, []) == {"candidate-location", "country"}


def test_a_vision_pass_that_raises_leaves_the_run_alone(tmp_path):
    class _Exploding(_VisionAdapterStub):
        def fill_unfilled_fields_with_vision(self, answerer, *, debug_dir=None):
            raise RuntimeError("boom")

    all_results = [FieldFillResult("first_name", "first_name", "Ada", 0.95, True)]
    manager = _bare_manager(tmp_path, vision_answerer=MagicMock())

    assert manager._run_vision_pass(_Exploding(), all_results) == set()
    assert len(all_results) == 1


def test_required_fields_the_vision_pass_read_as_answered_are_not_reported_missing(tmp_path, monkeypatch):
    manager = _bare_manager(tmp_path)
    monkeypatch.setattr(
        "automation.applications.application_flow_manager.find_unfilled_required_fields",
        lambda page: ["candidate-location", "question_37527990002"],
    )

    still_missing = manager._still_missing_required(MagicMock(), {"candidate-location"})

    assert still_missing == ["question_37527990002"]


def test_nothing_is_waived_when_the_vision_pass_confirmed_nothing(tmp_path, monkeypatch):
    manager = _bare_manager(tmp_path)
    monkeypatch.setattr(
        "automation.applications.application_flow_manager.find_unfilled_required_fields",
        lambda page: ["country"],
    )

    assert manager._still_missing_required(MagicMock(), set()) == ["country"]


def test_recheck_marks_the_resume_unfilled_when_the_form_dropped_it(tmp_path):
    """The bug this exists for: the upload verified at the time and the résumé
    was gone by the end. The run must NOT report it as uploaded."""
    resume_result = FieldFillResult("resume_upload", "resume_document", "resume.pdf", 1.0, True)
    manager = _bare_manager(tmp_path)

    manager._recheck_resume(_VisionAdapterStub(attached=False), resume_result)

    assert resume_result.filled is False
    assert resume_result.confidence == 0.0
    assert "resume_lost_after_upload" in manager.steps_completed


def test_recheck_upgrades_a_resume_that_was_re_attached(tmp_path):
    resume_result = FieldFillResult("resume_upload", "resume_document", "resume.pdf", 0.0, False)
    manager = _bare_manager(tmp_path)

    manager._recheck_resume(_VisionAdapterStub(attached=True), resume_result)

    assert resume_result.filled is True
    assert resume_result.confidence == 1.0
    assert "resume_reattached" in manager.steps_completed


def test_recheck_leaves_the_original_result_alone_when_there_is_no_upload_field(tmp_path):
    resume_result = FieldFillResult("resume_upload", "resume_document", "resume.pdf", 1.0, True)
    manager = _bare_manager(tmp_path)

    manager._recheck_resume(_VisionAdapterStub(attached=None), resume_result)

    assert resume_result.filled is True
    assert manager.steps_completed == []


# ---------------------------------------------------------------------------
# Long, multi-page applications (Workday-shaped: 3-5+ pages)
# ---------------------------------------------------------------------------
# The form below turns the page for real: the heading changes, the current
# page's fields are removed from the layout, the next page's appear, and the
# navigation button relabels itself to "Submit" on the last one. Everything the
# page loop relies on — a signature that genuinely differs, a Next control that
# disappears at the end — is therefore being exercised rather than simulated.


def _multi_page_form_url(page_count: int) -> str:
    """A `data:` URL for a `page_count`-page application. Page N holds a field
    named `page_N_field`, so a run that never leaves page 1 is trivially
    distinguishable from one that walked the whole form."""
    sections = "".join(
        f"<div id='p{index}' style=\"display:{'block' if index == 0 else 'none'}\">"
        f"<label for='f{index}'>Question {index}</label>"
        f"<input id='f{index}' name='page_{index}_field'>"
        f"</div>"
        for index in range(page_count)
    )
    # A one-page application shows "Submit" from the outset — there is no step
    # to advance to — exactly as a single-page Greenhouse posting does.
    initial_label = "Submit" if page_count == 1 else "Next"
    return (
        "data:text/html,<html><body>"
        f"<h1 id='hdr'>Step 1 of {page_count}</h1>"
        f"{sections}"
        f"<button id='nav' onclick='turn()'>{initial_label}</button>"
        "<script>"
        f"var i=0; var n={page_count};"
        "function turn(){"
        "document.getElementById('p'+i).style.display='none';"
        "i=i+1;"
        "document.getElementById('p'+i).style.display='block';"
        "document.getElementById('hdr').textContent='Step '+(i+1)+' of '+n;"
        "if(i>=n-1){document.getElementById('nav').textContent='Submit';}"
        "}"
        "</script>"
        "</body></html>"
    )


@pytest.mark.parametrize("page_count", [1, 2, 3, 5])
def test_run_walks_an_application_of_any_length_to_its_final_page(requires_chromium, tmp_path, page_count):
    """The headline requirement: 1, 2, 3, 5 pages — same code, no page count
    assumed anywhere. The old loop could only ever complete the first of these."""
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id=f"app-multi-{page_count}",
        user_id="user-1",
        job_url=_multi_page_form_url(page_count),
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
    )

    result = manager.run()

    # Every page was filled, and every gap between pages was crossed.
    assert adapter_cls.calls["fill_personal_information"] == page_count
    for index in range(page_count):
        assert f"step_{index}_filled" in manager.steps_completed
    for index in range(page_count - 1):
        assert f"step_{index}_advanced" in manager.steps_completed
    assert f"step_{page_count - 1}_advanced" not in manager.steps_completed  # nowhere left to go

    # ...and only then was the application submitted.
    assert result.status == "applied"
    assert adapter_cls.calls["submit_application"] == 1


def test_run_reaches_the_last_page_before_deciding_anything(requires_chromium, tmp_path):
    """A five-page form must be scored on page 5, not page 1. Before navigation
    was verified, a blocked click looked like a successful one and the
    auto-submit decision ran against a form that was four-fifths unanswered."""
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-multi-final",
        user_id="user-1",
        job_url=_multi_page_form_url(5),
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=False,  # copilot: stop, don't submit
    )

    result = manager.run()

    assert result.status == "copilot_review"
    assert adapter_cls.calls["submit_application"] == 0
    assert "step_4_filled" in manager.steps_completed
    assert "decision_copilot_review" in manager.steps_completed


def test_conditional_fields_revealed_by_an_answer_are_filled_before_moving_on(requires_chromium, tmp_path):
    """Answering one question can reveal another ("Do you require sponsorship?"
    -> "Which visa do you hold?"). A single fill pass per page would leave the
    follow-up blank, where it then blocks navigation as a missing required
    field with no indication of why."""
    reveal_calls = {"count": 0}

    class _RevealingAdapter(ATSAdapter):
        name = "fake-revealing"

        def detect(self) -> float:
            return 1.0

        def upload_resume(self) -> bool:
            return True

        def fill_personal_information(self):
            return list(_HIGH_CONFIDENCE_RESULTS)

        def answer_questions(self):
            reveal_calls["count"] += 1
            if reveal_calls["count"] == 1:
                # Simulates a fill that makes a conditional section appear.
                self.page.evaluate("document.getElementById('followup').style.display='block'")
            return []

        def submit_application(self) -> bool:
            self.page.set_content(_FAKE_SUBMIT_CONFIRMATION_HTML)
            return True

    conditional_form = (
        "data:text/html,<html><body><h1>Work Authorization</h1>"
        "<label for='auth'>Do you require sponsorship?</label>"
        "<input id='auth' name='needs_sponsorship'>"
        "<div id='followup' style='display:none'>"
        "<label for='visa'>Which visa do you hold?</label>"
        "<input id='visa' name='visa_type'></div>"
        "</body></html>"
    )

    manager = ApplicationFlowManager(
        application_id="app-conditional-1",
        user_id="user-1",
        job_url=conditional_form,
        ats_platform="greenhouse",
        adapter_cls=_RevealingAdapter,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
    )

    manager.run()

    # A second pass ran because the first one revealed a field...
    assert reveal_calls["count"] == 2
    assert "step_0_revealed_fields" in manager.steps_completed
    # ...and a third did not, because the second revealed nothing.
    assert "step_1_filled" not in manager.steps_completed


def test_the_resume_is_uploaded_on_whichever_page_asks_for_it(requires_chromium, tmp_path):
    """Workday's upload field is on page 2. The upload used to happen exactly
    once, before the loop, so those applications went out with no résumé — and
    the log blamed page 1, where there had never been a field to fill."""
    upload_calls = {"count": 0}
    # What the pages look like to `resume_attachment_state()`: page 1 has no
    # upload field at all, page 2 has an empty one, and it stays attached after.
    states = iter(["missing", "attached", "attached", "attached"])

    class _LateUploadAdapter(ATSAdapter):
        name = "fake-late-upload"

        def detect(self) -> float:
            return 1.0

        def upload_resume(self) -> bool:
            upload_calls["count"] += 1
            return upload_calls["count"] > 1  # page 1: no field, so it fails

        def resume_attachment_state(self) -> str:
            return next(states, "attached")

        def fill_personal_information(self):
            return list(_HIGH_CONFIDENCE_RESULTS)

        def answer_questions(self):
            return []

        def submit_application(self) -> bool:
            self.page.set_content(_FAKE_SUBMIT_CONFIRMATION_HTML)
            return True

    manager = ApplicationFlowManager(
        application_id="app-late-resume-1",
        user_id="user-1",
        job_url=_multi_page_form_url(3),
        ats_platform="greenhouse",
        adapter_cls=_LateUploadAdapter,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
    )

    result = manager.run()

    assert upload_calls["count"] == 2  # tried on page 1, succeeded on page 2
    assert "resume_upload_failed" in manager.steps_completed
    assert "resume_uploaded" in manager.steps_completed
    # One field, not two: a first-page miss followed by a later-page success is
    # one résumé that ended up attached — not one failure plus one success
    # halving the confidence score.
    assert result.confidence == 1.0


def test_a_page_that_will_not_advance_stops_the_run_with_the_forms_own_reason(requires_chromium, tmp_path):
    """Rather than retrying forever or pretending the page was the last one."""
    rejecting_form = (
        "data:text/html,<html><body><h1>Application Questions</h1>"
        "<input name='start_date' required>"
        "<div class='field-error' style='display:none' id='err'>Start date is required.</div>"
        "<button onclick=\"document.getElementById('err').style.display='block'\">Next</button>"
        "</body></html>"
    )
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-blocked-1",
        user_id="user-1",
        job_url=rejecting_form,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
    )

    result = manager.run()

    assert result.status == "manual_required"
    assert adapter_cls.calls["submit_application"] == 0
    assert "navigation_blocked" in manager.steps_completed

    error_log = Path(result.error_log).read_text(encoding="utf-8")
    assert "Could not advance past page 1" in error_log
    assert "Start date is required" in error_log
    assert "has NOT been submitted" in error_log


def test_a_human_gate_on_a_later_page_still_stops_the_run(requires_chromium, tmp_path):
    """A login wall at step 3 of a Workday application is exactly as much of a
    hard stop as one on the landing page. Before the loop checked every page,
    these could only ever be seen on page 1."""
    gated_form = (
        "data:text/html,<html><body>"
        "<h1 id='hdr'>Step 1 of 2</h1>"
        "<div id='p0'><input name='page_0_field'></div>"
        "<div id='p1' style='display:none'>"
        "<p>Please sign in to continue</p><input type='password' name='password'></div>"
        "<button id='nav' onclick='turn()'>Next</button>"
        "<script>function turn(){"
        "document.getElementById('p0').style.display='none';"
        "document.getElementById('p1').style.display='block';"
        "document.getElementById('hdr').textContent='Step 2 of 2';}"
        "</script></body></html>"
    )
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-loginwall-later-1",
        user_id="user-1",
        job_url=gated_form,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        browser_manager=_isolated_browser_manager(tmp_path),
        autopilot_enabled=True,
    )

    result = manager.run()

    assert result.status == "manual_required"
    assert "step_0_advanced" in manager.steps_completed  # it did reach page 2
    assert "human_gate_detected" in manager.steps_completed
    assert adapter_cls.calls["fill_personal_information"] == 1  # and filled nothing there
    assert adapter_cls.calls["submit_application"] == 0
