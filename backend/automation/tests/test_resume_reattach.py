"""
`ATSAdapter.resume_attachment_state` / `ensure_resume_attached`
(automation/ats/base.py) — the fix for a résumé that uploads successfully and
is then silently dropped by the form.

This is not a hypothetical failure. On a live Greenhouse posting
(`job-boards.greenhouse.io`), the trace shows `set_input_files` succeeding at
t=4.3s with verification passing (`el.files.length == 1`), React logging
"recovered from an error during hydration" at t=4.9s, and the same `#resume`
input reading empty from t=6.3s to the end of the run. The application was
handed over with no résumé while the run log said "resume upload succeeded",
because the only verification ever taken happened two seconds before the form
threw that DOM away.

A DOM-clearing hydration pass is awkward to reproduce faithfully, so these
tests reproduce its EFFECT — the input losing its file after a successful
upload — both by clearing the input's value and by replacing the element
outright, which is what React actually did.
"""

from __future__ import annotations

import pytest

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, ProfileDocument
from automation.ats.greenhouse.greenhouse_adapter import GreenhouseAdapter

# The new Greenhouse "Attach / Dropbox / Google Drive / Enter manually" upload
# widget, reduced to the parts that matter here: a visually-hidden real input
# inside a wrapper whose text is where an S3-style UI would render the filename.
UPLOAD_FORM_HTML = """
<html><body>
<form id="application_form">
  <input id="first_name" name="job_application[first_name]">
  <div class="file-upload">
    <div class="label">Resume/CV<span class="required">*</span></div>
    <div class="file-upload__wrapper" id="wrapper">
      <button type="button">Attach</button>
      <label class="visually-hidden" for="resume">Attach</label>
      <input id="resume" class="visually-hidden" type="file" accept=".pdf,.doc,.docx">
    </div>
  </div>
</form>
</body></html>
"""

NO_UPLOAD_FORM_HTML = """
<html><body><form id="application_form">
  <input id="first_name" name="job_application[first_name]">
</form></body></html>
"""


def _profile() -> CandidateProfile:
    profile = CandidateProfile(
        profile_id="profile-1", user_id="user-1",
        first_name="Ada", last_name="Lovelace", email="ada@example.com",
    )
    profile.phone_encrypted = encrypt_field("+1-555-0100")
    return profile


@pytest.fixture
def resume_file(tmp_path):
    path = tmp_path / "ada-resume.pdf"
    path.write_bytes(b"%PDF-1.4 fake resume bytes")
    return path


@pytest.fixture
def adapter(page, resume_file):
    document = ProfileDocument(
        document_id="doc-1", profile_id="profile-1", document_type="resume",
        original_filename="resume.pdf", stored_path=str(resume_file),
        file_hash="abc123", is_default=True,
    )
    return GreenhouseAdapter(page=page, profile=_profile(), resume_document=document)


# ---------------------------------------------------------------------------
# resume_attachment_state
# ---------------------------------------------------------------------------

def test_state_is_missing_before_anything_is_uploaded(adapter, page):
    page.set_content(UPLOAD_FORM_HTML)
    assert adapter.resume_attachment_state() == "missing"


def test_state_is_attached_after_a_successful_upload(adapter, page):
    page.set_content(UPLOAD_FORM_HTML)
    assert adapter.upload_resume() is True
    assert adapter.resume_attachment_state() == "attached"


def test_state_is_no_field_when_the_page_has_no_upload_input(adapter, page):
    """A later step of a multi-step form isn't a page that lost the résumé —
    conflating the two would make every such step re-attempt an upload."""
    page.set_content(NO_UPLOAD_FORM_HTML)
    assert adapter.resume_attachment_state() == "no_field"


def test_state_is_attached_when_only_the_widget_shows_the_filename(adapter, page, resume_file):
    """Some ATS UIs upload straight to S3 and CLEAR the input, keeping the
    filename only in their own rendered state. Reading `files` alone would call
    that "missing" and re-upload on every check."""
    page.set_content(UPLOAD_FORM_HTML)
    page.evaluate(
        "name => document.getElementById('wrapper').insertAdjacentHTML('beforeend', "
        "'<span>' + name + '</span>')",
        resume_file.name,
    )
    assert adapter.resume_attachment_state() == "attached"


# ---------------------------------------------------------------------------
# ensure_resume_attached
# ---------------------------------------------------------------------------

def test_ensure_returns_true_without_re_uploading_when_still_attached(adapter, page):
    page.set_content(UPLOAD_FORM_HTML)
    adapter.upload_resume()

    uploads = []
    monkeypatch_target = lambda: uploads.append(1) or True  # noqa: E731 - one-line spy
    adapter.upload_resume = monkeypatch_target

    assert adapter.ensure_resume_attached() is True
    assert uploads == []


def test_ensure_returns_none_when_there_is_no_upload_field(adapter, page):
    page.set_content(NO_UPLOAD_FORM_HTML)
    assert adapter.ensure_resume_attached() is None


def test_ensure_re_uploads_a_resume_the_form_cleared(adapter, page):
    page.set_content(UPLOAD_FORM_HTML)
    assert adapter.upload_resume() is True

    # What hydration did, in one line: the file is gone while the run still
    # believes its earlier verification.
    page.evaluate("document.getElementById('resume').value = ''")
    assert adapter.resume_attachment_state() == "missing"

    assert adapter.ensure_resume_attached() is True
    assert adapter.resume_attachment_state() == "attached"


def test_ensure_re_uploads_when_the_input_element_itself_was_replaced(adapter, page):
    """Closer to the real cause: React didn't clear the input, it re-created
    the widget — so the file went with the element that was thrown away."""
    page.set_content(UPLOAD_FORM_HTML)
    assert adapter.upload_resume() is True

    page.evaluate(
        "document.getElementById('wrapper').innerHTML = "
        "'<button type=\"button\">Attach</button>"
        "<input id=\"resume\" class=\"visually-hidden\" type=\"file\">'"
    )
    assert adapter.resume_attachment_state() == "missing"

    assert adapter.ensure_resume_attached() is True
    assert adapter.resume_attachment_state() == "attached"


def test_ensure_gives_up_and_reports_false_when_re_upload_cannot_stick(adapter, page, monkeypatch):
    """A form that drops the file every single time must NOT be reported as
    attached — that's the outcome that submits an application with no résumé."""
    page.set_content(UPLOAD_FORM_HTML)

    def _upload_then_lose_it() -> bool:
        page.evaluate("document.getElementById('resume').value = ''")
        return True

    monkeypatch.setattr(adapter, "upload_resume", _upload_then_lose_it)

    assert adapter.ensure_resume_attached(max_attempts=2) is False
