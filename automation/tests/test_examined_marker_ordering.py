"""
The mark-before-describe bug, pinned at all three call sites.

`_AUTOMATION_EXAMINED_ATTR` appears in `_MAPPABLE_FIELD_SELECTOR_TEMPLATE`, and
`Locator.all()` does not snapshot elements — it returns `nth=i` re-queries. So
writing the examined marker mid-loop used to (a) make the locator being held
resolve to ZERO, stalling the next `describe_field()` for Playwright's full 30s
timeout and reporting a present, fillable field as unfillable, and (b) shift
every later index as the match set shrank, silently skipping fields.

Every test here uses the REAL selector template so the exclusion behaviour is
actually exercised. The core assertion is the one the bug violated: at the
moment `describe_field()` is called, its locator must still resolve to exactly
one element.
"""

from __future__ import annotations

import pytest

from automation.ats.base import (
    _AUTOMATION_CANDIDATE_ATTR,
    _AUTOMATION_EXAMINED_ATTR,
    _MAPPABLE_FIELD_SELECTOR_TEMPLATE,
)
from automation.ats.greenhouse.greenhouse_adapter import GreenhouseAdapter
from automation.forms import field_handlers
from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, ProfileDocument

SELECTOR = _MAPPABLE_FIELD_SELECTOR_TEMPLATE.format(marker=_AUTOMATION_EXAMINED_ATTR)


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(
        profile_id="profile-1", user_id="user-1",
        first_name="Ada", last_name="Lovelace", email="ada@example.com",
        linkedin_url="https://linkedin.com/in/ada",
        github_url="https://github.com/ada",
    )
    defaults.update(overrides)
    profile = CandidateProfile(**defaults)
    profile.phone_encrypted = encrypt_field("+1-555-0100")
    return profile


@pytest.fixture
def resume_file(tmp_path):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4 fake resume bytes")
    return path


def _resume_document(path) -> ProfileDocument:
    return ProfileDocument(
        document_id="doc-1", profile_id="profile-1", document_type="resume",
        original_filename="resume.pdf", stored_path=str(path), file_hash="abc123", is_default=True,
    )


def _adapter(page, resume_file, **kw):
    return GreenhouseAdapter(
        page=page, profile=_profile(), resume_document=_resume_document(resume_file), **kw
    )


@pytest.fixture
def describe_spy(monkeypatch):
    """Records `locator.count()` at the instant `describe_field` is entered —
    the exact measurement the bug failed. A count of 0 means the locator was
    invalidated by an early mark and the real code would now hang for 30s."""
    counts = []
    real = field_handlers.describe_field

    def spy(locator, **kwargs):
        try:
            counts.append(locator.count())
        except Exception:
            counts.append(-1)
        return real(locator, **kwargs)

    # Patch where each module looked it up, not just the definition site.
    monkeypatch.setattr(field_handlers, "describe_field", spy)
    monkeypatch.setattr("automation.ats.base.describe_field", spy)
    return counts


# ---------------------------------------------------------------------------
# Site 1 — _fill_questions_by_name_or_placeholder
# ---------------------------------------------------------------------------

def test_name_placeholder_pass_locator_still_resolves_at_describe_time(page, resume_file, describe_spy):
    page.set_content('<html><body><input name="linkedin_url"></body></html>')
    adapter = _adapter(page, resume_file)

    adapter._fill_questions_by_name_or_placeholder()

    assert describe_spy, "describe_field was never reached"
    assert all(c == 1 for c in describe_spy), f"locator resolved to {describe_spy} at describe time"


def test_name_placeholder_pass_actually_fills_the_bare_input(page, resume_file):
    page.set_content('<html><body><input name="linkedin_url"></body></html>')
    adapter = _adapter(page, resume_file)

    results = adapter._fill_questions_by_name_or_placeholder()

    assert page.locator("input[name='linkedin_url']").input_value() == "https://linkedin.com/in/ada"
    assert any(r.filled and r.profile_path == "linkedin_url" for r in results)


def test_the_field_is_marked_examined_after_the_fill(page, resume_file):
    """Marking must still happen — it's what stops a later pass re-counting the
    same field. Only the timing changed."""
    page.set_content('<html><body><input name="linkedin_url"></body></html>')
    adapter = _adapter(page, resume_file)

    adapter._fill_questions_by_name_or_placeholder()

    assert page.locator(f"input[{_AUTOMATION_EXAMINED_ATTR}]").count() == 1
    assert page.locator(SELECTOR).count() == 0  # excluded from any later pass


