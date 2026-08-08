"""
automation/forms/field_handlers.py — tested against real rendered widgets via
Playwright's `page.set_content()` (same convention as test_selectors.py /
test_greenhouse_adapter.py), including hand-built react-select-style and
virtualized-dropdown-style fixtures (small inline `<script>`s) so the
open/search/scroll/verify algorithm is exercised for real, not mocked.
"""

from __future__ import annotations

import pytest

from automation.forms.field_handlers import (
    DEFAULT_HANDLER_REGISTRY,
    ComboboxHandler,
    CountryPickerHandler,
    NativeSelectHandler,
    ReactSelectHandler,
    TextInputHandler,
    describe_field,
    fill_field,
)


def _render(page, html: str):
    page.set_content(f"<!DOCTYPE html><html><body>{html}</body></html>")


# ---------------------------------------------------------------------------
# Registry ordering / handler selection
# ---------------------------------------------------------------------------

def test_registry_picks_text_input_handler_for_a_plain_input(page):
    _render(page, '<input id="f" type="text">')
    field = describe_field(page.locator("#f"), label="f", page=page)
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), TextInputHandler)


def test_registry_picks_native_select_handler_for_a_select_even_when_labeled_country(page):
    _render(page, '<select id="f"><option>United States</option></select>')
    field = describe_field(page.locator("#f"), label="Country", page=page, profile_attribute="country")
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), NativeSelectHandler)


def test_registry_picks_country_picker_over_generic_combobox_for_a_country_field(page):
    _render(page, '<div id="f" role="combobox" aria-haspopup="listbox"></div>')
    field = describe_field(page.locator("#f"), label="Country", page=page, profile_attribute="country")
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), CountryPickerHandler)


def test_registry_returns_none_for_an_unrecognizable_widget(page):
    _render(page, '<span id="f">just some text</span>')
    field = describe_field(page.locator("#f"), label="f", page=page)
    assert DEFAULT_HANDLER_REGISTRY.get_handler(field) is None


# ---------------------------------------------------------------------------
# fill_field() orchestration: happy path, unknown type, retry -> structured failure
# ---------------------------------------------------------------------------

def test_fill_field_fills_and_verifies_a_plain_text_input(page):
    _render(page, '<input id="f" type="text">')
    field = describe_field(page.locator("#f"), label="Email", page=page)

    outcome = fill_field(field, "ada@example.com")

    assert outcome.filled is True
    assert outcome.failure is None
    assert page.locator("#f").input_value() == "ada@example.com"


def test_fill_field_accepts_phone_display_formatting(page):
    # Mirrors Greenhouse's international-phone widget: it reformats a valid
    # number while keeping every digit intact.
    _render(
        page,
        """
        <input id="phone" type="tel">
        <script>
          document.getElementById('phone').addEventListener('input', (event) => {
            if (event.target.value.replace(/\\D/g, '') === '15550100') {
              event.target.value = '+1 555-010-0';
            }
          });
        </script>
        """,
    )
    field = describe_field(page.locator("#phone"), label="Phone", page=page)

    outcome = fill_field(field, "+15550100")

    assert outcome.filled is True
    assert outcome.failure is None
    assert page.locator("#phone").input_value() == "+1 555-010-0"


def test_fill_field_skips_empty_values_without_touching_the_dom(page):
    _render(page, '<input id="f" type="text" value="untouched">')
    field = describe_field(page.locator("#f"), label="f", page=page)

    outcome = fill_field(field, None)

    assert outcome.filled is False
    assert outcome.failure is None
    assert page.locator("#f").input_value() == "untouched"


def test_fill_field_reports_a_structured_failure_for_an_unknown_widget(page):
    _render(page, '<span id="f">not a form control</span>')
    field = describe_field(page.locator("#f"), label="Mystery Field", page=page)

    outcome = fill_field(field, "some value")

    assert outcome.filled is False
    assert outcome.failure is not None
    assert outcome.failure.failure_reason == "no_handler_matched"
    assert outcome.failure.field_label == "Mystery Field"
    assert outcome.failure.retry_count == 0


