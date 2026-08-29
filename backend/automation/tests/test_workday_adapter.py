"""
WorkdayAdapter (automation/ats/workday/workday_adapter.py).

Workday markup is `data-automation-id` all the way down, and the fixtures below
use the real ids rather than invented ones — `legalNameSection_firstName`,
`bottom-navigation-next-button`, `pageHeader` — because the entire argument for
targeting them (they are Workday's own component-library ids, stable across
tenants and redesigns in a way visible labels are not) is worthless if the
tests assert against something else.

The behaviour that actually distinguishes this adapter from the generic one is
the navigation: Workday uses ONE button for the whole application, labelled
"Next"/"Save and Continue" throughout and "Submit" on the review page. Getting
that wrong in either direction is a real failure — treating the review page's
Submit as a Next button walks off the end of the form forever, and treating a
mid-form Next as final stops a five-page application on page one.
"""

import pytest

from app.models.db_models import CandidateProfile, ProfileDocument
from automation.ats.workday.workday_adapter import WorkdayAdapter
from automation.forms.field_handlers import Field


def _profile() -> CandidateProfile:
    return CandidateProfile(
        profile_id="profile-1",
        user_id="user-1",
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
    )


def _resume_document() -> ProfileDocument:
    return ProfileDocument(
        document_id="doc-1",
        profile_id="profile-1",
        document_type="resume",
        original_filename="resume.pdf",
        stored_path="storage/resumes/resume.pdf",
        file_hash="abc123",
        is_default=True,
    )


def _adapter(page) -> WorkdayAdapter:
    return WorkdayAdapter(page=page, profile=_profile(), resume_document=_resume_document())


_MY_INFORMATION_PAGE = """
<html><body>
  <h2 data-automation-id="pageHeader">My Information</h2>
  <div data-automation-id="progressBar">Step 1 of 5</div>
  <label for="fn">First Name</label>
  <input id="fn" data-automation-id="legalNameSection_firstName">
  <label for="ln">Last Name</label>
  <input id="ln" data-automation-id="legalNameSection_lastName">
  <label for="em">Email</label>
  <input id="em" data-automation-id="email">
  <button data-automation-id="bottom-navigation-next-button">Save and Continue</button>
</body></html>
"""

_REVIEW_PAGE = """
<html><body>
  <h2 data-automation-id="pageHeader">Review</h2>
  <div data-automation-id="progressBar">Step 5 of 5</div>
  <p>Please review your application before submitting.</p>
  <button data-automation-id="bottom-navigation-next-button">Submit</button>
</body></html>
"""

_QUESTIONS_PAGE = """
<html><body>
  <h2 data-automation-id="pageHeader">Application Questions</h2>
  <label for="q1">Are you legally authorized to work in the United States?</label>
  <input id="q1" name="authorized">
  <button data-automation-id="bottom-navigation-next-button">Next</button>
</body></html>
"""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_detect_is_confident_on_workday_markup(page):
    page.set_content(_MY_INFORMATION_PAGE)
    assert _adapter(page).detect() >= 0.9


def test_detect_declines_a_page_that_is_not_workday(page):
    page.set_content("<html><body><form id='application_form'><input name='email'></form></body></html>")
    assert _adapter(page).detect() == 0.0


# ---------------------------------------------------------------------------
# Navigation: one button, two meanings
# ---------------------------------------------------------------------------

def test_a_mid_form_page_offers_a_next_control_and_is_not_final(page):
    page.set_content(_MY_INFORMATION_PAGE)
    adapter = _adapter(page)

    assert adapter.find_next_control() is not None
    assert adapter.is_final_page() is False


def test_the_review_page_is_final_and_offers_no_next_control(page):
    """The button is still there and still has the same automation id — only
    its label changed. The generic "no Next button means the last page" rule
    would keep clicking it, submitting the application as a navigation step."""
    page.set_content(_REVIEW_PAGE)
    adapter = _adapter(page)

    assert adapter.find_next_control() is None
    assert adapter.is_final_page() is True
    assert adapter.is_review_page() is True


def test_the_review_pages_submit_button_is_found_as_a_submit_control(page):
    page.set_content(_REVIEW_PAGE)
    submit = _adapter(page).find_submit_control()

    assert submit is not None
    assert "Submit" in submit.inner_text()


def test_a_mid_form_next_button_is_never_offered_as_a_submit_control(page):
    """The safety property behind the navigation split: nothing on a mid-form
    page may be handed back as something to submit with."""
    page.set_content(_QUESTIONS_PAGE)
    assert _adapter(page).find_submit_control() is None


