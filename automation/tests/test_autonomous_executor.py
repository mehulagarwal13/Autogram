"""
ActionExecutor tests using fake Playwright Page/Locator objects — no real
browser. Focused on the three safety nets that must hold regardless of what
the LLM decided: the sensitive-field gate, the submit-button gate, and the
verification-code gate (a code may only ever be written through the
deterministic human-response path, never an LLM-decided action).
"""

import os

import pytest

os.environ["AUTOMATION_HUMAN_PACING"] = "0"  # deterministic, no jittered typing delays in tests

from automation.agents.autonomous.actions import AgentAction
from automation.agents.autonomous.executor import (
    ActionExecutor,
    is_sensitive_field_name,
    is_submit_control_name,
    is_verification_code_field_name,
)


class FakeLocator:
    def __init__(self):
        self.filled_with = None
        self.clicked = False
        self.checked_state = None

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None, position=None):
        self.clicked = True

    def fill(self, value):
        self.filled_with = value

    def select_option(self, label=None, value=None):
        self.filled_with = label or value

    def check(self, timeout=None):
        self.checked_state = True

    def uncheck(self, timeout=None):
        self.checked_state = False

    def set_input_files(self, path):
        self.filled_with = path

    def inner_text(self, timeout=None):
        return "some text"


class FakePage:
    def __init__(self):
        self.locators: dict[str, FakeLocator] = {}
        self.url = "https://example.com/apply"

    def locator(self, selector: str) -> FakeLocator:
        loc = self.locators.setdefault(selector, FakeLocator())
        return loc

    def wait_for_timeout(self, ms):
        pass


def _executor(page, auto_submit_approved=False):
    return ActionExecutor(page, auto_submit_approved=auto_submit_approved)


def test_is_sensitive_field_name_matches_known_categories():
    assert is_sensitive_field_name("Are you authorized to work in the US?")
    assert is_sensitive_field_name("Visa sponsorship required?")
    assert is_sensitive_field_name("Veteran status")
    assert is_sensitive_field_name("Have you been convicted of a felony?")
    assert not is_sensitive_field_name("First name")


def test_is_submit_control_name_matches_final_submit_only():
    assert is_submit_control_name("Submit Application")
    assert is_submit_control_name("Apply Now")
    assert not is_submit_control_name("Next")
    assert not is_submit_control_name("Save and continue")


def test_fill_on_ordinary_field_succeeds():
    page = FakePage()
    action = AgentAction(action_type="fill", element_ref=0, value="Jane")
    result = _executor(page).execute(action, element_name="First name", is_sourced=False)
    assert result.success
    assert page.locator('[data-agent-ref="0"]').filled_with == "Jane"


def test_fill_on_sensitive_field_without_source_is_blocked():
    page = FakePage()
    action = AgentAction(action_type="fill", element_ref=0, value="No")
    result = _executor(page).execute(action, element_name="Are you authorized to work in the US?", is_sourced=False)
    assert not result.success
    assert result.blocked_reason == "sensitive_field_requires_human"
    assert page.locator('[data-agent-ref="0"]').filled_with is None


def test_fill_on_sensitive_field_with_confirmed_source_is_allowed():
    page = FakePage()
    action = AgentAction(action_type="fill", element_ref=0, value="Yes")
    result = _executor(page).execute(action, element_name="Are you authorized to work in the US?", is_sourced=True)
    assert result.success
    assert page.locator('[data-agent-ref="0"]').filled_with == "Yes"


def test_click_on_submit_button_without_approval_is_blocked():
    page = FakePage()
    action = AgentAction(action_type="click", element_ref=1)
    result = _executor(page, auto_submit_approved=False).execute(action, element_name="Submit Application")
    assert not result.success
    assert result.blocked_reason == "submit_requires_approval"
    assert page.locator('[data-agent-ref="1"]').clicked is False


def test_click_on_submit_button_with_approval_succeeds():
    page = FakePage()
    action = AgentAction(action_type="click", element_ref=1)
    result = _executor(page, auto_submit_approved=True).execute(action, element_name="Submit Application")
    assert result.success
    assert page.locator('[data-agent-ref="1"]').clicked is True


def test_click_on_non_submit_button_never_needs_approval():
    page = FakePage()
    action = AgentAction(action_type="click", element_ref=2)
    result = _executor(page, auto_submit_approved=False).execute(action, element_name="Next")
    assert result.success


# ---------------------------------------------------------------------------
# Verification-code gate — the third safety net (see executor.py's
# VERIFICATION_CODE_FIELD_PATTERNS docstring for why it exists on top of
# observer.py::detect_blocker).
# ---------------------------------------------------------------------------

