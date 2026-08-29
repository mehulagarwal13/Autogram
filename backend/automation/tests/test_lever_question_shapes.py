"""
The three required questions a live Lever posting left blank, and why.

Source: jobs.lever.co/terrahq/0db904b6-be20-4a37-aeae-955e5300aa3c/apply — all
fixtures below are the DOM shapes read off that page, not invented ones.

Three unrelated root causes, one per section:

1. **The radio group was never asked.** `_NON_FILLABLE_INPUT_TYPES` contained
   `"radio"`, so every member of "Are you fluent in English?" returned early
   from `_collect_for_answer_engine` and the group reached the engine zero
   times. Compounding it, each radio's own `<label>` holds the OPTION text
   ("Yes"), and the group's question sits four ancestors up in a container
   holding all three radios — where `_NEARBY_QUESTION_TEXT_JS` structurally
   cannot look, because it stops at the first ancestor with >1 control.
2. **"When are you available to start working?" reached the LLM starved.** It
   matched no `question_classifier` phrase, and the LLM's view of the candidate
   was four fields that did not include `notice_period_days` — so `null` was
   the correct answer to give, and the field stayed blank.
3. **The required marker is part of the text.** Lever renders U+2731 HEAVY
   ASTERISK, so the recovered question was
   'When are you available to start working?✱' — which travels into the prompt,
   the answer cache key, and synonym matching.
"""

from __future__ import annotations

import json

import pytest

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, ProfileDocument
from automation.ats.base import _strip_required_marker
from automation.ats.lever.lever_adapter import LeverAdapter
from automation.forms.answer_engine import ApplicationAnswerEngine
from automation.forms.question_classifier import (
    CATEGORY_DEMOGRAPHIC_GENDER,
    CATEGORY_NOTICE_PERIOD,
    classify_question,
    is_demographic,
)


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(
        profile_id="profile-1", user_id="user-1",
        first_name="Ada", last_name="Lovelace", email="ada@example.com",
        location="Noida, India", current_company="Navikenz",
        current_role="Senior Backend Engineer", years_of_experience=6.0,
        notice_period_days=30,
        website_url="https://ada.example.dev/",
    )
    defaults.update(overrides)
    profile = CandidateProfile(**defaults)
    profile.phone_encrypted = encrypt_field("+1-555-0100")
    return profile


@pytest.fixture
def resume_file(tmp_path):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return path


def _adapter(page, resume_file, engine=None):
    return LeverAdapter(
        page=page, profile=_profile(),
        resume_document=ProfileDocument(
            document_id="doc-1", profile_id="profile-1", document_type="resume",
            original_filename="resume.pdf", stored_path=str(resume_file),
            file_hash="abc", is_default=True,
        ),
        answer_engine=engine,
    )


def _recording_engine(answer_for, capture: list):
    """A real `ApplicationAnswerEngine` with a stub provider, so the whole
    option-awareness path (`read_field_options` -> prompt -> `_match_option`)
    runs for real. `answer_for` maps a question substring to the answer."""

    def _llm_fn(*, task, prompt, system=None, **kwargs):
        payload = json.loads(prompt)
        options_list = payload.get("options") or [None] * len(payload["questions"])
        answers = []
        for question, options in zip(payload["questions"], options_list):
            capture.append({"question": question, "options": options, "profile": payload["candidate_profile"]})
            answer = next((v for k, v in answer_for.items() if k.lower() in question.lower()), None)
            answers.append({"answer": answer, "confidence": 0.95 if answer else 0.0})
        return json.dumps({"answers": answers})

    return ApplicationAnswerEngine(profile=_profile(), llm_fn=_llm_fn)


# The exact nesting from the live page: label > li > ul > div.application-field
# > div (holds the question) > li.application-question.
_FLUENCY_RADIO_HTML = """
<html><body><ul>
  <li class="application-question custom-question">
    <div>Are you fluent in English?<span class="required">✱</span>
      <div class="application-field full-width required-field">
        <ul>
          <li><label><input type="radio" name="cards[abc][field0]" value="Yes" required>Yes</label></li>
          <li><label><input type="radio" name="cards[abc][field0]" value="No" required>No</label></li>
          <li><label><input type="radio" name="cards[abc][field0]" value="Limited Working Proficiency" required>Limited Working Proficiency</label></li>
        </ul>
      </div>
    </div>
  </li>
</ul></body></html>
"""


