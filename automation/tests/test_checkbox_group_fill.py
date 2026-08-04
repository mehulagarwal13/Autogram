"""
Checkbox GROUPS, and the marketing opt-in — the two shapes on a live Lever form
that no fill pass could structurally reach.

Source: jobs.leverdemo-8 .../apply. The HTML below is that page's shape:
"Pronouns" (11 checkboxes), "I identify my ethnicity as / Select all that apply"
(8 checkboxes), and a "Yes, <company> can contact me about future job
opportunities" opt-in. All three came out empty on a run where every text field,
dropdown and radio group filled correctly.

Why they were unreachable, pass by pass:

- `_fill_questions_by_label` sees each member's own `<label>` — "He/him" — which
  is an OPTION, not a question, and matches no `FieldMapper` synonym.
- `_collect_for_answer_engine` then drops the field: `_NON_FILLABLE_INPUT_TYPES`
  excludes checkboxes.
- `_fill_questions_by_name_or_placeholder`'s selector excludes them too.
- `_fill_consent_checkboxes` only looks at REQUIRED legal-consent text.

`_fill_checkbox_groups` recovers the question its members collectively ask and
answers it from stored data only (`ApplicationAnswerEngine.stored_choices`,
which never calls the LLM). `_fill_opt_in_checkboxes` acts on
`profile.marketing_opt_in`, and only on an explicit `True`.
"""

from __future__ import annotations

import pytest

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateDemographics, CandidateProfile, ProfileDocument
from automation.ats.lever.lever_adapter import LeverAdapter
from automation.forms import answer_engine as answer_engine_module
from automation.forms.answer_engine import ApplicationAnswerEngine

PRONOUN_OPTIONS = (
    "He/him", "She/her", "They/them", "Xe/xem", "Ze/hir", "Ey/em",
    "Hir/hir", "Fae/faer", "Hu/hu",
)
ETHNICITY_OPTIONS = (
    "White / Caucasian", "Hispanic, Latino, or Spanish origin",
    "Black or African American", "Asian", "Native Hawaiian or other Pacific Islander",
    "Indigenous Peoples, First Nations, Native American, or Alaska Native",
    "Middle Eastern or North African", "Some other race, ethnicity, or origin",
)


def _checkboxes(name_prefix: str, options) -> str:
    return "".join(
        f'<li><label><input type="checkbox" name="{name_prefix}[]" '
        f'value="{option}">{option}</label></li>'
        for option in options
    )


PRONOUN_GROUP_HTML = f"""
<form>
  <li class="application-question">
    <label class="application-label">Pronouns</label>
    <div class="application-field">
      <ul class="alternatives">{_checkboxes("cards[abc][pronouns]", PRONOUN_OPTIONS)}</ul>
      <div class="description">Let the employer know what pronouns you use so that they can address you correctly.</div>
    </div>
  </li>
</form>
"""

ETHNICITY_GROUP_HTML = f"""
<form>
  <div class="application-question">
    <div class="text">I identify my ethnicity as</div>
    <div class="description">Select all that apply</div>
    <ul class="alternatives">{_checkboxes("eeo[ethnicity]", ETHNICITY_OPTIONS)}</ul>
  </div>
</form>
"""

OPT_IN_HTML = """
<form>
  <label id="consent">
    <input type="checkbox" name="consentMarketing">
    Yes, Lever Implementation Training Environment can contact me about future job opportunities for up to 1 year
  </label>
</form>
"""

