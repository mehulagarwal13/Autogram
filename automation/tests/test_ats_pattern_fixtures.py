"""
Integration fixtures approximating real ATS markup conventions (audit item
6) — run through the REAL `DEFAULT_HANDLER_REGISTRY`/`fill_field()` pipeline,
not mocked. These are hand-built approximations (no live network access in
this environment — same constraint every prior phase's tests operate
under), modeled on each platform's well-documented public conventions:

- Greenhouse: `job_application[answers_attributes][N][...]`-style radio
  groups for custom Yes/No screening questions.
- Lever: a custom `.application-dropdown` trigger + options list for
  non-native-select fields (Lever uses plain `<select>` for some fields —
  already covered by `NativeSelectHandler` tests — and a custom dropdown for
  others).
- Workday: `data-automation-id`-tagged, ARIA-combobox-based custom widgets
  whose options mount asynchronously and are rendered in a limited,
  scrollable window — Workday itself has no dedicated `ATSAdapter` yet
  (`automation/ats/workday/` is still a stub), so this exercises the generic
  `ComboboxHandler` fallback's readiness for that platform's markup shape.

Where a real widget likely omits the ARIA attributes these fixtures include,
that's noted inline — `field_handlers.py`'s widget-detection is
ARIA/class-convention-based by design (see its module docstring), so a
production adapter for these platforms would need to confirm the real
markup exposes (or add) equivalent hints.
"""

from __future__ import annotations

from automation.forms.field_handlers import (
    ComboboxHandler,
    CountryPickerHandler,
    DEFAULT_HANDLER_REGISTRY,
    RadioHandler,
    describe_field,
    fill_field,
)


def _render(page, html: str):
    page.set_content(f"<!DOCTYPE html><html><body>{html}</body></html>")


# ---------------------------------------------------------------------------
# Greenhouse-style radio group (native <input type=radio>, Rails-array name)
# ---------------------------------------------------------------------------

_GREENHOUSE_RADIO_HTML = """
<div class="field" id="s3q_9182736">
  <label for="job_application_answers_attributes_0_text_value">
    Will you now or in the future require sponsorship to work in the United States?
  </label>
  <div class="select_wrapper">
    <input type="radio" name="job_application[answers_attributes][0][text_value]"
           value="Yes" id="job_application_answers_attributes_0_text_value_1508267007">
    <label for="job_application_answers_attributes_0_text_value_1508267007">Yes</label>
    <input type="radio" name="job_application[answers_attributes][0][text_value]"
           value="No" id="job_application_answers_attributes_0_text_value_1508267008">
    <label for="job_application_answers_attributes_0_text_value_1508267008">No</label>
  </div>
</div>
"""


def test_greenhouse_style_sponsorship_radio_group_selects_no(page):
    _render(page, _GREENHOUSE_RADIO_HTML)
    # Whichever radio a label-sweep happens to resolve first — RadioHandler
    # re-derives the whole group via the shared Rails-array `name`.
    field = describe_field(
        page.locator("#job_application_answers_attributes_0_text_value_1508267007"),
        label="Will you now or in the future require sponsorship to work in the United States?",
        page=page,
    )
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), RadioHandler)

    outcome = fill_field(field, "No")

    assert outcome.filled is True
    assert page.locator("#job_application_answers_attributes_0_text_value_1508267008").is_checked() is True
    assert page.locator("#job_application_answers_attributes_0_text_value_1508267007").is_checked() is False


# ---------------------------------------------------------------------------
# Lever-style custom dropdown (non-native-select field)
# ---------------------------------------------------------------------------
# NOTE: modeled with aria-haspopup/aria-expanded on the trigger — Lever's own
# production markup may or may not expose these; field_handlers.py's
# ComboboxHandler is ARIA/class-driven by design, so a real LeverAdapter
# would need to confirm (or, if absent, that's a gap to raise against the
# live markup, not something to guess around here).

