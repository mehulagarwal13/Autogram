"""
Phase 8 additions to automation/forms/field_handlers.py: ToggleHandler,
VirtualizedListboxHandler, the hardened CheckboxHandler (hidden input +
label pattern) and RadioHandler (ARIA role=radio, button-based choices), and
structured failure context/reporting (PART 6/8/9/10/13).

Same convention as test_field_handlers.py: real rendered widgets via
Playwright's `page.set_content()`, including hand-built fixtures for widget
shapes no real ATS-agnostic library provides out of the box.
"""

from __future__ import annotations

from automation.forms.field_handlers import (
    DEFAULT_HANDLER_REGISTRY,
    RadioHandler,
    ToggleHandler,
    VirtualizedListboxHandler,
    FieldHandlerRegistry,
    describe_field,
    fill_field,
    format_failure_report,
)


def _render(page, html: str):
    page.set_content(f"<!DOCTYPE html><html><body>{html}</body></html>")


# ---------------------------------------------------------------------------
# ToggleHandler — role="switch" (PART 10)
# ---------------------------------------------------------------------------

_TOGGLE_HTML = """
<div id="relocate" role="switch" aria-checked="false" tabindex="0">Willing to relocate</div>
<script>
  const el = document.getElementById('relocate');
  el.addEventListener('click', () => {
    const now = el.getAttribute('aria-checked') === 'true';
    el.setAttribute('aria-checked', (!now).toString());
  });
</script>
"""


def test_registry_picks_toggle_handler_for_a_switch_role(page):
    _render(page, _TOGGLE_HTML)
    field = describe_field(page.locator("#relocate"), label="Willing to relocate?", page=page)
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), ToggleHandler)


def test_toggle_handler_switches_from_off_to_on(page):
    _render(page, _TOGGLE_HTML)
    field = describe_field(page.locator("#relocate"), label="Willing to relocate?", page=page)

    outcome = fill_field(field, True)

    assert outcome.filled is True
    assert page.locator("#relocate").get_attribute("aria-checked") == "true"


def test_toggle_handler_leaves_an_already_correct_switch_alone(page):
    _render(page, _TOGGLE_HTML.replace('aria-checked="false"', 'aria-checked="true"'))
    field = describe_field(page.locator("#relocate"), label="Willing to relocate?", page=page)

    outcome = fill_field(field, True)  # already "true" — must NOT click and flip it to "false"

    assert outcome.filled is True
    assert page.locator("#relocate").get_attribute("aria-checked") == "true"


# ---------------------------------------------------------------------------
# VirtualizedListboxHandler — always-expanded role="listbox" panel (PART 6)
# ---------------------------------------------------------------------------

def _virtualized_listbox_html(total: int) -> str:
    # ITEM_HEIGHT=20, container height 200 (10 fully visible at once — "only
    # ~10 visible initially" per the PART 5/6 spec), render window 12 items.
    # scrollBy per attempt is ~160 (clientHeight*0.8); reaching the very last
    # item of a 120-item list takes ~14 scroll iterations — comfortably
    # inside VirtualizedListboxHandler's default 20-attempt cap.
    return f"""
    <div id="countries" role="listbox" style="height:200px; overflow-y:auto; position:relative;">
      <div id="spacer" style="position:relative;"></div>
    </div>
    <script>
      const ITEM_HEIGHT = 20;
      const TOTAL = {total};
      const LABELS = Array.from({{length: TOTAL}}, (_, i) => "Country " + i);
      LABELS[TOTAL - 1] = "India";  // the target sits at the very end
      const listbox = document.getElementById('countries');
      const spacer = document.getElementById('spacer');
      spacer.style.height = (TOTAL * ITEM_HEIGHT) + "px";
      let selected = null;

      function render() {{
        const scrollTop = listbox.scrollTop;
        const startIndex = Math.max(0, Math.floor(scrollTop / ITEM_HEIGHT) - 1);
        spacer.querySelectorAll('.opt').forEach(el => el.remove());
        for (let i = startIndex; i < Math.min(TOTAL, startIndex + 12); i++) {{
          const div = document.createElement('div');
          div.className = 'opt';
          div.setAttribute('role', 'option');
          div.setAttribute('aria-selected', (i === selected) ? 'true' : 'false');
          div.textContent = LABELS[i];
          div.style.position = 'absolute';
          div.style.top = (i * ITEM_HEIGHT) + 'px';
          div.style.height = ITEM_HEIGHT + 'px';
          div.addEventListener('click', () => {{ selected = i; render(); }});
          spacer.appendChild(div);
        }}
      }}
      listbox.addEventListener('scroll', render);
      render();
    </script>
    """