def test_fill_field_retries_and_reports_structured_failure_when_verification_never_passes(page):
    # A hostile input that resets itself on every change — simulates a
    # widget whose value never actually "sticks" so verification always fails.
    _render(
        page,
        """
        <input id="f" type="text">
        <script>
            // Synchronous reset within the same 'input' event dispatch that
            // Playwright's fill() triggers — simulates a widget whose value
            // never actually "sticks".
            document.getElementById('f').addEventListener('input', (e) => {
                e.target.value = '';
            });
        </script>
        """,
    )
    field = describe_field(page.locator("#f"), label="Stubborn Field", page=page)

    outcome = fill_field(field, "Ada")

    assert outcome.filled is False
    assert outcome.failure is not None
    assert outcome.failure.failure_reason == "verification_failed"
    assert outcome.failure.retry_count == 2  # DEFAULT_MAX_ATTEMPTS (3) - 1
    assert outcome.failure.expected_value == "Ada"


# ---------------------------------------------------------------------------
# TextAreaHandler / NativeSelectHandler
# ---------------------------------------------------------------------------

def test_textarea_handler_fills_and_verifies(page):
    _render(page, '<textarea id="f"></textarea>')
    field = describe_field(page.locator("#f"), label="Cover letter", page=page)
    outcome = fill_field(field, "I would love to work here.")
    assert outcome.filled is True
    assert page.locator("#f").input_value() == "I would love to work here."


def test_native_select_handler_matches_a_slightly_longer_option_label(page):
    _render(
        page,
        """
        <select id="f">
          <option value="">Select a country</option>
          <option value="US">United States of America</option>
          <option value="CA">Canada</option>
        </select>
        """,
    )
    field = describe_field(page.locator("#f"), label="Country", page=page, profile_attribute="country")
    outcome = fill_field(field, "United States")
    assert outcome.filled is True
    assert page.locator("#f").input_value() == "US"


# ---------------------------------------------------------------------------
# CheckboxHandler / RadioHandler
# ---------------------------------------------------------------------------

def test_checkbox_handler_checks_for_an_affirmative_value(page):
    _render(page, '<input id="f" type="checkbox">')
    field = describe_field(page.locator("#f"), label="I am authorized to work", page=page)
    outcome = fill_field(field, "Yes")
    assert outcome.filled is True
    assert page.locator("#f").is_checked() is True


@pytest.mark.parametrize("profile_text", [
    "Authorized",
    "Not authorized",
    "I am not authorized to work",
    "Requires H1B sponsorship",
    "US Citizen",
])
def test_checkbox_handler_refuses_free_text_instead_of_ticking_a_declaration(page, profile_text):
    """This test previously asserted the OPPOSITE for "Authorized": any string
    not on a small negative-words blocklist ticked the box.

    That was not a harmless default. The identical code path ticked "I am
    authorized to work" when the profile's free-text `work_authorization`
    column said "Not authorized" or "Requires H1B sponsorship" — a false legal
    declaration on a real job application, because "not"/"requires" simply
    weren't on the blocklist. A checkbox is now only ever set from a genuinely
    boolean-shaped value; anything else refuses and goes to a human.
    """
    _render(page, '<input id="f" type="checkbox">')
    field = describe_field(page.locator("#f"), label="I am authorized to work", page=page)

    outcome = fill_field(field, profile_text)

    assert outcome.filled is False
    assert outcome.failure is not None
    assert outcome.failure.failure_reason == "non_boolean_checkbox_value"
    assert page.locator("#f").is_checked() is False


def test_checkbox_handler_unchecks_for_a_negative_value(page):
    _render(page, '<input id="f" type="checkbox" checked>')
    field = describe_field(page.locator("#f"), label="Subscribe to newsletter", page=page)
    outcome = fill_field(field, "No")
    assert outcome.filled is True
    assert page.locator("#f").is_checked() is False


def test_radio_handler_checks_the_option_matching_the_target_text(page):
    _render(
        page,
        """
        <label for="r-yes">Yes</label><input id="r-yes" type="radio" name="sponsorship" value="yes">
        <label for="r-no">No</label><input id="r-no" type="radio" name="sponsorship" value="no">
        """,
    )
    # Whichever radio the caller happened to resolve, RadioHandler re-derives
    # the whole group via its shared `name` and picks the one whose own
    # label text matches — here it's asked to fill the group with "No" even
    # though the resolved locator (r-yes) is the FIRST/wrong one.
    field = describe_field(page.locator("#r-yes"), label="Do you require sponsorship?", page=page)
    outcome = fill_field(field, "No")
    assert outcome.filled is True
    assert page.locator("#r-no").is_checked() is True
    assert page.locator("#r-yes").is_checked() is False


