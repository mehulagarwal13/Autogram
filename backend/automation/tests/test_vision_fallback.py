"""
VisionFormAnswerer (automation/forms/vision_fallback.py) — the prompt/validation
half of the vision fallback, tested with a stub LLM and no browser.

The screenshots themselves are opaque bytes to this class (it only forwards
them), so these tests use short byte strings as stand-ins and assert on what
actually decides whether a real form gets a real answer: that the images reach
the provider at all, in the same order as the numbered fields; that a
demographic question never reaches the model; that an option-bearing answer is
re-resolved against the DOM's own options; and that an answer is dropped rather
than typed when it's meta-commentary, unmatched, low-confidence, or missing.

`automation/tests/test_vision_pass_on_page.py` covers the other half — the
adapter-side screenshotting and filling — against a real rendered page.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.db_models import CandidateProfile
from automation.forms.answer_engine import ApplicationAnswerEngine
from automation.forms.vision_fallback import (
    MAX_FIELDS_PER_CALL,
    NOT_APPLICABLE,
    VISION_TASK,
    VisionField,
    VisionFormAnswerer,
    save_debug_crops,
)


def _profile() -> CandidateProfile:
    return CandidateProfile(
        profile_id="profile-1",
        user_id="user-1",
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        current_company="Analytical Engines Ltd",
        current_role="Backend Engineer",
    )


class _StubLLM:
    """Records every call and replays a canned JSON response."""

    def __init__(self, response: dict | str = None, raises: Exception | None = None):
        self.response = response if response is not None else {"answers": []}
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        if isinstance(self.response, str):
            return self.response
        return json.dumps(self.response)


def _answerer(llm: _StubLLM, **kwargs) -> VisionFormAnswerer:
    engine = ApplicationAnswerEngine(profile=_profile())
    return VisionFormAnswerer(engine, llm_fn=llm, **kwargs)


def _field(question: str, *, name: str = "q1", options=(), screenshot: bytes = b"png-1") -> VisionField:
    return VisionField(name=name, question=question, screenshot=screenshot, options=tuple(options))


# ---------------------------------------------------------------------------
# The call itself
# ---------------------------------------------------------------------------

def test_no_fields_means_no_llm_call():
    llm = _StubLLM()
    assert _answerer(llm).answer([]) == []
    assert llm.calls == []


def test_sends_one_image_per_field_in_field_order():
    llm = _StubLLM({"answers": [
        {"field": 1, "answer": "Yes", "confidence": 0.9},
        {"field": 2, "answer": "No", "confidence": 0.9},
    ]})
    fields = [
        _field("Are you willing to relocate?", name="a", screenshot=b"first"),
        _field("Do you need sponsorship?", name="b", screenshot=b"second"),
    ]

    _answerer(llm).answer(fields)

    assert llm.calls[0]["task"] == VISION_TASK
    assert llm.calls[0]["images"] == [b"first", b"second"]
    # The images arrive unlabeled, so the prompt's numbering is the ONLY thing
    # tying image k to field k — see _SYSTEM_PROMPT.
    payload = json.loads(llm.calls[0]["prompt"])
    assert [f["field"] for f in payload["fields"]] == [1, 2]
    assert payload["fields"][0]["question_text_from_page"] == "Are you willing to relocate?"


def test_prompt_carries_the_engines_profile_and_the_fields_options():
    llm = _StubLLM({"answers": [{"field": 1, "answer": "Yes", "confidence": 0.9}]})
    _answerer(llm).answer([_field("Willing to work onsite?", options=("Yes", "No"))])

    payload = json.loads(llm.calls[0]["prompt"])
    assert payload["candidate_profile"]["current_company"] == "Analytical Engines Ltd"
    assert payload["fields"][0]["options"] == ["Yes", "No"]


def test_a_field_with_no_label_says_so_rather_than_sending_an_empty_string():
    llm = _StubLLM({"answers": [{"field": 1, "answer": "x", "confidence": 0.9}]})
    _answerer(llm).answer([_field("", name="question_123")])

    payload = json.loads(llm.calls[0]["prompt"])
    assert "read the question off the screenshot" in payload["fields"][0]["question_text_from_page"]


# ---------------------------------------------------------------------------
# What the pass exists for: the conditional follow-up
# ---------------------------------------------------------------------------

def test_conditional_follow_up_answered_not_applicable_is_used_verbatim():
    """The case that motivated this pass: a required "If yes to the above..."
    box under a question the candidate answered "No". Unanswerable from the
    field's own text, obvious from a screenshot."""
    llm = _StubLLM({"answers": [{
        "field": 1,
        "answer": NOT_APPLICABLE,
        "confidence": 0.95,
        "reason": "question above answered No",
    }]})
    question = "If yes to the above question, what role and what governmental organization?"

    [answer] = _answerer(llm).answer([_field(question)])

    assert answer.answered
    assert answer.answer == NOT_APPLICABLE
    assert answer.confidence == 0.95
    assert answer.reason == "question above answered No"


def test_already_filled_is_reported_and_never_answered():
    llm = _StubLLM({"answers": [{
        "field": 1, "answer": None, "already_filled": True, "reason": "shows Noida, Uttar Pradesh",
    }]})

    [answer] = _answerer(llm).answer([_field("Location (City)", name="candidate-location")])

    assert answer.already_filled is True
    assert answer.answered is False
    assert answer.answer == ""


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

