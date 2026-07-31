"""
GreenhouseAdapter (automation/ats/greenhouse/greenhouse_adapter.py) — tested
against a simplified but realistic Greenhouse-style form rendered via
page.set_content(). Uses real CandidateProfile/ProfileDocument ORM instances
built in-memory (no database needed — automation depends on app.models.db_models
directly now, see automation/interfaces.py).
"""

import pytest

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, ProfileDocument
from automation.ats.base import ANSWER_REVIEW_CONFIDENCE_THRESHOLD
from automation.ats.greenhouse.greenhouse_adapter import GreenhouseAdapter
from automation.forms.answer_engine import AnswerResult

GREENHOUSE_FORM_HTML = """
<html><body>
<form id="application_form">
  <input id="first_name" name="job_application[first_name]">
  <input id="last_name" name="job_application[last_name]">
  <input id="email" type="email" name="job_application[email]">
  <input id="phone" type="tel" name="job_application[phone]">
  <input id="company" name="job_application[company]">
  <input id="title" name="job_application[title]">
  <input type="file" name="job_application[resume]">
  <label for="linkedin_q">LinkedIn Profile</label>
  <input id="linkedin_q" name="job_application[question_1]">
  <button type="submit">Submit Application</button>
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
        current_company="Analytical Engines Ltd",
        current_role="Backend Engineer",
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


def test_detect_returns_high_confidence_for_greenhouse_markup(page, resume_file):
    page.set_content(GREENHOUSE_FORM_HTML)
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.detect() >= 0.9


def test_detect_returns_zero_for_unrelated_page(page, resume_file):
    page.set_content("<html><body><form><input name='x'></form></body></html>")
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.detect() == 0.0


def test_fill_personal_information_fills_known_fields(page, resume_file):
    page.set_content(GREENHOUSE_FORM_HTML)
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    results = adapter.fill_personal_information()

    assert page.locator("#first_name").input_value() == "Ada"
    assert page.locator("#last_name").input_value() == "Lovelace"
    assert page.locator("#email").input_value() == "ada@example.com"
    assert page.locator("#phone").input_value() == "+1-555-0100"
    assert page.locator("#company").input_value() == "Analytical Engines Ltd"
    assert page.locator("#title").input_value() == "Backend Engineer"

    filled_keys = {r.field_key for r in results if r.filled}
    assert {"first_name", "last_name", "email", "phone", "current_company", "current_role"} <= filled_keys
    assert all(r.confidence == 0.95 for r in results if r.filled)


def test_fill_personal_information_skips_missing_values(page, resume_file):
    page.set_content(GREENHOUSE_FORM_HTML)
    profile = _profile(current_company=None, current_role=None)
    adapter = GreenhouseAdapter(page=page, profile=profile, resume_document=_resume_document(resume_file))

    results = adapter.fill_personal_information()

    unfilled = {r.field_key for r in results if not r.filled}
    assert {"current_company", "current_role"} <= unfilled
    assert page.locator("#company").input_value() == ""


def test_upload_resume_sets_the_file_input(page, resume_file):
    page.set_content(GREENHOUSE_FORM_HTML)
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    assert adapter.upload_resume() is True
    uploaded_count = page.evaluate("document.querySelector('input[type=file]').files.length")
    assert uploaded_count == 1


def test_upload_resume_returns_false_when_no_file_input(page, resume_file):
    page.set_content("<html><body><form></form></body></html>")
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.upload_resume() is False


def test_answer_questions_fills_linkedin_via_label_match(page, resume_file):
    page.set_content(GREENHOUSE_FORM_HTML)
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()

    assert page.locator("#linkedin_q").input_value() == "https://linkedin.com/in/ada"
    assert any(r.filled and r.profile_path == "linkedin_url" for r in results)


# ---------- Phase 6: ApplicationAnswerEngine handoff ----------
# `test_answer_questions_leaves_unmatched_labels_alone` (below) is the
# critical backward-compat regression: with no answer_engine injected (the
# default, and every call site before Phase 6), behavior must stay exactly
# what it was under Phase 5 alone.

class _FakeAnswerEngine:
    """Stands in for automation.forms.answer_engine.ApplicationAnswerEngine —
    records what it was asked and returns canned answers, so these tests
    verify the *handoff* (base.py's collection/fill wiring), not the real
    engine's own deterministic/LLM/cache logic (see test_answer_engine.py)."""

    # Default confidence sits ABOVE base.ANSWER_REVIEW_CONFIDENCE_THRESHOLD on
    # purpose: these tests exercise the handoff/fill wiring, and a generated
    # answer below that threshold is now deliberately withheld for a human
    # (see test_answer_questions_withholds_a_low_confidence_generated_answer),
    # which would make every wiring assertion below fail for the wrong reason.
    def __init__(self, answers: dict[str, str], source: str = "llm", confidence: float = 0.9):
        self.answers = answers
        self.source = source
        self.confidence = confidence
        self.batches_seen: list[list[str]] = []

    def answer_batch(self, questions):
        self.batches_seen.append(list(questions))
        return [
            AnswerResult(question=q, answer=self.answers.get(q, ""), source=self.source, confidence=self.confidence)
            for q in questions
        ]


def test_answer_questions_routes_unmatched_labels_to_the_injected_answer_engine(page, resume_file):
    page.set_content(
        """<html><body>
        <label for="why_q">Why do you want to work here?</label>
        <textarea id="why_q"></textarea>
        </body></html>"""
    )
    engine = _FakeAnswerEngine({"Why do you want to work here?": "Because I love building things."})
    adapter = GreenhouseAdapter(
        page=page, profile=_profile(), resume_document=_resume_document(resume_file), answer_engine=engine,
    )

    results = adapter.answer_questions()

    assert page.locator("#why_q").input_value() == "Because I love building things."
    assert engine.batches_seen == [["Why do you want to work here?"]]
    matched = [r for r in results if r.filled and r.value_used == "Because I love building things."]
    assert len(matched) == 1
    assert matched[0].profile_path == "answer_engine:llm"
    assert matched[0].confidence == 0.9


def test_answer_questions_withholds_a_low_confidence_generated_answer(page, resume_file):
    """Both specs gate acting on your own output at 0.80 confidence. A
    generated answer the engine isn't confident in must be left blank for a
    human rather than typed into a real application — and reported as
    unfilled, so it correctly drags the run's aggregate confidence down
    instead of looking like a handled field."""
    page.set_content(
        """<html><body>
        <label for="why_q">Why do you want to work here?</label>
        <textarea id="why_q"></textarea>
        </body></html>"""
    )
    engine = _FakeAnswerEngine(
        {"Why do you want to work here?": "A vague guess."},
        confidence=ANSWER_REVIEW_CONFIDENCE_THRESHOLD - 0.01,
    )
    adapter = GreenhouseAdapter(
        page=page, profile=_profile(), resume_document=_resume_document(resume_file), answer_engine=engine,
    )

    results = adapter.answer_questions()

    assert page.locator("#why_q").input_value() == ""  # nothing typed on the candidate's behalf
    assert engine.batches_seen == [["Why do you want to work here?"]]
    withheld = [r for r in results if r.field_key == "Why do you want to work here?"]
    assert len(withheld) == 1
    assert withheld[0].filled is False
    assert withheld[0].value_used is None
    assert withheld[0].confidence == 0.0


def test_answer_questions_leaves_field_unfilled_when_answer_engine_returns_nothing(page, resume_file):
    page.set_content(
        """<html><body>
        <label for="why_q">Why do you want to work here?</label>
        <textarea id="why_q"></textarea>
        </body></html>"""
    )
    engine = _FakeAnswerEngine({})  # no answer for this question — LLM path failed/declined
    adapter = GreenhouseAdapter(
        page=page, profile=_profile(), resume_document=_resume_document(resume_file), answer_engine=engine,
    )

    results = adapter.answer_questions()

    assert page.locator("#why_q").input_value() == ""
    assert any(not r.filled and r.profile_path == "answer_engine:llm" for r in results)


def test_answer_questions_survives_a_broken_answer_engine(page, resume_file):
    page.set_content(
        """<html><body>
        <label for="why_q">Why do you want to work here?</label>
        <textarea id="why_q"></textarea>
        </body></html>"""
    )

    class _BrokenEngine:
        def answer_batch(self, questions):
            raise RuntimeError("LLM provider is down")

    adapter = GreenhouseAdapter(
        page=page, profile=_profile(), resume_document=_resume_document(resume_file), answer_engine=_BrokenEngine(),
    )

    results = adapter.answer_questions()  # must not raise

    assert page.locator("#why_q").input_value() == ""
    assert any(not r.filled for r in results)


def test_answer_questions_leaves_unmatched_labels_alone(page, resume_file):
    page.set_content(
        """<html><body>
        <label for="why_q">Why do you want to work here?</label>
        <textarea id="why_q"></textarea>
        </body></html>"""
    )
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()

    assert results == []  # genuinely subjective — Phase 6 territory, not this adapter's job
    assert page.locator("#why_q").input_value() == ""


def test_submit_application_clicks_submit_button(page, resume_file):
    page.set_content(GREENHOUSE_FORM_HTML)
    # Prevent an actual form submission/navigation from happening mid-test —
    # we only care that our code found and clicked the right control.
    page.evaluate("document.querySelector('form').addEventListener('submit', e => e.preventDefault())")
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    assert adapter.submit_application() is True


def test_submit_application_returns_false_when_no_submit_control(page, resume_file):
    page.set_content("<html><body><form></form></body></html>")
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))
    assert adapter.submit_application() is False


