"""
LeverAdapter (automation/ats/lever/lever_adapter.py) — tested against a
simplified Lever-style single-page form via page.set_content(). Mirrors
test_greenhouse_adapter.py's fixture pattern, with the one behavioral
difference Lever actually has: a single combined "full name" field instead of
separate first/last inputs.
"""

import pytest

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, ProfileDocument
from automation.ats.lever.lever_adapter import LeverAdapter

LEVER_FORM_HTML = """
<html><body>
<div class="application-form">
  <input name="name" id="name-input">
  <input name="email" id="email-input" type="email">
  <input name="phone" id="phone-input" type="tel">
  <input name="org" id="org-input">
  <input type="file" name="resume">
  <label for="linkedin_q">LinkedIn URL</label>
  <input id="linkedin_q" name="urls[LinkedIn]">
  <button type="submit">Submit Application</button>
</div>
</body></html>
"""


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(
        profile_id="profile-1",
        user_id="user-1",
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        current_company="Analytical Engines Ltd",
        linkedin_url="https://linkedin.com/in/ada",
    )
    defaults.update(overrides)
    profile = CandidateProfile(**defaults)
    profile.phone_encrypted = encrypt_field("+1-555-0100")
    return profile


def _resume_document(path) -> ProfileDocument:
    return ProfileDocument(
        document_id="doc-1",
        profile_id="profile-1",
        document_type="resume",
        original_filename="resume.pdf",
        stored_path=str(path),
        file_hash="abc123",
        is_default=True,
    )


@pytest.fixture
def resume_file(tmp_path):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4 fake resume bytes")
    return path


def test_detect_returns_high_confidence_for_lever_markup(page, resume_file):
    page.set_content(LEVER_FORM_HTML)
    adapter = LeverAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.detect() >= 0.9


def test_detect_returns_zero_for_unrelated_page(page, resume_file):
    page.set_content("<html><body><form><input name='x'></form></body></html>")
    adapter = LeverAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.detect() == 0.0


def test_fill_personal_information_combines_first_and_last_name(page, resume_file):
    page.set_content(LEVER_FORM_HTML)
    adapter = LeverAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    results = adapter.fill_personal_information()

    assert page.locator("#name-input").input_value() == "Ada Lovelace"
    assert page.locator("#email-input").input_value() == "ada@example.com"
    assert page.locator("#phone-input").input_value() == "+1-555-0100"
    assert page.locator("#org-input").input_value() == "Analytical Engines Ltd"

    full_name_result = next(r for r in results if r.field_key == "full_name")
    assert full_name_result.filled is True
    assert full_name_result.value_used == "Ada Lovelace"


def test_fill_personal_information_prefers_explicit_full_name_when_set(page, resume_file):
    page.set_content(LEVER_FORM_HTML)
    profile = _profile(full_name="Augusta Ada King")
    adapter = LeverAdapter(page=page, profile=profile, resume_document=_resume_document(resume_file))

    adapter.fill_personal_information()

    assert page.locator("#name-input").input_value() == "Augusta Ada King"


def test_fill_personal_information_skips_name_when_nothing_available(page, resume_file):
    page.set_content(LEVER_FORM_HTML)
    profile = _profile(first_name=None, last_name=None)
    adapter = LeverAdapter(page=page, profile=profile, resume_document=_resume_document(resume_file))

    results = adapter.fill_personal_information()

    full_name_result = next(r for r in results if r.field_key == "full_name")
    assert full_name_result.filled is False
    assert page.locator("#name-input").input_value() == ""


def test_upload_resume_sets_the_file_input(page, resume_file):
    page.set_content(LEVER_FORM_HTML)
    adapter = LeverAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    assert adapter.upload_resume() is True
    uploaded_count = page.evaluate("document.querySelector('input[type=file]').files.length")
    assert uploaded_count == 1


def test_upload_resume_returns_false_when_no_file_input(page, resume_file):
    page.set_content("<html><body><div class='application-form'></div></body></html>")
    adapter = LeverAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.upload_resume() is False


def test_answer_questions_fills_linkedin_via_label_match(page, resume_file):
    page.set_content(LEVER_FORM_HTML)
    adapter = LeverAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()

    assert page.locator("#linkedin_q").input_value() == "https://linkedin.com/in/ada"
    assert any(r.filled and r.profile_path == "linkedin_url" for r in results)


def test_submit_application_clicks_submit_button(page, resume_file):
    page.set_content(LEVER_FORM_HTML)
    page.evaluate(
        "document.querySelector('.application-form').addEventListener('submit', e => e.preventDefault())"
    )
    adapter = LeverAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    assert adapter.submit_application() is True


def test_submit_application_returns_false_when_no_submit_control(page, resume_file):
    page.set_content("<html><body><div class='application-form'></div></body></html>")
    adapter = LeverAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.submit_application() is False


# ---------- Phase 5: FieldMapper-driven name/placeholder discovery ----------
# (behavior lives on ATSAdapter itself — see automation/ats/base.py and
# automation/tests/test_field_mapper.py; test_greenhouse_adapter.py covers the
# no-double-count guarantee in depth, so only the bare-input case is repeated
# here to confirm it's genuinely adapter-agnostic.)

def test_answer_questions_fills_a_bare_input_via_name_attribute_with_no_label(page, resume_file):
    page.set_content('<html><body><input name="github_url"></body></html>')
    profile = _profile()
    profile.github_url = "https://github.com/ada"
    adapter = LeverAdapter(page=page, profile=profile, resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()

    assert page.locator("input[name='github_url']").input_value() == "https://github.com/ada"
    assert any(r.filled and r.profile_path == "github_url" for r in results)
