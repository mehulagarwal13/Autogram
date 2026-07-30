"""
Regression tests for the Phase 8 production-readiness audit — each test
here corresponds to a specific finding:

1. Handler selection is deterministic by registry order even when more than
   one handler's `supports()` legitimately returns true for the same field.
2. `_read_dropdown_displayed_value`'s fallback used to read `text_content()`,
   which includes hidden descendant text and could report a false-positive
   "filled" when a widget's popup happens to be a hidden CHILD of the
   resolved control rather than a sibling. Fixed to use `inner_text()`.
3. `VirtualizedListboxHandler` now declines a `role="listbox"` element that
   isn't actually visible yet (a click-to-open popup before it's opened),
   yielding to `ComboboxHandler` instead of claiming and then failing it.
4. `FieldFailure` now carries the last raised exception and a best-effort
   element HTML snapshot.
"""

from __future__ import annotations

from automation.forms.field_handlers import (
    ComboboxHandler,
    CountryPickerHandler,
    DEFAULT_HANDLER_REGISTRY,
    VirtualizedListboxHandler,
    describe_field,
    fill_field,
)


def _render(page, html: str):
    page.set_content(f"<!DOCTYPE html><html><body>{html}</body></html>")


# ---------------------------------------------------------------------------
# 1. Deterministic resolution when multiple handlers could match
# ---------------------------------------------------------------------------

def test_country_field_rendered_as_a_listbox_still_resolves_to_country_picker(page):
    # role="listbox" alone would match VirtualizedListboxHandler; the same
    # field also has profile_attribute="country", which CountryPickerHandler
    # claims. Registry order must make this deterministic every time.
    _render(page, '<div id="c" role="listbox"><div role="option">United States</div></div>')
    field = describe_field(page.locator("#c"), label="Country", page=page, profile_attribute="country")

    matches = DEFAULT_HANDLER_REGISTRY.get_all_matches(field)
    assert len(matches) >= 2
    assert isinstance(matches[0], CountryPickerHandler)
    assert DEFAULT_HANDLER_REGISTRY.get_handler(field) is matches[0]


def test_ambiguous_listbox_field_resolves_the_same_handler_every_call(page):
    _render(page, '<div id="c" role="listbox"><div role="option">Option A</div></div>')
    field = describe_field(page.locator("#c"), label="Custom question", page=page)

    resolved = [type(DEFAULT_HANDLER_REGISTRY.get_handler(field)).__name__ for _ in range(5)]
    assert len(set(resolved)) == 1  # always the same handler, never flaps


# ---------------------------------------------------------------------------
# 2. VirtualizedListboxHandler yields a hidden (not-yet-opened) listbox
# ---------------------------------------------------------------------------

def test_virtualized_listbox_handler_declines_a_hidden_listbox(page):
    _render(page, '<div id="menu" role="listbox" style="display:none;"><div role="option">Yes</div></div>')
    field = describe_field(page.locator("#menu"), label="Hidden panel", page=page)

    assert VirtualizedListboxHandler().supports(field) is False


def test_hidden_listbox_falls_through_to_combobox_handler_when_marked_as_a_trigger(page):
    # A combobox TRIGGER (role="combobox") whose popup hasn't opened yet —
    # ComboboxHandler must still be reachable for this shape; the trigger
    # itself is never role="listbox", so VirtualizedListboxHandler never
    # even considers it.
    _render(
        page,
        """
        <div id="trigger" role="combobox" aria-haspopup="listbox" aria-expanded="false">Select...</div>
        <div id="menu" role="listbox" style="display:none;"><div role="option">Yes</div></div>
        """,
    )
    field = describe_field(page.locator("#trigger"), label="Relocate?", page=page)
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), ComboboxHandler)


# ---------------------------------------------------------------------------
# 3. inner_text() fix — false-positive verification regression
# ---------------------------------------------------------------------------

_HIDDEN_NESTED_MENU_HTML = """
<div id="ctrl" aria-expanded="false">
  <span>Select an option</span>
  <ul style="display:none;"><li>United States</li><li>Canada</li></ul>
</div>
"""


def test_dropdown_verify_does_not_false_positive_on_hidden_descendant_text(page):
    _render(page, _HIDDEN_NESTED_MENU_HTML)
    field = describe_field(page.locator("#ctrl"), label="Country", page=page)

    # Nothing on the page can actually be clicked/searched to select
    # "United States" (the <ul> has no class hints and stays display:none no
    # matter what's clicked) — the fill should fail, and CRITICALLY,
    # verification must not report success just because "United States"
    # exists as hidden text inside the control.
    outcome = fill_field(field, "United States")

    assert outcome.filled is False
    assert outcome.actual_value == "Select an option"  # only the VISIBLE text


# ---------------------------------------------------------------------------
# 4. Structured failure: exception + element HTML snapshot
# ---------------------------------------------------------------------------

def test_failure_captures_element_html_snapshot_for_an_unknown_widget(page):
    _render(page, '<span id="f" class="mystery">not a form control</span>')
    field = describe_field(page.locator("#f"), label="Mystery Field", page=page)

    outcome = fill_field(field, "some value")

    assert outcome.failure is not None
    assert outcome.failure.element_html is not None
    assert "mystery" in outcome.failure.element_html
    assert "not a form control" in outcome.failure.element_html


def test_failure_report_includes_element_html_when_present(page):
    _render(page, '<span id="f">not a form control</span>')
    field = describe_field(page.locator("#f"), label="Mystery Field", page=page)

    from automation.forms.field_handlers import format_failure_report

    outcome = fill_field(field, "some value")
    report = format_failure_report(outcome.failure)

    assert "Element HTML:" in report
