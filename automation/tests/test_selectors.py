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
    find_apply_entry_button,
    find_file_upload_input,
    find_human_gate,
    find_job_posting_title_and_company,
    find_next_button,
    find_submission_confirmation,
    find_submit_button,
    find_unfilled_required_fields,
    find_upload_trigger_button,
    find_validation_errors,
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


# ---------- LinkedIn autofill exclusion ----------

def test_submit_button_ignores_apply_with_linkedin_and_finds_the_real_button(page):
    # The LinkedIn button is styled prominently and appears BEFORE the real
    # submit control in DOM order — exactly the Lever "Apply with LinkedIn"
    # layout this exclusion exists for. Without it, the loose substring pass
    # for "Apply" would match the LinkedIn button first (DOM order) and never
    # even reach the real one.
    _render(
        page,
        """
        <button class="linkedin-autofill">Apply with LinkedIn</button>
        <form class="application-form">
            <button id="real-submit">Apply for this job</button>
        </form>
        """,
    )
    found = find_submit_button(page)
    assert found is not None
    assert found.get_attribute("id") == "real-submit"


def test_next_button_ignores_continue_with_linkedin(page):
    _render(
        page,
        """
        <button>Continue with LinkedIn</button>
        <button id="real-continue">Continue</button>
        """,
    )
    found = find_next_button(page)
    assert found is not None
    assert found.get_attribute("id") == "real-continue"


def test_returns_none_rather_than_the_linkedin_button_when_nothing_else_matches(page):
    _render(page, '<button>Apply with LinkedIn</button>')
    assert find_submit_button(page) is None


def test_ignores_every_third_party_auth_and_account_control(page):
    # Each of these embeds a word this module actively searches for
    # ("Apply"/"Continue"/"Submit"), so each could be clicked by mistake.
    for label in [
        "Apply with Indeed",
        "Continue with Google",
        "Continue with GitHub",
        "Sign in to apply",
        "Sign up to continue",
        "Log in to submit your application",
        "Create an account to apply",
    ]:
        _render(page, f"<button>{label}</button>")
        assert find_submit_button(page) is None, f"must never offer to click {label!r}"
        assert find_next_button(page) is None, f"must never offer to click {label!r}"


def test_ignores_a_continue_button_inside_a_cookie_banner(page):
    # Textually indistinguishable from a real form step — only the container
    # gives it away. Clicking it dismisses an overlay while the flow manager
    # believes it advanced a step.
    _render(
        page,
        """
        <div class="cookie-consent-banner"><button>Continue</button></div>
        <form><button id="real-next">Continue</button></form>
        """,
    )
    found = find_next_button(page)
    assert found is not None
    assert found.get_attribute("id") == "real-next"


def test_ignores_a_chat_widget_and_newsletter_control(page):
    _render(
        page,
        """
        <div id="intercom-container"><button>Continue</button></div>
        <div class="newsletter-modal"><button>Continue</button></div>
        """,
    )
    assert find_next_button(page) is None


# ---------- find_submission_confirmation ----------

def test_detects_a_success_message_as_confirmation(page):
    _render(page, "<h1>Thank you for applying!</h1><p>We'll be in touch.</p>")
    assert find_submission_confirmation(page) is not None


def test_detects_an_application_reference_as_confirmation(page):
    _render(page, "<p>Application ID: AB-77213</p>")
    confirmation = find_submission_confirmation(page)
    assert confirmation is not None
    assert "reference" in confirmation


def test_no_confirmation_on_a_form_that_was_never_submitted(page):
    _render(page, '<form><input name="email"><button>Submit Application</button></form>')
    assert find_submission_confirmation(page) is None


def test_the_lever_verification_error_is_not_treated_as_confirmation(page):
    # The exact real-world banner that must never be recorded as "applied".
    _render(page, "<div>There was an error verifying your application. Please try again.</div>")
    assert find_submission_confirmation(page) is None


def test_a_hidden_success_template_is_not_confirmation(page):
    _render(page, '<div style="display:none">Thank you for applying</div>')
    assert find_submission_confirmation(page) is None


# ---------- find_validation_errors ----------

def test_finds_a_visible_validation_error_message(page):
    _render(page, '<span class="field-error">Please enter a valid phone number</span>')
    assert find_validation_errors(page) == ["Please enter a valid phone number"]


def test_ignores_an_empty_error_container(page):
    # Real ATS markup ships these permanently; they must not register as
    # failures on every run.
    _render(page, '<span class="field-error"></span><div class="error-message">  </div>')
    assert find_validation_errors(page) == []