# ---------- Phase 5: FieldMapper-driven name/placeholder discovery ----------
# (behavior lives on ATSAdapter itself — see automation/ats/base.py and
# automation/tests/test_field_mapper.py for FieldMapper's own unit tests —
# exercised here via GreenhouseAdapter since it's a real, non-stub adapter.)

def test_answer_questions_fills_a_bare_input_via_name_attribute_with_no_label(page, resume_file):
    # No <label> at all — only a label-based pass could never fill this.
    page.set_content('<html><body><input name="linkedin_url"></body></html>')
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()

    assert page.locator("input[name='linkedin_url']").input_value() == "https://linkedin.com/in/ada"
    assert any(r.filled and r.profile_path == "linkedin_url" for r in results)


def test_answer_questions_fills_a_bare_input_via_placeholder_when_name_does_not_match(page, resume_file):
    page.set_content('<html><body><input name="q_47" placeholder="LinkedIn Profile"></body></html>')
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()

    assert page.locator("input[name='q_47']").input_value() == "https://linkedin.com/in/ada"
    assert any(r.filled and r.profile_path == "linkedin_url" for r in results)


def test_fill_personal_information_then_answer_questions_does_not_double_count_the_same_field(page, resume_file):
    """The name/placeholder pass must skip whatever `fill_personal_information`
    already filled — otherwise the same field is counted twice toward
    `ApplicationFlowManager`'s confidence score. `field_key` differs between
    the two paths (attribute name vs. raw name/label text), so this checks
    the thing that's actually shared and matters: `profile_path`."""
    page.set_content(GREENHOUSE_FORM_HTML)
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    personal_info_results = adapter.fill_personal_information()
    question_results = adapter.answer_questions()

    personal_info_paths = {r.profile_path for r in personal_info_results if r.filled}
    assert personal_info_paths == {"first_name", "last_name", "email", "phone", "current_company", "current_role"}

    # Every one of those was already filled (and marked examined) by
    # fill_personal_information — answer_questions' name/placeholder pass
    # must not rediscover and re-report any of them.
    question_paths = {r.profile_path for r in question_results if r.filled}
    assert question_paths & personal_info_paths == set()

    # linkedin_q (only reachable via the label pass, no matching name/
    # placeholder) still gets filled — the marker isn't over-suppressing.
    assert question_paths == {"linkedin_url"}


