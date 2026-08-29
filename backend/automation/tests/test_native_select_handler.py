"""
`NativeSelectHandler` — the four issues confirmed by the code audit.

Same convention as test_dropdown_live_form_shapes.py: real rendered widgets via
`page.set_content()`, and each test pins a specific way the previous
implementation went wrong rather than just exercising the happy path.

The headline one is tier-3 ambiguity. The old last-resort loop took the FIRST
live option whose text satisfied `_values_match` — which is substring-tolerant
in both directions — so filling "No" against ["Not applicable", "None"] quietly
selected "Not applicable", and `verify()` confirmed it as success because it
used the same loose comparison. That made this path looser than
`answer_engine._match_option`, which refuses a tie outright.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Error as PlaywrightError

from automation.forms.field_handlers import (
    DEFAULT_HANDLER_REGISTRY,
    NativeSelectHandler,
    describe_field,
    fill_field,
)


def _render(page, html: str):
    page.set_content(f"<!DOCTYPE html><html><body>{html}</body></html>")


def _select(page, options: list[str], *, attrs: str = "", label: str = "Q") -> tuple:
    opts = "".join(f"<option>{o}</option>" for o in options)
    _render(page, f'<label for="s">{label}</label><select id="s" {attrs}>{opts}</select>')
    loc = page.locator("#s")
    return loc, describe_field(loc, label=label, page=page)


# ---------------------------------------------------------------------------
# 1. Tier ambiguity — the wrong-data bug
# ---------------------------------------------------------------------------

def test_a_tie_at_the_loose_tier_refuses_instead_of_picking_one(page):
    """"No" substring-matches BOTH "Not applicable" and "None", and neither is
    an exact match — so there is no defensible choice. The old code selected
    "Not applicable" and reported success."""
    loc, field = _select(page, ["Not applicable", "None"], label="Do you have X?")

    outcome = fill_field(field, "No")

    assert outcome.filled is False
    assert outcome.failure is not None
    assert outcome.failure.failure_reason == "ambiguous_option_match"
    assert set(outcome.failure.context["ambiguous_options"]) == {"Not applicable", "None"}
    assert loc.input_value() == "Not applicable"  # still the browser default; nothing was chosen
    assert page.evaluate("document.getElementById('s').selectedIndex") == 0


def test_an_exact_match_wins_and_is_not_treated_as_ambiguous(page):
    """The counterpart, and the reason ambiguity is only checked per-tier:
    "No" against ["Not applicable", "No"] is NOT ambiguous — it exactly equals
    one option. Refusing here would break every Yes/No select that also offers
    "Not applicable", which is a very common shape."""
    loc, field = _select(page, ["Not applicable", "No"], label="Do you have X?")

    outcome = fill_field(field, "No")

    assert outcome.filled is True
    assert loc.input_value() == "No"


def test_zero_matches_refuses_rather_than_falling_back_to_a_guess(page):
    loc, field = _select(page, ["Red", "Green", "Blue"], label="Colour")

    outcome = fill_field(field, "Chartreuse")

    assert outcome.filled is False
    assert outcome.failure.failure_reason == "no_matching_option"
    assert outcome.failure.context["available_options"] == ["Red", "Green", "Blue"]


def test_a_loose_match_still_works_when_it_is_unambiguous(page):
    """The tolerance exists for a reason — "United States" vs an option
    literally titled "United States of America" — and must survive."""
    loc, field = _select(page, ["Canada", "United States of America"], label="Country")

    outcome = fill_field(field, "United States")

    assert outcome.filled is True
    assert loc.input_value() == "United States of America"


# ---------------------------------------------------------------------------
# 1b. Placeholder guard
# ---------------------------------------------------------------------------

def test_a_placeholder_option_is_never_selected_even_when_it_loosely_matches(page):
    """"Select an option" contains "option"; a target of "Option" would
    otherwise loosely match the placeholder itself."""
    loc, field = _select(page, ["Select an option", "Option A", "Option B"], label="Pick")

    outcome = fill_field(field, "Option A")

    assert outcome.filled is True
    assert loc.input_value() == "Option A"


def test_a_target_matching_only_the_placeholder_refuses(page):
    loc, field = _select(page, ["-- Choose one --", "Yes", "No"], label="Pick")

    outcome = fill_field(field, "Choose one")

    assert outcome.filled is False
    assert outcome.failure.failure_reason in {"no_matching_option", "ambiguous_option_match"}
    assert page.evaluate("document.getElementById('s').selectedIndex") == 0


def test_a_select_offering_only_a_placeholder_refuses(page):
    loc, field = _select(page, ["Select..."], label="Pick")

    outcome = fill_field(field, "Yes")

    assert outcome.filled is False
    assert outcome.failure.failure_reason == "no_selectable_options"


def test_a_disabled_option_is_not_selectable(page):
    _render(page, """
        <label for="s">Pick</label>
        <select id="s">
          <option>Select...</option>
          <option disabled>Yes</option>
          <option>No</option>
        </select>
    """)
    field = describe_field(page.locator("#s"), label="Pick", page=page)

    outcome = fill_field(field, "Yes")

    assert outcome.filled is False
    assert outcome.failure.failure_reason == "no_matching_option"


# ---------------------------------------------------------------------------
# 2. Introspection failure is surfaced, not masked as "unknown"
# ---------------------------------------------------------------------------

def test_a_failed_introspection_reports_the_real_cause(page, monkeypatch):
    """Previously `tag_name` degraded to "" and the field died as
    `no_handler_matched` with the cause thrown away — the signature of several
    long-standing suite failures. Now the reason is `introspection_failed` and
    the exception text travels with it."""
    _render(page, '<label for="s">Pick</label><select id="s"><option>Yes</option></select>')

    calls = {"n": 0}
    real_evaluate = type(page.locator("#s")).evaluate

    def flaky(self, expression, *a, **kw):
        if "tagName" in str(expression):
            calls["n"] += 1
            raise PlaywrightError("synthetic introspection failure")
        return real_evaluate(self, expression, *a, **kw)

    monkeypatch.setattr(type(page.locator("#s")), "evaluate", flaky)

    field = describe_field(page.locator("#s"), label="Pick", page=page)
    assert field.tag_name == ""
    assert "synthetic introspection failure" in (field.introspection_error or "")
    assert calls["n"] == 2  # retried once before giving up

    outcome = fill_field(field, "Yes")

    assert outcome.filled is False
    assert outcome.failure.failure_reason == "introspection_failed"
    assert "synthetic introspection failure" in (outcome.failure.last_exception or "")
    assert outcome.failure.context.get("introspection_failed") is True


def test_a_transient_introspection_failure_recovers_on_the_retry(page, monkeypatch):
    """The retry exists because the usual cause is an element that hasn't
    attached yet — one wait should be enough."""
    _render(page, '<label for="s">Pick</label><select id="s"><option>Yes</option></select>')

    state = {"failed": False}
    real_evaluate = type(page.locator("#s")).evaluate

    def once(self, expression, *a, **kw):
        if "tagName" in str(expression) and not state["failed"]:
            state["failed"] = True
            raise PlaywrightError("not attached yet")
        return real_evaluate(self, expression, *a, **kw)

    monkeypatch.setattr(type(page.locator("#s")), "evaluate", once)

    field = describe_field(page.locator("#s"), label="Pick", page=page)

    assert field.tag_name == "select"
    assert field.introspection_error is None


# ---------------------------------------------------------------------------
# 3. Hidden <select>
# ---------------------------------------------------------------------------

_HIDDEN_WITH_PROXY = """
<label for="s">Pick</label>
<div class="select-shell">
  <select id="s" style="display:none"><option>Yes</option><option>No</option></select>
  <div role="combobox" aria-haspopup="listbox" tabindex="0">Select...</div>
