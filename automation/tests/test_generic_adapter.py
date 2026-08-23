"""
GenericAdapter (automation/ats/generic/generic_adapter.py) — the fallback
that makes "apply to any job portal by link" real. Tested against a
hand-built, deliberately un-Greenhouse-like custom careers page (arbitrary
label text, no ATS-specific markup conventions at all) to prove it reuses
the same platform-agnostic machinery every real adapter is built on, with
no ATS-specific knowledge of its own. See test_greenhouse_adapter.py for the
same fixture/profile conventions this file follows.
"""

import pytest

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, ProfileDocument
from automation.ats.generic.generic_adapter import GenericAdapter

# A totally arbitrary custom careers page — no "job_application[...]" naming
# convention, no #application_form, nothing any specialized adapter would
# recognize. If GenericAdapter can fill this, it can fill an unknown portal.
CUSTOM_FORM_HTML = """
<html><body>
<form>
  <label for="applicant_first">Given name</label>
  <input id="applicant_first">
  <label for="applicant_last">Family name</label>
  <input id="applicant_last">
  <label for="applicant_email">Your email address</label>
  <input id="applicant_email" type="email">
  <label for="applicant_phone">Contact number</label>
  <input id="applicant_phone" type="tel">
  <input type="file" name="cv_upload">
  <label for="linkedin_field">LinkedIn</label>
  <input id="linkedin_field">
  <button type="submit">Send Application</button>
</form>
</body></html>
"""


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(
        profile_id="profile-1",
        user_id="user-1",
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
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


def test_detect_is_always_the_same_low_confidence(page, resume_file):
    """Never wins over a specialized adapter's own detect() — see
    ATSDetector's tiered strategy and test_ats_scaffolding.py's existing
    `test_generic_adapter_is_always_low_confidence_fallback`."""
    page.set_content(CUSTOM_FORM_HTML)
    adapter = GenericAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert 0.0 <= adapter.detect() < 0.5


def test_fill_personal_information_defers_everything_to_answer_questions(page, resume_file):
    """No known selector table exists for an unrecognized page — see the
    adapter's own docstring for why this is deliberately a no-op rather than
    a second, duplicate field sweep."""
    page.set_content(CUSTOM_FORM_HTML)
    adapter = GenericAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.fill_personal_information() == []
    assert page.locator("#applicant_first").input_value() == ""


def test_answer_questions_fills_every_labeled_field_via_the_shared_sweep(page, resume_file):
    page.set_content(CUSTOM_FORM_HTML)
    adapter = GenericAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()

    assert page.locator("#applicant_first").input_value() == "Ada"
    assert page.locator("#applicant_last").input_value() == "Lovelace"
    assert page.locator("#applicant_email").input_value() == "ada@example.com"
    assert page.locator("#applicant_phone").input_value() == "+1-555-0100"
    assert page.locator("#linkedin_field").input_value() == "https://linkedin.com/in/ada"

    filled_paths = {r.profile_path for r in results if r.filled}
    assert {"first_name", "last_name", "email", "phone", "linkedin_url"} <= filled_paths


def test_answer_questions_fills_a_bare_input_with_no_label_at_all(page, resume_file):
    """The exact shape an arbitrary custom portal is most likely to use —
    no <label>, just a name/placeholder — is already fully generic in the
    shared sweep (see ATSAdapter._fill_known_questions)."""
    page.set_content('<html><body><input name="linkedin_url"></body></html>')
    adapter = GenericAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()

    assert page.locator("input[name='linkedin_url']").input_value() == "https://linkedin.com/in/ada"
    assert any(r.filled and r.profile_path == "linkedin_url" for r in results)


def test_answer_questions_returns_nothing_on_a_page_with_no_fields_at_all(page, resume_file):
    """A plain description page with nothing fillable must produce an empty,
    zero-confidence result rather than raise — this is exactly the case
    ApplicationFlowManager relies on to land cleanly at NEEDS_REVIEW instead
    of crashing (see test_application_flow_manager.py's
    TestApplyFromJobLink.test_run_reaches_needs_review_via_the_generic_adapter_when_nothing_is_resolvable)."""
    page.set_content("<html><body><p>Just a plain description page.</p></body></html>")
    adapter = GenericAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.answer_questions() == []


def test_upload_resume_sets_the_file_input_like_every_other_adapter(page, resume_file):
    """Inherited from ATSAdapter, unmodified — confirms GenericAdapter did
    not (re-)stub this out."""
    page.set_content(CUSTOM_FORM_HTML)
    adapter = GenericAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    assert adapter.upload_resume() is True
    uploaded_count = page.evaluate("document.querySelector('input[type=file]').files.length")
    assert uploaded_count == 1


def test_upload_resume_returns_false_when_no_file_input(page, resume_file):
    page.set_content("<html><body><form></form></body></html>")
    adapter = GenericAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.upload_resume() is False


def test_submit_application_clicks_a_send_application_button(page, resume_file):
    """`find_submit_button`'s text candidates include "Send Application" —
    not just Greenhouse's "Submit Application" — proving this isn't secretly
    tied to Greenhouse's own wording."""
    page.set_content(CUSTOM_FORM_HTML)
    page.evaluate("document.querySelector('form').addEventListener('submit', e => e.preventDefault())")
    adapter = GenericAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.submit_application() is True


def test_submit_application_returns_false_when_no_submit_control(page, resume_file):
    page.set_content("<html><body><form></form></body></html>")
    adapter = GenericAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.submit_application() is False


def test_is_final_page_true_with_no_next_control(page, resume_file):
    """No override on GenericAdapter — a page with no "Next"/"Continue"
    control is treated as the last page, exactly like Greenhouse/Lever's
    single-page forms (see ATSAdapter.is_final_page's generic default)."""
    page.set_content(CUSTOM_FORM_HTML)
    adapter = GenericAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.is_final_page() is True
