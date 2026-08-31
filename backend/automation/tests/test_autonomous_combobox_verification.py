"""
Real-DOM regression tests for the autonomous agent's custom-dropdown support
(spec §15): (1) an opened dropdown's options become real, clickable
`element_ref`s (they didn't before — see `observer.py::_EXTRACT_ELEMENTS_JS`'s
`[role="option"]` addition), and (2) committed-state verification for both a
clicked option (`ActionExecutor._do_click`) and a typed-then-committed
combobox (`ActionExecutor._do_fill` -> `combobox_verify.verify_combobox_commit`)
uses the same WAI-ARIA signals the deterministic engine already proved out
against a real American Express widget, not just a page-signature diff.

Standalone `page` fixture (own `sync_playwright()` context, gated by
`requires_chromium`), same pattern `test_combobox_committed_value.py` uses —
these fixtures are inline HTML via `page.set_content()`, so this file must be
run on its own (or before any file using the shared session-scoped `page`/
`browser` fixture), exactly like that file.
"""

from __future__ import annotations

import pytest

from automation.agents.autonomous.actions import AgentAction
from automation.agents.autonomous.executor import ActionExecutor
from automation.agents.autonomous.loop import _widget_type
from automation.agents.autonomous.observer import observe_page


@pytest.fixture
def page(requires_chromium):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page()
        yield pg
        browser.close()


def _find(state, predicate):
    return next((e for e in state.elements if predicate(e)), None)


def test_opened_dropdown_options_become_real_element_refs(page):
    page.set_content("""
      <label for="f">Country</label>
      <input id="f" role="combobox" aria-controls="lb" aria-expanded="true">
      <ul id="lb" role="listbox">
        <li id="opt-us" role="option" aria-selected="false">United States</li>
        <li id="opt-de" role="option" aria-selected="true">Germany</li>
      </ul>
    """)
    state = observe_page(page)

    trigger = _find(state, lambda e: _widget_type(e) == "combobox")
    assert trigger is not None, "the combobox trigger itself must still be extracted"

    us_option = _find(state, lambda e: _widget_type(e) == "option" and "United States" in (e.name or ""))
    de_option = _find(state, lambda e: _widget_type(e) == "option" and "Germany" in (e.name or ""))
    assert us_option is not None, "an opened listbox's options must become clickable element_refs"
    assert de_option is not None

    # aria_selected is surfaced tri-state, straight off the DOM.
    assert us_option.aria_selected is False
    assert de_option.aria_selected is True


def test_click_on_an_option_that_marks_itself_selected_verifies_true(page):
    page.set_content("""
      <input id="f" role="combobox" aria-controls="lb" aria-expanded="true">
      <ul id="lb" role="listbox">
        <li id="opt-de" role="option" aria-selected="false"
            onclick="this.setAttribute('aria-selected','true')">Germany</li>
      </ul>
    """)
    state = observe_page(page)
    option = _find(state, lambda e: _widget_type(e) == "option")
    assert option is not None

    executor = ActionExecutor(page, auto_submit_approved=False)
    result = executor.execute(
        AgentAction(action_type="click", element_ref=option.ref),
        element_name=option.name, element_type=_widget_type(option),
    )

    assert result.success is True
    assert result.verified is True


def test_click_on_an_option_that_closes_without_selecting_verifies_false(page):
    """The false-positive case spec §15/§20 warns about: the popup visibly
    closes (a real structural change `capture_page_signature` would catch —
    it tracks visible `[role="listbox"]` controls) but the option itself
    never reports itself as selected — e.g. a missed click target, or a
    widget that failed silently. Verification must not read the signature
    change alone as success. The option stays ATTACHED to the DOM (hidden via
    the listbox's own collapse, not removed outright) so its own
    `aria-selected` is still readable — the realistic shape of "the popup
    visibly closed" a real widget produces."""
    page.set_content("""
      <div id="wrap">
        <input id="f" role="combobox" aria-controls="lb" aria-expanded="true">
        <ul id="lb" role="listbox">
          <li id="opt-de" role="option" aria-selected="false"
              onclick="document.getElementById('f').setAttribute('aria-expanded','false');
                       document.getElementById('lb').style.display='none';">Germany</li>
        </ul>
      </div>
    """)
    state = observe_page(page)
    option = _find(state, lambda e: _widget_type(e) == "option")
    assert option is not None

    executor = ActionExecutor(page, auto_submit_approved=False)
    result = executor.execute(
        AgentAction(action_type="click", element_ref=option.ref),
        element_name=option.name, element_type=_widget_type(option),
    )

    assert result.success is True
    assert result.verified is False, "a popup closing must never, by itself, count as a committed selection"


def test_combobox_fill_is_verified_via_aria_controls_when_the_input_stays_empty(page):
    """Mirrors the real American Express `cx-select-input` shape
    (`field_handlers.py::_selected_option_via_aria_controls`'s own docstring):
    the input's own value is never written, but the popup it `aria-controls`
    reports a real `aria-selected="true"` option."""
    page.set_content("""
      <label for="f">Country</label>
      <input id="f" name="country" type="text" role="combobox"
             aria-controls="lb" aria-expanded="true">
      <ul id="lb" role="listbox">
        <li role="option" aria-selected="true">Germany</li>
      </ul>
    """)
    state = observe_page(page)
    trigger = _find(state, lambda e: _widget_type(e) == "combobox")
    assert trigger is not None

    executor = ActionExecutor(page, auto_submit_approved=False)
    result = executor.execute(
        AgentAction(action_type="fill", element_ref=trigger.ref, value="Germany"),
        element_name=trigger.name, element_type=_widget_type(trigger),
    )

    assert result.success is True
    assert result.verified is True, "aria-controls/aria-selected must verify even though input_value() is empty"


def test_combobox_fill_fails_when_the_inspectable_popup_shows_nothing_selected(page):
    """A failed search leaves typed filter text sitting in the input — that
    text must never be read as a successful match when the widget's own
    popup is inspectable and reports no selection (spec §15's authoritative-
    negative rule)."""
    page.set_content("""
      <input id="f" name="country" type="text" role="combobox"
             aria-controls="lb" aria-expanded="true">
      <ul id="lb" role="listbox">
        <li role="option" aria-selected="false">United States</li>
        <li role="option" aria-selected="false">United Kingdom</li>
      </ul>
    """)
    state = observe_page(page)
    trigger = _find(state, lambda e: _widget_type(e) == "combobox")
    assert trigger is not None

    executor = ActionExecutor(page, auto_submit_approved=False)
    result = executor.execute(
        AgentAction(action_type="fill", element_ref=trigger.ref, value="Germany"),
        element_name=trigger.name, element_type=_widget_type(trigger),
    )

    assert result.success is True
    assert result.verified is False
