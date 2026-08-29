"""
Verifying that a custom dropdown actually COMMITTED its selection.

The bug these cover, seen on a real American Express application: the handler
logged `Option selected for 'Phone Number'` and then `Verification failed …
Actual value: (none)`, three times, for Phone, Country and State. Each field was
selected correctly; the check was reading the wrong thing.

`get_attribute("value")` returns the value baked into the HTML at parse time. A
JS-driven combobox commits to the DOM *property* and never writes the attribute,
so a correctly-filled field read back as empty — and, because a failed field was
also never marked examined, the whole retry budget was then spent a second time
under a different label.

Nothing here is Amex-specific. The fixtures below are the four ways real
component libraries expose a committed selection; a widget typically exposes
ONE of them, which is why verification consults several.
"""

from __future__ import annotations

import pytest

from automation.forms.field_handlers import (
    ComboboxHandler,
    CountryPickerHandler,
    _committed_value_from_hidden_twin,
    _read_dropdown_displayed_value,
    _selected_option_via_active_descendant,
    describe_field,
)


@pytest.fixture
def page(requires_chromium):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


# --- the four real-world shapes -------------------------------------------

def _property_only(field_name: str, label: str, committed: str) -> str:
    """An <input role=combobox> whose value is committed as a DOM PROPERTY, with
    no `value=` attribute, and whose popup is UNMOUNTED after selection.

    The unmounting matters and is not incidental. An earlier version of this
    fixture left the popup in the DOM with its option still
    `aria-selected="false"` — which is indistinguishable from a genuinely
    failed selection, and rightly failed verification. React-style components
    routinely unmount the listbox on close, and when they do the input property
    is the only surviving signal. That is the case this models.

    HONEST LIMITATION: this is a plausible model of Amex's `cx-select-input`,
    not a capture of it. The real post-selection DOM has not been inspected —
    doing so needs a run against the live site. If Amex instead keeps an
    unselected option mounted, verification will (deliberately) still fail and
    escalate to a human rather than risk a false "filled".
    """
    return f"""
      <label for="f">{label}</label>
      <input id="f" name="{field_name}" type="text" role="combobox"
             aria-autocomplete="list" aria-controls="lb" aria-expanded="false"
             class="cx-select-input">
      <div id="lb"></div>
      <script>
        // Commit: set the property, then unmount the listbox as the component does.
        const i = document.getElementById('f');
        i.value = {committed!r};
        document.getElementById('lb').remove();
      </script>
    """


def _active_descendant_only(committed: str) -> str:
    """Popup unmounted after commit; only `aria-activedescendant` survives."""
    return f"""
      <label for="f">Country</label>
      <input id="f" name="country" type="text" role="combobox"
             aria-activedescendant="opt-x" aria-expanded="false">
      <div id="opt-x" role="option" style="display:none">{committed}</div>
    """


def _aria_selected_only(committed: str) -> str:
    return f"""
      <label for="f">State</label>
      <div id="f" role="combobox" aria-controls="lb2" aria-expanded="false"></div>
      <div id="lb2" role="listbox">
        <div role="option" aria-selected="false">Somewhere else</div>
        <div role="option" aria-selected="true">{committed}</div>
      </div>
    """


def _hidden_twin_only(committed: str) -> str:
    """Visible input shows nothing; a hidden input of the same name carries the
    value that will actually be submitted."""
    return f"""
      <form>
        <label for="f">State</label>
        <input id="f" name="region" type="text" role="combobox" aria-expanded="false">
        <input type="hidden" name="region" value="{committed}">
      </form>
    """


def _field(page, html, label, attribute=None):
    page.set_content(html)
    return describe_field(page.locator("#f"), label=label, page=page, profile_attribute=attribute)


# ---------------------------------------------------------------------------
# The three fields that failed on the real form
# ---------------------------------------------------------------------------

def test_phone_selection_is_verified_from_the_live_dom_property(page):
    """Phone, exactly as Amex renders it. Before the fix this read `None`."""
    field = _field(page, _property_only("phoneNumber", "Phone Number", "+918979011405"), "Phone Number")
    ok, actual = ComboboxHandler().verify(field, "+918979011405")
    assert ok, f"committed value not detected; verification saw {actual!r}"
    assert actual == "+918979011405"


def test_country_selection_is_verified(page):
    field = _field(page, _property_only("country", "Country", "India"), "Country", attribute="country")
    ok, actual = CountryPickerHandler().verify(field, "India")
    assert ok, f"country not verified; saw {actual!r}"


def test_state_selection_is_verified(page):
    field = _field(page, _property_only("region2", "State", "Uttar Pradesh"), "State")
    ok, actual = ComboboxHandler().verify(field, "Uttar Pradesh")
    assert ok, f"state not verified; saw {actual!r}"


def test_the_old_attribute_read_is_what_used_to_fail(page):
    """Pins the ROOT CAUSE so a future refactor cannot quietly reintroduce it:
    the HTML attribute really is absent on a correctly-filled field."""
    page.set_content(_property_only("phoneNumber", "Phone Number", "+918979011405"))
    locator = page.locator("#f")
    assert locator.get_attribute("value") is None, "the attribute is absent — this was the bug"
    assert locator.input_value() == "+918979011405", "the live property holds the committed value"


