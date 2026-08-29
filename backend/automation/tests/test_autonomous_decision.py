"""
Tests for the LLM decision step's parsing/validation — the LLM call itself is
mocked (`generate_answer`), so these exercise only the JSON-contract
enforcement that stands between whatever a model returns and an `AgentAction`
actually being dispatched.
"""

import json
from unittest.mock import patch

import pytest

from automation.agents.autonomous.decision import DecisionError, decide_next_step
from automation.agents.autonomous.observer import PageElement, PageState
from automation.interfaces import LLMRouterError

_PAGE_STATE = PageState(
    url="https://example.com/apply", title="Apply",
    visible_text="Apply for Software Engineer",
    elements=[PageElement(ref=0, tag="input", type="text", name="Full name",
                           value=None, required=True, disabled=False, checked=None, options=None)],
)


def _decide(**overrides):
    kwargs = dict(
        job_url="https://example.com/apply", original_objective="Apply",
        resume_text="Jane Doe resume", parsed_resume={}, profile={},
        confirmed_answers={}, page_state=_PAGE_STATE, action_history=[],
        uploaded_documents=[], auto_submit_approved=False,
    )
    kwargs.update(overrides)
    return decide_next_step(**kwargs)


def test_execute_action_decision_parses():
    payload = {"decision": "EXECUTE_ACTION", "action": {"action_type": "fill", "element_ref": 0, "value": "Jane Doe"}}
    with patch("automation.agents.autonomous.decision.generate_answer", return_value=json.dumps(payload)):
        decision = _decide()
    assert decision.decision_type == "EXECUTE_ACTION"
    assert decision.action.action_type == "fill"
    assert decision.action.value == "Jane Doe"


def test_request_human_intervention_decision_parses():
    payload = {
        "decision": "REQUEST_HUMAN_INTERVENTION",
        "intervention": {"type": "authentication", "reason": "login wall", "message": "Please log in."},
    }
    with patch("automation.agents.autonomous.decision.generate_answer", return_value=json.dumps(payload)):
        decision = _decide()
    assert decision.decision_type == "REQUEST_HUMAN_INTERVENTION"
    assert decision.intervention["message"] == "Please log in."


def test_application_ready_for_submission_parses():
    payload = {"decision": "APPLICATION_READY_FOR_SUBMISSION", "evidence": "All fields filled."}
    with patch("automation.agents.autonomous.decision.generate_answer", return_value=json.dumps(payload)):
        decision = _decide()
    assert decision.decision_type == "APPLICATION_READY_FOR_SUBMISSION"


def test_task_completed_and_task_failed_parse():
    for dtype in ("TASK_COMPLETED", "TASK_FAILED"):
        payload = {"decision": dtype, "evidence": "details"}
        with patch("automation.agents.autonomous.decision.generate_answer", return_value=json.dumps(payload)):
            decision = _decide()
        assert decision.decision_type == dtype


def test_malformed_json_raises_decision_error():
    with patch("automation.agents.autonomous.decision.generate_answer", return_value="not json"):
        with pytest.raises(DecisionError):
            _decide()


def test_unknown_decision_type_raises_decision_error():
    payload = {"decision": "DO_WHATEVER"}
    with patch("automation.agents.autonomous.decision.generate_answer", return_value=json.dumps(payload)):
        with pytest.raises(DecisionError):
            _decide()


def test_execute_action_missing_action_object_raises():
    payload = {"decision": "EXECUTE_ACTION"}
    with patch("automation.agents.autonomous.decision.generate_answer", return_value=json.dumps(payload)):
        with pytest.raises(DecisionError):
            _decide()


def test_execute_action_with_disallowed_action_type_raises():
    payload = {"decision": "EXECUTE_ACTION", "action": {"action_type": "run_javascript"}}
    with patch("automation.agents.autonomous.decision.generate_answer", return_value=json.dumps(payload)):
        with pytest.raises(DecisionError):
            _decide()


def test_llm_router_error_becomes_decision_error():
    with patch("automation.agents.autonomous.decision.generate_answer", side_effect=LLMRouterError("boom")):
        with pytest.raises(DecisionError):
            _decide()


def test_system_prompt_is_used_verbatim():
    """Guards against an accidental edit to the required system prompt text."""
    from automation.agents.autonomous.decision import SYSTEM_PROMPT
    assert SYSTEM_PROMPT.startswith("You are Autogram's autonomous job application agent.")
    assert "Never claim that the application was successfully submitted" in SYSTEM_PROMPT
    assert "EXECUTE_ACTION, REQUEST_HUMAN_INTERVENTION, APPLICATION_READY_FOR_SUBMISSION, TASK_COMPLETED, or TASK_FAILED" in SYSTEM_PROMPT
