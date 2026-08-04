"""
Why a live Lever form came back with Gender, Race, Veteran status, Pronouns and
the ethnicity group all blank — and what now fills them.

Source: jobs.lever.co/leverdemo-8/a41e218e-01c6-4334-9849-dff3e0c027f6/apply.
Every option list below is that page's real wording, not invented.

Two independent root causes, tested separately here:

1. **Stored tokens never matched form wording.** `CandidateDemographics` stores
   `non_binary` / `decline_to_answer` / `not_veteran` / `no_disability`, forms
   offer "Non-binary" / "Decline to self-identify" / "I am not a protected
   veteran" / "No, I do not have a disability and have not had one in the past",
   and `option_matching.match_option` correctly refuses every one of those
   pairings. So `_demographic_answer` found a stored value, failed to map it, and
   surfaced the question for a human anyway — the exact outcome that storing the
   answer was meant to prevent. `demographic_matching` bridges the vocabularies
   without loosening the matcher everything else depends on.

2. **Pronouns and education level had no category at all.** Pronouns are now a
   demographic category (never LLM-answered — the only thing a model could infer
   them from is the candidate's name); "highest level of education" is a
   profile-backed factual one that still falls through to the LLM+résumé path
   when the profile is empty, so nothing it used to answer is taken away.
"""

from __future__ import annotations

from app.models.db_models import CandidateDemographics, CandidateProfile
from automation.forms import answer_engine as answer_engine_module
from automation.forms.answer_engine import (
    SOURCE_NEEDS_USER_INPUT,
    ApplicationAnswerEngine,
    Question,
)
from automation.forms.demographic_matching import (
    match_demographic_value,
    match_demographic_values,
    option_candidates,
)
from automation.forms.option_matching import match_option
from automation.forms.question_classifier import (
    CATEGORY_DEMOGRAPHIC_PRONOUNS,
    CATEGORY_DEMOGRAPHIC_RACE_ETHNICITY,
    CATEGORY_HIGHEST_EDUCATION,
    CATEGORY_WILLING_TO_RELOCATE,
    classify_question,
    is_demographic,
)

# --- the real option lists off that page ------------------------------------

LEVER_EEO_GENDER = ("Male", "Female", "Decline to self-identify")
LEVER_EEO_VETERAN = (
    "I identify as one or more of the classifications of a protected veteran",
    "I am not a protected veteran",
    "I don't wish to answer",
)
GREENHOUSE_DISABILITY = (
    "Yes, I have a disability, or have had one in the past",
    "No, I do not have a disability and have not had one in the past",
    "I do not want to answer",
)
LEVER_PRONOUNS = (
    "He/him", "She/her", "They/them", "Xe/xem", "Ze/hir", "Ey/em",
    "Hir/hir", "Fae/faer", "Hu/hu", "Use name only", "Custom",
)
LEVER_ETHNICITY = (
    "White / Caucasian", "Hispanic, Latino, or Spanish origin",
    "Black or African American", "Asian", "Native Hawaiian or other Pacific Islander",
    "Indigenous Peoples, First Nations, Native American, or Alaska Native",
    "Middle Eastern or North African", "Some other race, ethnicity, or origin",
)
LEVER_GENDER_IDENTITY = ("Female", "Male", "Non-binary")
LEVER_EDUCATION = ("High School", "Bachelor", "Masters")


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(profile_id="profile-1", user_id="user-1", skills={})
    defaults.update(overrides)
    return CandidateProfile(**defaults)


def _llm_that_must_not_be_called():
    def fake_llm_fn(*, task, prompt, system=None, **overrides):
        raise AssertionError("the LLM must never be called for this question")
    return fake_llm_fn


def _engine(monkeypatch, *, profile=None, llm_fn=None, **demographics):
    """An engine whose demographics row is whatever `demographics` says, with no
    live DB — same monkeypatch seam as test_answer_engine_phase8.py."""
    row = CandidateDemographics(id="d1", candidate_id="profile-1", **demographics) if demographics else None
    monkeypatch.setattr(answer_engine_module, "get_candidate_demographics", lambda db, profile_id: row)
    return ApplicationAnswerEngine(
        profile=profile or _profile(),
        db=object(),  # never queried — get_candidate_demographics is patched
        llm_fn=llm_fn or _llm_that_must_not_be_called(),
    )


