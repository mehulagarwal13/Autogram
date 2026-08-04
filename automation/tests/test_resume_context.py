"""
The candidate's résumé reaches the answering LLM, and the answer reaches the page.

The live failure this covers, on a real Greenhouse posting
(job-boards.greenhouse.io/winhomeinspection/jobs/4629237006): a required
"Degree" dropdown and "In which year did you complete your Bachelor's degree?"
both came back blank. Not a dropdown bug and not a model failure — the prompt
carried exactly four facts about the candidate (`current_role`,
`current_company`, `years_of_experience`, `skills`), so a graduation year was
genuinely not in the context, and `_SYSTEM_PROMPT` correctly forbids inventing
one. The education rows existed in the database the whole time; nothing under
`automation/` ever read them.

These tests pin both halves: what gets SENT (`resume_context.py` ->
`_build_prompt`) and what gets FILLED (answer -> `field_handlers` -> the real
DOM), plus the confidence gate that decides between the two.
"""

from __future__ import annotations

import json

import pytest

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, EducationEntry, ExperienceEntry, ProfileDocument
from automation.ats.greenhouse.greenhouse_adapter import GreenhouseAdapter
from automation.forms import answer_engine as answer_engine_module
from automation.forms.answer_engine import ApplicationAnswerEngine, Question
from automation.forms.resume_context import (
    MAX_EDUCATION_ENTRIES,
    MAX_EXPERIENCE_ENTRIES,
    ResumeContext,
    build_resume_context,
    load_resume_context,
)


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(
        profile_id="profile-1", user_id="user-1",
        first_name="Ada", last_name="Lovelace", email="ada@example.com",
        current_company="Navikenz", current_role="Senior Backend Engineer",
        years_of_experience=6.0,
    )
    defaults.update(overrides)
    profile = CandidateProfile(**defaults)
    profile.phone_encrypted = encrypt_field("+1-555-0100")
    return profile


def _education(**overrides) -> EducationEntry:
    defaults = dict(
        education_id="edu-1", profile_id="profile-1",
        degree="B.Tech", field_of_study="Computer Science",
        university="Delhi Technological University",
        start_date="2014", end_date="2018",
    )
    defaults.update(overrides)
    return EducationEntry(**defaults)


def _experience(**overrides) -> ExperienceEntry:
    defaults = dict(
        experience_id="exp-1", profile_id="profile-1",
        company_name="Navikenz", job_title="Senior Backend Engineer",
        start_date="2021", end_date=None,
        description="Owned the payments platform and cut p99 latency by 40%.",
    )
    defaults.update(overrides)
    return ExperienceEntry(**defaults)


_BACHELORS_YEAR_QUESTION = "In which year did you complete your Bachelor's degree?"
_DEGREE_OPTIONS = ("Bachelor's Degree", "Master's Degree", "Doctorate")


def _llm_answering_from_resume(capture: list | None = None):
    """A stub provider that answers ONLY from what the prompt actually contains
    — it reads `candidate_profile.education` out of the payload it was handed.

    Deliberately not a canned string: a hardcoded "2018" would pass whether or
    not the résumé ever reached the model, which is the entire thing under test.
    """

    def _llm_fn(*, task, prompt, system=None, **kwargs):
        if capture is not None:
            capture.append({"task": task, "prompt": prompt, "system": system})
        payload = json.loads(prompt)
        education = payload["candidate_profile"].get("education") or []
        options_per_question = payload.get("options") or [None] * len(payload["questions"])
        answers = []
        for question, options in zip(payload["questions"], options_per_question):
            answer = None
            if education:
                first = education[0]
                if "year" in question.lower():
                    answer = first.get("end_date")
                elif options and "degree" in question.lower():
                    # The résumé says "B.Tech"; the form offers "Bachelor's
                    # Degree" — the translation `_RESUME_PROMPT` asks for.
                    if (first.get("degree") or "").lower().startswith("b"):
                        answer = next((o for o in options if "bachelor" in o.lower()), None)
            answers.append({"answer": answer, "confidence": 0.95 if answer else 0.0})
        return json.dumps({"answers": answers})

    return _llm_fn


def _llm_returning(answers, confidence=0.95):
    def _llm_fn(*, task, prompt, system=None, **kwargs):
        return json.dumps({"answers": [{"answer": a, "confidence": confidence} for a in answers]})

    return _llm_fn


# ---------------------------------------------------------------------------
# Building the context
# ---------------------------------------------------------------------------

def test_education_facts_are_extracted_from_the_stored_rows():
    context = build_resume_context(_profile(), education=[_education()])

    assert context.education == ({
        "degree": "B.Tech",
        "field_of_study": "Computer Science",
        "university": "Delhi Technological University",
        "start_date": "2014",
        "end_date": "2018",
    },)


