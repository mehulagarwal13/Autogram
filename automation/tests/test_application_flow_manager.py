"""
ApplicationFlowManager (automation/applications/application_flow_manager.py)
— Phase 4.

`decide_action()` and `_aggregate_confidence()` are pure functions and are
tested directly with no browser involved. The orchestration logic (CAPTCHA
short-circuit, the multi-step Next-button loop and its MAX_STEPS safety cap,
and the final decision -> status mapping) is tested end-to-end against a real
(but headless, network-free) Playwright browser, using `data:` URLs the same
way test_detector.py does, and a small in-test fake ATSAdapter so the test
doesn't depend on any specific ATS's real markup.

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


def _make_fake_adapter_cls(fill_results, *, upload_ok=True, submit_ok=True):
    """A minimal ATSAdapter that never touches `self.page` itself — used to
    isolate ApplicationFlowManager's own orchestration logic (loop, decision,
    checkpoints) from any real ATS's markup, which is already covered by
    test_greenhouse_adapter.py / test_lever_adapter.py. `calls` records what
    the flow manager invoked, for assertions."""

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
            return submit_ok

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
    module's docstring for why that isolation matters here specifically."""
    return BrowserManager(
        user_id="user-1",
        ats_platform=ats_platform,
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


def test_run_enforces_the_max_steps_safety_cap(requires_chromium, tmp_path):
    # This page's "Next" button never disappears — simulates a broken
    # selector/loop rather than a real multi-step form. The manager must
    # stop after MAX_STEPS iterations instead of clicking forever.
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

    assert adapter_cls.calls["fill_personal_information"] == ApplicationFlowManager.MAX_STEPS
    assert f"step_{ApplicationFlowManager.MAX_STEPS - 1}_filled" in manager.steps_completed
    assert f"step_{ApplicationFlowManager.MAX_STEPS - 1}_advanced" in manager.steps_completed
    assert f"step_{ApplicationFlowManager.MAX_STEPS}_filled" not in manager.steps_completed
    assert result.status in ("applied", "copilot_review", "needs_review")  # loop cap hit; a decision still comes out


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
    false record in front of the candidate AND let idempotency block a retry."""
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
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