</div>
"""


def test_a_hidden_select_with_a_visible_proxy_is_not_claimed(page):
    """The visible control is the thing to drive; the select is its backing
    store. Claiming it here would fight the widget."""
    _render(page, _HIDDEN_WITH_PROXY)
    field = describe_field(page.locator("#s"), label="Pick", page=page)

    assert NativeSelectHandler().supports(field) is False
    assert not isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), NativeSelectHandler)


def test_a_hidden_select_with_no_proxy_is_still_claimed_and_forced(page):
    """No visible stand-in means this select IS the control, so it gets filled
    with `force` rather than spending every attempt on actionability timeouts."""
    _render(page, """
        <label for="s">Pick</label>
        <select id="s" style="display:none"><option>Yes</option><option>No</option></select>
    """)
    field = describe_field(page.locator("#s"), label="Pick", page=page)

    assert NativeSelectHandler().supports(field) is True

    outcome = fill_field(field, "No")

    assert outcome.filled is True
    assert page.locator("#s").input_value() == "No"


def test_a_visible_select_is_claimed_as_before(page):
    loc, field = _select(page, ["Yes", "No"], label="Pick")
    assert NativeSelectHandler().supports(field) is True


# ---------------------------------------------------------------------------
# 4. <select multiple>
# ---------------------------------------------------------------------------

def test_multi_select_selects_every_matching_option(page):
    loc, field = _select(page, ["Python", "Go", "Rust", "Java"], attrs="multiple", label="Languages")

    outcome = fill_field(field, ["Python", "Rust"])

    assert outcome.filled is True
    assert page.evaluate(
        "Array.from(document.getElementById('s').selectedOptions).map(o => o.textContent)"
    ) == ["Python", "Rust"]


def test_multi_select_accepts_a_comma_separated_string(page):
    loc, field = _select(page, ["Python", "Go", "Rust"], attrs="multiple", label="Languages")

    outcome = fill_field(field, "Python, Go")

    assert outcome.filled is True
    assert page.evaluate(
        "Array.from(document.getElementById('s').selectedOptions).map(o => o.textContent)"
    ) == ["Python", "Go"]


def test_multi_select_verification_compares_the_whole_set_not_one_value(page):
    """`input_value()` returns only ONE value for a multi-select, so the
    single-value verify path would pass on a partial selection."""
    loc, field = _select(page, ["Python", "Go", "Rust"], attrs="multiple", label="Languages")
    handler = NativeSelectHandler()

    page.evaluate("document.querySelectorAll('#s option')[0].selected = true")
    ok, actual = handler.verify(field, ["Python", "Go"])

    assert ok is False  # only Python selected, two were asked for
    assert actual == "Python"


def test_multi_select_refuses_when_one_of_several_targets_is_ambiguous(page):
    loc, field = _select(page, ["Not applicable", "None", "Python"], attrs="multiple", label="Languages")

    outcome = fill_field(field, ["Python", "No"])

    assert outcome.filled is False
    assert outcome.failure.failure_reason == "ambiguous_option_match"


def test_single_select_is_unaffected_by_the_multi_path(page):
    loc, field = _select(page, ["Yes", "No"], label="Pick")

    outcome = fill_field(field, "Yes")

    assert outcome.filled is True
    assert loc.input_value() == "Yes"