def test_page_label_reads_workdays_own_step_header(page):
    page.set_content(_MY_INFORMATION_PAGE)
    assert _adapter(page).page_label() == "My Information"


def test_navigation_falls_back_to_the_generic_lookup_without_workday_markup(page):
    """A tenant that renders a plain button still has to work — the override
    narrows nothing, it only adds a better signal when one is available."""
    page.set_content("<html><body><h1>Step</h1><button>Continue</button></body></html>")
    adapter = _adapter(page)

    assert adapter.find_next_control() is not None
    assert adapter.is_final_page() is False


def test_submit_click_reports_failure_rather_than_raising(page):
    page.set_content("<html><body><p>nothing to submit</p></body></html>")
    assert _adapter(page).submit_application() is False


# ---------------------------------------------------------------------------
# Filling
# ---------------------------------------------------------------------------

def test_personal_information_fills_by_automation_id(page):
    page.set_content(_MY_INFORMATION_PAGE)

    results = _adapter(page).fill_personal_information()

    filled = {r.profile_path: r for r in results if r.filled}
    assert filled.keys() >= {"first_name", "last_name", "email"}
    assert page.locator("#fn").input_value() == "Ada"
    assert page.locator("#ln").input_value() == "Lovelace"
    assert page.locator("#em").input_value() == "ada@example.com"


def test_personal_information_reports_nothing_on_a_page_that_has_none(page):
    """Called once per page on a 5-page form, four of which have no name/email
    fields at all. Reporting eight failed fields on each would bury the run's
    confidence score under fields that were never there to fill."""
    page.set_content(_QUESTIONS_PAGE)
    assert _adapter(page).fill_personal_information() == []


def test_fields_belonging_to_another_wizard_step_are_not_counted_as_failures(page):
    """A wizard that keeps every step in the DOM and toggles `display` — a very
    common shape, Workday's own accordions included — leaves page 1's fields at
    `count() == 1` while page 4 is on screen. Presence must therefore be judged
    by visibility: `_fill_first_match` already refuses to type into a hidden
    control, so counting them would report a page's worth of phantom failures.

    Measured on the five-page integration form: this is the difference between
    8/46 fields filled (0.17 confidence, an automatic `needs_review`) and
    8/14."""
    page.set_content(
        "<html><body>"
        "<div style='display:none'>"  # step 1, no longer on screen
        "  <input data-automation-id='legalNameSection_firstName'>"
        "  <input data-automation-id='legalNameSection_lastName'>"
        "  <input data-automation-id='email'>"
        "</div>"
        "<div>"  # step 4, the one actually showing
        "  <input data-automation-id='addressSection_city'>"
        "</div>"
        "</body></html>"
    )
    profile = _profile()
    profile.city = "London"
    adapter = WorkdayAdapter(page=page, profile=profile, resume_document=_resume_document())

    results = adapter.fill_personal_information()

    assert [r.profile_path for r in results] == ["city"]
    assert results[0].filled is True


def test_screening_questions_go_through_the_shared_cross_ats_sweep(page, monkeypatch):
    """Workday's question pages are ordinary labeled forms, so they must reuse
    the same sweep Greenhouse and Lever do rather than growing a parallel
    implementation."""
    page.set_content(_QUESTIONS_PAGE)
    adapter = _adapter(page)
    called = {"count": 0}

    def _fake_sweep():
        called["count"] += 1
        return []

    monkeypatch.setattr(adapter, "_fill_known_questions", _fake_sweep)
    adapter.answer_questions()

    assert called["count"] == 1


def test_resume_upload_input_is_found_by_the_shared_helper(page):
    """Workday's file input carries an automation id but is still a plain
    `input[type=file]`, which is why `upload_resume` is inherited unchanged."""
    page.set_content(
        "<html><body><h2 data-automation-id='pageHeader'>My Experience</h2>"
        "<input type='file' data-automation-id='file-upload-input-ref'>"
        "</body></html>"
    )
    adapter = _adapter(page)

    assert adapter._find_resume_input() is not None
    assert adapter.resume_attachment_state() == "missing"


def test_no_upload_field_is_reported_as_no_field_not_as_missing(page):
    """The distinction the multi-page résumé logic runs on: "this page doesn't
    ask for a résumé" is not "the résumé is gone"."""
    page.set_content(_QUESTIONS_PAGE)
    assert _adapter(page).resume_attachment_state() == "no_field"


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method",
    ["detect", "fill_personal_information", "answer_questions", "submit_application",
     "find_next_control", "find_submit_control", "is_final_page", "page_label"],
)
def test_adapter_implements_the_full_contract(method):
    adapter = WorkdayAdapter(page=None, profile=None, resume_document=None)
    assert callable(getattr(adapter, method))