# ---------------------------------------------------------------------------
# 1. The regression itself: stored token vs. the form's own wording
# ---------------------------------------------------------------------------

def test_the_bare_matcher_still_refuses_a_stored_token():
    """Not a bug in `match_option` — it is doing exactly its job. Documented as
    a test so nobody "fixes" it by loosening the matcher every other answer in
    the system flows through."""
    assert match_option("non_binary", LEVER_GENDER_IDENTITY) is None
    assert match_option("decline_to_answer", LEVER_EEO_GENDER) is None
    assert match_option("not_veteran", LEVER_EEO_VETERAN) is None
    assert match_option("no_disability", GREENHOUSE_DISABILITY) is None


def test_stored_tokens_now_resolve_to_the_real_option():
    assert match_demographic_value("non_binary", LEVER_GENDER_IDENTITY) == "Non-binary"
    assert match_demographic_value("decline_to_answer", LEVER_EEO_GENDER) == "Decline to self-identify"
    assert match_demographic_value("not_veteran", LEVER_EEO_VETERAN) == "I am not a protected veteran"
    assert match_demographic_value("veteran", LEVER_EEO_VETERAN) == (
        "I identify as one or more of the classifications of a protected veteran"
    )
    assert match_demographic_value("no_disability", GREENHOUSE_DISABILITY) == (
        "No, I do not have a disability and have not had one in the past"
    )
    assert match_demographic_value("has_disability", GREENHOUSE_DISABILITY) == (
        "Yes, I have a disability, or have had one in the past"
    )


def test_decline_resolves_to_whichever_wording_this_form_uses():
    """One shared list of decline phrasings, tried in order against THIS form's
    options — a gender select and a disability select word it differently and
    both resolve."""
    assert match_demographic_value("decline_to_answer", LEVER_EEO_VETERAN) == "I don't wish to answer"
    assert match_demographic_value("decline_to_answer", GREENHOUSE_DISABILITY) == "I do not want to answer"
    assert match_demographic_value("decline_to_answer", ("Male", "Female", "Prefer not to say")) == "Prefer not to say"


def test_the_long_phrasing_is_tried_before_the_bare_yes_or_no():
    """Order inside each table entry is load-bearing. "No" against the
    disability list is genuinely ambiguous under containment — "no" is also
    inside "I do not want to answer" — so a table that tried it first would
    resolve to nothing at all."""
    assert match_option("No", GREENHOUSE_DISABILITY) is None
    assert match_demographic_value("no_disability", GREENHOUSE_DISABILITY).startswith("No, I do not have")


def test_a_plain_yes_no_form_still_resolves():
    """The long OFCCP phrasings miss, and "Yes"/"No" — deliberately last in each
    entry — is what a two-option form matches on."""
    assert match_demographic_value("veteran", ("Yes", "No")) == "Yes"
    assert match_demographic_value("not_veteran", ("Yes", "No")) == "No"
    assert match_demographic_value("has_disability", ("Yes", "No")) == "Yes"


def test_a_value_stored_in_the_forms_own_words_matches_first():
    """The stored value is always candidate #1, so a user who typed the form's
    wording bypasses the phrasing table entirely."""
    assert option_candidates("Asian")[0] == "Asian"
    assert match_demographic_value("Asian", LEVER_ETHNICITY) == "Asian"
    assert match_demographic_value("He/him", LEVER_PRONOUNS) == "He/him"


def test_pronouns_never_resolve_to_a_different_pronoun_set():
    """"he" is a substring of "She/her". A pronoun table containing bare "he"
    would resolve `he/him` to "She/her" — worse than leaving it blank."""
    assert match_demographic_value("he/him", LEVER_PRONOUNS) == "He/him"
    assert match_demographic_value("he_him", LEVER_PRONOUNS) == "He/him"
    assert match_demographic_value("they/them", LEVER_PRONOUNS) == "They/them"
    assert "he" not in option_candidates("he/him")