_LEVER_DROPDOWN_HTML = """
<div class="application-dropdown" data-qa="select-dropdown" tabindex="0"
     aria-haspopup="listbox" aria-expanded="false">Select an option...</div>
<div class="dropdown-options" style="display:none;">
  <div class="dropdown-option" data-qa="select-option">LinkedIn</div>
  <div class="dropdown-option" data-qa="select-option">Referral</div>
  <div class="dropdown-option" data-qa="select-option">Company Website</div>
</div>
<script>
  const trigger = document.querySelector('.application-dropdown');
  const menu = document.querySelector('.dropdown-options');
  trigger.addEventListener('click', () => { menu.style.display = 'block'; });
  document.querySelectorAll('.dropdown-option').forEach(opt => {
    opt.addEventListener('click', () => {
      trigger.textContent = opt.textContent;
      menu.style.display = 'none';
    });
  });
</script>
"""


def test_lever_style_dropdown_selects_referral(page):
    _render(page, _LEVER_DROPDOWN_HTML)
    field = describe_field(page.locator(".application-dropdown"), label="How did you hear about this position?", page=page)
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), ComboboxHandler)

    outcome = fill_field(field, "Referral")

    assert outcome.filled is True
    assert page.locator(".application-dropdown").text_content() == "Referral"


# ---------------------------------------------------------------------------
# Workday-style dynamic component: data-automation-id, async render, small
# rendered window (Workday has no dedicated adapter yet — see module
# docstring — this proves the generic ComboboxHandler fallback is ready).
# ---------------------------------------------------------------------------

_WORKDAY_DYNAMIC_HTML = """
<div data-automation-id="multiSelectContainer">
  <div data-automation-id="selectWidget" role="combobox" aria-haspopup="listbox"
       aria-expanded="false" tabindex="0">Select One</div>
</div>
<div data-automation-id="selectWidgetMenu" role="listbox" style="display:none; height:80px; overflow-y:auto;">
  <div id="wd-spacer" style="position:relative;"></div>
</div>
<script>
  const ITEM_HEIGHT = 20;
  const LABELS = ["United States", "Canada", "United Kingdom", "India", "Germany", "Australia"];
  const trigger = document.querySelector('[data-automation-id="selectWidget"]');
  const menu = document.querySelector('[data-automation-id="selectWidgetMenu"]');
  const spacer = document.getElementById('wd-spacer');
  spacer.style.height = (LABELS.length * ITEM_HEIGHT) + 'px';

  function render() {
    const scrollTop = menu.scrollTop;
    const startIndex = Math.max(0, Math.floor(scrollTop / ITEM_HEIGHT) - 1);
    spacer.querySelectorAll('.wd-opt').forEach(el => el.remove());
    for (let i = startIndex; i < Math.min(LABELS.length, startIndex + 3); i++) {
      const div = document.createElement('div');
      div.className = 'wd-opt';
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

  trigger.addEventListener('click', () => {
    // Workday's SPA re-render commonly lands a render tick (or more) after
    // the click, not synchronously inside the click handler.
    setTimeout(() => { menu.style.display = 'block'; render(); }, 250);
  });
  menu.addEventListener('scroll', render);
</script>
"""


def test_workday_style_async_virtualized_combobox_selects_a_late_option(page):
    _render(page, _WORKDAY_DYNAMIC_HTML)
    field = describe_field(
        page.locator('[data-automation-id="selectWidget"]'), label="Country", page=page,
    )
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), ComboboxHandler)

    # "India" (not the very last item) — reachable well inside one scroll
    # step at this fixture's dimensions; see the CountryPickerHandler test
    # below for a target deliberately placed near the scroll-cap boundary.
    outcome = fill_field(field, "India")

    assert outcome.filled is True
    assert page.locator('[data-automation-id="selectWidget"]').text_content() == "India"


# ---------------------------------------------------------------------------
# CountryPickerHandler: virtualized dropdown + alias matching together
# (previously untested combination — audit item 4)
# ---------------------------------------------------------------------------

