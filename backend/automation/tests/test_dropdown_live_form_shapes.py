"""
Regression tests for three bugs that only appear on a REAL application form
and that every previous fixture was too clean to catch. All three were found
by running `field_handlers` against the live Greenhouse posting at
`job-boards.greenhouse.io` and are pinned here so they stay fixed:

1. **A decoy option container.** `_LISTBOX_SELECTOR`'s `div[class*='dropdown' i]`
   branch matches intl-tel-input's phone-country wrapper, which is permanently
   visible with its 244 country options hidden inside. Anything that stopped at
   the FIRST visible container (`_find_listbox_container`) inspected that decoy,
   saw no visible options, and concluded the dropdown was empty.

2. **Page-global search-input lookup.** Every react-select question is an
   `input[role='combobox'][aria-autocomplete='list']`, so a form with several
   of them has several `_SEARCH_INPUT_SELECTOR` matches, all visible.
   `_find_search_input(page)` returns the first one — so filling question 3
   typed into question 1.

3. **The value is not on the resolved element.** `<label for=...>` points at
   react-select's `input`, which has no children, goes `opacity: 0` once a
   value is chosen, and has its `value` cleared by the library. Verification
   read that element and saw nothing, so a correctly-filled dropdown reported
   `verification_failed` after burning all 3 attempts.

The fixture below reproduces that markup — two react-select widgets plus the
phone-widget decoy — rather than the single, tidy dropdown the other test files
use.
"""

from __future__ import annotations

from automation.forms.field_handlers import describe_field, fill_field, read_field_options

#: A visible `div[class*='dropdown' i]` whose options are all hidden — the
#: intl-tel-input phone widget, present on every Greenhouse application.
_PHONE_DECOY = """
<div class="iti iti--allow-dropdown iti--show-flags iti--inline-dropdown">
  <div class="iti__dropdown-content iti__hide" role="dialog" style="display:none">
    <ul class="iti__country-list" role="listbox">
      <li class="iti__country" role="option">India (+91)</li>
      <li class="iti__country" role="option">United States (+1)</li>
    </ul>
  </div>
  <input type="tel" id="phone" class="iti__tel-input">
</div>
"""


def _react_select(widget_id: str, label: str, options: list[str]) -> str:
    """One react-select-shaped question, matching the real class names
    (`select__container`/`select__control`/`select__input`/`select__menu-list`)
    and the real behaviours that broke things: the menu mounts only on click,
    the input goes `opacity: 0` on selection, its `value` is cleared, and the
    chosen text is written to a SIBLING `select__single-value`."""
    opts = "".join(f"<div class=\"select__option\" role=\"option\">{o}</div>" for o in options)
    return f"""
<div class="select__container">
  <label id="{widget_id}-label" for="{widget_id}" class="label select__label">{label}</label>
  <div class="select-shell">
    <div class="select__control">
      <div class="select__value-container">
        <div class="select__placeholder" id="{widget_id}-placeholder">Select...</div>
        <div class="select__input-container">
          <input class="select__input" id="{widget_id}" type="text" role="combobox"
                 aria-autocomplete="list" aria-expanded="false" aria-haspopup="true"
                 autocomplete="off" tabindex="0" style="opacity:1">
        </div>
      </div>
    </div>
    <div class="menu-host" id="{widget_id}-host"></div>
  </div>
</div>
<script>
(function() {{
  const input = document.getElementById('{widget_id}');
  const host = document.getElementById('{widget_id}-host');
  const container = input.closest('.select__container');
  const control = container.querySelector('.select__control');
  const placeholder = document.getElementById('{widget_id}-placeholder');
  function close() {{ host.innerHTML = ''; input.setAttribute('aria-expanded', 'false'); }}
  function open() {{
    host.innerHTML = '<div class="select__menu"><div class="select__menu-list" role="listbox">{opts}</div></div>';
    input.setAttribute('aria-expanded', 'true');
    host.querySelectorAll('.select__option').forEach(function(opt) {{
      opt.addEventListener('click', function() {{
        // react-select's real post-selection state
        const vc = control.querySelector('.select__value-container');
        let sv = vc.querySelector('.select__single-value');
        if (!sv) {{ sv = document.createElement('div'); sv.className = 'select__single-value'; vc.prepend(sv); }}
        sv.textContent = opt.textContent;
        if (placeholder) placeholder.style.display = 'none';
        input.value = '';
        input.style.opacity = '0';
        close();
      }});
    }});
  }}
  input.addEventListener('click', function() {{
    input.getAttribute('aria-expanded') === 'true' ? close() : open();
  }});
  document.addEventListener('keydown', function(e) {{ if (e.key === 'Escape') close(); }});
}})();
</script>
"""