#: Two REQUIRED consent boxes under one heading are technically a two-member
#: checkbox "group" — the regression guard for `_fill_checkbox_groups` marking
#: fields it couldn't answer, which would make `_fill_consent_checkboxes` skip
#: them and silently block submission.
CONSENT_PAIR_HTML = """
<form>
  <div class="legal">
    <p>Before you submit</p>
    <label><input type="checkbox" required>I agree to the Privacy Policy*</label>
    <label><input type="checkbox" required>I accept the terms and conditions*</label>
  </div>
</form>
"""


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(
        profile_id="profile-1", user_id="user-1",
        first_name="Ada", last_name="Lovelace", email="ada@example.com",
        skills={},
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


def _adapter(page, resume_file, *, profile=None, engine=None) -> LeverAdapter:
    return LeverAdapter(
        page=page,
        profile=profile or _profile(),
        resume_document=ProfileDocument(
            document_id="doc-1", profile_id="profile-1", document_type="resume",
            original_filename="resume.pdf", stored_path=str(resume_file),
            file_hash="abc", is_default=True,
        ),
        answer_engine=engine,
    )


def _engine(monkeypatch, *, profile=None, **demographics) -> ApplicationAnswerEngine:
    row = CandidateDemographics(id="d1", candidate_id="profile-1", **demographics) if demographics else None
    monkeypatch.setattr(answer_engine_module, "get_candidate_demographics", lambda db, profile_id: row)

    def llm_fn(*, task, prompt, system=None, **overrides):
        raise AssertionError("a checkbox group must never be answered by the LLM")

    return ApplicationAnswerEngine(profile=profile or _profile(), db=object(), llm_fn=llm_fn)


def _checked_labels(page) -> list[str]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('input[type=checkbox]'))
                     .filter(el => el.checked)
                     .map(el => el.value || (el.closest('label') || {}).textContent.trim())"""
    )


# ---------------------------------------------------------------------------
# Pronouns — a single-answer demographic category rendered as checkboxes
# ---------------------------------------------------------------------------

def test_the_stored_pronoun_is_the_only_box_ticked(page, resume_file, monkeypatch):
    page.set_content(PRONOUN_GROUP_HTML)
    adapter = _adapter(page, resume_file, engine=_engine(monkeypatch, pronouns="they/them"))

    results = adapter._fill_checkbox_groups()

    assert _checked_labels(page) == ["They/them"]
    assert [r.filled for r in results] == [True]
    assert results[0].profile_path == "checkbox_group"
    assert results[0].value_used == "They/them"


def test_a_pronoun_group_with_nothing_stored_is_left_untouched(page, resume_file, monkeypatch):
    page.set_content(PRONOUN_GROUP_HTML)
    adapter = _adapter(page, resume_file, engine=_engine(monkeypatch, gender="female"))

    assert adapter._fill_checkbox_groups() == []
    assert _checked_labels(page) == []


def test_a_stored_token_spelling_still_resolves(page, resume_file, monkeypatch):
    """`he_him` is the snake_case spelling a user following the gender/veteran
    column convention would store — see `demographic_matching`."""
    page.set_content(PRONOUN_GROUP_HTML)
    adapter = _adapter(page, resume_file, engine=_engine(monkeypatch, pronouns="he_him"))

    adapter._fill_checkbox_groups()

    assert _checked_labels(page) == ["He/him"]


# ---------------------------------------------------------------------------
# Ethnicity — "select all that apply", where more than one answer is correct
# ---------------------------------------------------------------------------

def test_every_stored_ethnicity_is_ticked(page, resume_file, monkeypatch):
    page.set_content(ETHNICITY_GROUP_HTML)
    engine = _engine(monkeypatch, ethnicities=["Asian", "White / Caucasian"])
    adapter = _adapter(page, resume_file, engine=engine)

    results = adapter._fill_checkbox_groups()

    assert sorted(_checked_labels(page)) == ["Asian", "White / Caucasian"]
    assert all(r.filled for r in results)
    assert len(results) == 2


def test_an_ethnicity_this_form_has_no_option_for_is_dropped_not_fatal(page, resume_file, monkeypatch):
    """One unmatched entry must not throw away the ones that did match."""
    page.set_content(ETHNICITY_GROUP_HTML)
    engine = _engine(monkeypatch, ethnicities=["Asian", "Martian"])
    adapter = _adapter(page, resume_file, engine=engine)

    adapter._fill_checkbox_groups()

    assert _checked_labels(page) == ["Asian"]


# ---------------------------------------------------------------------------
# Safety properties
# ---------------------------------------------------------------------------

def test_no_engine_means_no_checkbox_group_pass_at_all(page, resume_file):
    """Same way every other Phase 6+ pass degrades: the engine is what holds the
    DB session the demographics row is read through."""
    page.set_content(PRONOUN_GROUP_HTML)
    adapter = _adapter(page, resume_file, engine=None)

    assert adapter._fill_checkbox_groups() == []
    assert _checked_labels(page) == []


def test_required_consent_boxes_are_not_eaten_by_the_group_pass(page, resume_file, monkeypatch):
    """The group pass sees two checkboxes under one heading, can't answer it, and
    must leave them UNMARKED so the consent pass still reaches them."""
    page.set_content(CONSENT_PAIR_HTML)
    adapter = _adapter(page, resume_file, engine=_engine(monkeypatch, pronouns="she/her"))

    assert adapter._fill_checkbox_groups() == []
    consent_results = adapter._fill_consent_checkboxes()

    assert len(consent_results) == 2
    assert all(r.filled for r in consent_results)
    assert len(_checked_labels(page)) == 2


def test_the_group_pass_does_not_touch_a_lone_checkbox(page, resume_file, monkeypatch):
    """A group needs 2+ members. A single checkbox is the consent/opt-in path's
    business, not this one's."""
    page.set_content(OPT_IN_HTML)
    adapter = _adapter(page, resume_file, engine=_engine(monkeypatch, pronouns="she/her"))

    assert adapter._fill_checkbox_groups() == []
    assert _checked_labels(page) == []


# ---------------------------------------------------------------------------
# The marketing opt-in — acted on only for an explicit True
# ---------------------------------------------------------------------------

def test_the_opt_in_is_ticked_when_the_candidate_opted_in(page, resume_file):
    page.set_content(OPT_IN_HTML)
    adapter = _adapter(page, resume_file, profile=_profile(marketing_opt_in=True))

    results = adapter._fill_opt_in_checkboxes()

    assert [r.filled for r in results] == [True]
    assert results[0].profile_path == "marketing_opt_in"
    assert page.locator("input[name=consentMarketing]").is_checked() is True


@pytest.mark.parametrize("stored", [None, False])
def test_an_unanswered_or_declined_opt_in_is_left_alone(page, resume_file, stored):
    """`None` means the user was never asked. Reading that as consent is how an
    automation subscribes someone to a mailing list they never agreed to."""
    page.set_content(OPT_IN_HTML)
    adapter = _adapter(page, resume_file, profile=_profile(marketing_opt_in=stored))

    assert adapter._fill_opt_in_checkboxes() == []
    assert page.locator("input[name=consentMarketing]").is_checked() is False


def test_the_opt_in_pass_ignores_a_required_legal_consent_box(page, resume_file):
    """"I agree to the Privacy Policy" is not marketing prose — it stays with
    `_fill_consent_checkboxes`, which gates on required-ness."""
    page.set_content(CONSENT_PAIR_HTML)
    adapter = _adapter(page, resume_file, profile=_profile(marketing_opt_in=True))

    assert adapter._fill_opt_in_checkboxes() == []
    assert _checked_labels(page) == []
