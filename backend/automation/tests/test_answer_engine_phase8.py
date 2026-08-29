"""
Phase 8 additions to ApplicationAnswerEngine: the granular
work_authorized/requires_sponsorship/visa_type booleans, and the hard rule
that a demographic question is NEVER answered by the LLM.

Same conventions as tests/test_answer_engine.py: a real (unsaved)
CandidateProfile ORM instance, a fake `llm_fn` injected in place of
`automation.interfaces.generate_answer`, and `automation.interfaces.get_candidate_demographics`
monkeypatched for the demographics-specific tests (no live DB needed).
"""

from app.models.db_models import CandidateDemographics, CandidateProfile
from automation.forms import answer_engine as answer_engine_module
from automation.forms.answer_engine import ApplicationAnswerEngine, SOURCE_NEEDS_USER_INPUT


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(
        profile_id="profile-1",
        user_id="user-1",
        work_authorization=None,
        visa_status=None,
        work_authorized=None,
        requires_sponsorship=None,
        visa_type=None,
        skills={},
    )
    defaults.update(overrides)
    return CandidateProfile(**defaults)


def _llm_that_must_not_be_called():
    def fake_llm_fn(*, task, prompt, system=None, **overrides):
        raise AssertionError("the LLM must never be called for this question")
    return fake_llm_fn


# ---------- requires_sponsorship / work_authorized booleans ----------

def test_requires_sponsorship_true_answers_yes_with_visa_type():
    profile = _profile(requires_sponsorship=True, visa_type="H1B")
    engine = ApplicationAnswerEngine(profile=profile, llm_fn=_llm_that_must_not_be_called())

    result = engine.answer("Will you now or in the future require sponsorship?")

    assert result.source == "deterministic"
    assert result.answer == "Yes (H1B)"


def test_requires_sponsorship_false_answers_no():
    profile = _profile(requires_sponsorship=False)
    engine = ApplicationAnswerEngine(profile=profile, llm_fn=_llm_that_must_not_be_called())

    result = engine.answer("Will you require sponsorship?")

    assert result.answer == "No"


def test_work_authorized_true_answers_yes():
    profile = _profile(work_authorized=True)
    engine = ApplicationAnswerEngine(profile=profile, llm_fn=_llm_that_must_not_be_called())

    result = engine.answer("Are you legally authorized to work in the United States?")

    assert result.answer == "Yes"


def test_work_authorized_false_answers_no():
    profile = _profile(work_authorized=False)
    engine = ApplicationAnswerEngine(profile=profile, llm_fn=_llm_that_must_not_be_called())

    result = engine.answer("Are you legally authorized to work in the United States?")

    assert result.answer == "No"


def test_sponsorship_falls_back_to_legacy_free_text_when_boolean_never_set():
    # Backward compatibility: a profile that only ever filled in the old
    # free-text work_authorization field (pre-Phase-8) still gets a sensible
    # echoed answer instead of nothing at all.
    profile = _profile(work_authorization="Requires H1B sponsorship")
    engine = ApplicationAnswerEngine(profile=profile, llm_fn=_llm_that_must_not_be_called())

    result = engine.answer("Do you require visa sponsorship now or in the future?")

    assert result.source == "deterministic"
    assert result.answer == "Requires H1B sponsorship"


def test_visa_type_question_answers_from_profile():
    profile = _profile(visa_type="F1 OPT")
    engine = ApplicationAnswerEngine(profile=profile, llm_fn=_llm_that_must_not_be_called())

    result = engine.answer("What type of visa do you currently hold?")

    assert result.answer == "F1 OPT"


def test_sponsorship_question_falls_through_to_llm_when_nothing_on_file():
    profile = _profile()  # nothing set anywhere
    engine = ApplicationAnswerEngine(
        profile=profile,
        llm_fn=lambda *, task, prompt, system=None, **kw: '{"answers": ["I am open to discussing sponsorship."]}',
    )

    result = engine.answer("Will you require sponsorship?")

    assert result.source == "llm"


# ---------- demographic questions: never guessed, never sent to the LLM ----------

def test_demographic_question_answers_from_a_stored_preference(monkeypatch):
    monkeypatch.setattr(
        answer_engine_module, "get_candidate_demographics",
        lambda db, profile_id: CandidateDemographics(id="d1", candidate_id=profile_id, gender="female"),
    )
    engine = ApplicationAnswerEngine(profile=_profile(), db=object(), user_id="user-1", llm_fn=_llm_that_must_not_be_called())

    result = engine.answer("What is your gender?")

    assert result.source == "deterministic"
    assert result.answer == "female"


def test_demographic_question_with_nothing_stored_needs_user_input_not_llm():
    # No db/user_id at all — `_get_demographics` can't look anything up, so
    # this must land on SOURCE_NEEDS_USER_INPUT, and critically must NOT
    # reach the LLM (the injected llm_fn raises if it's ever called).
    engine = ApplicationAnswerEngine(profile=_profile(), llm_fn=_llm_that_must_not_be_called())

    result = engine.answer("Are you a protected veteran?")

    assert result.source == SOURCE_NEEDS_USER_INPUT
    assert result.answer == ""
    assert result.confidence == 0.0


def test_demographic_question_with_a_db_but_no_stored_row_needs_user_input_not_llm(monkeypatch):
    monkeypatch.setattr(answer_engine_module, "get_candidate_demographics", lambda db, profile_id: None)
    engine = ApplicationAnswerEngine(profile=_profile(), db=object(), user_id="user-1", llm_fn=_llm_that_must_not_be_called())

    result = engine.answer("What is your race/ethnicity?")

    assert result.source == SOURCE_NEEDS_USER_INPUT


def test_demographic_question_never_appears_in_the_batched_llm_call(monkeypatch):
    monkeypatch.setattr(answer_engine_module, "get_candidate_demographics", lambda db, profile_id: None)
    calls = []

    def fake_llm_fn(*, task, prompt, system=None, **overrides):
        import json
        calls.append(json.loads(prompt)["questions"])
        return json.dumps({"answers": ["I'm excited about your mission."]})

    engine = ApplicationAnswerEngine(profile=_profile(), db=object(), user_id="user-1", llm_fn=fake_llm_fn)

    results = engine.answer_batch(["Do you have a disability?", "Why do you want to work here?"])

    assert len(calls) == 1
    assert calls[0] == ["Why do you want to work here?"]  # the demographic question was excluded
    assert results[0].source == SOURCE_NEEDS_USER_INPUT
    assert results[1].source == "llm"