def test_registry_picks_virtualized_listbox_handler_for_an_expanded_listbox(page):
    _render(page, _virtualized_listbox_html(20))
    field = describe_field(page.locator("#countries"), label="Country", page=page)
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), VirtualizedListboxHandler)


def test_virtualized_listbox_handler_scrolls_to_find_an_option_among_100_plus(page):
    _render(page, _virtualized_listbox_html(120))  # only ~5 options ever rendered at once
    field = describe_field(page.locator("#countries"), label="Country", page=page)

    outcome = fill_field(field, "India")

    assert outcome.filled is True
    assert "India" in (outcome.actual_value or "")


def test_virtualized_listbox_handler_reports_structured_failure_without_hanging(page):
    _render(page, _virtualized_listbox_html(20))
    field = describe_field(page.locator("#countries"), label="Country", page=page)
    # A small custom registry with a tight scroll-attempt cap — proves the
    # search terminates (PART 12: "no infinite loops") rather than looping
    # forever hunting for a value that will never appear.
    handler = VirtualizedListboxHandler()
    handler.max_scroll_attempts = 3
    registry = FieldHandlerRegistry([handler])

    outcome = fill_field(field, "Nonexistent Country", registry=registry)

    assert outcome.filled is False
    assert outcome.failure is not None
    assert outcome.failure.failure_reason == "verification_failed"
    assert outcome.failure.widget_type == "VirtualizedListboxHandler"


# ---------------------------------------------------------------------------
# CheckboxHandler — hidden <input> + <label> (PART 9)
# ---------------------------------------------------------------------------

def test_checkbox_handler_clicks_the_label_when_the_input_itself_is_hidden(page):
    _render(page, '<input id="consent" type="checkbox" hidden><label for="consent">I agree to the Privacy Policy</label>')
    field = describe_field(page.locator("#consent"), label="I agree to the Privacy Policy", page=page)

    outcome = fill_field(field, True)

    assert outcome.filled is True
    assert page.locator("#consent").is_checked() is True


# A real custom checkbox is a styled, clickable box. The original fixture gave
# the <div> no dimensions, and an empty unstyled <div> renders at zero height —
# so every click strategy failed on an element with no clickable surface, and
# the test was measuring an impossible fixture rather than the handler.
#
# `isTrusted` separates a genuine browser click (Playwright's, `true`) from a
# `dispatchEvent` one (the JS fallback's, `false`), which is what lets the tests
# below assert WHICH strategy actually did the work instead of just checking the
# end state — the distinction that matters, since the JS fallback is the least
# preferred path and must not quietly become the one that always runs.
_CUSTOM_CHECKBOX_HTML = """
<div id="custom" role="checkbox" aria-checked="false" tabindex="0"
     style="width:20px;height:20px;border:1px solid #666"></div>
<script>
  window.__trustedClicks = 0;
  window.__syntheticClicks = 0;
  document.getElementById('custom').addEventListener('click', function(e) {
    if (e.isTrusted) { window.__trustedClicks++; } else { window.__syntheticClicks++; }
    this.setAttribute('aria-checked', this.getAttribute('aria-checked') === 'true' ? 'false' : 'true');
  });
</script>
"""

# Same widget, but nothing listens for the click — a "dead" custom checkbox
# (handler failed to bind, or the framework hydrated only part of the page).
_INERT_CUSTOM_CHECKBOX_HTML = """
<div id="custom" role="checkbox" aria-checked="false" tabindex="0"
     style="width:20px;height:20px;border:1px solid #666"></div>
"""

