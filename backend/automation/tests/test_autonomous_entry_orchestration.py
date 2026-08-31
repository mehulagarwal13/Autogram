"""Real-browser regressions for generic application-entry orchestration."""

from automation.agents.autonomous.actions import AgentAction
from automation.agents.autonomous.executor import ActionExecutor
from automation.agents.autonomous.observer import observe_page
from automation.tests.fixtures.hitl_test_site import HitlTestSite


def test_apply_now_click_opens_and_verifies_an_application_form(page):
    page.set_content("""
      <main>
        <h1>Data Engineer</h1>
        <p>Location: Remote</p>
        <h2>Job description</h2>
        <button id="apply" onclick="document.body.innerHTML = `
          <main><h1>Application</h1><label>First name
          <input name='first_name' required></label><button>Next</button></main>`">
          Apply Now
        </button>
      </main>
    """)
    listing = observe_page(page)
    apply = next(element for element in listing.elements if element.name == "Apply Now")

    result = ActionExecutor(page, auto_submit_approved=False).execute(
        AgentAction(action_type="click", element_ref=apply.ref),
        element_name=apply.name,
        element_type=apply.type,
        element_semantic_action=apply.semantic_action,
    )

    assert listing.page_type == "JOB_LISTING"
    assert result.success is True
    assert result.verified is True
    assert result.as_dict()["postcondition_verified"] is True
    assert observe_page(page).page_type == "application_page"


def test_navigation_to_http_404_is_a_failed_verified_outcome(page):
    with HitlTestSite() as site:
        page.goto(site.url("/apply"))
        result = ActionExecutor(page, auto_submit_approved=False).execute(
            AgentAction(action_type="navigate", url=site.url("/definitely-missing"))
        )

    assert result.success is False
    assert result.verified is False
    assert result.result_code == "NAVIGATION_FAILED"
    assert result.as_dict()["action_attempted"] is True
    assert result.as_dict()["postcondition_verified"] is False


def test_click_that_reaches_an_error_page_is_not_success(page):
    with HitlTestSite() as site:
        page.goto(site.url("/apply"))
        page.evaluate("""() => {
          document.body.innerHTML = '<h1>Job description</h1><button id="apply">Apply Now</button>';
          document.querySelector('#apply').onclick = () => { window.location.href = '/404'; };
        }""")
        listing = observe_page(page)
        apply = next(element for element in listing.elements if element.name == "Apply Now")
        result = ActionExecutor(page, auto_submit_approved=False).execute(
            AgentAction(action_type="click", element_ref=apply.ref),
            element_name=apply.name,
            element_type=apply.type,
            element_semantic_action=apply.semantic_action,
        )

    assert result.success is False
    assert result.verified is False
    assert result.result_code == "ERROR_PAGE"
