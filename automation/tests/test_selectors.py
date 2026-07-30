"""
selectors.py (automation/browser/selectors.py) — tested against real rendered
HTML via Playwright's `page.set_content()`, not mocked Locator objects: this
module's whole job is DOM querying, so the meaningful test is "does it find
the right element in a real page." Requires Chromium to be installed
(`playwright install chromium`) — if it isn't, these tests skip with a clear
message rather than erroring, since that's an environment setup step, not a
code bug. The `browser`/`page` fixtures live in `conftest.py` (shared with
`test_detector.py`).
"""

from automation.browser.selectors import (
    find_file_upload_input,
    find_next_button,
    find_submit_button,
    find_upload_trigger_button,
    page_has_captcha,
)


def _render(page, html: str):
    page.set_content(f"<!DOCTYPE html><html><body>{html}</body></html>")


# ---------- find_next_button / find_submit_button ----------

def test_finds_continue_button_among_distractors(page):
    _render(
        page,
        """
        <button disabled>Continue</button>            <!-- disabled duplicate, skip -->
        <div style="display:none"><button>Continue</button></div>  <!-- hidden duplicate, skip -->
        <button id="real-continue">Continue</button>
        """,
    )
    found = find_next_button(page)
    assert found is not None
    assert found.get_attribute("id") == "real-continue"


def test_next_button_falls_back_to_loose_text_match_on_anchor(page):
    # Some ATS templates render "Next" as a styled <a>, not a <button>.
    _render(page, '<a href="#" class="btn-next">Next Step</a>')
    found = find_next_button(page)
    assert found is not None
    assert found.evaluate("el => el.tagName.toLowerCase()") == "a"


def test_returns_none_when_no_next_control_present(page):
    _render(page, "<button>Some Unrelated Button</button>")
    assert find_next_button(page) is None


def test_finds_submit_application_button(page):
    _render(page, '<button type="submit">Submit Application</button>')
    found = find_submit_button(page)
    assert found is not None


def test_submit_and_next_do_not_cross_match(page):
    # A page with only a "Next" button shouldn't be mistaken for having a submit control.
    _render(page, "<button>Next</button>")
    assert find_next_button(page) is not None
    assert find_submit_button(page) is None


# ---------- find_file_upload_input ----------

def test_finds_file_upload_input(page):
    _render(page, '<input type="text" name="email"><input type="file" name="resume">')
    found = find_file_upload_input(page)
    assert found is not None
    assert found.get_attribute("name") == "resume"


def test_returns_none_when_no_file_input(page):
    _render(page, '<input type="text" name="email">')
    assert find_file_upload_input(page) is None


def test_prefers_the_resume_hinted_input_when_multiple_file_inputs_exist(page):
    _render(
        page,
        """
        <input type="file" name="cover_letter">
        <input type="file" name="resume">
        <input type="file" name="additional_document">
        """,
    )
    found = find_file_upload_input(page)
    assert found is not None
    assert found.get_attribute("name") == "resume"


def test_falls_back_to_the_first_file_input_when_no_hint_matches(page):
    _render(
        page,
        """
        <input type="file" name="attachment_one">
        <input type="file" name="attachment_two">
        """,
    )
    found = find_file_upload_input(page)
    assert found is not None
    assert found.get_attribute("name") == "attachment_one"


# ---------- find_upload_trigger_button ----------

def test_finds_an_attach_button_for_lazily_revealed_upload_inputs(page):
    _render(page, '<button>Attach Resume</button>')
    found = find_upload_trigger_button(page)
    assert found is not None


def test_returns_none_when_no_upload_trigger_exists(page):
    _render(page, '<input type="text" name="email">')
    assert find_upload_trigger_button(page) is None


# ---------- page_has_captcha ----------

def test_detects_recaptcha_iframe(page):
    _render(page, '<iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>')
    assert page_has_captcha(page) is True


def test_detects_captcha_flavored_class(page):
    _render(page, '<div class="h-captcha" data-sitekey="x"></div>')
    assert page_has_captcha(page) is True


def test_no_false_positive_on_plain_form(page):
    _render(page, "<form><input type='text' name='first_name'></form>")
    assert page_has_captcha(page) is False