def test_blank_columns_become_absent_keys_rather_than_empty_strings():
    """An empty string reads to a model as a real (empty) answer; an absent key
    reads as "not known", which is the truth."""
    context = build_resume_context(education=[_education(university="   ", gpa=None, field_of_study=None)])

    assert context.education[0] == {"degree": "B.Tech", "start_date": "2014", "end_date": "2018"}


def test_entries_are_ordered_most_recent_first():
    context = build_resume_context(education=[
        _education(education_id="a", degree="B.Tech", end_date="2018"),
        _education(education_id="b", degree="M.Tech", end_date="2021"),
    ])

    assert [entry["degree"] for entry in context.education] == ["M.Tech", "B.Tech"]


def test_an_entry_with_no_end_date_is_treated_as_current_and_sorts_first():
    """Both `EducationEntry` and `ExperienceEntry` use empty/None end_date for
    "ongoing" — an in-progress master's or the present job."""
    context = build_resume_context(education=[
        _education(education_id="a", degree="B.Tech", end_date="2018"),
        _education(education_id="b", degree="M.Tech", end_date=None),
    ])

    assert [entry["degree"] for entry in context.education] == ["M.Tech", "B.Tech"]


def test_experience_job_descriptions_are_never_included():
    """The largest text on a résumé, the least useful for a screening answer,
    and prose a model can paste verbatim into a form field."""
    context = build_resume_context(experience=[_experience()])

    assert context.experience == ({
        "job_title": "Senior Backend Engineer",
        "company_name": "Navikenz",
        "start_date": "2021",
    },)
    assert "description" not in context.experience[0]
    assert "latency" not in json.dumps(context.as_prompt_payload())


def test_long_histories_are_capped():
    context = build_resume_context(
        education=[_education(education_id=f"e{i}", end_date=f"20{i:02d}") for i in range(12)],
        experience=[_experience(experience_id=f"x{i}", end_date=f"20{i:02d}") for i in range(20)],
    )

    assert len(context.education) == MAX_EDUCATION_ENTRIES
    assert len(context.experience) == MAX_EXPERIENCE_ENTRIES


def test_a_row_with_every_column_blank_is_dropped():
    context = build_resume_context(education=[_education(
        degree=None, field_of_study=None, university=None, start_date=None, end_date=None,
    )])

    assert context.education == ()


def test_certifications_come_from_the_skills_json():
    profile = _profile(skills={"programming_languages": ["Python"], "certifications": ["AWS SAA", " CKA "]})

    context = build_resume_context(profile)

    assert context.certifications == ("AWS SAA", "CKA")


@pytest.mark.parametrize("skills", [None, [], "Python", {"certifications": "AWS SAA"}])
def test_a_skills_column_that_is_not_a_dict_of_lists_is_ignored(skills):
    assert build_resume_context(_profile(skills=skills)).certifications == ()


def test_an_empty_context_is_falsey_and_contributes_no_payload():
    context = ResumeContext()

    assert bool(context) is False
    assert context.as_prompt_payload() == {}


def test_no_db_yields_an_empty_context_without_raising():
    assert load_resume_context(None, _profile()).education == ()


def test_a_failing_query_degrades_to_the_base_profile_instead_of_raising():
    """Résumé enrichment is a best-effort improvement; losing it costs one
    unfilled field, whereas an escaping exception aborts the whole run."""

    class _ExplodingSession:
        def __getattr__(self, name):
            raise RuntimeError("db is down")

    context = load_resume_context(_ExplodingSession(), _profile())

    assert bool(context) is False


# ---------------------------------------------------------------------------
# What actually reaches the model
# ---------------------------------------------------------------------------

def test_education_and_experience_are_sent_in_the_prompt():
    calls = []
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_answering_from_resume(calls),
        resume_context=build_resume_context(_profile(), education=[_education()], experience=[_experience()]),
    )

    engine.answer_batch([_BACHELORS_YEAR_QUESTION])

    sent = json.loads(calls[0]["prompt"])["candidate_profile"]
    assert sent["education"][0]["degree"] == "B.Tech"
    assert sent["education"][0]["end_date"] == "2018"
    assert sent["experience"][0]["company_name"] == "Navikenz"
    # The four original facts are still there — this adds, never replaces.
    assert sent["current_company"] == "Navikenz"
    assert sent["years_of_experience"] == 6.0


def test_a_candidate_with_no_resume_rows_produces_the_original_prompt_and_system():
    """Backward compatibility: no stored education/experience means byte-for-byte
    the prompt and system message this engine sent before résumé context existed."""
    calls = []
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning(["Noida, India"]),
        resume_context=ResumeContext(),
    )
    engine._llm_fn = _llm_answering_from_resume(calls)

    engine.answer_batch(["Where are you based?"])

    sent = json.loads(calls[0]["prompt"])["candidate_profile"]
    assert set(sent) == {"current_role", "current_company", "years_of_experience", "skills"}
    assert calls[0]["system"] == answer_engine_module._SYSTEM_PROMPT


