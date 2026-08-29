"""Unit tests for the constrained action vocabulary — no browser, no LLM."""

import pytest

from automation.agents.autonomous.actions import AgentAction, InvalidActionError


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