def test_an_unmatchable_stored_value_is_still_refused():
    """Widening what can be RECOGNIZED never widens what can be FILLED — a value
    no option corresponds to still comes back `None` so the caller leaves the
    question for a human."""
    assert match_demographic_value("non_binary", ("Male", "Female")) is None
    assert match_demographic_value("Klingon", LEVER_ETHNICITY) is None
    assert match_demographic_value("", LEVER_ETHNICITY) is None
    assert match_demographic_value("Asian", ()) is None


def test_multi_value_matching_keeps_what_matched_and_drops_what_did_not():
    matched = match_demographic_values(["Asian", "White / Caucasian", "Martian"], LEVER_ETHNICITY)
    assert matched == ["Asian", "White / Caucasian"]


def test_multi_value_matching_deduplicates():
    assert match_demographic_values(["Asian", "asian"], LEVER_ETHNICITY) == ["Asian"]


# ---------------------------------------------------------------------------
# 2. New categories
# ---------------------------------------------------------------------------

def test_the_five_questions_that_matched_no_category_now_do():
    assert classify_question("Pronouns") == CATEGORY_DEMOGRAPHIC_PRONOUNS
    assert classify_question(
        "Pronouns Let the employer know what pronouns you use so that they can address you correctly."
    ) == CATEGORY_DEMOGRAPHIC_PRONOUNS
    assert classify_question("I identify my ethnicity as Select all that apply") == CATEGORY_DEMOGRAPHIC_RACE_ETHNICITY
    assert classify_question("What is your highest level of education?") == CATEGORY_HIGHEST_EDUCATION
    assert classify_question("Are you willing to relocate?") == CATEGORY_WILLING_TO_RELOCATE


def test_pronouns_are_a_demographic_category():
    """The one field where an LLM answer would be worst: the only thing it could
    infer pronouns from is the candidate's name."""
    assert is_demographic(CATEGORY_DEMOGRAPHIC_PRONOUNS) is True


def test_gender_pronouns_is_classified_as_pronouns_not_gender():
    """Both bare phrases legitimately match this label; PRONOUNS is checked
    first for exactly that reason."""
    assert classify_question("Gender pronouns") == CATEGORY_DEMOGRAPHIC_PRONOUNS


def test_relocation_assistance_is_not_a_willingness_question():
    """A question about money, not willingness — answering it from
    `willing_to_relocate` would be confidently wrong."""
    assert classify_question("Do you require relocation assistance?") is None


def test_an_education_start_date_year_is_not_an_education_level_question():
    """Greenhouse's Education block has its own date fields (see
    test_resume_context.py) — "level of education" must not claim them."""
    assert classify_question("Start date year") is None
    assert classify_question("Education end date year") is None


# ---------------------------------------------------------------------------
# 3. The engine, end to end
# ---------------------------------------------------------------------------

def test_gender_select_is_answered_from_the_stored_token(monkeypatch):
    engine = _engine(monkeypatch, gender="non_binary")

    result = engine.answer("What gender do you identify as?", LEVER_GENDER_IDENTITY)

    assert result.source == "deterministic"
    assert result.answer == "Non-binary"


def test_veteran_status_is_answered_from_the_stored_token(monkeypatch):
    engine = _engine(monkeypatch, veteran_status="not_veteran")

    result = engine.answer("Veteran status", LEVER_EEO_VETERAN)

    assert result.answer == "I am not a protected veteran"


def test_pronouns_are_answered_from_the_stored_value(monkeypatch):
    engine = _engine(monkeypatch, pronouns="they/them")

    result = engine.answer("What are your pronouns?", LEVER_PRONOUNS)

    assert result.source == "deterministic"
    assert result.answer == "They/them"


def test_pronouns_with_nothing_stored_ask_a_human_rather_than_the_llm(monkeypatch):
    engine = _engine(monkeypatch, gender="female")  # a row exists, but no pronouns on it

    result = engine.answer("What are your pronouns?", LEVER_PRONOUNS)

    assert result.source == SOURCE_NEEDS_USER_INPUT
    assert result.answer == ""
    assert result.available_options == LEVER_PRONOUNS


