"""Regression coverage for Oracle HCM's styled native radio groups."""

from __future__ import annotations

import json

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, ProfileDocument
from automation.ats.generic.generic_adapter import GenericAdapter
from automation.forms.answer_engine import ApplicationAnswerEngine


def _profile() -> CandidateProfile:
    profile = CandidateProfile(
        profile_id="profile-1",
        user_id="user-1",
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        location="Noida, India",
    )
    profile.phone_encrypted = encrypt_field("+1-555-0100")
    return profile


def _adapter(page, tmp_path, *, asked: list[dict]) -> GenericAdapter:
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 fake")

    def llm_fn(*, task, prompt, system=None, **kwargs):
        payload = json.loads(prompt)
        for question, options in zip(payload["questions"], payload["options"]):
            asked.append({"question": question, "options": options})
        return json.dumps({
            "answers": [
                {"answer": "No", "confidence": 0.95}
                for _question in payload["questions"]
            ],
        })

    profile = _profile()
    return GenericAdapter(
        page=page,
        profile=profile,
        resume_document=ProfileDocument(
            document_id="doc-1",
            profile_id="profile-1",
            document_type="resume",
            original_filename="resume.pdf",
            stored_path=str(resume_path),
            file_hash="abc",
            is_default=True,
        ),
        answer_engine=ApplicationAnswerEngine(profile=profile, llm_fn=llm_fn),
    )


_QUESTION = (
    "Do you or your spouse or life partner have an Immediate Family Member "
    "or a Close Personal Relationship with anyone who works at American Express?"
)


def _oracle_radio_html(*, hidden_section: bool = False) -> str:
    section_style = "display:none" if hidden_section else ""
    return f"""
    <html><body>
      <section style="{section_style}">
        <div class="input-row input-row--radiogroup input-row--invalid"
             role="radiogroup" aria-labelledby="conflict-question-label">
          <label id="conflict-question-label" class="input-row__label">{_QUESTION}</label>
          <div class="input-row__instructions">
            Immediate Family Member includes spouses, domestic partners, parents,
            children, siblings, grandparents, grandchildren, in-laws and other
            relatives. Close Personal Relationship includes dating, romantic or
            intimate relationships. This deliberately long instruction is not
            part of the radiogroup's accessible question name.
          </div>
          <div class="input-row__control-container">
            <input style="display:none" type="radio" id="conflict-yes"
                   name="oracle-conflict" value="oracle-yes" required>
            <label class="apply-flow-input-radio" for="conflict-yes"><span>Yes</span></label>
            <input style="display:none" type="radio" id="conflict-no"
                   name="oracle-conflict" value="oracle-no" required>
            <label class="apply-flow-input-radio" for="conflict-no"><span>No</span></label>
            <p class="input-row__validation" role="alert">This info is required.</p>
          </div>
        </div>
      </section>
    </body></html>
    """


def test_visible_styled_oracle_radio_group_is_answered(page, tmp_path):
    asked: list[dict] = []
    page.set_content(_oracle_radio_html())

    results = _adapter(page, tmp_path, asked=asked).answer_questions()

    assert asked == [{"question": _QUESTION, "options": ["Yes", "No"]}]
    assert page.locator("#conflict-no").is_checked() is True
    assert page.locator("#conflict-yes").is_checked() is False
    assert [result.field_key for result in results if result.filled] == [_QUESTION]


def test_styled_radios_in_an_inactive_oracle_section_are_skipped(page, tmp_path):
    asked: list[dict] = []
    page.set_content(_oracle_radio_html(hidden_section=True))

    results = _adapter(page, tmp_path, asked=asked).answer_questions()

    assert asked == []
    assert results == []
    assert page.locator("#conflict-yes").get_attribute("data-automation-examined") is None
    assert page.locator("#conflict-no").get_attribute("data-automation-examined") is None
