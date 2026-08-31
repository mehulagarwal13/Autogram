"""Unit tests for the constrained action vocabulary — no browser, no LLM."""

import pytest

from automation.agents.autonomous.actions import AgentAction, InvalidActionError, validate_action_grounding
from automation.agents.autonomous.observer import PageElement, PageState


def test_valid_click_action_parses():
    action = AgentAction.from_dict({"action_type": "click", "element_ref": 3})
    assert action.action_type == "click"
    assert action.element_ref == 3


def test_valid_fill_action_parses():
    action = AgentAction.from_dict({"action_type": "fill", "element_ref": 1, "value": "Jane Doe"})
    assert action.value == "Jane Doe"


def test_unknown_action_type_rejected():
    with pytest.raises(InvalidActionError):
        AgentAction.from_dict({"action_type": "run_javascript", "code": "alert(1)"})


def test_click_without_element_ref_rejected():
    with pytest.raises(InvalidActionError):
        AgentAction.from_dict({"action_type": "click"})


def test_navigate_without_url_rejected():
    with pytest.raises(InvalidActionError):
        AgentAction.from_dict({"action_type": "navigate"})


def test_fill_without_value_rejected():
    with pytest.raises(InvalidActionError):
        AgentAction.from_dict({"action_type": "fill", "element_ref": 1})


def test_wait_and_get_page_state_require_nothing_extra():
    AgentAction.from_dict({"action_type": "wait", "wait_ms": 500})
    AgentAction.from_dict({"action_type": "get_page_state"})


def test_arbitrary_javascript_action_type_is_not_in_allowed_set():
    """Explicit regression guard for the compliance requirement: there must
    never be an action type that accepts arbitrary code."""
    from automation.agents.autonomous.actions import ALLOWED_ACTION_TYPES
    for forbidden in ("execute_script", "eval", "run_javascript", "run_code"):
        assert forbidden not in ALLOWED_ACTION_TYPES


def _element(ref=1, *, name="Apply Now", href=None, semantic="APPLY", confidence="HIGH", disabled=False):
    return PageElement(
        ref=ref, tag="button", type="button", name=name, value=None,
        required=False, disabled=disabled, checked=None, options=None,
        href=href, semantic_action=semantic, action_confidence=confidence,
    )


def test_invented_navigation_url_is_rejected():
    state = PageState(url="https://jobs.example/role/123", title="Role", visible_text="Role details")
    result = validate_action_grounding(
        AgentAction(action_type="navigate", url="https://jobs.example/role/123/apply"), state,
        canonical_urls=(state.url,),
    )
    assert result.grounded is False
    assert "not observed" in result.reason


def test_observed_href_navigation_is_grounded_when_no_better_control_exists():
    state = PageState(
        url="https://jobs.example/role/123", title="Role", visible_text="Role details",
        elements=[_element(name="Application portal", href="/portal", semantic="UNKNOWN", confidence="LOW")],
    )
    result = validate_action_grounding(
        AgentAction(action_type="navigate", url="https://jobs.example/portal"), state,
    )
    assert result.grounded is True


def test_observed_apply_control_wins_over_navigation():
    state = PageState(
        url="https://jobs.example/role/123", title="Role", visible_text="Role details",
        elements=[_element(ref=12)],
    )
    result = validate_action_grounding(
        AgentAction(action_type="navigate", url=state.url), state,
        canonical_urls=(state.url,),
    )
    assert result.grounded is False
    assert result.preferred_element_ref == 12

    assert not validate_action_grounding(AgentAction(action_type="wait", wait_ms=1000), state).grounded
    assert validate_action_grounding(AgentAction(action_type="click", element_ref=12), state).grounded


def test_targeted_action_must_reference_an_observed_enabled_element():
    state = PageState(url="https://jobs.example/role/123", title="Role", visible_text="")
    assert not validate_action_grounding(AgentAction(action_type="click", element_ref=99), state).grounded

    state.elements = [_element(ref=99, disabled=True)]
    assert not validate_action_grounding(AgentAction(action_type="click", element_ref=99), state).grounded