# Covers the viewport, so Playwright's click hit-target check fails on every
# attempt (`safe_click` never uses force=True) and the handler is driven all the
# way down to attempt 5 — the only reliable way to exercise the JS fallback
# through the real `fill_field` path rather than by calling it directly.
_BLOCKING_OVERLAY_HTML = (
    "<div id='overlay' style='position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.01)'></div>"
)


def test_checkbox_handler_verifies_via_aria_checked_for_a_custom_widget(page):
    _render(page, _CUSTOM_CHECKBOX_HTML)
    field = describe_field(page.locator("#custom"), label="I agree", page=page)

    outcome = fill_field(field, True)

    assert outcome.filled is True
    assert page.locator("#custom").get_attribute("aria-checked") == "true"


def test_a_real_click_is_what_checks_a_properly_sized_custom_checkbox(page):
    """The preferred path, pinned explicitly: a clickable custom checkbox is
    ticked by a genuine browser click (attempt 3), never by the JS fallback."""
    _render(page, _CUSTOM_CHECKBOX_HTML)
    field = describe_field(page.locator("#custom"), label="I agree", page=page)

    outcome = fill_field(field, True)

    assert outcome.filled is True
    assert page.locator("#custom").get_attribute("aria-checked") == "true"
    assert page.evaluate("() => window.__trustedClicks") == 1
    assert page.evaluate("() => window.__syntheticClicks") == 0


def test_the_js_fallback_flips_aria_checked_when_every_click_is_blocked(page):
    """The real gap. With clicks intercepted, the fallback used to assign
    `el.checked` on a `<div>` — an expando property with no effect on a
    `role="checkbox"` widget — and dispatch input/change, which such widgets
    don't listen for. It now dispatches a click the widget's own handler
    receives, so the component's internal state stays consistent with the
    attribute."""
    _render(page, _CUSTOM_CHECKBOX_HTML + _BLOCKING_OVERLAY_HTML)
    field = describe_field(page.locator("#custom"), label="I agree", page=page)

    outcome = fill_field(field, True)

    assert outcome.filled is True
    assert page.locator("#custom").get_attribute("aria-checked") == "true"
    # It really was the fallback, not a click that squeezed past the overlay.
    assert page.evaluate("() => window.__trustedClicks") == 0
    assert page.evaluate("() => window.__syntheticClicks") >= 1
    # And the meaningless write is gone: `.checked` was never assigned, so it
    # is still `undefined` on this <div>.
    assert page.locator("#custom").evaluate("el => el.checked") is None


def test_the_js_fallback_forces_aria_checked_on_a_widget_with_no_click_handler(page):
    """The other half of the fallback: dispatching a click achieves nothing on
    a widget with no listener, so the attribute is written directly — the
    genuine last resort, and still correct state rather than a no-op."""
    _render(page, _INERT_CUSTOM_CHECKBOX_HTML + _BLOCKING_OVERLAY_HTML)
    field = describe_field(page.locator("#custom"), label="I agree", page=page)

    outcome = fill_field(field, True)

    assert outcome.filled is True
    assert page.locator("#custom").get_attribute("aria-checked") == "true"
    assert page.locator("#custom").evaluate("el => el.checked") is None


def test_the_js_fallback_still_uses_checked_for_a_native_input(page):
    """The native branch must be untouched: a real `<input type=checkbox>` does
    have `.checked`, and input/change are the right events for it."""
    _render(page, "<input id='native' type='checkbox'>" + _BLOCKING_OVERLAY_HTML)
    field = describe_field(page.locator("#native"), label="I agree", page=page)

    outcome = fill_field(field, True)

    assert outcome.filled is True
    assert page.locator("#native").is_checked() is True


# ---------------------------------------------------------------------------
# RadioHandler — ARIA role="radio" group (PART 8)
# ---------------------------------------------------------------------------

_ARIA_RADIO_HTML = """
<div role="radiogroup" aria-label="Willing to relocate">
  <div role="radio" aria-checked="false" tabindex="0">Yes</div>
  <div role="radio" aria-checked="false" tabindex="0">No</div>
</div>
<script>
  const options = document.querySelectorAll('[role="radio"]');
  options.forEach(opt => {
    opt.addEventListener('click', () => {
      options.forEach(o => o.setAttribute('aria-checked', 'false'));
      opt.setAttribute('aria-checked', 'true');
    });
  });
</script>
"""