def test_ignores_a_hidden_validation_error(page):
    _render(page, '<span class="field-error" style="display:none">Stale error</span>')
    assert find_validation_errors(page) == []


def test_reports_an_aria_invalid_field_by_name(page):
    _render(page, '<input name="phone" aria-invalid="true">')
    errors = find_validation_errors(page)
    assert len(errors) == 1
    assert "phone" in errors[0]


def test_clean_form_has_no_validation_errors(page):
    _render(page, '<form><input name="email" value="a@b.com"></form>')
    assert find_validation_errors(page) == []


# ---------- find_human_gate ----------

def test_a_visible_password_field_is_a_login_wall(page):
    _render(page, '<form><input type="password" name="pw"><button>Sign in</button></form>')
    gate = find_human_gate(page)
    assert gate is not None
    assert "login" in gate


def test_a_hidden_password_field_is_not_a_login_wall(page):
    # Combined sign-in/apply templates ship a dormant password input; treating
    # its mere presence as a wall would block every run against that posting.
    _render(page, '<input type="password" name="pw" style="display:none">')
    assert find_human_gate(page) is None


def test_detects_an_otp_gate_structurally(page):
    _render(page, '<input autocomplete="one-time-code" name="code">')
    gate = find_human_gate(page)
    assert gate is not None
    assert "one-time" in gate


def test_detects_an_mfa_gate_from_prose(page):
    _render(page, "<p>Enter the verification code we sent to your phone.</p>")
    assert find_human_gate(page) is not None


def test_detects_a_payment_gate(page):
    _render(page, "<p>Please enter your credit card number to continue.</p>")
    gate = find_human_gate(page)
    assert gate is not None
    assert "payment" in gate


def test_detects_an_identity_verification_gate(page):
    _render(page, "<p>Identity verification is required before you apply.</p>")
    assert find_human_gate(page) is not None


def test_no_human_gate_on_an_ordinary_application_form(page):
    _render(
        page,
        """
        <form>
            <label for="n">Full name</label><input id="n" name="name">
            <label for="e">Email</label><input id="e" name="email" type="email">
            <input type="file" name="resume">
            <button>Submit Application</button>
        </form>
        """,
    )
    assert find_human_gate(page) is None


# ---------- find_unfilled_required_fields ----------

def test_finds_an_empty_required_text_input(page):
    _render(page, '<input type="text" name="linkedin_url" required>')
    missing = find_unfilled_required_fields(page)
    assert missing == ["linkedin_url"]


def test_does_not_flag_a_required_field_that_already_has_a_value(page):
    _render(page, '<input type="text" name="full_name" value="Ada Lovelace" required>')
    assert find_unfilled_required_fields(page) == []


def test_does_not_flag_an_optional_empty_field(page):
    _render(page, '<input type="text" name="middle_name">')
    assert find_unfilled_required_fields(page) == []


def test_does_not_flag_a_hidden_required_field(page):
    _render(page, '<input type="text" name="hidden_required" required style="display:none">')
    assert find_unfilled_required_fields(page) == []


def test_flags_an_unchecked_required_checkbox(page):
    _render(page, '<input type="checkbox" name="consent" required>')
    assert find_unfilled_required_fields(page) == ["consent"]


def test_does_not_flag_a_checked_required_checkbox(page):
    _render(page, '<input type="checkbox" name="consent" checked required>')
    assert find_unfilled_required_fields(page) == []


def test_recognizes_aria_required_as_well_as_the_native_attribute(page):
    _render(page, '<input type="text" name="portfolio_url" aria-required="true">')
    assert find_unfilled_required_fields(page) == ["portfolio_url"]


def test_prefers_aria_label_over_name_when_describing_a_missing_field(page):
    _render(page, '<input type="text" name="q1" aria-label="Why do you want this role?" required>')
    assert find_unfilled_required_fields(page) == ["Why do you want this role?"]


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
    # Explicit size: an unstyled, empty <div> naturally renders at zero
    # height (no content, no default height) — that's actually what hCaptcha's
    # OWN dormant/invisible container looks like (see
    # test_ignores_an_invisible_hcaptcha_widget below), not a real, visible
    # challenge. Sized here to represent one actually being presented
    # (hCaptcha's real checkbox widget renders at roughly this size).
    _render(page, '<div class="h-captcha" data-sitekey="x" style="width:300px;height:78px"></div>')
    assert page_has_captcha(page) is True


def test_no_false_positive_on_plain_form(page):
    _render(page, "<form><input type='text' name='first_name'></form>")
    assert page_has_captcha(page) is False