# ---------------------------------------------------------------------------
# FileUploadHandler
# ---------------------------------------------------------------------------

def test_file_upload_handler_verifies_via_the_files_property(page, tmp_path):
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 fake resume bytes")
    _render(page, '<input id="f" type="file">')

    field = describe_field(page.locator("#f"), label="resume_upload", page=page)
    outcome = fill_field(field, str(resume_path))

    assert outcome.filled is True
    assert outcome.actual_value == "resume.pdf"


def test_file_upload_handler_works_on_a_hidden_input(page, tmp_path):
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 fake resume bytes")
    _render(page, '<input id="f" type="file" style="display:none;">')

    field = describe_field(page.locator("#f"), label="resume_upload", page=page)
    outcome = fill_field(field, str(resume_path))

    assert outcome.filled is True  # FileUploadHandler never checks is_visible()


def test_file_upload_handler_accepts_a_windows_style_stored_path(page, tmp_path):
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 fake resume bytes")
    _render(page, '<input id="f" type="file">')

    field = describe_field(page.locator("#f"), label="resume_upload", page=page)
    windows_style_path = str(resume_path).replace("/", "\\")
    outcome = fill_field(field, windows_style_path)

    assert outcome.filled is True
    assert outcome.actual_value == "resume.pdf"


def test_file_upload_handler_reports_a_missing_file_without_crashing(page):
    _render(page, '<input id="f" type="file">')
    field = describe_field(page.locator("#f"), label="resume_upload", page=page)

    outcome = fill_field(field, "storage\\documents\\resume\\missing.pdf")

    assert outcome.filled is False
    assert outcome.failure is not None
    assert outcome.failure.failure_reason == "upload_file_missing"


# ---------------------------------------------------------------------------
# ReactSelectHandler — fake react-select-style widget (search-based)
# ---------------------------------------------------------------------------

_REACT_SELECT_HTML = """
<div id="country">
  <div class="react-select__control" tabindex="0">
    <div class="react-select__value-container">
      <div class="react-select__placeholder">Select...</div>
    </div>
  </div>
  <div class="react-select__menu" role="listbox" style="display:none;">
    <input class="react-select__input" aria-autocomplete="list">
    <div class="react-select__menu-list">
      <div class="react-select__option" role="option">United States</div>
      <div class="react-select__option" role="option">Canada</div>
      <div class="react-select__option" role="option">United Kingdom</div>
    </div>
  </div>
</div>
<script>
  const control = document.querySelector('.react-select__control');
  const menu = document.querySelector('.react-select__menu');
  const input = document.querySelector('.react-select__input');
  const valueContainer = document.querySelector('.react-select__value-container');
  control.addEventListener('click', () => { menu.style.display = 'block'; input.focus(); });
  input.addEventListener('input', () => {
    const query = input.value.toLowerCase();
    document.querySelectorAll('.react-select__option').forEach(opt => {
      opt.style.display = opt.textContent.toLowerCase().includes(query) ? '' : 'none';
    });
  });
  document.querySelectorAll('.react-select__option').forEach(opt => {
    opt.addEventListener('click', () => {
      valueContainer.innerHTML = '<div class="react-select__single-value">' + opt.textContent + '</div>';
      menu.style.display = 'none';
    });
  });
</script>
"""


def test_react_select_handler_is_selected_and_fills_via_search(page):
    _render(page, _REACT_SELECT_HTML)
    field = describe_field(page.locator(".react-select__control"), label="Country", page=page)
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), ReactSelectHandler)

    outcome = fill_field(field, "United States")

    assert outcome.filled is True
    assert "United States" in (outcome.actual_value or "")
    assert page.locator(".react-select__menu").is_visible() is False  # closed after selecting