# ---------------------------------------------------------------------------
# 1. The radio group
# ---------------------------------------------------------------------------

def test_the_radio_group_is_asked_as_one_question_with_its_real_options(page, resume_file):
    asked = []
    page.set_content(_FLUENCY_RADIO_HTML)

    _adapter(page, resume_file, _recording_engine({"fluent in English": "Yes"}, asked)).answer_questions()

    assert len(asked) == 1, f"a 3-member group must be ONE question, got {[a['question'] for a in asked]}"
    assert asked[0]["question"] == "Are you fluent in English?"
    assert asked[0]["options"] == ["Yes", "No", "Limited Working Proficiency"]


def test_the_chosen_option_is_actually_selected_on_the_page(page, resume_file):
    page.set_content(_FLUENCY_RADIO_HTML)

    _adapter(page, resume_file, _recording_engine({"fluent in English": "Yes"}, [])).answer_questions()

    assert page.locator("input[value='Yes']").is_checked() is True
    assert page.locator("input[value='No']").is_checked() is False
    assert page.locator("input[value='Limited Working Proficiency']").is_checked() is False


def test_a_non_first_option_is_selected_just_as_well(page, resume_file):
    """Guards against "it only ever works for the first radio"."""
    page.set_content(_FLUENCY_RADIO_HTML)

    _adapter(page, resume_file, _recording_engine(
        {"fluent in English": "Limited Working Proficiency"}, [])).answer_questions()

    assert page.locator("input[value='Limited Working Proficiency']").is_checked() is True
    assert page.locator("input[value='Yes']").is_checked() is False


def test_the_group_produces_exactly_one_result_not_one_per_member(page, resume_file):
    """Every member is marked examined, so no later pass re-counts the group —
    which would inflate `ApplicationFlowManager`'s confidence average."""
    page.set_content(_FLUENCY_RADIO_HTML)

    results = _adapter(page, resume_file, _recording_engine({"fluent in English": "Yes"}, [])).answer_questions()

    fluency = [r for r in results if "fluent" in r.field_key.lower()]
    assert len(fluency) == 1
    assert fluency[0].filled is True


def test_an_option_label_is_never_asked_as_the_question(page, resume_file):
    """The pre-fix failure mode if radios were simply let through: the engine
    gets asked "Yes", "No" and "Limited Working Proficiency" — three questions,
    none of them a question."""
    asked = []
    page.set_content(_FLUENCY_RADIO_HTML)

    _adapter(page, resume_file, _recording_engine({}, asked)).answer_questions()

    for item in asked:
        assert item["question"] not in ("Yes", "No", "Limited Working Proficiency")


def test_a_radio_group_with_no_recoverable_question_is_left_alone(page, resume_file):
    """No prose anywhere near it — better to leave it for a human than to ask
    the engine about an option label."""
    asked = []
    page.set_content(
        "<html><body><ul>"
        "<li><label><input type='radio' name='g' value='Yes'>Yes</label></li>"
        "<li><label><input type='radio' name='g' value='No'>No</label></li>"
        "</ul></body></html>"
    )

    _adapter(page, resume_file, _recording_engine({}, asked)).answer_questions()

    assert asked == []
    assert page.locator("input[value='Yes']").is_checked() is False


def test_a_radio_group_still_needs_no_engine_to_be_harmless(page, resume_file):
    """No engine injected — the group must simply be skipped, exactly as before."""
    page.set_content(_FLUENCY_RADIO_HTML)

    _adapter(page, resume_file, engine=None).answer_questions()

    assert page.locator("input[value='Yes']").is_checked() is False


# ---------------------------------------------------------------------------
# 2. "When are you available to start working?"
# ---------------------------------------------------------------------------

_AVAILABILITY_HTML = """
<html><body>
  <div><div>When are you available to start working?<span>✱</span>
    <input type="text" name="cards[abc][field1]" placeholder="Type your response" required>
  </div></div>
</body></html>
"""


@pytest.mark.parametrize("question", [
    "When are you available to start working?",
    "When can you join?",
    "What is your earliest start date?",
    "How soon can you join us?",
    "When would you be able to start?",
    "What is your notice period?",
])
def test_availability_phrasings_resolve_to_the_notice_period_fact(question):
    assert classify_question(question) == CATEGORY_NOTICE_PERIOD