def test_demographic_questions_are_never_sent_to_the_model():
    llm = _StubLLM({"answers": [{"field": 1, "answer": "Backend Engineer", "confidence": 0.9}]})
    fields = [
        _field("What is your gender?", name="gender"),
        _field("What is your current job title?", name="title"),
    ]

    demographic, other = _answerer(llm).answer(fields)

    assert demographic.answered is False
    assert "demographic" in demographic.reason
    # Only the non-demographic field was asked about — one image, not two.
    assert llm.calls[0]["images"] == [b"png-1"]
    payload = json.loads(llm.calls[0]["prompt"])
    assert [f["question_text_from_page"] for f in payload["fields"]] == ["What is your current job title?"]
    assert other.answer == "Backend Engineer"


def test_a_batch_of_only_demographic_questions_makes_no_call_at_all():
    llm = _StubLLM()
    [answer] = _answerer(llm).answer([_field("Do you have a disability?")])
    assert llm.calls == []
    assert answer.answered is False


def test_an_answer_that_is_not_one_of_the_options_is_discarded():
    llm = _StubLLM({"answers": [{"field": 1, "answer": "Maybe next year", "confidence": 0.99}]})

    [answer] = _answerer(llm).answer([_field("Willing to work onsite?", options=("Yes", "No"))])

    assert answer.answered is False
    assert "options" in answer.reason


def test_an_answer_matching_an_option_loosely_is_replaced_by_the_verbatim_option():
    llm = _StubLLM({"answers": [{"field": 1, "answer": "yes", "confidence": 0.9}]})

    [answer] = _answerer(llm).answer([
        _field("Willing to work onsite?", options=("Yes, from day one", "No")),
    ])

    assert answer.answer == "Yes, from day one"


def test_meta_commentary_is_discarded():
    llm = _StubLLM({"answers": [{
        "field": 1, "answer": "The candidate profile does not specify this.", "confidence": 0.95,
    }]})

    [answer] = _answerer(llm).answer([_field("What are your salary expectations?")])

    assert answer.answered is False
    assert "meta-commentary" in answer.reason


def test_a_missing_confidence_is_treated_as_unanswered():
    llm = _StubLLM({"answers": [{"field": 1, "answer": "Something plausible"}]})

    [answer] = _answerer(llm).answer([_field("Why do you want to work here?")])

    assert answer.answered is False


def test_confidence_is_clamped_rather_than_trusted():
    llm = _StubLLM({"answers": [{"field": 1, "answer": "N/A", "confidence": 4.2}]})
    [answer] = _answerer(llm).answer([_field("If yes, explain")])
    assert answer.confidence == 1.0


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------

def test_entries_are_matched_by_field_number_not_by_position():
    """A reordered response must not shift answers onto the wrong questions —
    the one failure mode here that types a real answer into the wrong
    employer's field."""
    llm = _StubLLM({"answers": [
        {"field": 2, "answer": "Second", "confidence": 0.9},
        {"field": 1, "answer": "First", "confidence": 0.9},
    ]})

    first, second = _answerer(llm).answer([_field("One", name="a"), _field("Two", name="b")])

    assert (first.answer, second.answer) == ("First", "Second")


def test_a_field_the_model_skipped_comes_back_unanswered():
    llm = _StubLLM({"answers": [{"field": 1, "answer": "First", "confidence": 0.9}]})

    first, second = _answerer(llm).answer([_field("One", name="a"), _field("Two", name="b")])

    assert first.answer == "First"
    assert second.answered is False
    assert second.reason == "no answer returned"


def test_a_failed_llm_call_declines_every_field_instead_of_raising():
    llm = _StubLLM(raises=RuntimeError("provider exploded"))

    answers = _answerer(llm).answer([_field("One", name="a"), _field("Two", name="b")])

    assert [a.answered for a in answers] == [False, False]
    assert all("vision call failed" in a.reason for a in answers)


def test_an_unparseable_response_declines_every_field():
    answers = _answerer(_StubLLM("not json at all")).answer([_field("One")])
    assert answers[0].answered is False
    assert "unparseable" in answers[0].reason


def test_fields_over_the_cap_are_reported_not_silently_dropped(caplog):
    over = MAX_FIELDS_PER_CALL + 2
    llm = _StubLLM({"answers": [
        {"field": n, "answer": "x", "confidence": 0.9} for n in range(1, MAX_FIELDS_PER_CALL + 1)
    ]})
    fields = [_field(f"Question {n}", name=f"q{n}") for n in range(over)]

    with caplog.at_level("WARNING"):
        answers = _answerer(llm).answer(fields)

    assert len(answers) == over
    assert len(llm.calls[0]["images"]) == MAX_FIELDS_PER_CALL
    assert [a.answered for a in answers[MAX_FIELDS_PER_CALL:]] == [False, False]
    assert "cap" in caplog.text


def test_save_debug_crops_writes_one_png_per_field(tmp_path):
    fields = [_field("One", screenshot=b"aaa"), _field("Two", screenshot=b"bbb")]

    written = save_debug_crops(fields, tmp_path)

    assert [Path(p).name for p in written] == ["vision-field-1.png", "vision-field-2.png"]
    assert (tmp_path / "vision-field-2.png").read_bytes() == b"bbb"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_blank_answer_is_a_decline(value):
    llm = _StubLLM({"answers": [{"field": 1, "answer": value, "confidence": 0.99}]})
    [answer] = _answerer(llm).answer([_field("Anything")])
    assert answer.answered is False