def test_the_resume_instructions_are_appended_only_when_there_are_resume_facts():
    calls = []
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_answering_from_resume(calls),
        resume_context=build_resume_context(education=[_education()]),
    )

    engine.answer_batch([_BACHELORS_YEAR_QUESTION])

    assert answer_engine_module._RESUME_PROMPT in calls[0]["system"]


def test_both_resume_and_option_instructions_appear_for_an_option_bearing_question():
    calls = []
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_answering_from_resume(calls),
        resume_context=build_resume_context(education=[_education()]),
    )

    engine.answer_batch([Question("Degree", _DEGREE_OPTIONS)])

    assert answer_engine_module._RESUME_PROMPT in calls[0]["system"]
    assert calls[0]["system"].endswith(answer_engine_module._OPTION_PROMPT)


# ---------------------------------------------------------------------------
# The questions that were blank on the real form
# ---------------------------------------------------------------------------

def test_the_graduation_year_question_is_now_answered_from_the_resume():
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_answering_from_resume(),
        resume_context=build_resume_context(education=[_education()]),
    )

    result = engine.answer(_BACHELORS_YEAR_QUESTION)

    assert result.answer == "2018"
    assert result.source == "llm"


def test_the_same_question_stays_unanswered_without_resume_facts():
    """The old behavior, kept explicit: with nothing to answer from, `null` is
    correct — the never-invent rule, not a regression."""
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_answering_from_resume(),
        resume_context=ResumeContext(),
    )

    result = engine.answer(_BACHELORS_YEAR_QUESTION)

    assert result.answer == ""
    assert result.confidence == 0.0


def test_a_resume_degree_is_translated_into_the_forms_own_option():
    """"B.Tech" is not one of the form's choices, and `_match_option` discards
    an answer that isn't — so the translation has to happen in the answer."""
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_answering_from_resume(),
        resume_context=build_resume_context(education=[_education()]),
    )

    result = engine.answer(Question("Degree", _DEGREE_OPTIONS))

    assert result.answer == "Bachelor's Degree"


# ---------------------------------------------------------------------------
# ...and the answer lands in the real DOM
# ---------------------------------------------------------------------------

_EDUCATION_FORM_HTML = """
<html><body>
  <label for="degree_q">Degree*</label>
  <select id="degree_q">
    <option value="">Select...</option>
    <option>Bachelor's Degree</option>
    <option>Master's Degree</option>
    <option>Doctorate</option>
  </select>

  <label for="grad_year_q">In which year did you complete your Bachelor's degree?*</label>
  <input type="text" id="grad_year_q">
</body></html>
"""


@pytest.fixture
def resume_file(tmp_path):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4 fake resume bytes")
    return path


def _resume_document(path) -> ProfileDocument:
    return ProfileDocument(
        document_id="doc-1", profile_id="profile-1", document_type="resume",
        original_filename="resume.pdf", stored_path=str(path), file_hash="abc", is_default=True,
    )


def _adapter(page, resume_file, engine):
    return GreenhouseAdapter(
        page=page, profile=_profile(), resume_document=_resume_document(resume_file),
        answer_engine=engine,
    )


def test_the_answers_are_typed_and_selected_on_the_real_page(page, resume_file):
    """End to end through Playwright: résumé -> prompt -> answer -> DOM. The
    dropdown gets the form's own option string and the text input gets the year."""
    page.set_content(_EDUCATION_FORM_HTML)
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_answering_from_resume(),
        resume_context=build_resume_context(education=[_education()]),
    )

    _adapter(page, resume_file, engine).answer_questions()

    assert page.locator("#grad_year_q").input_value() == "2018"
    assert page.locator("#degree_q").input_value() == "Bachelor's Degree"


def test_a_low_confidence_answer_is_left_blank_for_a_human(page, resume_file):
    """The `ANSWER_REVIEW_CONFIDENCE_THRESHOLD` (0.80) gate still governs, even
    for a résumé-derived answer: below it, nothing is typed. Pinned because it
    is the difference between "the résumé is in the prompt" and "the field gets
    filled" — a model that under-scores a copied fact still leaves it empty,
    which is why `_RESUME_PROMPT` tells it to score copied facts >= 0.9."""
    page.set_content(_EDUCATION_FORM_HTML)
    engine = ApplicationAnswerEngine(
        profile=_profile(),
        llm_fn=_llm_returning(["2018", "Bachelor's Degree"], confidence=0.6),
        resume_context=build_resume_context(education=[_education()]),
    )

    results = _adapter(page, resume_file, engine).answer_questions()

    assert page.locator("#grad_year_q").input_value() == ""
    assert any(r.filled is False for r in results)