def test_a_start_date_year_field_is_not_mistaken_for_a_notice_period():
    """Greenhouse's Education block has "Start date year" / "End date year"
    inputs. Answering those with "30 days" would be confidently wrong, which is
    why no bare "start date" phrase is in the notice-period list."""
    assert classify_question("Start date year") != CATEGORY_NOTICE_PERIOD
    assert classify_question("End date year") != CATEGORY_NOTICE_PERIOD


def test_the_availability_question_is_answered_from_the_profile_not_the_llm(page, resume_file):
    asked = []
    page.set_content(_AVAILABILITY_HTML)

    results = _adapter(page, resume_file, _recording_engine({}, asked)).answer_questions()

    assert page.locator("input[name='cards[abc][field1]']").input_value() == "30 days"
    filled = [r for r in results if r.filled]
    assert filled and filled[0].confidence == pytest.approx(0.9)  # deterministic, not the 0.6 LLM tier
    assert asked == [], "a fact already on the profile must never cost an LLM call"


# ---------------------------------------------------------------------------
# 3. The required marker
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("When are you available to start working?✱", "When are you available to start working?"),
    ("Are you fluent in English? *", "Are you fluent in English?"),
    ("Degree*", "Degree"),
    ("  Email ✱ ", "Email"),
    ("Please share a link to your portfolio or previous work.✱",
     "Please share a link to your portfolio or previous work."),
    ("No marker here", "No marker here"),
])
def test_required_markers_are_stripped_from_recovered_question_text(raw, expected):
    assert _strip_required_marker(raw) == expected


def test_the_question_reaching_the_engine_carries_no_marker(page, resume_file):
    asked = []
    page.set_content(
        "<html><body><div><div>Please share a link to your portfolio or previous work.<span>✱</span>"
        "<input type='text' name='cards[abc][field2]' placeholder='Type your response' required>"
        "</div></div></body></html>"
    )

    _adapter(page, resume_file, _recording_engine({"portfolio": "https://ada.example.dev/"}, asked)).answer_questions()

    assert asked[0]["question"] == "Please share a link to your portfolio or previous work."
    assert "✱" not in asked[0]["question"]


# ---------------------------------------------------------------------------
# What the model is now told about the candidate
# ---------------------------------------------------------------------------

def test_form_relevant_profile_facts_reach_the_prompt(page, resume_file):
    asked = []
    page.set_content(
        "<html><body><div><div>Tell us something about your setup.<span>✱</span>"
        "<input type='text' name='cards[abc][field9]' placeholder='Type your response' required>"
        "</div></div></body></html>"
    )

    _adapter(page, resume_file, _recording_engine({}, asked)).answer_questions()

    sent = asked[0]["profile"]
    assert sent["notice_period_days"] == 30
    assert sent["location"] == "Noida, India"
    assert sent["website_url"] == "https://ada.example.dev/"


def test_contact_pii_is_never_sent_to_the_model(page, resume_file):
    """Names/email are owned by `FieldMapper` at 0.97 and phone/address are
    encrypted at rest — none of them belong in an outbound prompt."""
    asked = []
    page.set_content(
        "<html><body><div><div>Tell us something about your setup.<span>✱</span>"
        "<input type='text' name='cards[abc][field9]' placeholder='Type your response' required>"
        "</div></div></body></html>"
    )

    _adapter(page, resume_file, _recording_engine({}, asked)).answer_questions()

    sent = json.dumps(asked[0]["profile"])
    for secret in ("ada@example.com", "+1-555-0100", "Lovelace"):
        assert secret not in sent


# ---------------------------------------------------------------------------
# EEO questions still never reach the LLM
# ---------------------------------------------------------------------------

def test_a_lever_style_gender_select_label_is_classified_demographic():
    """The live label text, option strings and all. It previously matched no
    gender phrase and was routed to the LLM — which both this module and
    answer_engine.py state must never happen for a demographic question. Its
    `eeo[race]` / `eeo[veteran]` siblings were caught only because their option
    text happens to contain "hispanic or latino" / "veteran status"."""
    category = classify_question("GenderSelect ...MaleFemaleDecline to self-identify")

    assert category == CATEGORY_DEMOGRAPHIC_GENDER
    assert is_demographic(category) is True


def test_a_gender_question_is_never_sent_to_the_llm():
    asked = []
    engine = _recording_engine({}, asked)

    result = engine.answer("GenderSelect ...MaleFemaleDecline to self-identify")

    assert asked == []
    assert result.answer == ""
