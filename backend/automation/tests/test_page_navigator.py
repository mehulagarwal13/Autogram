"""
automation/applications/page_navigator.py — the "did the form actually
advance?" layer that makes long, multi-page applications work.

Everything here runs against a real rendered page (the session `browser`
fixture), because every claim being made is about real DOM behaviour: what
changes when a field is filled versus when a page turns, what a click does when
something is covering the button, and what a form looks like when it silently
refuses to move on.

The two properties that matter most, and that the rest of the multi-page loop
is built on top of:

- filling a field must NOT look like navigation (otherwise every page reports
  success and the run never leaves page 1), and
- a page that revealed new fields must NOT be confused with a page that turned
  (otherwise a conditional follow-up question is skipped as though it were on
  the previous page).
"""

import pytest

from automation.applications import page_navigator
from automation.applications.page_navigator import (
    PageSignature,
    advance_to_next_page,
    capture_page_signature,
    click_control,
    visible_control_identities,
)
from automation.browser.selectors import (
    dismiss_overlays,
    find_page_heading,
    find_step_indicator,
    has_loading_overlay,
    looks_like_review_page,
    wait_for_overlays_to_clear,
)

# A two-page form that behaves the way a real one does: the heading changes,
# page 1's fields go away, page 2's appear, and the button relabels itself to
# "Submit" at the end.
_TWO_PAGE_FORM = """
<html><body>
  <h1 id="hdr">Step 1 of 2</h1>
  <div id="p0"><input name="first_name"><input name="email"></div>
  <div id="p1" style="display:none"><textarea name="why_us"></textarea></div>
  <button id="nav" onclick="turn()">Next</button>
  <script>
    var i = 0;
    function turn() {
      document.getElementById('p' + i).style.display = 'none';
      i = i + 1;
      document.getElementById('p' + i).style.display = 'block';
      document.getElementById('hdr').textContent = 'Step ' + (i + 1) + ' of 2';
      document.getElementById('nav').textContent = 'Submit';
    }
  </script>
</body></html>
"""

# A page whose Next button does nothing except show an error — the shape that
# used to be indistinguishable from a successful step.
_REJECTING_FORM = """
<html><body>
  <h1>Application Questions</h1>
  <input name="start_date" required>
  <div class="field-error" style="display:none" id="err">Start date is required.</div>
  <button onclick="document.getElementById('err').style.display='block'">Next</button>
</body></html>
"""

_CONDITIONAL_FORM = """
<html><body>
  <h1>Work Authorization</h1>
  <select name="needs_sponsorship" onchange="document.getElementById('followup').style.display='block'">
    <option value="">Select</option><option value="yes">Yes</option>
  </select>
  <div id="followup" style="display:none"><input name="visa_type"></div>
</body></html>
"""


# ---------------------------------------------------------------------------
# Control identities and page signatures
# ---------------------------------------------------------------------------

def test_signature_is_stable_when_nothing_changes(page):
    page.set_content(_TWO_PAGE_FORM)
    first = capture_page_signature(page)
    second = capture_page_signature(page)
    assert not first.differs_from(second)


def test_filling_a_field_does_not_look_like_navigation(page):
    """The single most important property in this module. The signature is
    built from control IDENTITIES, never values — a check that fired on "the
    page changed because we typed into it" would report every page as
    successfully advanced and the run would never leave page 1."""
    page.set_content(_TWO_PAGE_FORM)
    before = capture_page_signature(page)

    page.fill("input[name='first_name']", "Ada")
    page.fill("input[name='email']", "ada@example.com")

    assert not capture_page_signature(page).differs_from(before)


def test_turning_the_page_changes_the_signature(page):
    page.set_content(_TWO_PAGE_FORM)
    before = capture_page_signature(page)

    page.click("#nav")

    after = capture_page_signature(page)
    assert after.differs_from(before)
    assert after.heading == "Step 2 of 2"
    assert before.heading == "Step 1 of 2"