def test_no_field_is_skipped_when_several_candidates_are_marked_in_sequence(page, resume_file):
    """The index-shift half of the bug: with [e0, e1, e2], marking e0 made
    `nth=1` resolve to e2, so e1 was never examined at all."""
    page.set_content(
        '<html><body>'
        '<input name="linkedin_url">'
        '<input name="github_url">'
        '<input name="email">'
        '</body></html>'
    )
    adapter = _adapter(page, resume_file)

    adapter._fill_questions_by_name_or_placeholder()

    assert page.locator("input[name='linkedin_url']").input_value() == "https://linkedin.com/in/ada"
    assert page.locator("input[name='github_url']").input_value() == "https://github.com/ada"
    assert page.locator("input[name='email']").input_value() == "ada@example.com"


# ---------------------------------------------------------------------------
# Site 2 — _fill_questions_by_nearby_text
# ---------------------------------------------------------------------------

_LEVER_SHAPED = """
<html><body>
<li class="application-question custom-question">
  <div>Are you able to work in person from our Mountain View office?</div>
  <div class="application-field"><input type="text" name="cards[abc][field0]" placeholder="Type your response"></div>
</li>
</body></html>
"""


def test_nearby_text_pass_leaves_queued_fields_unmarked_so_describe_still_resolves(page, resume_file):
    """A queued field is described later, in `_fill_questions_via_answer_engine`
    — so the collecting pass must not mark it."""
    page.set_content(_LEVER_SHAPED)
    adapter = _adapter(page, resume_file)

    adapter._fill_questions_by_nearby_text()

    assert len(adapter._pending_answer_engine_questions) == 1
    _question, locator = adapter._pending_answer_engine_questions[0]
    assert locator.count() == 1, "queued locator was invalidated before describe_field could run"


def test_nearby_text_pass_stamps_a_stable_id_not_a_marker_dependent_locator(page, resume_file):
    page.set_content(_LEVER_SHAPED)
    adapter = _adapter(page, resume_file)

    adapter._fill_questions_by_nearby_text()

    _question, locator = adapter._pending_answer_engine_questions[0]
    assert _AUTOMATION_CANDIDATE_ATTR in repr(locator)
    # Marking must not invalidate it.
    adapter._mark_examined(locator)
    assert locator.count() == 1


# ---------------------------------------------------------------------------
# Site 3 — _fill_questions_via_answer_engine
# ---------------------------------------------------------------------------

class _Engine:
    def answer_batch(self, questions):
        return [
            type("R", (), {
                "question": str(q), "answer": "Yes", "source": "llm",
                "confidence": 0.95, "available_options": getattr(q, "options", ()),
            })()
            for q in questions
        ]


def test_answer_engine_pass_describes_before_marking(page, resume_file, describe_spy):
    page.set_content(_LEVER_SHAPED)
    adapter = _adapter(page, resume_file, answer_engine=_Engine())

    adapter._fill_questions_by_nearby_text()
    adapter._fill_questions_via_answer_engine()

    assert describe_spy, "describe_field was never reached"
    assert all(c == 1 for c in describe_spy), f"locator resolved to {describe_spy} at describe time"


def test_answer_engine_pass_fills_the_unlabeled_question_and_then_marks_it(page, resume_file):
    page.set_content(_LEVER_SHAPED)
    adapter = _adapter(page, resume_file, answer_engine=_Engine())

    adapter._fill_questions_by_nearby_text()
    results = adapter._fill_questions_via_answer_engine()

    assert page.locator("input[name='cards[abc][field0]']").input_value() == "Yes"
    assert any(r.filled for r in results)
    assert page.locator(f"input[{_AUTOMATION_EXAMINED_ATTR}]").count() == 1


# ---------------------------------------------------------------------------
# The underlying Playwright behaviour the old comment got wrong
# ---------------------------------------------------------------------------

def test_locator_all_does_not_snapshot_and_marking_invalidates_it(page):
    """Documents WHY `_stamp_candidates` exists. If a future Playwright makes
    `.all()` snapshot, this test fails and the workaround can be reconsidered."""
    page.set_content('<html><body><input name="linkedin_url"></body></html>')

    held = page.locator(SELECTOR).all()[0]
    assert held.count() == 1
    assert "nth=0" in repr(held)  # an index re-query, not a snapshot

    held.evaluate(f"el => el.setAttribute('{_AUTOMATION_EXAMINED_ATTR}', '1')")

    assert held.count() == 0  # same locator, now matches nothing
    assert page.locator("input[name='linkedin_url']").count() == 1  # element is still there


def test_stamped_candidates_survive_marking(page, resume_file):
    page.set_content(
        '<html><body><input name="linkedin_url"><input name="github_url"></body></html>'
    )
    adapter = _adapter(page, resume_file)

    candidates = adapter._stamp_candidates(SELECTOR)
    assert len(candidates) == 2

    for locator in candidates:
        adapter._mark_examined(locator)
    for locator in candidates:
        assert locator.count() == 1