def test_is_verification_code_field_name_matches_code_fields_only():
    assert is_verification_code_field_name("One-time password")
    assert is_verification_code_field_name("Verification code")
    assert is_verification_code_field_name("Security code")
    assert is_verification_code_field_name("Enter your OTP")
    assert is_verification_code_field_name("Authenticator code")
    assert is_verification_code_field_name("Two-factor code")
    assert not is_verification_code_field_name("First name")
    assert not is_verification_code_field_name("Postal code")
    assert not is_verification_code_field_name("Employee code")


@pytest.mark.parametrize("field_name", ["Verification code", "One-time password", "Enter your OTP"])
def test_llm_decided_fill_into_a_verification_code_field_is_refused(field_name):
    """An LLM-proposed action must never be able to write into a
    verification-code field — even a value it somehow guessed or scraped."""
    page = FakePage()
    action = AgentAction(action_type="fill", element_ref=0, value="123456")
    result = _executor(page).execute(action, element_name=field_name, is_sourced=True)
    assert not result.success
    assert result.blocked_reason == "verification_code_requires_deterministic_path"
    assert page.locator('[data-agent-ref="0"]').filled_with is None
    # The refusal message itself must not echo the attempted value.
    assert "123456" not in result.detail


# ---------------------------------------------------------------------------
# Upload allowlist — the fourth safety net. `file_path` on an upload_file
# action comes from the LLM, so without this gate a model-named path (`.env`,
# an SSH key) would be uploaded to a third-party career site.
# ---------------------------------------------------------------------------

def test_upload_is_refused_when_no_documents_were_offered(tmp_path):
    """The pre-fix default: `uploaded_documents` was always empty, so ANY
    upload attempt must be refused rather than silently sending a file."""
    page = FakePage()
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    action = AgentAction(action_type="upload_file", element_ref=0, file_path=str(secret))

    result = _executor(page).execute(action)

    assert not result.success
    assert result.blocked_reason == "upload_path_not_allowed"
    assert page.locator('[data-agent-ref="0"]').filled_with is None


def test_upload_is_refused_for_a_path_outside_the_allowlist(tmp_path):
    """An LLM naming a different local file than the one it was offered."""
    page = FakePage()
    allowed = tmp_path / "resume.pdf"
    allowed.write_text("%PDF-fake")
    forbidden = tmp_path / "dotenv_secret"
    forbidden.write_text("OPENAI_API_KEY=sk-real")

    executor = ActionExecutor(page, auto_submit_approved=False, allowed_upload_paths=[str(allowed)])
    result = executor.execute(AgentAction(action_type="upload_file", element_ref=0, file_path=str(forbidden)))

    assert not result.success
    assert result.blocked_reason == "upload_path_not_allowed"
    assert page.locator('[data-agent-ref="0"]').filled_with is None


def test_upload_is_allowed_for_an_offered_document(tmp_path):
    page = FakePage()
    allowed = tmp_path / "resume.pdf"
    allowed.write_text("%PDF-fake")

    executor = ActionExecutor(page, auto_submit_approved=False, allowed_upload_paths=[str(allowed)])
    result = executor.execute(AgentAction(action_type="upload_file", element_ref=0, file_path=str(allowed)))

    assert result.success
    assert page.locator('[data-agent-ref="0"]').filled_with == str(allowed)


def test_upload_allowlist_is_not_defeated_by_path_spelling(tmp_path):
    """Same file, different spelling (relative segments / separators) must
    still match — otherwise the gate would reject legitimate uploads. And on
    Windows, case must not matter either."""
    page = FakePage()
    allowed = tmp_path / "resume.pdf"
    allowed.write_text("%PDF-fake")
    roundabout = str(tmp_path / "sub" / ".." / "resume.pdf")
    (tmp_path / "sub").mkdir()

    executor = ActionExecutor(page, auto_submit_approved=False, allowed_upload_paths=[str(allowed)])
    result = executor.execute(AgentAction(action_type="upload_file", element_ref=0, file_path=roundabout))

    assert result.success, result.detail


def test_deterministic_path_may_write_a_verification_code():
    """`loop.py::_try_consume_pending_secret` — the one trusted caller —
    passes `verification_code_write=True` and IS allowed through."""
    page = FakePage()
    action = AgentAction(action_type="fill", element_ref=0, value="123456")
    result = _executor(page).execute(
        action, element_name="verification code", is_sourced=False, verification_code_write=True,
    )
    assert result.success
    assert page.locator('[data-agent-ref="0"]').filled_with == "123456"