def test_country_picker_handles_alias_matching_inside_a_virtualized_dropdown(page):
    _render(
        page,
        """
        <div id="trigger" role="combobox" aria-haspopup="listbox" aria-expanded="false">Select a country...</div>
        <div id="menu" role="listbox" style="display:none; height:200px; overflow-y:auto;">
          <div id="spacer" style="position:relative;"></div>
        </div>
        <script>
          // clientHeight 200 -> ~160px scrollBy per attempt (see
          // scroll_container_until_option_found: max(clientHeight*0.8, 40)).
          // Target sits at index 55 of 60 — reachable in ~7 scroll
          // iterations, comfortably inside CountryPickerHandler's inherited
          // (from _DropdownHandler) default max_scroll_attempts=12. Placing
          // it at the absolute last index would NOT be reachable at these
          // dimensions within that cap — this is deliberately tuned to stay
          // inside the real, unmodified default rather than the handler
          // being tuned to fit the test.
          const ITEM_HEIGHT = 20;
          const TOTAL = 60;
          const LABELS = Array.from({length: TOTAL}, (_, i) => "Country " + i);
          LABELS[55] = "United States";  // profile stores the alias "USA"
          const trigger = document.getElementById('trigger');
          const menu = document.getElementById('menu');
          const spacer = document.getElementById('spacer');
          spacer.style.height = (TOTAL * ITEM_HEIGHT) + "px";

          function render() {
            const scrollTop = menu.scrollTop;
            const startIndex = Math.max(0, Math.floor(scrollTop / ITEM_HEIGHT) - 1);
            spacer.querySelectorAll('.opt').forEach(el => el.remove());
            for (let i = startIndex; i < Math.min(TOTAL, startIndex + 10); i++) {
              const div = document.createElement('div');
              div.className = 'opt';
              div.setAttribute('role', 'option');
              div.textContent = LABELS[i];
              div.style.position = 'absolute';
              div.style.top = (i * ITEM_HEIGHT) + 'px';
              div.style.height = ITEM_HEIGHT + 'px';
              div.addEventListener('click', () => { trigger.textContent = LABELS[i]; menu.style.display = 'none'; });
              spacer.appendChild(div);
            }
          }
          trigger.addEventListener('click', () => { menu.style.display = 'block'; render(); });
          menu.addEventListener('scroll', render);
        </script>
        """,
    )
    field = describe_field(page.locator("#trigger"), label="Country", page=page, profile_attribute="country")
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), CountryPickerHandler)

    outcome = fill_field(field, "USA")  # profile value is the alias; the option reads "United States"

    assert outcome.filled is True
    assert outcome.actual_value == "United States"


# ---------------------------------------------------------------------------
# Phone country-code selector: DOCUMENTED GAP, not silently claimed as solved
# ---------------------------------------------------------------------------
# `FieldMapper.FIELD_SYNONYMS` has no entry that maps a "phone country code"
# label/name to the `country` profile attribute (and shouldn't — conflating
# "which country is your candidate profile's address in" with "which
# calling-code prefix does your phone number use" would be a real, silent
# correctness bug: the two can legitimately differ). The practical
# consequence, made explicit here rather than left as an unstated
# assumption: a phone country-code dropdown never gets `profile_attribute
# == "country"`, so `CountryPickerHandler`'s alias-aware matching never
# applies to it — it falls to whatever generic handler matches its DOM shape
# (`ComboboxHandler`/`NativeSelectHandler`) with plain substring matching
# only. This is a known, unresolved gap (would need either a new profile
# concept for "phone calling code" or a deliberate product decision on how
# to derive one from `country`), not something this audit silently patches.

def test_phone_country_code_selector_is_not_routed_through_country_picker(page):
    _render(
        page,
        """
        <select id="phone_code">
          <option value="+1">United States (+1)</option>
          <option value="+91">India (+91)</option>
        </select>
        """,
    )
    # No profile_attribute resolved for this field today — FieldMapper has no
    # synonym mapping a phone-country-code label to "country" (see comment
    # above), so the caller would pass profile_attribute=None here, exactly
    # as FieldMapper's real output would be for this label.
    field = describe_field(page.locator("#phone_code"), label="Phone Country Code", page=page, profile_attribute=None)

    handler = DEFAULT_HANDLER_REGISTRY.get_handler(field)

    assert not isinstance(handler, CountryPickerHandler)  # confirmed gap, not a false claim of support