# ---------------------------------------------------------------------------
# Each signal independently
# ---------------------------------------------------------------------------

def test_a_committed_value_is_found_via_active_descendant_alone(page):
    """The popup is gone; only the ARIA pointer remains."""
    field = _field(page, _active_descendant_only("India"), "Country", attribute="country")
    assert _selected_option_via_active_descendant(field) == "India"
    assert CountryPickerHandler().verify(field, "India")[0]


def test_a_committed_value_is_found_via_aria_selected_alone(page):
    """A div-based combobox with no input to read at all."""
    field = _field(page, _aria_selected_only("Uttar Pradesh"), "State")
    assert ComboboxHandler().verify(field, "Uttar Pradesh")[0]


def test_a_committed_value_is_found_via_a_hidden_twin(page):
    field = _field(page, _hidden_twin_only("Uttar Pradesh"), "State")
    assert _committed_value_from_hidden_twin(field) == "Uttar Pradesh"
    assert ComboboxHandler().verify(field, "Uttar Pradesh")[0]


def test_a_hidden_twin_elsewhere_on_the_page_is_not_borrowed(page):
    """Scoped to the field's own form. A same-named hidden input belonging to a
    different form must never be read as this field's committed value — that
    would report success for a field that was never filled."""
    page.set_content("""
      <form><input id="f" name="region" type="text" role="combobox"></form>
      <form><input type="hidden" name="region" value="Somewhere Else"></form>
    """)
    field = describe_field(page.locator("#f"), label="State", page=page)
    assert _committed_value_from_hidden_twin(field) is None


# ---------------------------------------------------------------------------
# Genuine failures must still fail
# ---------------------------------------------------------------------------

def test_an_uncommitted_selection_still_fails_verification(page):
    """The safety half. Loosening verification must not make it accept a field
    the widget never actually filled — that would let the run click Next past
    an empty required field."""
    page.set_content("""
      <label for="f">Phone Number</label>
      <input id="f" name="phoneNumber" type="text" role="combobox" aria-expanded="false">
      <div id="lb" role="listbox" style="display:none">
        <div role="option" aria-selected="false">+918979011405</div>
      </div>
    """)
    field = describe_field(page.locator("#f"), label="Phone Number", page=page)
    ok, actual = ComboboxHandler().verify(field, "+918979011405")
    assert ok is False, f"empty field wrongly verified as filled (saw {actual!r})"


def test_the_wrong_option_is_reported_as_wrong_not_as_missing(page):
    """Distinguishes "committed the wrong thing" from "committed nothing" — the
    failure log's `Actual value` is how that is diagnosed."""
    field = _field(page, _property_only("country", "Country", "Indonesia"), "Country", attribute="country")
    ok, actual = CountryPickerHandler().verify(field, "India")
    assert ok is False
    assert actual == "Indonesia", "the observed value must be reported, not None"


def test_a_mounted_popup_reporting_no_selection_beats_leftover_search_text(page):
    """The safety rule that constrains all of the above.

    A searchable combobox still holds whatever text the automation typed to
    filter it. If the popup is present and says nothing is selected, that is
    authoritative — trusting the input text instead would verify a failed
    search for "Germany" as a successful selection of Germany, and let the run
    click Next past an empty required field.
    """
    page.set_content("""
      <input id="f" name="country" type="text" role="combobox"
             aria-controls="lb" aria-expanded="false">
      <div id="lb" role="listbox" style="display:none">
        <div role="option" aria-selected="false">India</div>
      </div>
      <script>document.getElementById('f').value = 'Germany';</script>
    """)
    field = describe_field(page.locator("#f"), label="Country", page=page, profile_attribute="country")
    assert CountryPickerHandler().verify(field, "Germany")[0] is False


def test_verification_never_reads_a_closed_menus_options_as_the_value(page):
    """Regression guard on an older fix: the target text sitting in a CLOSED
    menu must not count as a committed selection."""
    page.set_content("""
      <div id="f" role="combobox" aria-expanded="false">
        <div style="display:none"><div role="option">India</div></div>
      </div>
    """)
    field = describe_field(page.locator("#f"), label="Country", page=page)
    assert ComboboxHandler().verify(field, "India")[0] is False


# ---------------------------------------------------------------------------
# No duplicate retries
# ---------------------------------------------------------------------------

def test_a_field_is_marked_examined_even_when_the_fill_fails():
    """The other half of the wasted-time bug: an unmarked failure was
    rediscovered by the name/placeholder sweep and retried to the identical
    outcome. Both call sites must mark unconditionally."""
    import inspect

    from automation.ats import base

    for fn in (base.ATSAdapter._fill_first_match, base.ATSAdapter._fill_questions_by_label):
        src = inspect.getsource(fn)
        assert "_mark_examined" in src
        marker = src.index("_mark_examined")
        preceding = src[:marker].rstrip().splitlines()[-1].strip()
        assert not preceding.startswith("if outcome.filled"), (
            f"{fn.__name__} marks examined only on success — a failed field will be retried twice"
        )
