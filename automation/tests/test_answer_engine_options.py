"""
Option-aware answering: a question that arrives as a `Question` carrying the
on-page control's real choices must be answered with one of THOSE strings,
verbatim, or not at all.

Same conventions as test_answer_engine.py / test_answer_engine_phase8.py: a
real (unsaved) CandidateProfile ORM instance, a fake `llm_fn` injected in
place of `automation.interfaces.generate_answer`, and no DB.

The invariant every test here defends: nothing this engine returns for an
option-bearing question may be a string the form doesn't offer. Prompting is
not the mechanism — `_match_option` is — so most of these deliberately
simulate a model that ignores the instructions.
"""

import json

from app.models.db_models import CandidateDemographics, CandidateProfile
from automation.forms import answer_engine as answer_engine_module
from automation.forms.answer_engine import (
    ApplicationAnswerEngine,
    Question,
    SOURCE_NEEDS_USER_INPUT,
)

YES_NO = ("Yes", "No")


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(
        profile_id="profile-1",
        user_id="user-1",
        current_role="Backend Engineer",
        current_company="Analytical Engines Ltd",
        years_of_experience=5.0,
        work_authorization=None,
        visa_status=None,
        work_authorized=None,
        requires_sponsorship=None,
        visa_type=None,
        skills={"programming_languages": ["Python"]},
    )
    defaults.update(overrides)
    return CandidateProfile(**defaults)


def _llm_returning(answers: list, capture: list | None = None):
    def fake_llm_fn(*, task, prompt, system=None, **overrides):
        if capture is not None:
            capture.append({"prompt": json.loads(prompt), "system": system})
        return json.dumps({"answers": answers})
    return fake_llm_fn


def _llm_that_must_not_be_called():
    def fake_llm_fn(*, task, prompt, system=None, **overrides):
        raise AssertionError("the LLM must never be called for this question")
    return fake_llm_fn


# ---------- what actually reaches the model ----------

def test_options_are_sent_alongside_the_questions_index_parallel():
    calls = []
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning(["Yes", "I thrive under pressure."], capture=calls),
    )

    engine.answer_batch([
        Question(text="Do you have a security clearance?", options=YES_NO),
        "Describe a time you handled pressure.",
    ])

    payload = calls[0]["prompt"]
    # `questions` keeps its original flat-string shape; options ride alongside.
    assert payload["questions"] == [
        "Do you have a security clearance?",
        "Describe a time you handled pressure.",
    ]
    assert payload["options"] == [["Yes", "No"], None]


def test_a_free_text_only_batch_sends_no_options_key_and_no_option_instructions():
    """Backward compatibility: a form with nothing but prose questions must
    produce exactly the prompt it did before options existed."""
    calls = []
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning(["I'm excited about your mission."], capture=calls),
    )

    engine.answer_batch(["Why do you want to work here?"])

    assert "options" not in calls[0]["prompt"]
    assert calls[0]["system"] == answer_engine_module._SYSTEM_PROMPT


def test_option_instructions_are_appended_only_when_a_question_has_options():
    calls = []
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning(["Yes"], capture=calls),
    )

    engine.answer_batch([Question(text="Do you have a security clearance?", options=YES_NO)])

    assert calls[0]["system"].endswith(answer_engine_module._OPTION_PROMPT)


# ---------- the answer is snapped to a real option, or dropped ----------

def test_a_differently_cased_answer_is_returned_as_the_verbatim_option():
    engine = ApplicationAnswerEngine(profile=_profile(), llm_fn=_llm_returning(["yes"]))

    result = engine.answer(Question(text="Do you have a security clearance?", options=YES_NO))

    assert result.answer == "Yes"  # the DOM's casing, not the model's
    assert result.source == "llm"


def test_an_answer_that_is_not_an_option_is_discarded_rather_than_typed():
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning(["It depends on the role, honestly."]),
    )

    result = engine.answer(Question(text="Do you have a security clearance?", options=YES_NO))

    assert result.answer == ""
    assert result.confidence == 0.0
    assert result.available_options == YES_NO


def test_a_uniquely_contained_answer_resolves_to_the_longer_option():
    options = ("Yes, now or in the future", "No")
    engine = ApplicationAnswerEngine(profile=_profile(), llm_fn=_llm_returning([{"answer": "Yes", "confidence": 0.95}]))

    result = engine.answer(Question(text="Will you require sponsorship?", options=options))

    assert result.answer == "Yes, now or in the future"
    assert result.confidence == 0.95


