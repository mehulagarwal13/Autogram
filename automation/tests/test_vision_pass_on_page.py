"""
`ATSAdapter.fill_unfilled_fields_with_vision` (automation/ats/base.py) — the
adapter half of the vision fallback, against a real rendered page.

`test_vision_fallback.py` covers the prompt/validation half with no browser.
What needs a real page is everything this half does: finding the fields that
are still empty, recovering the question each one is asking, cropping a
screenshot that actually contains the surrounding form, and filling what comes
back through the normal `fill_field()` pipeline so the value is verified
against the live DOM.

The form below is modeled on the real Greenhouse posting that motivated the
pass — a "government official?" question answered "No", followed by two
REQUIRED follow-up boxes that only make sense if the answer had been "Yes".
Every other pass leaves those two empty (the text engine correctly refuses to
invent an answer from the field's own words), which is what sends the whole run
to a human.
"""

from __future__ import annotations

import pytest

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, ProfileDocument
from automation.ats.greenhouse.greenhouse_adapter import GreenhouseAdapter
from automation.forms.vision_fallback import NOT_APPLICABLE, VisionAnswer

FORM_HTML = """
<html><body style="font-family: sans-serif">
<form id="application_form">
  <label for="official">Are/were you or anyone in your immediate family a government official?*</label>
  <select id="official" required><option value="No" selected>No</option><option>Yes</option></select>

  <label for="role">If yes to the above question, what role and what governmental organization?*</label>
  <input id="role" type="text" required>

  <label for="onsite">Are you willing to work onsite in Bangalore?*</label>
  <select id="onsite" required><option value="">Select...</option><option>Yes</option><option>No</option></select>

  <label for="optional_note">Anything else? (optional)</label>
  <input id="optional_note" type="text">
</form>
</body></html>
"""


def _profile() -> CandidateProfile:
    profile = CandidateProfile(
        profile_id="profile-1", user_id="user-1",
        first_name="Ada", last_name="Lovelace", email="ada@example.com",
    )
    profile.phone_encrypted = encrypt_field("+1-555-0100")
    return profile


class _StubAnswerer:
    """Stands in for `VisionFormAnswerer`. Records the `VisionField`s it was
    handed (so the crop/label work can be asserted on) and replays answers by
    the field's DOM name."""

    def __init__(self, answers: dict[str, VisionAnswer]):
        self._answers = answers
        self.seen: list = []

    def answer(self, fields):
        self.seen = list(fields)
        return [
            self._answers.get(
                item.name,
                VisionAnswer(name=item.name, question=item.question, answer="", confidence=0.0, reason="stub decline"),
            )
            for item in fields
        ]


@pytest.fixture
def adapter(page, tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    document = ProfileDocument(
        document_id="doc-1", profile_id="profile-1", document_type="resume",
        original_filename="resume.pdf", stored_path=str(resume), file_hash="abc", is_default=True,
    )
    page.set_content(FORM_HTML)
    return GreenhouseAdapter(page=page, profile=_profile(), resume_document=document)


def _answer(name: str, question: str, value: str, confidence: float = 0.95, **kwargs) -> VisionAnswer:
    return VisionAnswer(name=name, question=question, answer=value, confidence=confidence, **kwargs)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def test_collects_only_the_required_fields_that_are_still_empty(adapter):
    collected = adapter.collect_unfilled_fields_for_vision()

    names = [item.name for item, _field in collected]
    # `official` is required but already answered; `optional_note` is empty but
    # not required. Neither is this pass's business.
    assert names == ["role", "onsite"]


def test_each_collected_field_carries_its_question_and_a_real_png(adapter):
    collected = adapter.collect_unfilled_fields_for_vision()
    role = next(item for item, _f in collected if item.name == "role")

    assert role.question == "If yes to the above question, what role and what governmental organization?"
    # A real PNG, not an empty capture — the crop is the entire point of the pass.
    assert role.screenshot.startswith(b"\x89PNG")
    assert len(role.screenshot) > 1000


def test_a_dropdowns_real_options_are_read_and_passed_along(adapter):
    collected = adapter.collect_unfilled_fields_for_vision()
    onsite = next(item for item, _f in collected if item.name == "onsite")

    # The placeholder is excluded; the model can only be offered real choices.
    assert onsite.options == ("Yes", "No")
    assert onsite.widget == "select"


# ---------------------------------------------------------------------------
# Filling
# ---------------------------------------------------------------------------

def test_fills_the_conditional_follow_up_the_other_passes_left_empty(adapter, page):
    answerer = _StubAnswerer({
        "role": _answer("role", "If yes...", NOT_APPLICABLE, reason="above answered No"),
        "onsite": _answer("onsite", "Willing to work onsite?", "Yes"),
    })

    outcome = adapter.fill_unfilled_fields_with_vision(answerer)

    assert page.input_value("#role") == NOT_APPLICABLE
    assert page.input_value("#onsite") == "Yes"
    assert all(r.filled for r in outcome.results)
    assert [r.profile_path for r in outcome.results] == ["vision", "vision"]


def test_a_low_confidence_answer_is_left_for_a_human(adapter, page):
    answerer = _StubAnswerer({
        "role": _answer("role", "If yes...", "Something guessed", confidence=0.5),
    })

    outcome = adapter.fill_unfilled_fields_with_vision(answerer)

    assert page.input_value("#role") == ""
    role_result = next(r for r in outcome.results if "what role" in r.field_key)
    assert role_result.filled is False
    assert role_result.value_used is None


def test_already_filled_fields_are_reported_and_not_typed_over(adapter, page):
    """The react-select / country-picker case: the scan sees an empty control,
    the screenshot shows an answer. Nothing may be typed over it."""
    answerer = _StubAnswerer({
        "onsite": VisionAnswer(
            name="onsite", question="Willing to work onsite?", answer="", confidence=0.0,
            already_filled=True, reason="screenshot shows Yes",
        ),
    })

    outcome = adapter.fill_unfilled_fields_with_vision(answerer)

    assert outcome.confirmed_already_filled == ["onsite"]
    assert page.input_value("#onsite") == ""      # untouched, not "corrected"
    assert all(r.field_key != "onsite" for r in outcome.results)


def test_a_form_with_nothing_left_to_fill_makes_no_call(adapter, page):
    page.fill("#role", "N/A")
    page.select_option("#onsite", "Yes")
    answerer = _StubAnswerer({})

    outcome = adapter.fill_unfilled_fields_with_vision(answerer)

    assert outcome.results == []
    assert answerer.seen == []


def test_an_answerer_that_raises_never_breaks_the_run(adapter):
    class _Exploding:
        def answer(self, fields):
            raise RuntimeError("vision provider down")

    outcome = adapter.fill_unfilled_fields_with_vision(_Exploding())

    assert outcome.results == []
    assert outcome.confirmed_already_filled == []


def test_crops_are_saved_next_to_the_runs_other_artifacts(adapter, tmp_path):
    debug_dir = tmp_path / "logs" / "app-1"
    debug_dir.mkdir(parents=True)

    adapter.fill_unfilled_fields_with_vision(_StubAnswerer({}), debug_dir=debug_dir)

    saved = sorted(p.name for p in debug_dir.glob("vision-field-*.png"))
    assert saved == ["vision-field-1.png", "vision-field-2.png"]