def _render_form(page):
    page.set_content(
        "<!DOCTYPE html><html><body>"
        + _PHONE_DECOY
        + _react_select("q_aws", "Have you worked on AWS in production?", ["Yes", "No"])
        + _react_select("q_backend", "Do you have 3+ years of backend development experience?", ["Yes", "No"])
        + "</body></html>"
    )


def _field(page, widget_id: str, label: str):
    return describe_field(page.locator(f"#{widget_id}"), label=label, page=page)


# ---------------------------------------------------------------------------
# 1. The decoy container must not hide the real options
# ---------------------------------------------------------------------------

def test_options_are_read_past_a_visible_container_whose_options_are_hidden(page):
    _render_form(page)
    field = _field(page, "q_aws", "Have you worked on AWS in production?")

    assert read_field_options(field) == ("Yes", "No")


def test_the_hidden_phone_country_list_is_never_offered_as_this_fields_options(page):
    _render_form(page)
    field = _field(page, "q_aws", "Have you worked on AWS in production?")

    options = read_field_options(field)

    assert not any("+91" in o or "+1" in o for o in options)


# ---------------------------------------------------------------------------
# 2. Each widget must be filled through its OWN input
# ---------------------------------------------------------------------------

def test_filling_the_second_dropdown_does_not_type_into_the_first(page):
    """The bug this pins: with several `input[role='combobox']` on the page,
    a page-global search-input lookup returns the FIRST one, so every answer
    landed in question 1."""
    _render_form(page)
    second = _field(page, "q_backend", "Do you have 3+ years of backend development experience?")

    outcome = fill_field(second, "Yes")

    assert outcome.filled is True
    first_container = page.locator("#q_aws").locator("xpath=ancestor::div[contains(@class,'select__container')][1]")
    assert "Yes" not in (first_container.inner_text() or "")
    assert first_container.locator(".select__single-value").count() == 0


def test_both_dropdowns_can_be_filled_independently_with_different_values(page):
    _render_form(page)

    first = fill_field(_field(page, "q_aws", "AWS?"), "Yes")
    second = fill_field(_field(page, "q_backend", "Backend?"), "No")

    assert (first.filled, second.filled) == (True, True)
    assert page.locator("#q_aws-label").locator(
        "xpath=following::div[contains(@class,'select__single-value')][1]"
    ).text_content().strip() == "Yes"
    assert page.locator("#q_backend-label").locator(
        "xpath=following::div[contains(@class,'select__single-value')][1]"
    ).text_content().strip() == "No"


# ---------------------------------------------------------------------------
# 3. Verification must find the value on the sibling, not the resolved input
# ---------------------------------------------------------------------------

def test_a_correctly_filled_dropdown_verifies_even_though_the_input_is_empty(page):
    """react-select clears the input's value and sets `opacity: 0` on
    selection; the chosen text lives in a sibling `select__single-value`.
    Reading only the resolved element reported `verification_failed` on a
    dropdown that was in fact filled."""
    _render_form(page)
    field = _field(page, "q_aws", "Have you worked on AWS in production?")

    outcome = fill_field(field, "Yes")

    assert outcome.filled is True
    assert outcome.actual_value == "Yes"
    assert page.locator("#q_aws").input_value() == ""  # the input really is empty


def test_verification_does_not_read_the_label_as_the_value(page):
    """`select__container` includes the question's own label, and
    `_values_match` is substring-tolerant both ways — so verifying "No"
    against a whole container's text would be satisfied by the word "not" in
    the label. Only the explicit display-value node may be read."""
    page.set_content(
        "<!DOCTYPE html><html><body>"
        + _react_select(
            "q_reloc",
            "Are you currently staying in Bangalore? If not, are you willing to relocate?",
            ["Yes", "No"],
        )
        + "</body></html>"
    )
    field = _field(page, "q_reloc", "Are you currently staying in Bangalore? If not, are you willing to relocate?")

    # Nothing has been selected yet, so verification must NOT claim "No"
    # is already displayed just because the label contains "not".
    handler_outcome = fill_field(field, "No")

    assert handler_outcome.filled is True
    assert handler_outcome.actual_value == "No"
    assert page.locator(".select__single-value").text_content().strip() == "No"
