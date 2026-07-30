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

These end-to-end tests depend on the session-scoped `browser` fixture from
conftest.py purely to trigger its "skip if Chromium isn't installed" logic —
ApplicationFlowManager launches its own BrowserManager/browser internally, so
the fixture's browser object itself is never used directly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, ProfileDocument
from automation.ats.base import ATSAdapter, FieldFillResult
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

_NO_CAPTCHA_NO_NEXT_HTML = "data:text/html,<html><body><p>Single-step application form.</p></body></html>"
_CAPTCHA_HTML = (
    "data:text/html,<html><body><div class='g-recaptcha'></div><p>Verify you're human.</p></body></html>"
)
_ALWAYS_NEXT_HTML = "data:text/html,<html><body><button>Next</button></body></html>"


# ---------------------------------------------------------------------------
# End-to-end orchestration tests (real headless browser, data: URLs)
# ---------------------------------------------------------------------------

def test_run_auto_submits_on_high_confidence_public_ats(browser, tmp_path):
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-auto-1",
        user_id="user-1",
        job_url=_NO_CAPTCHA_NO_NEXT_HTML,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
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


def test_run_holds_for_copilot_review_when_autopilot_is_off(browser, tmp_path):
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-copilot-1",
        user_id="user-1",
        job_url=_NO_CAPTCHA_NO_NEXT_HTML,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        autopilot_enabled=False,
    )

    result = manager.run()

    assert result.status == "copilot_review"
    assert adapter_cls.calls["submit_application"] == 0
    assert "decision_copilot_review" in manager.steps_completed


def test_run_stops_before_filling_anything_when_captcha_is_present(browser, tmp_path):
    adapter_cls = _make_fake_adapter_cls(_HIGH_CONFIDENCE_RESULTS)
    manager = ApplicationFlowManager(
        application_id="app-captcha-1",
        user_id="user-1",
        job_url=_CAPTCHA_HTML,
        ats_platform="greenhouse",
        adapter_cls=adapter_cls,
        profile=_profile(),
        resume_document=_resume_document(tmp_path / "resume.pdf"),
        autopilot_enabled=True,
    )

    result = manager.run()

    assert result.status == "manual_required"
    assert result.confidence == 0.0
    assert adapter_cls.calls["fill_personal_information"] == 0
    assert adapter_cls.calls["upload_resume"] == 0
    assert manager.steps_completed == ["navigated", "captcha_detected"]


def test_run_enforces_the_max_steps_safety_cap(browser, tmp_path):
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
        autopilot_enabled=True,
    )

    result = manager.run()

    assert adapter_cls.calls["fill_personal_information"] == ApplicationFlowManager.MAX_STEPS
    assert f"step_{ApplicationFlowManager.MAX_STEPS - 1}_filled" in manager.steps_completed
    assert f"step_{ApplicationFlowManager.MAX_STEPS - 1}_advanced" in manager.steps_completed
    assert f"step_{ApplicationFlowManager.MAX_STEPS}_filled" not in manager.steps_completed
    assert result.status in ("applied", "copilot_review", "needs_review")  # loop cap hit; a decision still comes out


def test_run_reports_failure_status_when_navigation_fails(browser, monkeypatch, tmp_path):
    # Skip BrowserManager's own retry/backoff (already covered by
    # test_browser_manager.py) so this test fails fast instead of waiting
    # through real retry sleeps.
    from automation.browser.browser_manager import BrowserManager

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
        autopilot_enabled=False,
    )

    result = manager.run()

    assert result.status == "failed"
    assert result.confidence == 0.0
    assert adapter_cls.calls["fill_personal_information"] == 0


def test_run_passes_the_answer_engine_through_to_the_adapter_it_constructs(browser, tmp_path):
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
        autopilot_enabled=True,
        answer_engine=sentinel_engine,
    )

    manager.run()

    assert seen_engines == [sentinel_engine]
