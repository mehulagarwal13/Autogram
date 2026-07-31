"""
`field_handlers.read_field_options()` — what the answer engine gets to choose
among, read off real rendered widgets via Playwright's `page.set_content()`
(same convention as test_field_handlers.py).

The Greenhouse-shaped fixture below is the one that matters: on a real
posting every screening question is a custom dropdown whose options don't
exist in the DOM until the trigger is clicked. An earlier version of this
function refused to open them on the principle that pre-answer introspection
should be side-effect-free, which meant the engine answered every real
dropdown blind. These tests pin both halves of the compromise: the options
DO get read, and the widget is left closed and unset afterwards.
"""

from __future__ import annotations

from automation.forms.field_handlers import describe_field, read_field_options


def _render(page, html: str):
    page.set_content(f"<!DOCTYPE html><html><body>{html}</body></html>")


# ---------------------------------------------------------------------------
# Native <select> — read directly, no interaction
# ---------------------------------------------------------------------------

def test_native_select_options_are_read_verbatim(page):
    _render(page, """
        <label for="auth">Are you legally authorized to work?</label>
        <select id="auth">
          <option value="">Select...</option>
          <option value="y">Yes</option>
          <option value="n">No</option>
        </select>
    """)
    field = describe_field(page.locator("#auth"), label="Are you legally authorized to work?", page=page)

    assert read_field_options(field) == ("Yes", "No")


def test_placeholder_entries_are_not_offered_as_choices(page):
    _render(page, """
        <select id="years">
          <option>-- Please select --</option>
          <option>0-2</option>
          <option>3-5</option>
          <option>5+</option>
        </select>
    """)
    field = describe_field(page.locator("#years"), label="Years of experience", page=page)

    assert read_field_options(field) == ("0-2", "3-5", "5+")


# ---------------------------------------------------------------------------
# Radio groups — reuse RadioHandler's own grouping, no interaction
# ---------------------------------------------------------------------------

def test_native_radio_group_options_are_read_from_their_labels(page):
    _render(page, """
        <input type="radio" name="relocate" id="r_yes"><label for="r_yes">Yes, I will relocate</label>
        <input type="radio" name="relocate" id="r_no"><label for="r_no">No</label>
    """)
    field = describe_field(page.locator("#r_yes"), label="Willing to relocate?", page=page)

    assert read_field_options(field) == ("Yes, I will relocate", "No")


def test_reading_radio_options_does_not_select_any_of_them(page):
    _render(page, """
        <input type="radio" name="relocate" id="r_yes"><label for="r_yes">Yes</label>
        <input type="radio" name="relocate" id="r_no"><label for="r_no">No</label>
    """)
    field = describe_field(page.locator("#r_yes"), label="Willing to relocate?", page=page)

    read_field_options(field)

    assert page.locator("#r_yes").is_checked() is False
    assert page.locator("#r_no").is_checked() is False


# ---------------------------------------------------------------------------
# Custom dropdown — the real Greenhouse shape: options exist only once open
# ---------------------------------------------------------------------------

#: Mirrors what a Greenhouse screening dropdown renders: a combobox trigger
#: showing "Select...", and a menu that is only added to the DOM on click.
#: Escape closes it, exactly as react-select and friends behave.
_GREENHOUSE_DROPDOWN = """
<label for="aws">Have you worked on AWS in production?</label>
<div id="aws" role="combobox" aria-expanded="false" aria-haspopup="listbox" tabindex="0">Select...</div>
<div id="menu-host"></div>
<script>
  const trigger = document.getElementById('aws');
  const host = document.getElementById('menu-host');
  function close() {
    host.innerHTML = '';
    trigger.setAttribute('aria-expanded', 'false');
  }
  trigger.addEventListener('click', () => {
    if (trigger.getAttribute('aria-expanded') === 'true') { close(); return; }
    host.innerHTML =
      '<div role="listbox">' +
      '<div role="option">Yes</div>' +
      '<div role="option">No</div>' +
      '</div>';
    trigger.setAttribute('aria-expanded', 'true');
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  host.addEventListener('click', (e) => {
    if (e.target.getAttribute('role') === 'option') {
      trigger.textContent = e.target.textContent;
      close();
    }
  });
</script>
"""


def test_a_custom_dropdown_is_opened_to_read_its_options(page):
    _render(page, _GREENHOUSE_DROPDOWN)
    field = describe_field(page.locator("#aws"), label="Have you worked on AWS in production?", page=page)

    assert read_field_options(field) == ("Yes", "No")


def test_probing_a_custom_dropdown_closes_it_again(page):
    _render(page, _GREENHOUSE_DROPDOWN)
    field = describe_field(page.locator("#aws"), label="Have you worked on AWS in production?", page=page)

    read_field_options(field)

    assert page.locator("#aws").get_attribute("aria-expanded") == "false"
    assert page.locator("[role='listbox']").count() == 0


def test_probing_a_custom_dropdown_never_selects_a_value(page):
    """The trigger is clicked; an option never is. The field must read
    exactly as it did before — this runs before anything has been decided."""
    _render(page, _GREENHOUSE_DROPDOWN)
    field = describe_field(page.locator("#aws"), label="Have you worked on AWS in production?", page=page)

    read_field_options(field)

    assert (page.locator("#aws").text_content() or "").strip() == "Select..."


def test_a_dropdown_that_never_opens_degrades_to_free_text(page):
    """An `aria-haspopup` trigger whose click handler does nothing (or is
    blocked) must come back as "no options" rather than hang or raise —
    the question then takes the free-text path exactly as before."""
    _render(page, """
        <div id="dead" role="combobox" aria-haspopup="listbox" tabindex="0">Select...</div>
    """)
    field = describe_field(page.locator("#dead"), label="Broken dropdown", page=page)

    assert read_field_options(field) == ()


def test_an_opened_menu_with_no_option_container_is_not_scraped_from_the_page(page):
    """Guard against the tempting `body` fallback: if opening the widget
    produces no real listbox, unrelated list items elsewhere on the page must
    NOT be returned as though they were this field's choices."""
    _render(page, """
        <ul><li>Careers</li><li>About us</li><li>Privacy</li></ul>
        <div id="odd" role="combobox" aria-haspopup="listbox" tabindex="0">Select...</div>
    """)
    field = describe_field(page.locator("#odd"), label="Odd widget", page=page)

    assert read_field_options(field) == ()


# ---------------------------------------------------------------------------
# Everything else stays free-text
# ---------------------------------------------------------------------------

def test_a_plain_text_input_has_no_options(page):
    _render(page, '<label for="why">Why do you want to work here?</label><textarea id="why"></textarea>')
    field = describe_field(page.locator("#why"), label="Why do you want to work here?", page=page)

    assert read_field_options(field) == ()
