"""
Real-browser regression test for `observer.py::observe_page`'s element
extraction (spec §4) and `compute_page_completion` (spec §17).

Every other `test_autonomous_*` file monkeypatches `observe_page` entirely
(see `test_autonomous_loop.py::_install_fakes`), so none of them exercise the
real extraction script against a real DOM. That gap is exactly how a
previous bug shipped undetected: `observe_page` assigned the extraction
script's whole `{elements, headings, dialogs, validation_messages, forms}`
return value to `raw_elements` instead of its `"elements"` key, so iterating
it walked five string keys instead of element dicts and raised `TypeError`
on the very first element of every real page. This file is the one that
would have caught it.

Uses the shared `page` fixture from `automation/tests/conftest.py` (a real,
session-scoped headless Chromium) and the local HTTP fixture site already
built for the HITL e2e suite — no new fixture, no live Postgres needed.
"""

from automation.agents.autonomous.action_semantics import classify_semantic_action
from automation.agents.autonomous.observer import (
    PageElement,
    classify_page_type,
    compute_page_completion,
    field_identity,
    observe_page,
)
from automation.tests.fixtures.hitl_test_site import HitlTestSite


def test_observe_page_extracts_real_elements_without_crashing(page):
    with HitlTestSite() as site:
        page.goto(site.url("/apply"))
        state = observe_page(page)

    assert state.elements, "expected the /apply form's fields to be extracted"
    assert any(e.tag == "input" for e in state.elements)
    assert any("first" in (e.name or "").lower() for e in state.elements)


def test_observe_page_populates_headings_and_reports_no_validation_errors_on_a_clean_form(page):
    with HitlTestSite() as site:
        page.goto(site.url("/apply"))
        state = observe_page(page)

    assert any("Software Engineer Application" in h for h in state.headings)
    assert state.validation_messages == []
    assert state.dialogs == []


def test_observe_page_flags_a_required_empty_field_and_the_completion_gate_blocks(page):
    with HitlTestSite() as site:
        page.goto(site.url("/blocked"))
        state = observe_page(page)

    required = [e for e in state.elements if e.required]
    assert required, "the /blocked fixture page has one required, empty field"
    assert all(not (e.value or "").strip() for e in required)

    completion = compute_page_completion(state)
    assert completion.ready is False
    assert completion.missing_required_refs


def test_observe_page_completion_gate_is_ready_once_the_required_field_is_filled(page):
    with HitlTestSite() as site:
        page.goto(site.url("/blocked?filled=1"))
        state = observe_page(page)

    completion = compute_page_completion(state)
    assert completion.ready is True
    assert completion.missing_required_refs == []


def _make_element(**overrides) -> PageElement:
    base = dict(
        ref=0, tag="button", type="button", name="", value=None,
        required=False, disabled=False, checked=None, options=None,
    )
    base.update(overrides)
    return PageElement(**base)


def test_observe_page_classifies_the_save_and_continue_button_as_a_semantic_action(page):
    """`/apply` (`hitl_test_site.py`) has a real `<button id="next-btn">Save
    and continue</button>` — spec §5's normalization should recognize it
    without any per-site special-casing."""
    with HitlTestSite() as site:
        page.goto(site.url("/apply"))
        state = observe_page(page)

    next_btn = next((e for e in state.elements if "save and continue" in (e.name or "").lower()), None)
    assert next_btn is not None
    assert next_btn.semantic_action == "SAVE_AND_CONTINUE"
    assert next_btn.action_confidence in ("HIGH", "MEDIUM")
    assert next_btn.irreversible is False


def test_observe_page_sets_page_type_to_application_page_for_a_real_form(page):
    with HitlTestSite() as site:
        page.goto(site.url("/apply"))
        state = observe_page(page)

    assert state.page_type == "application_page"