def test_pronouns_never_reach_the_batched_llm_call(monkeypatch):
    calls = []

    def recording_llm_fn(*, task, prompt, system=None, **overrides):
        import json
        calls.append(json.loads(prompt)["questions"])
        return '{"answers": [{"answer": "Because I admire the team.", "confidence": 0.9}]}'

    engine = _engine(monkeypatch, llm_fn=recording_llm_fn, gender="female")
    results = engine.answer_batch([
        Question("What are your pronouns?", LEVER_PRONOUNS),
        "Why do you want to work here?",
    ])

    assert calls == [["Why do you want to work here?"]]
    assert results[0].source == SOURCE_NEEDS_USER_INPUT


def test_highest_education_level_is_answered_from_the_profile(monkeypatch):
    engine = _engine(monkeypatch, profile=_profile(highest_education_level="Bachelor"))

    result = engine.answer("What is your highest level of education?", LEVER_EDUCATION)

    assert result.source == "deterministic"
    assert result.answer == "Bachelor"


def test_an_empty_education_level_still_falls_through_to_the_llm(monkeypatch):
    """The résumé path already answers this question (see test_resume_context.py)
    and must keep doing so — the new column makes the common case free, it does
    not replace the fallback."""
    def llm_fn(*, task, prompt, system=None, **overrides):
        return '{"answers": [{"answer": "Bachelor", "confidence": 0.95}]}'

    engine = _engine(monkeypatch, profile=_profile(highest_education_level=None), llm_fn=llm_fn)

    result = engine.answer("What is your highest level of education?", LEVER_EDUCATION)

    assert result.source == "llm"
    assert result.answer == "Bachelor"


def test_willing_to_relocate_answers_yes_or_no(monkeypatch):
    yes = _engine(monkeypatch, profile=_profile(willing_to_relocate=True))
    no = _engine(monkeypatch, profile=_profile(willing_to_relocate=False))

    assert yes.answer("Are you willing to relocate?", ("Yes", "No")).answer == "Yes"
    assert no.answer("Are you willing to relocate?", ("Yes", "No")).answer == "No"


# ---------------------------------------------------------------------------
# 4. stored_choices — the multi-select path, deterministic by construction
# ---------------------------------------------------------------------------

def test_stored_choices_returns_every_matching_ethnicity(monkeypatch):
    engine = _engine(monkeypatch, ethnicities=["Asian", "White / Caucasian"])

    chosen = engine.stored_choices(
        Question("I identify my ethnicity as Select all that apply", LEVER_ETHNICITY)
    )

    assert chosen == ["Asian", "White / Caucasian"]


def test_stored_choices_falls_back_to_the_single_value_column(monkeypatch):
    """A candidate who only ever answered a pick-one race question still has
    something to say to a select-all-that-apply one."""
    engine = _engine(monkeypatch, race_ethnicity="Asian")

    chosen = engine.stored_choices(
        Question("I identify my ethnicity as Select all that apply", LEVER_ETHNICITY)
    )

    assert chosen == ["Asian"]


def test_stored_choices_returns_one_option_for_a_single_value_category(monkeypatch):
    engine = _engine(monkeypatch, pronouns="she/her")

    assert engine.stored_choices(Question("Pronouns", LEVER_PRONOUNS)) == ["She/her"]


def test_stored_choices_is_empty_when_nothing_is_stored(monkeypatch):
    engine = _engine(monkeypatch)

    assert engine.stored_choices(Question("Pronouns", LEVER_PRONOUNS)) == []
    assert engine.stored_choices(
        Question("I identify my ethnicity as Select all that apply", LEVER_ETHNICITY)
    ) == []


def test_stored_choices_never_calls_the_llm_even_for_a_novel_question(monkeypatch):
    """Stricter than `answer_batch` on purpose: a wrongly-ticked checkbox is
    indistinguishable from a deliberately-ticked one on a review screen."""
    engine = _engine(monkeypatch, pronouns="she/her")

    assert engine.stored_choices(Question("Which of these excite you?", ("Rust", "Go"))) == []


def test_stored_choices_needs_options_to_choose_among(monkeypatch):
    engine = _engine(monkeypatch, pronouns="she/her")

    assert engine.stored_choices("Pronouns") == []