# ---------------------------------------------------------------------------
# CountryPickerHandler — alias-aware matching ("USA" -> "United States").
# Deliberately a plain, un-filtered listbox with no search input: a fake
# widget's own JS-side substring filter (see _REACT_SELECT_HTML above) would
# hide "United States" the instant "USA" is typed into it (same limitation
# a real react-select's default filter would have) — the alias resolution
# this handler adds is about matching against the FULL option list, not
# about out-smarting a widget's own search filtering.
# ---------------------------------------------------------------------------

_COUNTRY_LISTBOX_NO_SEARCH_HTML = """
<div id="trigger" role="combobox" aria-haspopup="listbox">Select a country...</div>
<div id="menu" role="listbox" style="display:none;">
  <div class="opt" role="option">United States</div>
  <div class="opt" role="option">Canada</div>
  <div class="opt" role="option">United Kingdom</div>
</div>
<script>
  const trigger = document.getElementById('trigger');
  const menu = document.getElementById('menu');
  trigger.addEventListener('click', () => { menu.style.display = 'block'; });
  document.querySelectorAll('.opt').forEach(opt => {
    opt.addEventListener('click', () => {
      trigger.textContent = opt.textContent;
      menu.style.display = 'none';
    });
  });
</script>
"""


def test_country_picker_handler_resolves_a_common_alias(page):
    _render(page, _COUNTRY_LISTBOX_NO_SEARCH_HTML)
    field = describe_field(page.locator("#trigger"), label="Country", page=page, profile_attribute="country")
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), CountryPickerHandler)

    outcome = fill_field(field, "USA")  # profile stores "USA"; the option reads "United States"

    assert outcome.filled is True
    assert outcome.actual_value == "United States"


# ---------------------------------------------------------------------------
# ComboboxHandler — generic ARIA dropdown, no search input, scrollable/
# virtualized-lite listbox (Issue 4: only a window of options is ever
# rendered; only the CONTAINER should scroll, never the page).
# ---------------------------------------------------------------------------

_SCROLLABLE_DROPDOWN_HTML = """
<div id="trigger" role="combobox" aria-haspopup="listbox" aria-expanded="false">Select a role...</div>
<div id="menu" role="listbox" style="display:none; height:90px; overflow-y:auto;">
  <div id="spacer" style="position:relative;"></div>
</div>
<script>
  const ITEM_HEIGHT = 30;
  const TOTAL = 10;
  const LABELS = Array.from({length: TOTAL}, (_, i) => "Option " + i);
  const trigger = document.getElementById('trigger');
  const menu = document.getElementById('menu');
  const spacer = document.getElementById('spacer');
  spacer.style.height = (TOTAL * ITEM_HEIGHT) + "px";

  function render() {
    const scrollTop = menu.scrollTop;
    const startIndex = Math.max(0, Math.floor(scrollTop / ITEM_HEIGHT) - 1);
    spacer.querySelectorAll('.opt').forEach(el => el.remove());
    for (let i = startIndex; i < Math.min(TOTAL, startIndex + 4); i++) {
      const div = document.createElement('div');
      div.className = 'opt';
      div.setAttribute('role', 'option');
      div.textContent = LABELS[i];
      div.style.position = 'absolute';
      div.style.top = (i * ITEM_HEIGHT) + 'px';
      div.style.height = ITEM_HEIGHT + 'px';
      div.addEventListener('click', () => {
        trigger.textContent = LABELS[i];
        menu.style.display = 'none';
      });
      spacer.appendChild(div);
    }
  }
  trigger.addEventListener('click', () => { menu.style.display = 'block'; render(); });
  menu.addEventListener('scroll', render);
  render();
</script>
"""


def test_combobox_handler_is_selected_for_a_generic_aria_dropdown(page):
    _render(page, _SCROLLABLE_DROPDOWN_HTML)
    field = describe_field(page.locator("#trigger"), label="Role", page=page)
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), ComboboxHandler)


def test_combobox_handler_scrolls_the_container_not_the_page_to_find_a_later_option(page):
    _render(page, _SCROLLABLE_DROPDOWN_HTML)
    field = describe_field(page.locator("#trigger"), label="Role", page=page)

    outcome = fill_field(field, "Option 7")  # only ~4 options are ever rendered at once

    assert outcome.filled is True
    assert page.locator("#trigger").text_content() == "Option 7"
    assert page.locator("#menu").is_visible() is False  # closed after selecting