def test_revealed_conditional_fields_are_reported_by_name(page):
    """A conditional follow-up appearing is a different event from the page
    turning, and the fill loop needs to tell them apart to know it must do
    another pass before navigating."""
    page.set_content(_CONDITIONAL_FORM)
    before = capture_page_signature(page)

    page.select_option("select[name='needs_sponsorship']", "yes")

    after = capture_page_signature(page)
    revealed = after.newly_visible_controls(before)
    assert any("visa_type" in control for control in revealed)
    assert after.control_count == before.control_count + 1


def test_hidden_controls_are_not_counted_as_visible(page):
    page.set_content(_TWO_PAGE_FORM)
    identities = visible_control_identities(page)
    assert any("first_name" in item for item in identities)
    assert not any("why_us" in item for item in identities)  # page 2, still display:none


def test_repeated_controls_stay_distinguishable(page):
    """Radio group members share a name; numbering the occurrences is what
    stops a group growing from two options to four from reading as unchanged."""
    page.set_content(
        "<html><body>"
        "<input type='radio' name='fluent' value='yes'>"
        "<input type='radio' name='fluent' value='no'>"
        "</body></html>"
    )
    identities = visible_control_identities(page)
    assert len(identities) == 2
    assert len(set(identities)) == 2


def test_signature_describe_is_log_friendly(page):
    page.set_content(_TWO_PAGE_FORM)
    description = capture_page_signature(page).describe()
    assert "Step 1 of 2" in description
    assert "field(s)" in description


def test_empty_signatures_compare_equal():
    assert not PageSignature().differs_from(PageSignature())


# ---------------------------------------------------------------------------
# advance_to_next_page
# ---------------------------------------------------------------------------

def test_advance_reports_success_when_the_form_really_moves(page):
    page.set_content(_TWO_PAGE_FORM)
    before = capture_page_signature(page)

    outcome = advance_to_next_page(page, page.locator("#nav"), before=before)

    assert outcome.advanced is True
    assert outcome.after.heading == "Step 2 of 2"
    assert outcome.click_failed is False


def test_advance_reports_failure_and_the_forms_own_reason_when_blocked(page):
    """The regression this whole module exists for: the click LANDS, the form
    stays put, and the old loop counted that as a completed step."""
    page.set_content(_REJECTING_FORM)
    before = capture_page_signature(page)

    outcome = advance_to_next_page(page, page.get_by_role("button", name="Next"), before=before)

    assert outcome.advanced is False
    assert outcome.click_failed is False  # the click itself was fine
    assert any("Start date is required" in message for message in outcome.validation_errors)
    assert "did not advance" in outcome.reason


def test_advance_says_so_when_the_click_cannot_be_delivered(page, monkeypatch):
    """`click_failed` separates "the button could not be pressed" from "the
    form pressed it and refused to move". The flow manager needs the
    distinction: only the first is worth another attempt."""
    monkeypatch.setattr(page_navigator, "_CLICK_TIMEOUT_MS", 300)
    monkeypatch.setattr(page_navigator, "_SCROLL_TIMEOUT_MS", 300)
    page.set_default_timeout(300)  # bounds the JS-fallback attempt too
    page.set_content("<html><body><h1>Page</h1></body></html>")

    outcome = advance_to_next_page(page, page.locator("#nowhere"), before=capture_page_signature(page))

    assert outcome.advanced is False
    assert outcome.click_failed is True


def test_a_visually_hidden_navigation_button_is_still_clicked(page, monkeypatch):
    """Not a curiosity: an ATS that keeps its real control off-screen behind a
    styled proxy is common enough that giving up on `display:none` would strand
    those forms. The DOM click ignores visibility, which is exactly why it is
    the last resort rather than the default."""
    monkeypatch.setattr(page_navigator, "_CLICK_TIMEOUT_MS", 500)
    page.set_content(
        "<html><body><h1 id='h'>Step 1</h1>"
        "<button style='display:none' onclick=\"document.getElementById('h').textContent='Step 2'\">Next</button>"
        "</body></html>"
    )

    outcome = advance_to_next_page(page, page.locator("button"), before=capture_page_signature(page))

    assert outcome.advanced is True
    assert outcome.after.heading == "Step 2"


# ---------------------------------------------------------------------------
# click_control — getting past what covers the button
# ---------------------------------------------------------------------------