def test_answer_questions_skips_a_label_pointing_at_an_already_filled_field(page, resume_file):
    """Regression test: a real Greenhouse posting (Point72's) had a second
    <label> elsewhere on the page (a resume-autofill preview panel) whose
    `for` pointed at the SAME #first_name input `fill_personal_information`
    had already filled. The label pass didn't check for that, so it
    redundantly re-filled (and re-counted) the field a second time."""
    page.set_content(GREENHOUSE_FORM_HTML.replace(
        '<label for="linkedin_q">',
        '<label for="first_name">First Name</label><label for="linkedin_q">',
    ))
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    adapter.fill_personal_information()
    question_results = adapter.answer_questions()

    assert not any(r.profile_path == "first_name" for r in question_results)
    # the rest of the label pass still runs normally afterward
    assert any(r.filled and r.profile_path == "linkedin_url" for r in question_results)


# ---------- regression: array-notation label "for" ids must not crash ----------
# A real run against a live Greenhouse posting failed with:
#   SyntaxError: Failed to execute 'querySelectorAll' on 'Document':
#   '#question_11371103007[]' is not a valid selector.
# Greenhouse renders checkbox-group labels with `for="question_123[]"`
# (Rails/PHP array-field convention) — `_input_for_label` used to build a
# bare `#id` CSS selector directly from that string, which isn't valid CSS
# and aborted the ENTIRE application run rather than just skipping that one
# field. See automation/ats/base.py::_input_for_label.