def test_registry_picks_radio_handler_for_an_aria_radio_group(page):
    _render(page, _ARIA_RADIO_HTML)
    field = describe_field(page.locator('[role="radio"]').first, label="Willing to relocate", page=page)
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), RadioHandler)


def test_radio_handler_selects_the_matching_aria_radio_option(page):
    _render(page, _ARIA_RADIO_HTML)
    field = describe_field(page.locator('[role="radio"]').first, label="Willing to relocate", page=page)

    outcome = fill_field(field, "No")

    assert outcome.filled is True
    options = page.locator('[role="radio"]')
    assert options.nth(0).get_attribute("aria-checked") == "false"
    assert options.nth(1).get_attribute("aria-checked") == "true"


# ---------------------------------------------------------------------------
# RadioHandler — button-based choices (PART 8)
# ---------------------------------------------------------------------------

_BUTTON_CHOICE_HTML = """
<div id="group">
  <button type="button" aria-pressed="false">Yes</button>
  <button type="button" aria-pressed="false">No</button>
</div>
<script>
  const buttons = document.querySelectorAll('#group button');
  buttons.forEach(btn => {
    btn.addEventListener('click', () => {
      buttons.forEach(b => b.setAttribute('aria-pressed', 'false'));
      btn.setAttribute('aria-pressed', 'true');
    });
  });
</script>
"""


def test_registry_picks_radio_handler_for_aria_pressed_buttons(page):
    _render(page, _BUTTON_CHOICE_HTML)
    field = describe_field(page.locator("#group button").first, label="Requires sponsorship", page=page)
    assert isinstance(DEFAULT_HANDLER_REGISTRY.get_handler(field), RadioHandler)


def test_radio_handler_selects_the_matching_button_choice(page):
    _render(page, _BUTTON_CHOICE_HTML)
    field = describe_field(page.locator("#group button").first, label="Requires sponsorship", page=page)

    outcome = fill_field(field, "No")

    assert outcome.filled is True
    buttons = page.locator("#group button")
    assert buttons.nth(0).get_attribute("aria-pressed") == "false"
    assert buttons.nth(1).get_attribute("aria-pressed") == "true"


def test_registry_does_not_claim_an_ordinary_button_with_no_aria_pressed(page):
    _render(page, '<button type="button">Submit</button>')
    field = describe_field(page.locator("button"), label="Submit", page=page)
    handler = DEFAULT_HANDLER_REGISTRY.get_handler(field)
    assert not isinstance(handler, RadioHandler)


# ---------------------------------------------------------------------------
# Structured failure reporting (PART 13)
# ---------------------------------------------------------------------------

def test_fill_field_attaches_caller_supplied_context_to_the_failure(page):
    _render(page, '<span id="f">not a form control</span>')
    field = describe_field(page.locator("#f"), label="Mystery Field", page=page)

    outcome = fill_field(field, "some value", context={"ats_type": "greenhouse", "url": "https://boards.greenhouse.io/acme/jobs/1"})

    assert outcome.failure is not None
    assert outcome.failure.context == {"ats_type": "greenhouse", "url": "https://boards.greenhouse.io/acme/jobs/1"}
    assert outcome.failure.widget_type == "unknown"


def test_format_failure_report_includes_every_field(page):
    _render(page, '<span id="f">not a form control</span>')
    field = describe_field(page.locator("#f"), label="Mystery Field", page=page)

    outcome = fill_field(field, "some value", context={"ats_type": "lever", "url": "https://jobs.lever.co/acme/1"})
    report = format_failure_report(outcome.failure)

    assert "FIELD AUTOMATION FAILURE" in report
    assert "Mystery Field" in report
    assert "no_handler_matched" in report
    assert "ats_type: lever" in report
    assert "url: https://jobs.lever.co/acme/1" in report


def test_format_failure_report_never_raises_with_empty_context(page):
    _render(page, '<span id="f">not a form control</span>')
    field = describe_field(page.locator("#f"), label="Mystery Field", page=page)

    outcome = fill_field(field, "some value")  # no context supplied at all
    report = format_failure_report(outcome.failure)

    assert "FIELD AUTOMATION FAILURE" in report