def test_an_ambiguous_answer_matching_two_options_is_left_for_a_human():
    """"Yes" is a substring of BOTH options — picking either would be a coin
    flip, so the engine declines instead."""
    options = ("Yes, I am authorized", "Yes, with sponsorship")
    engine = ApplicationAnswerEngine(profile=_profile(), llm_fn=_llm_returning(["Yes"]))

    result = engine.answer(Question(text="Authorized to work?", options=options))

    assert result.answer == ""
    assert result.confidence == 0.0


def test_a_null_answer_from_the_model_is_honored_as_declining():
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning([{"answer": None, "confidence": 0.0}]),
    )

    result = engine.answer(Question(text="Do you have a security clearance?", options=YES_NO))

    assert result.answer == ""
    assert result.confidence == 0.0


def test_available_options_are_echoed_on_a_successful_answer_too():
    engine = ApplicationAnswerEngine(profile=_profile(), llm_fn=_llm_returning(["No"]))

    result = engine.answer(Question(text="Do you have a security clearance?", options=YES_NO))

    assert result.available_options == YES_NO


# ---------- deterministic path ----------

def test_a_deterministic_answer_is_snapped_to_the_forms_own_wording():
    profile = _profile(requires_sponsorship=False)
    engine = ApplicationAnswerEngine(profile=profile, llm_fn=_llm_that_must_not_be_called())

    result = engine.answer(Question(text="Will you require sponsorship?", options=("YES", "NO")))

    assert result.source == "deterministic"
    assert result.answer == "NO"  # the form's casing


def test_an_unmappable_profile_fact_falls_through_to_the_llm_for_semantic_matching():
    """"US Citizen" is not mechanically "Yes" — but it IS the answer to "are
    you authorized to work?", and only the LLM can make that leap. Falling
    through is what lets the question get answered at all."""
    calls = []
    profile = _profile(work_authorization="US Citizen")
    engine = ApplicationAnswerEngine(profile=profile, llm_fn=_llm_returning(["Yes"], capture=calls))

    result = engine.answer(Question(text="Are you legally authorized to work in the US?", options=YES_NO))

    assert result.source == "llm"
    assert result.answer == "Yes"
    assert calls[0]["prompt"]["questions"] == ["Are you legally authorized to work in the US?"]


def test_a_deterministic_answer_still_works_when_the_question_has_no_options():
    profile = _profile(requires_sponsorship=True, visa_type="H1B")
    engine = ApplicationAnswerEngine(profile=profile, llm_fn=_llm_that_must_not_be_called())

    result = engine.answer("Will you require sponsorship?")

    assert result.answer == "Yes (H1B)"
    assert result.available_options == ()


# ---------- demographics stay hard-walled ----------

def test_a_stored_demographic_is_snapped_to_the_forms_wording(monkeypatch):
    monkeypatch.setattr(
        answer_engine_module, "get_candidate_demographics",
        lambda db, profile_id: CandidateDemographics(id="d1", candidate_id=profile_id, gender="female"),
    )
    engine = ApplicationAnswerEngine(
        profile=_profile(), db=object(), user_id="user-1", llm_fn=_llm_that_must_not_be_called(),
    )

    result = engine.answer(Question(text="What is your gender?", options=("Male", "Female", "Prefer not to say")))

    assert result.source == "deterministic"
    assert result.answer == "Female"


def test_an_unmappable_demographic_goes_to_a_human_and_never_to_the_llm(monkeypatch):
    """The one place that must NOT fall through to the LLM the way a factual
    question does — inferring a demographic answer is exactly what this path
    exists to prevent. `llm_fn` raises if it's ever reached."""
    monkeypatch.setattr(
        answer_engine_module, "get_candidate_demographics",
        lambda db, profile_id: CandidateDemographics(id="d1", candidate_id=profile_id, gender="non-binary"),
    )
    engine = ApplicationAnswerEngine(
        profile=_profile(), db=object(), user_id="user-1", llm_fn=_llm_that_must_not_be_called(),
    )

    result = engine.answer(Question(text="What is your gender?", options=("Male", "Female")))

    assert result.source == SOURCE_NEEDS_USER_INPUT
    assert result.answer == ""
    assert result.available_options == ("Male", "Female")


# ---------- cache ----------