def test_ignores_an_invisible_hcaptcha_widget(page):
    # Real-world case: many ATS postings (Lever included) embed hCaptcha in
    # "invisible" mode — a zero-size, never-shown bot-deterrent that only
    # activates for a visitor its risk engine flags as suspicious. A normal
    # applicant sees nothing, so this must NOT be treated as a blocking
    # CAPTCHA (regression: it used to, purely from DOM presence, aborting
    # every run against such a page before it ever filled anything).
    _render(page, '<div class="h-captcha" data-sitekey="x" style="height:0;overflow:hidden"></div>')
    assert page_has_captcha(page) is False


def test_detects_a_hidden_captcha_iframe_the_same_way(page):
    _render(page, '<iframe src="https://newassets.hcaptcha.com/captcha/v1/x" style="display:none"></iframe>')
    assert page_has_captcha(page) is False


def test_still_detects_a_genuinely_visible_captcha_alongside_a_dormant_one(page):
    # The dormant widget existing elsewhere on the page must not mask a
    # real, visible challenge actually being presented.
    _render(
        page,
        """
        <div class="h-captcha" style="height:0;overflow:hidden"></div>
        <iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>
        """,
    )
    assert page_has_captcha(page) is True


# ---------- find_apply_entry_button ("Apply from Job Link") ----------

def test_finds_an_apply_now_button_on_a_job_listing_page(page):
    _render(page, '<h1>Software Engineer</h1><p>We are hiring...</p><button>Apply Now</button>')
    found = find_apply_entry_button(page)
    assert found is not None
    assert "Apply Now" in found.inner_text()


def test_finds_start_application_as_an_apply_entry_control(page):
    _render(page, '<a href="/apply" class="cta">Start Application</a>')
    found = find_apply_entry_button(page)
    assert found is not None
    assert found.evaluate("el => el.tagName.toLowerCase()") == "a"


def test_prefers_the_more_specific_apply_phrase_over_a_bare_apply_elsewhere(page):
    # A bare "Apply" (e.g. a filters/search widget's own button) must not
    # outrank the posting's actual entry control when both are present.
    _render(page, '<button class="filter">Apply</button><button id="real-apply">Apply Now</button>')
    found = find_apply_entry_button(page)
    assert found is not None
    assert found.get_attribute("id") == "real-apply"


def test_returns_none_when_no_apply_control_is_present(page):
    _render(page, "<p>Just some unrelated page text.</p>")
    assert find_apply_entry_button(page) is None


def test_never_matches_apply_with_linkedin_as_an_entry_control(page):
    # Same third-party-autofill exclusion every other button lookup in this
    # module respects — see _THIRD_PARTY_AUTOFILL_TEXT.
    _render(page, '<button>Apply with LinkedIn</button>')
    assert find_apply_entry_button(page) is None


# ---------- find_job_posting_title_and_company ("Apply from Job Link") ----------

def test_reads_title_and_company_from_jobposting_json_ld(page):
    _render(
        page,
        """
        <script type="application/ld+json">
        {"@context": "https://schema.org/", "@type": "JobPosting",
         "title": "Senior Backend Engineer",
         "hiringOrganization": {"@type": "Organization", "name": "Acme Corp"}}
        </script>
        <h1>Senior Backend Engineer</h1>
        """,
    )
    title, company = find_job_posting_title_and_company(page)
    assert title == "Senior Backend Engineer"
    assert company == "Acme Corp"


def test_reads_from_a_json_ld_array(page):
    _render(
        page,
        """
        <script type="application/ld+json">
        [{"@type": "WebPage", "name": "Careers"},
         {"@type": "JobPosting", "title": "Data Scientist", "hiringOrganization": {"name": "Beta Inc"}}]
        </script>
        """,
    )
    title, company = find_job_posting_title_and_company(page)
    assert title == "Data Scientist"
    assert company == "Beta Inc"


def test_falls_back_to_open_graph_tags_when_no_json_ld_is_present(page):
    _render(
        page,
        """
        <meta property="og:title" content="Product Manager">
        <meta property="og:site_name" content="Gamma LLC">
        """,
    )
    title, company = find_job_posting_title_and_company(page)
    assert title == "Product Manager"
    assert company == "Gamma LLC"


def test_returns_none_none_when_nothing_is_readable(page):
    _render(page, "<p>A page with no structured job metadata at all.</p>")
    assert find_job_posting_title_and_company(page) == (None, None)


def test_ignores_malformed_json_ld_rather_than_raising(page):
    _render(page, '<script type="application/ld+json">{not valid json</script>')
    assert find_job_posting_title_and_company(page) == (None, None)
