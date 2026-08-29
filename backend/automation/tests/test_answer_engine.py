"""
ApplicationAnswerEngine (Phase 6) — pure-logic tests. Uses a real
CandidateProfile ORM instance (no DB needed, same convention
test_greenhouse_adapter.py uses) and a fake `llm_fn` injected in place of
`automation.interfaces.generate_answer`, so these tests never touch a real
LLM provider or DB. The cache-wiring tests monkeypatch
`app.services.answer_cache_repository` directly (the same module object
answer_engine.py imports and calls through).
"""

import json

from app.models.db_models import CandidateProfile
from automation.forms import answer_engine as answer_engine_module
from automation.forms.answer_engine import ApplicationAnswerEngine


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(
        profile_id="profile-1",
        user_id="user-1",
        current_role="Backend Engineer",
        current_company="Analytical Engines Ltd",
        years_of_experience=5.0,
        notice_period_days=30,
        expected_salary=120000.0,
        expected_salary_currency="USD",
        work_authorization="US Citizen",
        visa_status=None,
        skills={"programming_languages": ["Python"]},
    )
    defaults.update(overrides)
    return CandidateProfile(**defaults)


def _llm_returning(answers: list[str]):
    def fake_llm_fn(*, task, prompt, system=None, **overrides):
        assert task == "application_answer"
        payload = json.loads(prompt)
        assert len(payload["questions"]) == len(answers)
        return json.dumps({"answers": answers})
    return fake_llm_fn


# ---------- deterministic path ----------

def test_notice_period_question_is_answered_deterministically():
    engine = ApplicationAnswerEngine(profile=_profile(), llm_fn=_llm_returning(["should not be called"]))

    result = engine.answer("What is your notice period?")

    assert result.source == "deterministic"
    assert result.answer == "30 days"
    assert result.confidence == answer_engine_module.DETERMINISTIC_CONFIDENCE


def test_expected_salary_question_is_formatted_with_currency():
    engine = ApplicationAnswerEngine(profile=_profile(expected_salary=95000.0, expected_salary_currency="INR"))

    result = engine.answer("What is your expected salary?")

    assert result.source == "deterministic"
    assert result.answer == "INR 95,000"


def test_years_of_experience_question_drops_trailing_zero():
    engine = ApplicationAnswerEngine(profile=_profile(years_of_experience=5.0))
    result = engine.answer("How many years of experience do you have?")
    assert result.answer == "5 years"


def test_work_authorization_echoes_profile_text_without_inferring_yes_no():
    engine = ApplicationAnswerEngine(profile=_profile(work_authorization="Requires H1B sponsorship"))
    result = engine.answer("Do you require visa sponsorship now or in the future?")
    assert result.source == "deterministic"
    assert result.answer == "Requires H1B sponsorship"


def test_falls_back_to_llm_when_deterministic_category_matches_but_profile_field_is_empty():
    """A factual question this class recognizes, but the profile has nothing
    to say — must NOT guess; it should defer to the LLM path instead."""
    profile = _profile(notice_period_days=None)
    engine = ApplicationAnswerEngine(profile=profile, llm_fn=_llm_returning(["I can start within two weeks."]))

    result = engine.answer("When can you start?")

    assert result.source == "llm"
    assert result.answer == "I can start within two weeks."


# ---------- LLM path / batching ----------

def test_answer_batch_sends_only_unmatched_questions_to_the_llm_in_one_call():
    calls = []

    def fake_llm_fn(*, task, prompt, system=None, **overrides):
        calls.append(json.loads(prompt)["questions"])
        return json.dumps({"answers": ["I'm excited about your mission.", "I thrive under pressure."]})

    engine = ApplicationAnswerEngine(profile=_profile(), llm_fn=fake_llm_fn)
    questions = [
        "What is your notice period?",          # deterministic
        "Why do you want to work here?",        # llm
        "Describe a time you handled pressure.", # llm
    ]

    results = engine.answer_batch(questions)

    assert len(calls) == 1  # one batched call, not three
    assert calls[0] == ["Why do you want to work here?", "Describe a time you handled pressure."]
    assert [r.answer for r in results] == [
        "30 days",
        "I'm excited about your mission.",
        "I thrive under pressure.",
    ]
    assert [r.question for r in results] == questions  # original order preserved
    assert results[0].source == "deterministic"
    assert results[1].source == "llm" and results[2].source == "llm"


def test_llm_failure_yields_an_empty_answer_instead_of_a_placeholder_string():
    def broken_llm_fn(*, task, prompt, system=None, **overrides):
        raise RuntimeError("provider is down")

    engine = ApplicationAnswerEngine(profile=_profile(), llm_fn=broken_llm_fn)

    result = engine.answer("Why do you want to work here?")

    assert result.answer == ""
    assert result.confidence == 0.0


def test_malformed_llm_json_yields_an_empty_answer_instead_of_raising():
    def malformed_llm_fn(*, task, prompt, system=None, **overrides):
        return "not json at all"

    engine = ApplicationAnswerEngine(profile=_profile(), llm_fn=malformed_llm_fn)

    result = engine.answer("Why do you want to work here?")

    assert result.answer == ""


def test_llm_answer_count_mismatch_yields_empty_answers_instead_of_misaligning():
    def mismatched_llm_fn(*, task, prompt, system=None, **overrides):
        return json.dumps({"answers": ["only one answer"]})

    engine = ApplicationAnswerEngine(profile=_profile(), llm_fn=mismatched_llm_fn)

    results = engine.answer_batch(["Why do you want to work here?", "Describe your biggest strength."])

    assert [r.answer for r in results] == ["", ""]