def test_a_cached_answer_outside_this_forms_options_is_treated_as_a_miss(monkeypatch):
    """The cache is keyed by question TEXT alone, so the same question can
    come back with a different option set. A stale-shaped hit must not be
    handed to the page."""
    class _Cached:
        answer = "Yes, now or in the future"
        confidence = 0.9

    monkeypatch.setattr(
        answer_engine_module.answer_cache_repository, "get_cached_answer",
        lambda db, user_id, question: _Cached(),
    )
    monkeypatch.setattr(
        answer_engine_module.answer_cache_repository, "save_answer",
        lambda *a, **kw: None,
    )
    engine = ApplicationAnswerEngine(
        profile=_profile(), db=object(), user_id="user-1",
        llm_fn=_llm_returning(["Nope"]),
    )

    result = engine.answer(Question(text="Will you require sponsorship?", options=("Affirmative", "Negative")))

    # Re-derived rather than replayed — and the fresh answer isn't an option
    # either, so it correctly ends up with a human.
    assert result.source == "llm"
    assert result.answer == ""


def test_a_cached_answer_inside_this_forms_options_is_snapped_and_reused(monkeypatch):
    class _Cached:
        answer = "yes"
        confidence = 0.9

    monkeypatch.setattr(
        answer_engine_module.answer_cache_repository, "get_cached_answer",
        lambda db, user_id, question: _Cached(),
    )
    engine = ApplicationAnswerEngine(
        profile=_profile(), db=object(), user_id="user-1",
        llm_fn=_llm_that_must_not_be_called(),
    )

    result = engine.answer(Question(text="Do you have a security clearance?", options=YES_NO))

    assert result.source == "cache"
    assert result.answer == "Yes"


# ---------- meta-commentary never reaches the form ----------
# Every string below was either observed on, or is the same shape as, output
# that a real Greenhouse posting received: "The candidate profile does not
# specify CTC expectations." typed into the salary field. The model reported
# high confidence, so the 0.80 review gate did not catch it — only a code-side
# check can.

def test_meta_commentary_about_a_missing_profile_field_is_discarded():
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning([
            {"answer": "The candidate profile does not specify CTC expectations.", "confidence": 0.95},
        ]),
    )

    result = engine.answer("What is your CTC expectation?")

    assert result.answer == ""
    assert result.confidence == 0.0


def test_meta_commentary_addressed_to_the_operator_is_discarded():
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning([
            {"answer": "My preferred name is not specified in the profile; please use the candidate's legal name.",
             "confidence": 0.9},
        ]),
    )

    result = engine.answer("What is your preferred name?")

    assert result.answer == ""
    assert result.confidence == 0.0


def test_an_i_cannot_determine_answer_is_discarded():
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning([{"answer": "I cannot determine this from the information given.", "confidence": 0.88}]),
    )

    result = engine.answer("How many years of Kubernetes experience do you have?")

    assert result.answer == ""


def test_a_genuine_prose_answer_mentioning_a_candidate_is_not_discarded():
    """The guard must not fire on real answers. "the ideal candidate" is
    normal cover-letter phrasing; only "the candidate profile" is commentary."""
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning([
            "I think the ideal candidate for this role pairs backend depth with product sense, and that's how I work.",
        ]),
    )

    result = engine.answer("Why do you want to work here?")

    assert result.answer.startswith("I think the ideal candidate")


def test_a_terse_factual_answer_is_not_discarded():
    engine = ApplicationAnswerEngine(profile=_profile(), llm_fn=_llm_returning(["Navikenz"]))

    result = engine.answer("Please list your current/most recent employer")

    assert result.answer == "Navikenz"


def test_meta_commentary_is_discarded_even_when_it_would_match_an_option():
    """Order matters: the commentary check runs BEFORE option matching, so a
    sentence that happens to contain a valid option's text ("...does not
    specify, so No.") can't sneak through as that option."""
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning([{"answer": "The candidate profile does not specify, so No.", "confidence": 0.95}]),
    )

    result = engine.answer(Question(text="Have you worked on AWS in production?", options=YES_NO))

    assert result.answer == ""
    assert result.confidence == 0.0


# ---------- call-shape compatibility ----------

def test_answer_accepts_options_as_a_convenience_argument():
    engine = ApplicationAnswerEngine(profile=_profile(), llm_fn=_llm_returning(["No"]))

    result = engine.answer("Do you have a security clearance?", ["Yes", "No"])

    assert result.answer == "No"
    assert result.available_options == ("Yes", "No")


def test_strings_and_questions_mix_freely_in_one_batch():
    calls = []
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning(["I'm excited about your mission.", "Yes"], capture=calls),
    )

    results = engine.answer_batch([
        "Why do you want to work here?",
        Question(text="Do you have a security clearance?", options=YES_NO),
    ])

    assert len(calls) == 1  # still one batched call
    assert [r.answer for r in results] == ["I'm excited about your mission.", "Yes"]
    assert [r.available_options for r in results] == [(), YES_NO]