def test_observe_page_recognizes_a_generic_job_listing_with_an_incidental_input(page):
    page.set_content("""
        <main>
          <h1>Senior Platform Engineer</h1>
          <p>Location: Remote</p>
          <h2>Job description</h2>
          <p>Responsibilities and qualifications for the role.</p>
          <label>Job alerts <input type="email" /></label>
          <button>Apply Now</button>
        </main>
    """)
    state = observe_page(page)
    apply = next(element for element in state.elements if element.name == "Apply Now")
    assert apply.semantic_action == "APPLY"
    assert apply.irreversible is False
    assert state.page_type == "JOB_LISTING"


def test_classify_page_type_maps_a_blocker_hint_straight_to_its_vocabulary_entry():
    from automation.agents.autonomous.observer import PageState

    otp_state = PageState(url="https://x", title="", visible_text="", blocker_hint={"request_type": "OTP_REQUIRED"})
    assert classify_page_type(otp_state) == "verification"

    captcha_state = PageState(url="https://x", title="", visible_text="", blocker_hint={"request_type": "CAPTCHA_REQUIRED"})
    assert classify_page_type(captcha_state) == "captcha"

    login_state = PageState(url="https://x", title="", visible_text="", blocker_hint={"request_type": "LOGIN_REQUIRED"})
    assert classify_page_type(login_state) == "login"


def test_classify_page_type_recognizes_a_confirmation_page():
    from automation.agents.autonomous.observer import PageState

    state = PageState(url="https://x", title="Thank you", visible_text="Thank you for applying! Application received.")
    assert classify_page_type(state) == "confirmation"


def test_field_identity_is_stable_across_two_observations_of_the_same_page(page):
    with HitlTestSite() as site:
        page.goto(site.url("/apply"))
        first = observe_page(page)
        second = observe_page(page)

    first_field = next(e for e in first.elements if e.tag == "input")
    second_field = next(e for e in second.elements if e.tag == "input" and e.name == first_field.name)
    assert field_identity(first_field) == field_identity(second_field)

    other_field = next((e for e in first.elements if e.tag == "input" and e.name != first_field.name), None)
    if other_field is not None:
        assert field_identity(first_field) != field_identity(other_field)


def test_classify_semantic_action_normalizes_common_button_verbs():
    assert classify_semantic_action(_make_element(name="Submit Application"))[0] == "SUBMIT"
    assert classify_semantic_action(_make_element(name="Submit Application"))[2] is True  # irreversible
    assert classify_semantic_action(_make_element(name="Apply Now"))[0] == "APPLY"
    assert classify_semantic_action(_make_element(name="Apply Now"))[2] is False

    assert classify_semantic_action(_make_element(name="Start Application"))[0] == "START_APPLICATION"
    assert classify_semantic_action(_make_element(name="Next"))[0] == "NEXT"
    assert classify_semantic_action(_make_element(name="Save and Continue"))[0] == "SAVE_AND_CONTINUE"
    assert classify_semantic_action(_make_element(name="Upload Resume"))[0] == "UPLOAD"
    assert classify_semantic_action(_make_element(name="Add Another Experience"))[0] == "ADD"


def test_classify_semantic_action_returns_none_for_a_non_actionable_element():
    # A plain text input's accessible name is a QUESTION, not a command — it
    # should never be forced into the button vocabulary.
    text_field = _make_element(tag="input", type="text", name="Phone Number")
    assert classify_semantic_action(text_field) == (None, None, False)


def test_classify_semantic_action_falls_back_to_unknown_for_an_unrecognized_button():
    weird_button = _make_element(name="Whoosh")
    assert classify_semantic_action(weird_button) == ("UNKNOWN", "LOW", False)


def test_bare_submit_uses_application_context_instead_of_text_alone():
    newsletter = _make_element(name="Submit", surrounding_text="Subscribe to weekly company news")
    application = _make_element(name="Submit", section="Final application review")
    assert classify_semantic_action(newsletter) == ("UNKNOWN", "LOW", False)
    assert classify_semantic_action(application) == ("SUBMIT", "HIGH", True)
