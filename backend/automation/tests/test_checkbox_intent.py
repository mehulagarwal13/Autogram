"""
A checkbox must never be ticked from an arbitrary string.

Observed live: the marketing opt-in "Send me job alerts by email" matched
`FieldMapper`'s `email` synonym (correctly, on a word boundary — "by email"),
resolved to `('email', 0.9)`, and `CheckboxHandler` received the candidate's
email ADDRESS as the checkbox's "value". Because `_coerce_checkbox_intent` was
a blocklist — anything not in `_CHECKBOX_NEGATIVE_TEXT` meant "check it" — the
box was ticked, at a confidence above the auto-submit bar. A candidate would
have been silently subscribed to marketing email on a real application.

Two independent defects, fixed separately and tested separately here:

1. `field_mapper.looks_like_opt_in_label` — opt-in prose never resolves to a
   profile-value attribute, whatever synonym matches inside it.
2. `_coerce_checkbox_intent` — now an allowlist in both directions, returning
   `None` (a `FieldFillRefused`) for anything that isn't boolean-shaped. This
   holds for ANY mis-mapped text field, not just email, so a future synonym
   false-positive fails safe instead of ticking a box.
"""

from __future__ import annotations

import pytest

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, ProfileDocument
from automation.ats.greenhouse.greenhouse_adapter import GreenhouseAdapter
from automation.forms.field_handlers import (
    _coerce_checkbox_intent,
    describe_field,
    fill_field,
)
from automation.forms.field_mapper import FieldMapper, looks_like_opt_in_label


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(
        profile_id="profile-1", user_id="user-1",
        first_name="Ada", last_name="Lovelace", email="ada@example.com",
        location="Noida, India", current_company="Navikenz",
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
        original_filename="resume.pdf", stored_path=str(path), file_hash="abc", is_default=True,
    )


def _adapter(page, resume_file):
    return GreenhouseAdapter(
        page=page, profile=_profile(), resume_document=_resume_document(resume_file)
    )


# ---------------------------------------------------------------------------
# Fix 1 — opt-in labels don't map to profile values
# ---------------------------------------------------------------------------

def test_the_original_marketing_label_no_longer_maps_to_email():
    assert FieldMapper.map_field(label="Send me job alerts by email") is None


@pytest.mark.parametrize("label", [
    "Send me job alerts by email",
    "Notify me about similar roles",
    "Subscribe to our newsletter",
    "Sign up for product updates",
    "I would like to receive marketing communications",
    "Keep me posted about future openings",
    "Opt-in to recruiter emails",
    "Email me when new jobs are posted",
])
def test_opt_in_phrasings_are_recognised(label):
    assert looks_like_opt_in_label(label) is True
    assert FieldMapper.map_field(label=label) is None


@pytest.mark.parametrize("label,expected", [
    ("Email", "email"),
    ("Email Address", "email"),
    ("Work email*", "email"),
    ("Phone number", "phone"),
    ("Current location", "location"),
    ("LinkedIn Profile", "linkedin_url"),
])
def test_ordinary_value_labels_are_unaffected(label, expected):
    match = FieldMapper.map_field(label=label)
    assert match is not None and match[0] == expected


def test_a_machine_name_attribute_is_never_suppressed():
    """Suppression is for prose only — `name="email"` is not marketing copy,
    and treating it as such would break ordinary email inputs."""
    match = FieldMapper.map_field(name="email")
    assert match is not None and match[0] == "email"


def test_consent_labels_are_not_treated_as_opt_in_marketing():
    """"I agree to the Privacy Policy" is a required legal consent handled by
    `_fill_consent_checkboxes`, not a marketing opt-in. It maps to nothing
    either way, but it must not be swept up by these patterns."""
    assert looks_like_opt_in_label("I agree to the Privacy Policy") is False
    assert looks_like_opt_in_label("I consent to the Terms of Service") is False


# ---------------------------------------------------------------------------
# Fix 2 — _coerce_checkbox_intent refuses non-boolean values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [True, False])
def test_real_booleans_pass_through(value):
    assert _coerce_checkbox_intent(value) is value


