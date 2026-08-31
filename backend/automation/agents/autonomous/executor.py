"""
ActionExecutor — dispatches exactly one `AgentAction` against a live
Playwright `Page`, and nothing else. This is the ONLY place in the
autonomous-agent path that touches the browser to make a change (the
observer only reads).

Two safety nets live here, independent of whatever the LLM decided, because
"the model's own judgment" is not an acceptable sole safeguard for either of
these:

1. **Sensitive-field gate** — a fill/select/check whose target element's
   accessible name matches a sensitive-topic keyword (work authorization,
   visa, demographic, criminal history, legal declaration, ...) is refused
   UNLESS the value being written already comes from a verified source
   (`confirmed_answers` or `profile`), enforced by requiring the caller
   (`loop.py`) to pass `is_sourced=True` for such a value. Same rule the
   system prompt states in prose; this is the code-level backstop.
2. **Submission gate** — a click on an element whose visible text/name looks
   like a final-submit control is refused outright unless the task's
   `auto_submit_approved` flag is set, which is only ever set by the
   `/approve` endpoint after explicit user action
   (`app/services/autonomous_task_repository.py::approve_submission`).

A blocked action returns an `ActionResult(success=False, blocked_reason=...)`
— it never raises past the caller, so `loop.py` can turn a blocked action
into a `REQUEST_HUMAN_INTERVENTION`-style pause instead of crashing the task.
"""

from __future__ import annotations

import logging
import os
import re

from playwright.sync_api import Error as PlaywrightError, Page

from automation.agents.autonomous.action_semantics import (
    SUBMIT_BUTTON_PATTERNS,
    is_submit_control_name,
)
from automation.agents.autonomous.actions import ActionResult, AgentAction
from automation.agents.autonomous.combobox_verify import verify_combobox_commit
from automation.agents.autonomous.observer import observe_page
from automation.agents.autonomous.page_validation import detect_error_page
from automation.applications.page_navigator import capture_page_signature, wait_for_page_settled
from automation.utils.element_actions import safe_click
from automation.utils.human_input import human_type

#: How long to let a click settle before re-reading the page's structural
#: signature to check for an effect (spec §7). Short and bounded: this runs
#: on every mutating action inside an 80-iteration-per-resume loop, not once
#: per page navigation like `page_navigator.py`'s own (much longer) wait.
_CLICK_VERIFY_SETTLE_MS = 2000

logger = logging.getLogger(__name__)

#: Keyword patterns (case-insensitive) that mark a field as "sensitive" per
#: the spec: work authorization/visa, demographic (race/gender/veteran/
#: disability), criminal history, and legal declarations/attestations.
SENSITIVE_FIELD_PATTERNS = [
    # Work authorization / immigration status — order-independent: forms phrase
    # this both as "work authorization" and "authorized to work" / "eligible to
    # work" / "legally authorized" / "authorization to work in the [country]".
    r"work\s*authoriz", r"authoriz\w*\s*(to\s*work|for\s*employment)",
    r"authoriz\w*.{0,20}\bwork\b", r"\bwork\b.{0,20}authoriz",
    r"eligib\w*\s*(to\s*work|for\s*employment)", r"eligib\w*.{0,20}\bwork\b",
    r"legally\s*(authorized|permitted|entitled|eligible)",
    r"\bvisa\b", r"sponsor", r"citizenship", r"\bimmigration\b",
    r"\brace\b", r"ethnicit", r"\bgender\b", r"\bsex\b", r"veteran",
    r"disabilit", r"criminal", r"felony", r"convict", r"background\s*check",
    r"social\s*security", r"\bssn\b", r"i\s*certify", r"i\s*declare",
    r"legally\s*binding", r"under\s*penalty", r"date\s*of\s*birth", r"\bdob\b",
]
_SENSITIVE_RE = re.compile("|".join(SENSITIVE_FIELD_PATTERNS), re.IGNORECASE)

#: Keyword patterns marking a control as a FINAL submission action, as
#: opposed to "Next" / "Save and continue" on an intermediate page.
#: `SUBMIT_BUTTON_PATTERNS`/`is_submit_control_name` are imported from
#: `action_semantics.py` (the single source for this list — see that
#: module's docstring) rather than defined here, but kept accessible as
#: `executor.SUBMIT_BUTTON_PATTERNS`/`executor.is_submit_control_name` via
#: the import above, since existing call sites and tests reference them
#: at this path.