def test_click_falls_back_to_js_when_an_overlay_intercepts(page, monkeypatch):
    """A full-page overlay makes Playwright's click time out. Recovering via a
    direct DOM click is what stops one stray "we use cookies" layer from
    stalling a five-page application on page 1."""
    monkeypatch.setattr(page_navigator, "_CLICK_TIMEOUT_MS", 700)
    page.set_content(
        "<html><body>"
        "<button id='nav' onclick=\"document.title='clicked'\">Next</button>"
        "<div style='position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.5)'></div>"
        "</body></html>"
    )

    clicked, detail = click_control(page, page.locator("#nav"))

    assert clicked is True
    assert "JS fallback" in detail
    assert page.title() == "clicked"


def test_click_reports_failure_when_the_element_is_gone(page, monkeypatch):
    monkeypatch.setattr(page_navigator, "_CLICK_TIMEOUT_MS", 300)
    monkeypatch.setattr(page_navigator, "_SCROLL_TIMEOUT_MS", 300)
    page.set_default_timeout(300)
    page.set_content("<html><body><p>no buttons here</p></body></html>")

    clicked, detail = click_control(page, page.locator("#missing"))

    assert clicked is False
    assert detail


# ---------------------------------------------------------------------------
# Overlays, step indicators, review pages (browser/selectors.py)
# ---------------------------------------------------------------------------

def test_dismiss_overlays_closes_a_cookie_banner(page):
    page.set_content(
        "<html><body>"
        "<div class='cookie-banner'><p>We use cookies.</p>"
        "<button onclick=\"this.closest('.cookie-banner').remove()\">Accept all</button></div>"
        "<form><input name='first_name'></form>"
        "</body></html>"
    )

    dismissed = dismiss_overlays(page)

    assert dismissed == ["Accept all"]
    assert page.locator(".cookie-banner").count() == 0


def test_dismiss_overlays_never_touches_the_forms_own_consent_checkbox(page):
    """"I agree" is in the dismiss vocabulary, so this is the guard that keeps
    it pointed exclusively at cookie/chat/newsletter containers. The form's own
    required privacy-policy consent is filled deliberately elsewhere (see
    `ATSAdapter._fill_consent_checkboxes`) and must never be touched here."""
    page.set_content(
        "<html><body><form>"
        "<label><input type='checkbox' id='consent' required> I agree to the Privacy Policy</label>"
        "<button type='button'>I agree</button>"
        "</form></body></html>"
    )

    assert dismiss_overlays(page) == []
    assert page.locator("#consent").is_checked() is False


def test_dismiss_overlays_is_a_no_op_on_a_clean_page(page):
    page.set_content(_TWO_PAGE_FORM)
    assert dismiss_overlays(page) == []


def test_loading_overlay_is_detected_only_while_visible(page):
    page.set_content(
        "<html><body>"
        "<div id='spin' class='loading-overlay'>Loading…</div>"
        "<button onclick=\"document.getElementById('spin').style.display='none'\">Done</button>"
        "</body></html>"
    )
    assert has_loading_overlay(page) is True

    page.click("button")

    assert has_loading_overlay(page) is False
    assert wait_for_overlays_to_clear(page, timeout_ms=1_000) is True


def test_step_indicator_is_read_from_the_pages_own_progress_text(page):
    page.set_content("<html><body><div class='progress-bar'>Step 3 of 5</div></body></html>")
    assert find_step_indicator(page) == "Step 3 of 5"


def test_step_indicator_is_empty_when_the_form_does_not_show_one(page):
    page.set_content("<html><body><h1>Apply</h1><input name='email'></body></html>")
    assert find_step_indicator(page) == ""


def test_page_heading_prefers_the_visible_heading(page):
    page.set_content("<html><body><h1>My Experience</h1><input name='x'></body></html>")
    assert find_page_heading(page) == "My Experience"


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<h1>Review your application</h1>", True),
        ("<h1>Review</h1>", True),
        ("<h1>My Information</h1><p>Please review the information you entered</p>", True),
        ("<h1>My Experience</h1><p>Tell us about your work history.</p>", False),
    ],
)
def test_review_page_detection(page, html, expected):
    page.set_content(f"<html><body>{html}</body></html>")
    assert looks_like_review_page(page) is expected