def test_answer_questions_does_not_crash_on_bracket_notation_label_for_id(page, resume_file):
    page.set_content(
        '<html><body>'
        '<label for="question_11371103007[]">Are you authorized to work?</label>'
        '<input type="checkbox" name="question_11371103007[]" value="yes">'
        '</body></html>'
    )
    profile = _profile(work_authorization="Authorized")
    adapter = GreenhouseAdapter(page=page, profile=profile, resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()  # must not raise

    # The label matched, but there's no plain fillable control to resolve it
    # to (a checkbox isn't nested inside the label, and none of the sibling
    # elements have that literal id) — reported as unfilled, not crashed.
    matched = [r for r in results if r.profile_path == "work_authorization"]
    assert matched
    assert matched[0].filled is False


def test_answer_questions_recovers_from_a_broken_label_and_still_fills_the_rest_of_the_page(page, resume_file):
    page.set_content(
        '<html><body>'
        '<label for="question_11371103007[]">Are you authorized to work?</label>'
        '<input type="checkbox" name="question_11371103007[]" value="yes">'
        '<label for="linkedin_q">LinkedIn Profile</label>'
        '<input id="linkedin_q">'
        '</body></html>'
    )
    profile = _profile(work_authorization="Authorized")
    adapter = GreenhouseAdapter(page=page, profile=profile, resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()  # the broken label must not stop the sweep

    assert page.locator("#linkedin_q").input_value() == "https://linkedin.com/in/ada"
    assert any(r.filled and r.profile_path == "linkedin_url" for r in results)


# ---------- field_handlers.py wiring: a react-select-style country picker ----------
# End-to-end proof that a labeled-but-non-native dropdown reached through the
# REAL label sweep (not field_handlers.py's own unit tests) gets routed to
# CountryPickerHandler and filled/verified correctly. See
# automation/tests/test_field_handlers.py for the handler's own unit tests.

_COUNTRY_PICKER_HTML = """
<html><body>
<label for="country-control">Country</label>
<div id="country-control" class="react-select__control" tabindex="0">
  <div class="react-select__value-container">
    <div class="react-select__placeholder">Select...</div>
  </div>
</div>
<div class="react-select__menu" role="listbox" style="display:none;">
  <input class="react-select__input" aria-autocomplete="list">
  <div class="react-select__option" role="option">United States</div>
  <div class="react-select__option" role="option">Canada</div>
</div>
<script>
  const control = document.getElementById('country-control');
  const menu = document.querySelector('.react-select__menu');
  const input = document.querySelector('.react-select__input');
  const valueContainer = document.querySelector('.react-select__value-container');
  control.addEventListener('click', () => { menu.style.display = 'block'; input.focus(); });
  document.querySelectorAll('.react-select__option').forEach(opt => {
    opt.addEventListener('click', () => {
      valueContainer.innerHTML = '<div class="react-select__single-value">' + opt.textContent + '</div>';
      menu.style.display = 'none';
    });
  });
</script>
</body></html>
"""


def test_answer_questions_fills_a_react_select_country_picker_via_the_label_sweep(page, resume_file):
    page.set_content(_COUNTRY_PICKER_HTML)
    profile = _profile(country="USA")  # alias — the widget's own option reads "United States"
    adapter = GreenhouseAdapter(page=page, profile=profile, resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()

    assert page.locator(".react-select__single-value").text_content() == "United States"
    matched = [r for r in results if r.profile_path == "country"]
    assert matched and matched[0].filled is True


# ---------- regression: encrypted profile attributes (address/phone) in the
# generic label/name sweep ----------
# A real Greenhouse posting (PayPay India's) asked a custom "Please fill out
# your current residence address" question. FieldMapper correctly resolves
# that label to the canonical attribute "address" — but CandidateProfile has
# no plain `address` attribute at all, only the Fernet-encrypted
# `address_encrypted` column, so `getattr(self.profile, "address", None)`
# silently returned None even with a real address on file, and the field was
# reported as "nothing to fill" rather than a failure. See
# ATSAdapter._resolve_profile_value in automation/ats/base.py.

def test_answer_questions_fills_a_custom_address_question_from_the_encrypted_profile_field(page, resume_file):
    page.set_content(
        '<html><body>'
        '<label for="residence_q">Please fill out your current residence address *</label>'
        '<textarea id="residence_q"></textarea>'
        '</body></html>'
    )
    profile = _profile()
    profile.address_encrypted = encrypt_field("221B Baker Street, London")
    adapter = GreenhouseAdapter(page=page, profile=profile, resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()

    assert page.locator("#residence_q").input_value() == "221B Baker Street, London"
    matched = [r for r in results if r.profile_path == "address"]
    assert matched and matched[0].filled is True


# ---------- required consent checkboxes ("I agree to the Privacy Policy") ----------
# A real Greenhouse posting requires ticking "I agree" to its privacy policy
# before submitting. There is no CandidateProfile attribute for this — it
# isn't applicant data — so FieldMapper never matches it and the answer
# engine handoff explicitly excludes checkboxes, meaning nothing used to ever
# check it at all. See ATSAdapter._fill_consent_checkboxes.

def test_answer_questions_checks_a_required_privacy_policy_consent_checkbox(page, resume_file):
    page.set_content(
        '<html><body>'
        '<label for="agree_q">I agree to the <a href="/privacy">Privacy Policy for Job Applicants</a> *</label>'
        '<input type="checkbox" id="agree_q">'
        '</body></html>'
    )
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()

    assert page.locator("#agree_q").is_checked() is True
    matched = [r for r in results if r.profile_path == "consent_checkbox"]
    assert matched and matched[0].filled is True


def test_answer_questions_does_not_auto_check_an_optional_non_consent_checkbox(page, resume_file):
    """Regression safety: this pass must stay narrow. An optional marketing
    opt-in (not required, no consent-style wording) must never be silently
    ticked on the candidate's behalf."""
    page.set_content(
        '<html><body>'
        '<label for="newsletter_q">Send me job alerts by email</label>'
        '<input type="checkbox" id="newsletter_q">'
        '</body></html>'
    )
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()

    assert page.locator("#newsletter_q").is_checked() is False
    assert not any(r.profile_path == "consent_checkbox" for r in results)


def test_answer_questions_does_not_auto_check_a_required_but_non_consent_checkbox(page, resume_file):
    """The gate is required-ness AND consent wording — not either alone. A
    required checkbox with unrelated wording (no FieldMapper match either)
    is left alone rather than guessed at."""
    page.set_content(
        '<html><body>'
        '<label for="onsite_q">I am able to work on-site 5 days a week *</label>'
        '<input type="checkbox" id="onsite_q">'
        '</body></html>'
    )
    adapter = GreenhouseAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    results = adapter.answer_questions()

    assert page.locator("#onsite_q").is_checked() is False
    assert not any(r.profile_path == "consent_checkbox" for r in results)