#: Third safety net, independent of the LLM's own judgment and of
#: `observer.py::detect_blocker` — that deterministic detector is the
#: PRIMARY defense (it pauses BEFORE the LLM is ever asked to decide on an
#: OTP/MFA page at all), but it is heuristic and could in principle miss an
#: unusually-marked-up verification field. This is the code-level backstop
#: for that residual case: even if the LLM's decision step somehow still
#: proposes a `fill`/`select` targeting something that LOOKS like a
#: verification-code field, the executor refuses it outright — a
#: verification code may ONLY ever be written via the deterministic
#: `loop.py::_try_consume_pending_secret` path (which calls `execute()`
#: directly, bypassing this gate entirely since it's a same-process,
#: same-call trusted path, not an LLM-proposed action). This keeps a
#: verification code from ever reaching `action.value` on an LLM-decided
#: action — and therefore out of `action_history`'s logged `"value"` field —
#: even in that residual case.
VERIFICATION_CODE_FIELD_PATTERNS = [
    r"(?:one.?time|verification|security).{0,15}(?:code|pass)", r"\botp\b", r"\b2fa\b", r"\bmfa\b",
    r"authenticat(?:or|ion)\s*code", r"two[-\s]?factor",
]
_VERIFICATION_CODE_RE = re.compile("|".join(VERIFICATION_CODE_FIELD_PATTERNS), re.IGNORECASE)


class ExecutorSafetyError(Exception):
    """Raised only for a programming-error case (missing page/element ref
    for an action that requires one) — never for a policy refusal, which
    returns a blocked `ActionResult` instead so the loop can react to it."""


def is_sensitive_field_name(name: str) -> bool:
    return bool(_SENSITIVE_RE.search(name or ""))


def is_verification_code_field_name(name: str) -> bool:
    return bool(_VERIFICATION_CODE_RE.search(name or ""))


def normalize_upload_path(path: str | None) -> str:
    """Canonical form for comparing a requested upload path against the
    allowlist: absolute, symlink-free, and case/separator-normalized (Windows
    paths are case-insensitive and accept both separators, so a plain string
    compare is not sufficient)."""
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))
    except (OSError, ValueError):  # pragma: no cover - defensive
        return os.path.normcase(str(path))


def _locator_for_ref(page: Page, element_ref: int):
    return page.locator(f'[data-agent-ref="{element_ref}"]')