# ---------- cache wiring (app.services.answer_cache_repository) ----------

class _FakeCacheRow:
    def __init__(self, answer, confidence):
        self.answer = answer
        self.confidence = confidence


def test_answer_uses_a_cached_row_and_never_calls_the_llm(monkeypatch):
    monkeypatch.setattr(
        answer_engine_module.answer_cache_repository, "get_cached_answer",
        lambda db, user_id, question: _FakeCacheRow(answer="I love your mission.", confidence=0.6),
    )
    calls = []

    def fake_llm_fn(*, task, prompt, system=None, **overrides):
        calls.append(1)
        return json.dumps({"answers": ["should not be reached"]})

    engine = ApplicationAnswerEngine(profile=_profile(), db=object(), user_id="user-1", llm_fn=fake_llm_fn)

    result = engine.answer("Why do you want to work here?")

    assert result.source == "cache"
    assert result.answer == "I love your mission."
    assert calls == []


def test_answer_saves_a_fresh_llm_answer_to_the_cache(monkeypatch):
    monkeypatch.setattr(answer_engine_module.answer_cache_repository, "get_cached_answer", lambda *a, **k: None)
    saved = {}

    def fake_save_answer(db, user_id, question, *, answer, source, confidence):
        saved["question"] = question
        saved["answer"] = answer
        saved["source"] = source
        saved["confidence"] = confidence

    monkeypatch.setattr(answer_engine_module.answer_cache_repository, "save_answer", fake_save_answer)

    engine = ApplicationAnswerEngine(
        profile=_profile(), db=object(), user_id="user-1",
        llm_fn=_llm_returning(["I'm excited about the mission."]),
    )

    engine.answer("Why do you want to work here?")

    assert saved == {
        "question": "Why do you want to work here?",
        "answer": "I'm excited about the mission.",
        "source": "llm",
        "confidence": answer_engine_module.LLM_CONFIDENCE,
    }


def test_a_failed_cache_write_does_not_break_an_otherwise_good_answer(monkeypatch):
    monkeypatch.setattr(answer_engine_module.answer_cache_repository, "get_cached_answer", lambda *a, **k: None)

    def broken_save(*a, **k):
        raise RuntimeError("db is down")

    monkeypatch.setattr(answer_engine_module.answer_cache_repository, "save_answer", broken_save)

    engine = ApplicationAnswerEngine(
        profile=_profile(), db=object(), user_id="user-1",
        llm_fn=_llm_returning(["I'm excited about the mission."]),
    )

    result = engine.answer("Why do you want to work here?")  # must not raise

    assert result.answer == "I'm excited about the mission."


def test_no_db_or_user_id_skips_the_cache_entirely_without_erroring():
    engine = ApplicationAnswerEngine(profile=_profile(), llm_fn=_llm_returning(["An honest answer."]))
    result = engine.answer("Why do you want to work here?")
    assert result.answer == "An honest answer."


# ---------- model-reported per-answer confidence ----------
# Both specs require the model to report its own honest confidence per answer
# so the 0.80 review gate in automation/ats/base.py has a real number to act
# on. Previously every LLM answer was stamped with one flat constant, which
# made any such gate either a no-op or a blanket shutoff.

def _llm_returning_objects(entries: list[dict]):
    def fake_llm_fn(*, task, prompt, system=None, **overrides):
        return json.dumps({"answers": entries})
    return fake_llm_fn


def test_uses_the_confidence_the_model_reported():
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning_objects([{"answer": "A well-grounded answer.", "confidence": 0.91}]),
    )

    result = engine.answer("Why do you want to work here?")

    assert result.answer == "A well-grounded answer."
    assert result.confidence == 0.91
    assert result.source == "llm"


def test_a_bare_string_answer_still_works_and_falls_back_to_the_flat_confidence():
    """Backward compatibility: a model that ignores the new schema (or any
    caller/test written against the original contract) must still produce a
    usable answer rather than failing the whole batch."""
    engine = ApplicationAnswerEngine(
        profile=_profile(), llm_fn=_llm_returning(["A plain string answer."]),
    )

    result = engine.answer("Why do you want to work here?")

    assert result.answer == "A plain string answer."
    assert result.confidence == answer_engine_module.LLM_CONFIDENCE


def test_an_out_of_range_confidence_is_clamped_not_trusted():
    """A model must not be able to claim its way past the review gate."""
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning_objects([{"answer": "Overconfident.", "confidence": 7.5}]),
    )
    assert engine.answer("Why here?").confidence == 1.0

    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning_objects([{"answer": "Negative.", "confidence": -3}]),
    )
    assert engine.answer("Why here?").confidence == 0.0


def test_a_non_numeric_confidence_falls_back_instead_of_crashing():
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning_objects([{"answer": "Fine answer.", "confidence": "high"}]),
    )

    result = engine.answer("Why do you want to work here?")

    assert result.answer == "Fine answer."
    assert result.confidence == answer_engine_module.LLM_CONFIDENCE


def test_per_answer_confidence_is_kept_aligned_across_a_batch():
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning_objects([
            {"answer": "Grounded.", "confidence": 0.95},
            {"answer": "Guessing.", "confidence": 0.2},
        ]),
    )

    results = engine.answer_batch(["Why here?", "Describe a conflict you resolved."])

    assert [r.answer for r in results] == ["Grounded.", "Guessing."]
    assert [r.confidence for r in results] == [0.95, 0.2]