def test_workday_can_never_be_auto_submitted():
    """Workday applications always go to a human. Enforced by absence from
    `PUBLIC_ATS_PLATFORMS`, which `decide_action` gates AUTO_SUBMIT on — an
    application behind a login is never submitted unattended."""
    from automation.applications.application_flow_manager import PUBLIC_ATS_PLATFORMS, decide_action

    assert "workday" not in PUBLIC_ATS_PLATFORMS
    assert decide_action(1.0, "workday", autopilot_enabled=True) == "COPILOT_REVIEW"


def test_field_import_is_available_for_handler_typing():
    # Guards the import surface this module documents against — a rename in
    # field_handlers.py should fail here rather than at runtime on a live form.
    assert Field is not None


# ---------------------------------------------------------------------------
# Cross-page field isolation — the shared sweep in automation/ats/base.py
# ---------------------------------------------------------------------------
# A wizard that keeps every step in the DOM and toggles `display` (Workday's
# own accordions, and most hand-rolled multi-step forms) means the generic
# label/name sweep sees ALL of a form's labels on every single page visit, not
# just the current one. Getting this wrong has two independent failure modes,
# both reproduced on the real 5-page integration fixture before the fix:
# fields belonging to another step were counted as failures (confidence 0.17
# on a form that was actually complete) and, worse, a matched-but-hidden field
# used to be marked "examined" — permanently — so once the form reached the
# page that field actually lives on, it could never be filled at all.


def test_hidden_wizard_step_fields_are_neither_scored_nor_marked_examined(page):
    """The label-matched pass (`_fill_questions_by_label`)."""
    page.set_content(
        "<html><body>"
        "<div style='display:none'>"  # a step this run hasn't reached yet
        "<label for='ln'>LinkedIn Profile</label><input id='ln' name='candidate_linkedin'>"
        "</div>"
        "<div><label for='gh'>GitHub Profile</label><input id='gh' name='candidate_github'></div>"
        "</body></html>"
    )
    profile = _profile()
    profile.linkedin_url = "https://linkedin.com/in/ada"
    profile.github_url = "https://github.com/ada"
    adapter = WorkdayAdapter(page=page, profile=profile, resume_document=_resume_document())

    results = adapter._fill_questions_by_label()

    assert [r.profile_path for r in results] == ["github_url"]
    assert page.locator("#ln").get_attribute("data-automation-examined") is None
    assert page.locator("#gh").input_value() == "https://github.com/ada"


def test_a_field_reachable_later_can_still_be_filled_once_its_page_arrives(page):
    """The permanent half of the bug: a hidden match used to be marked
    examined regardless, which made it unfillable forever — even after the
    wizard advanced to the very page that field lives on."""
    page.set_content(
        "<html><body><div style='display:none'>"
        "<label for='ln'>LinkedIn Profile</label><input id='ln' name='candidate_linkedin'>"
        "</div></body></html>"
    )
    profile = _profile()
    profile.linkedin_url = "https://linkedin.com/in/ada"
    adapter = WorkdayAdapter(page=page, profile=profile, resume_document=_resume_document())

    adapter._fill_questions_by_label()  # step N: field exists, hidden — skipped
    page.eval_on_selector("div", "el => el.style.display = 'block'")  # the wizard reaches its page
    adapter._fill_questions_by_label()  # step N+1: same field, now visible

    assert page.locator("#ln").input_value() == "https://linkedin.com/in/ada"


def test_hidden_name_or_placeholder_matches_are_not_marked_examined_either(page):
    """The name/placeholder pass (`_fill_questions_by_name_or_placeholder`) —
    the second of the three sweeps that reads every label/name on the page
    regardless of which step it belongs to."""
    page.set_content(
        "<html><body>"
        "<input style='display:none' name='linkedin_url'>"
        "<input name='github_url'>"
        "</body></html>"
    )
    profile = _profile()
    profile.linkedin_url = "https://linkedin.com/in/ada"
    profile.github_url = "https://github.com/ada"
    adapter = WorkdayAdapter(page=page, profile=profile, resume_document=_resume_document())

    results = adapter._fill_questions_by_name_or_placeholder()

    assert [r.profile_path for r in results] == ["github_url"]
    assert page.locator("input[name='linkedin_url']").get_attribute("data-automation-examined") is None