class ActionExecutor:
    def __init__(
        self, page: Page, *, auto_submit_approved: bool,
        allowed_upload_paths: list[str] | None = None,
    ) -> None:
        self.page = page
        self.auto_submit_approved = auto_submit_approved
        #: Fourth safety gate. The ONLY local files an `upload_file` action may
        #: send to a website — normally exactly the candidate's own résumé (see
        #: `app/api/autonomous_agent.py::_build_uploadable_documents`).
        #:
        #: `file_path` on an `upload_file` action comes from the LLM. The system
        #: prompt tells it to use only a document it was given, but prose is not
        #: enforcement: without this allowlist a model could name ANY local path
        #: — `.env`, an SSH key, another user's résumé — and `set_input_files`
        #: would happily upload it to a third-party career site. That is
        #: arbitrary local-file exfiltration driven by model output, so it gets
        #: a code-level gate for the same reason the sensitive-field and
        #: submit-button gates do: the model's own judgment is not an
        #: acceptable sole safeguard.
        self.allowed_upload_paths = {
            normalize_upload_path(p) for p in (allowed_upload_paths or []) if p
        }
        self._current_element_type: str | None = None
        self._current_semantic_action: str | None = None

    def execute(
        self, action: AgentAction, *,
        element_name: str | None = None, element_type: str | None = None,
        element_semantic_action: str | None = None,
        is_sourced: bool = False, verification_code_write: bool = False,
    ) -> ActionResult:
        """`element_name` is the accessible name the observer recorded for
        `action.element_ref` (the caller — `loop.py` — looks it up from the
        `PageState` it already has, so this module never has to re-run
        extraction). `is_sourced=True` means the value being written is
        already confirmed to come from the resume/profile/a previously
        confirmed answer — required for the sensitive-field gate to pass.

        `element_type` is that same element's `PageElement.type` — used only
        to pick the right committed-state verification for a custom dropdown
        (`type == "combobox"`) or one of its options (`type == "option"`);
        see `_do_fill`/`_do_click` below. Reset on every call (this executor
        may be reused across more than one `.execute()` — see
        `loop.py::_try_consume_pending_secret`), so a stale value from a
        previous action can never leak into this one.

        `verification_code_write=True` is the ONLY way past the
        verification-code gate below — set ONLY by
        `loop.py::_try_consume_pending_secret` (the deterministic,
        never-LLM-decided path), never by the normal LLM-driven
        `_handle_execute_action` call site. It exists so that ONE trusted,
        same-process caller can write a verification code while every other
        caller — in particular, anything driven by an LLM `decide_next_step`
        decision — categorically cannot."""
        self._current_element_type = element_type
        self._current_semantic_action = element_semantic_action
        try:
            if action.action_type in ("fill", "select") and element_name and not verification_code_write:
                if is_verification_code_field_name(element_name):
                    return ActionResult(
                        success=False, action_type=action.action_type,
                        detail=f"Refused: {action.action_type} on a verification-code-shaped field {element_name!r} "
                               "via the LLM-decided action path — a code may only be entered through the "
                               "deterministic human-response flow.",
                        blocked_reason="verification_code_requires_deterministic_path",
                    )
            if action.action_type in ("fill", "select", "check", "uncheck") and element_name:
                if is_sensitive_field_name(element_name) and not is_sourced:
                    return ActionResult(
                        success=False, action_type=action.action_type,
                        detail=f"Refused: {action.action_type} on sensitive field {element_name!r} without a verified/confirmed source value.",
                        blocked_reason="sensitive_field_requires_human",
                    )
            if action.action_type == "click" and element_name and is_submit_control_name(element_name):
                if not self.auto_submit_approved:
                    return ActionResult(
                        success=False, action_type=action.action_type,
                        detail=f"Refused: click on final submit control {element_name!r} without explicit approval.",
                        blocked_reason="submit_requires_approval",
                    )

            dispatch = getattr(self, f"_do_{action.action_type}", None)
            if dispatch is None:
                return ActionResult(False, action.action_type, "No handler for this action type.", blocked_reason="unimplemented")
            return dispatch(action)
        except PlaywrightError as e:
            return ActionResult(False, action.action_type, f"Playwright error: {e}", verified=False)

    # ------------------------------------------------------------------

    def _do_navigate(self, action: AgentAction) -> ActionResult:
        before = self._safe_signature()
        response = self.page.goto(action.url, wait_until="domcontentloaded", timeout=30000)
        try:
            wait_for_page_settled(self.page, timeout_ms=10_000)
        except Exception:  # noqa: BLE001
            pass
        status = getattr(response, "status", None)
        error = detect_error_page(self.page, response_status=status)
        if error is not None:
            return ActionResult(
                False, "navigate", f"Navigation failed: {error.detail}", verified=False,
                result_code="NAVIGATION_FAILED", postcondition=error.code,
            )
        after = self._safe_signature()
        changed = before is None or after is None or after.differs_from(before)
        if not changed:
            return ActionResult(
                False, "navigate", "Navigation completed but produced no page state change",
                verified=False, result_code="NO_STATE_CHANGE", postcondition="NO_STATE_CHANGE",
            )
        return ActionResult(
            True, "navigate", f"Navigated to {self.page.url}", verified=True,
            result_code="NAVIGATION_SUCCESS", postcondition="destination loaded without error indicators",
        )

    def _do_click(self, action: AgentAction) -> ActionResult:
        locator = _locator_for_ref(self.page, action.element_ref)
        before = self._safe_signature()
        ok = safe_click(locator, self.page)
        if not ok:
            return ActionResult(False, "click", "Click did not succeed", verified=False)
        try:
            wait_for_page_settled(self.page, timeout_ms=_CLICK_VERIFY_SETTLE_MS)
        except Exception:  # noqa: BLE001 - a settle-wait failing must never block reporting the click
            pass
        after = self._safe_signature()
        changed = before is not None and after is not None and after.differs_from(before)
        verified = changed
        detail = (
            f"Clicked element ref {action.element_ref}" if changed
            else f"Clicked element ref {action.element_ref}, but nothing on the page changed"
        )
        if self._current_element_type == "option":
            # spec §15/§20: a popup closing is itself a real structural
            # change that must NOT, by itself, count as "the option was
            # committed" — false-positive verification is worse than
            # false-negative. The option's own aria-selected/aria-checked
            # state (WAI-ARIA listbox/grid pattern) is the authority here,
            # and overrides a signature-only "changed" reading in either
            # direction when it gives a conclusive answer.
            option_selected = self._verify_option_selected(locator)
            if option_selected is False:
                verified = False
                detail += " (the option does not report itself as selected)"
            elif option_selected is True:
                verified = True
        error = detect_error_page(self.page)
        if error is not None:
            return ActionResult(
                False, "click", f"Click reached an error page: {error.detail}", verified=False,
                result_code="ERROR_PAGE", postcondition=error.code,
            )
        if self._current_semantic_action in ("APPLY", "START_APPLICATION"):
            destination = observe_page(self.page)
            entered_application = destination.page_type in (
                "application_page", "login", "verification", "captcha", "review",
            )
            if not entered_application:
                return ActionResult(
                    False, "click",
                    f"Clicked application-entry control, but no application state was detected "
                    f"(page_type={destination.page_type})",
                    verified=False,
                    result_code="NO_STATE_CHANGE" if not changed else "POSTCONDITION_FAILED",
                    postcondition="application page or application gate not detected",
                )
            return ActionResult(
                True, "click", f"Clicked application-entry control and detected {destination.page_type}",
                verified=True, result_code="ACTION_SUCCEEDED",
                postcondition=f"application entry verified as {destination.page_type}",
            )
        if not verified:
            return ActionResult(
                False, "click", detail, verified=False,
                result_code="NO_STATE_CHANGE", postcondition="NO_STATE_CHANGE",
            )
        return ActionResult(
            True, "click", detail, verified=True,
            result_code="ACTION_SUCCEEDED", postcondition="page signature changed",
        )

    def _verify_option_selected(self, locator) -> bool | None:
        """`aria-selected`/`aria-checked` on a clicked `role="option"`
        element, tri-state: `True`/`False` when the attribute is present,
        `None` when it isn't (or couldn't be read) — inconclusive, not a
        pass or a fail, so the caller falls back to the page-signature diff
        instead of treating silence as either."""
        try:
            value = locator.get_attribute("aria-selected")
        except Exception:  # noqa: BLE001 - an inconclusive read must never crash the click
            return None
        if value is None:
            try:
                value = locator.get_attribute("aria-checked")
            except Exception:  # noqa: BLE001
                return None
        if value is None:
            return None
        return value == "true"

    def _safe_signature(self):
        """`capture_page_signature`, degrading to `None` (never raising) on
        any failure — a page this can't read yet (mid-navigation, or in a
        test double that doesn't implement every real-Playwright method) must
        never crash the action itself; it just can't be verified this time."""
        try:
            return capture_page_signature(self.page)
        except Exception:  # noqa: BLE001
            return None

    def _do_fill(self, action: AgentAction) -> ActionResult:
        locator = _locator_for_ref(self.page, action.element_ref)
        human_type(locator, action.value or "", self.page)
        detail = f"Filled element ref {action.element_ref} with {len(action.value or '')} chars"
        if self._current_element_type == "combobox":
            # A custom dropdown's committed value doesn't necessarily show up
            # in its own `input_value()` — reuse the same multi-signal
            # algorithm the deterministic engine already proved out (spec
            # §15). Inconclusive (`None`) falls back to the plain text check
            # rather than being treated as either a pass or a fail.
            committed, observed = verify_combobox_commit(locator, self.page, action.value or "")
            verified = committed if committed is not None else self._verify_text_committed(locator, action.value or "")
            if not verified:
                detail += f" (could not confirm the value stuck; last observed: {observed!r})" if observed else " (could not confirm the value stuck)"
            return ActionResult(True, "fill", detail, verified=verified)
        verified = self._verify_text_committed(locator, action.value or "")
        if not verified:
            detail += " (could not confirm the value stuck)"
        return ActionResult(True, "fill", detail, verified=verified)

    def _do_select(self, action: AgentAction) -> ActionResult:
        locator = _locator_for_ref(self.page, action.element_ref)
        try:
            locator.select_option(label=action.value)
        except PlaywrightError:
            locator.select_option(value=action.value)
        verified = self._verify_select_committed(locator, action.value)
        detail = f"Selected {action.value!r} on element ref {action.element_ref}"
        if not verified:
            detail += " (could not confirm the option is actually selected)"
        return ActionResult(True, "select", detail, verified=verified)

    def _do_check(self, action: AgentAction) -> ActionResult:
        locator = _locator_for_ref(self.page, action.element_ref)
        locator.check(timeout=3000)
        verified = self._verify_checked_state(locator, expected=True)
        return ActionResult(True, "check", f"Checked element ref {action.element_ref}", verified=verified)

    def _do_uncheck(self, action: AgentAction) -> ActionResult:
        locator = _locator_for_ref(self.page, action.element_ref)
        locator.uncheck(timeout=3000)
        verified = self._verify_checked_state(locator, expected=False)
        return ActionResult(True, "uncheck", f"Unchecked element ref {action.element_ref}", verified=verified)

    # ------------------------------------------------------------------
    # Committed-state verification — same "fill then verify" division of
    # responsibility `automation/forms/field_handlers.py::fill_field()` uses
    # for the deterministic path, simplified to one element per ref rather
    # than a per-widget-type handler hierarchy. A verification read that
    # itself fails (detached node, transient re-render) reports `False`
    # rather than raising — an inconclusive check must never crash the loop.

    def _verify_text_committed(self, locator, intended: str) -> bool:
        if not intended.strip():
            return True  # clearing a field has no non-empty value to confirm
        try:
            actual = locator.input_value(timeout=1000)
        except Exception:  # noqa: BLE001 - not every widget/test-double supports input_value
            try:
                actual = locator.inner_text(timeout=1000)
            except Exception:  # noqa: BLE001
                return False
        actual = (actual or "").strip()
        return bool(actual) and (actual == intended.strip() or intended.strip() in actual)

    def _verify_select_committed(self, locator, intended: str | None) -> bool:
        if not intended:
            return True
        try:
            selected = locator.evaluate(
                "el => Array.from(el.selectedOptions || []).map(o => (o.textContent || '').trim())"
            )
        except Exception:  # noqa: BLE001
            return False
        needle = str(intended).strip().lower()
        return any(needle == (s or "").strip().lower() or needle in (s or "").lower() for s in (selected or []))

    def _verify_checked_state(self, locator, *, expected: bool) -> bool:
        try:
            return bool(locator.is_checked(timeout=1000)) == expected
        except Exception:  # noqa: BLE001
            return False

    def _do_scroll(self, action: AgentAction) -> ActionResult:
        amount = action.amount or 600
        delta = -amount if action.direction == "up" else amount
        self.page.mouse.wheel(0, delta)
        return ActionResult(True, "scroll", f"Scrolled {action.direction or 'down'} by {amount}px")

    def _do_press_key(self, action: AgentAction) -> ActionResult:
        self.page.keyboard.press(action.value)
        return ActionResult(True, "press_key", f"Pressed key {action.value}")

    def _do_upload_file(self, action: AgentAction) -> ActionResult:
        # See `allowed_upload_paths`: refuse anything the caller didn't
        # explicitly offer, so an LLM-named path can never be uploaded.
        requested = normalize_upload_path(action.file_path)
        if requested not in self.allowed_upload_paths:
            return ActionResult(
                success=False, action_type="upload_file",
                detail=(
                    f"Refused: {action.file_path!r} is not one of this task's uploadable documents. "
                    f"({len(self.allowed_upload_paths)} document(s) available.)"
                ),
                blocked_reason="upload_path_not_allowed",
            )
        locator = _locator_for_ref(self.page, action.element_ref)
        locator.set_input_files(action.file_path)
        return ActionResult(True, "upload_file", f"Uploaded {action.file_path} to element ref {action.element_ref}")

    def _do_extract_text(self, action: AgentAction) -> ActionResult:
        if action.element_ref is not None:
            locator = _locator_for_ref(self.page, action.element_ref)
            text = locator.inner_text(timeout=3000)
        else:
            text = self.page.inner_text("body")
        text = " ".join(text.split())[:4000]
        return ActionResult(True, "extract_text", "Extracted text", extracted_text=text)

    def _do_wait(self, action: AgentAction) -> ActionResult:
        self.page.wait_for_timeout(min(action.wait_ms or 1000, 15000))
        return ActionResult(True, "wait", f"Waited {action.wait_ms or 1000}ms")

    def _do_go_back(self, action: AgentAction) -> ActionResult:
        before = self._safe_signature()
        response = self.page.go_back(wait_until="domcontentloaded", timeout=15000)
        error = detect_error_page(self.page, response_status=getattr(response, "status", None))
        after = self._safe_signature()
        if error is not None:
            return ActionResult(False, "go_back", error.detail, verified=False, result_code="NAVIGATION_FAILED")
        if before is not None and after is not None and not after.differs_from(before):
            return ActionResult(False, "go_back", "Back navigation produced no state change", verified=False, result_code="NO_STATE_CHANGE")
        return ActionResult(True, "go_back", "Navigated back", verified=True, result_code="NAVIGATION_SUCCESS")

    def _do_get_page_state(self, action: AgentAction) -> ActionResult:
        # No-op executor-side: `loop.py` always re-observes after every
        # action anyway, so this action exists for the LLM's own reasoning
        # ("explicitly re-check before deciding") rather than needing a
        # distinct code path.
        return ActionResult(True, "get_page_state", "Current page state will be re-observed.")