def test_combobox_handler_reports_structured_failure_when_no_option_ever_matches(page):
    _render(page, _SCROLLABLE_DROPDOWN_HTML)
    field = describe_field(page.locator("#trigger"), label="Role", page=page)

    outcome = fill_field(field, "Nonexistent Option")

    assert outcome.filled is False
    assert outcome.failure is not None
    assert outcome.failure.failure_reason == "verification_failed"


# ---------------------------------------------------------------------------
# Regression: dropdown-open timing/fallback-click robustness. Triggered by a
# real Greenhouse posting (PayPay India) where the react-select-style
# Country picker could not be selected. Two distinct, real-world failure
# modes for _DropdownHandler.fill()'s "click to open" step, fixed by
# _wait_for_popup (poll, don't blindly query once) and a fallback click on a
# nested control element.
# ---------------------------------------------------------------------------

_ASYNC_RENDER_DROPDOWN_HTML = """
<div id="trigger" role="combobox" aria-haspopup="listbox">Select a country...</div>
<div id="menu" role="listbox" style="display:none;">
  <div class="opt" role="option">United States</div>
  <div class="opt" role="option">Canada</div>
</div>
<script>
  const trigger = document.getElementById('trigger');
  const menu = document.getElementById('menu');
  trigger.addEventListener('click', () => {
    // Simulates a menu portal that mounts a render tick after the click
    // event fires, rather than synchronously inside the click handler —
    // querying for it immediately (no wait at all) misses it.
    setTimeout(() => { menu.style.display = 'block'; }, 400);
  });
  document.querySelectorAll('.opt').forEach(opt => {
    opt.addEventListener('click', () => {
      trigger.textContent = opt.textContent;
      menu.style.display = 'none';
    });
  });
</script>
"""


def test_dropdown_handler_waits_for_a_menu_that_renders_asynchronously_after_click(page):
    _render(page, _ASYNC_RENDER_DROPDOWN_HTML)
    field = describe_field(page.locator("#trigger"), label="Country", page=page)

    outcome = fill_field(field, "Canada")

    assert outcome.filled is True
    assert page.locator("#trigger").text_content() == "Canada"


_WRAPPED_REACT_SELECT_HTML = """
<div id="wrapper" style="position:relative; height:200px;">
  <div class="react-select__control" tabindex="0"
       style="position:absolute; top:0; left:0; width:100%; height:20px;">
    <div class="react-select__value-container">
      <div class="react-select__placeholder">Select...</div>
    </div>
  </div>
</div>
<div class="react-select__menu" role="listbox" style="display:none;">
  <div class="react-select__option" role="option">United States</div>
  <div class="react-select__option" role="option">Canada</div>
</div>
<script>
  const control = document.querySelector('.react-select__control');
  const menu = document.querySelector('.react-select__menu');
  const valueContainer = document.querySelector('.react-select__value-container');
  control.addEventListener('click', () => { menu.style.display = 'block'; });
  document.querySelectorAll('.react-select__option').forEach(opt => {
    opt.addEventListener('click', () => {
      valueContainer.innerHTML = '<div class="react-select__single-value">' + opt.textContent + '</div>';
      menu.style.display = 'none';
    });
  });
</script>
"""


def test_dropdown_handler_falls_back_to_a_nested_control_when_the_resolved_element_has_no_click_handler_of_its_own(page):
    # Simulates a <label for=...> resolving to a non-interactive OUTER
    # wrapper (e.g. a Greenhouse container div) that is considerably taller
    # than the actual react-select control nested inside it — Playwright's
    # click() lands at the wrapper's own center, which misses the control
    # entirely (the control is absolutely positioned at the very top), so
    # the click never reaches the element with the real click listener.
    _render(page, _WRAPPED_REACT_SELECT_HTML)
    field = describe_field(page.locator("#wrapper"), label="Country", page=page, profile_attribute="country")

    outcome = fill_field(field, "Canada")

    assert outcome.filled is True
    assert "Canada" in (outcome.actual_value or "")