@pytest.mark.parametrize("value,expected", [
    ("Yes", True), ("yes", True), ("Y", True), ("true", True), ("1", True),
    ("I agree", True), ("Accept", True), ("Yes, I agree", True),
    ("No", False), ("no", False), ("false", False), ("0", False),
    ("Decline", False), ("No, thanks", False),
])
def test_boolean_shaped_text_resolves(value, expected):
    assert _coerce_checkbox_intent(value) is expected


@pytest.mark.parametrize("value", [
    "ada@example.com",
    "Noida, India",
    "Navikenz",
    "https://linkedin.com/in/ada",
    "+1-555-0100",
    "Backend Engineer",
    "5 years",
    "USD 120,000",
])
def test_arbitrary_profile_text_refuses_instead_of_defaulting_to_checked(value):
    """The heart of fix 2. Every one of these previously meant "tick the box"
    purely because it wasn't on a negative-words blocklist."""
    assert _coerce_checkbox_intent(value) is None


def test_a_location_starting_with_no_is_not_read_as_negative():
    """Leading-token matching must not turn "Noida" into "no"."""
    assert _coerce_checkbox_intent("Noida, India") is None


# ---------------------------------------------------------------------------
# End to end — the original failing scenario
# ---------------------------------------------------------------------------

def test_an_optional_marketing_checkbox_is_left_unchecked(page, resume_file):
    """The original failure. Correct answer is "don't touch it at all" — this
    is the candidate's own preference to express, and it isn't required."""
    page.set_content(
        '<html><body>'
        '<label for="newsletter_q">Send me job alerts by email</label>'
        '<input type="checkbox" id="newsletter_q">'
        '</body></html>'
    )
    adapter = _adapter(page, resume_file)

    adapter.answer_questions()

    assert page.locator("#newsletter_q").is_checked() is False


def test_a_checkbox_mapped_to_an_unrelated_text_field_is_still_not_checked(page, resume_file):
    """Fix 2 in isolation, with email nowhere in sight: a label that maps to
    `current_company` on a checkbox must not tick it. This is what makes the
    fix general rather than a patch for the email case."""
    page.set_content(
        '<html><body>'
        '<label for="cb">Current company</label>'
        '<input type="checkbox" id="cb">'
        '</body></html>'
    )
    adapter = _adapter(page, resume_file)

    results = adapter.answer_questions()

    assert page.locator("#cb").is_checked() is False
    refusals = [r for r in results if r.failure and r.failure.failure_reason == "non_boolean_checkbox_value"]
    assert refusals, f"expected a refusal, got {[(r.field_key, r.failure) for r in results]}"


def test_the_refusal_is_reported_rather_than_silently_skipped(page, resume_file):
    page.set_content(
        '<html><body>'
        '<label for="cb">Current location</label>'
        '<input type="checkbox" id="cb">'
        '</body></html>'
    )
    adapter = _adapter(page, resume_file)

    results = adapter.answer_questions()

    failures = [r.failure for r in results if r.failure]
    assert any(f.failure_reason == "non_boolean_checkbox_value" for f in failures)
    assert any("rejected_checkbox_value" in (f.context or {}) for f in failures)


# ---------------------------------------------------------------------------
# The working consent path must be untouched
# ---------------------------------------------------------------------------

def test_a_required_consent_checkbox_is_still_checked(page, resume_file):
    """`_fill_consent_checkboxes` passes a real `True`, so fix 2 cannot affect
    it — pinned here so nobody has to take that on faith."""
    page.set_content(
        '<html><body>'
        '<label for="consent">I agree to the Privacy Policy*</label>'
        '<input type="checkbox" id="consent" required>'
        '</body></html>'
    )
    adapter = _adapter(page, resume_file)

    adapter.answer_questions()

    assert page.locator("#consent").is_checked() is True


def test_an_optional_consent_worded_checkbox_is_still_left_alone(page, resume_file):
    """Consent wording but NOT required — the existing narrowness of
    `_fill_consent_checkboxes` must survive both fixes."""
    page.set_content(
        '<html><body>'
        '<label for="consent">I agree to receive occasional updates</label>'
        '<input type="checkbox" id="consent">'
        '</body></html>'
    )
    adapter = _adapter(page, resume_file)

    adapter.answer_questions()

    assert page.locator("#consent").is_checked() is False
