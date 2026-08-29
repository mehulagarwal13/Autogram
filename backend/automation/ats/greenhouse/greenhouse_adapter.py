"""GreenhouseAdapter — Phase 4 (see ARCHITECTURE.md).

Greenhouse is a public, no-login ATS (`boards.greenhouse.io`) — the PoC
target from the project brief: a single-page `#application_form` with
predictable field IDs, no authentication wall, no multi-step navigation in
the common case (so `find_next_button` will simply find nothing and
`ApplicationFlowManager` goes straight to submit).
"""

from __future__ import annotations

import logging

from playwright.sync_api import Error as PlaywrightError

from automation.ats.base import ATSAdapter, FieldFillResult
from automation.ats.detector import DOM_FINGERPRINTS
from automation.browser.selectors import find_submit_button

logger = logging.getLogger(__name__)

# Known Greenhouse job-board field selectors, checked in order per attribute.
# Greenhouse has shipped more than one template generation over the years,
# so each attribute lists a couple of plausible selectors rather than one.
FIELD_SELECTORS: dict[str, list[str]] = {
    "first_name": ["#first_name", "input[name='job_application[first_name]']", "input[autocomplete='given-name']"],
    "last_name": ["#last_name", "input[name='job_application[last_name]']", "input[autocomplete='family-name']"],
    "email": ["#email", "input[name='job_application[email]']", "input[type='email']"],
    "phone": ["#phone", "input[name='job_application[phone]']", "input[type='tel']"],
    "current_company": ["#company", "input[name='job_application[company]']"],
    "current_role": ["#title", "input[name='job_application[title]']"],
}


class GreenhouseAdapter(ATSAdapter):
    name = "greenhouse"

    def detect(self) -> float:
        """Secondary DOM check (`ATSDetector` already ran the cheap URL/DOM
        tiers before choosing this adapter) — reuses the same fingerprint
        table as `ats/detector.py::DOM_FINGERPRINTS` rather than maintaining
        a second list of Greenhouse selectors."""
        for selector in DOM_FINGERPRINTS.get("greenhouse", []):
            try:
                if self.page.locator(selector).count() > 0:
                    return 0.9
            except PlaywrightError:
                continue
        return 0.0

    def fill_personal_information(self) -> list[FieldFillResult]:
        values = {
            "first_name": self.profile.first_name,
            "last_name": self.profile.last_name,
            "email": self.profile.email,
            "phone": self.phone,
            "current_company": self.profile.current_company,
            "current_role": self.profile.current_role,
        }
        results = []
        for attribute, value in values.items():
            filled, failure = self._fill_first_match(FIELD_SELECTORS[attribute], value)
            results.append(
                FieldFillResult(
                    field_key=attribute,
                    profile_path=attribute,
                    value_used=value,
                    confidence=0.95 if filled else 0.0,
                    filled=filled,
                    failure=failure,
                )
            )
        return results

    # upload_resume() is inherited from ATSAdapter (automation/ats/base.py) —
    # identical to Lever's, so it's implemented once, on the base class.

    def answer_questions(self) -> list[FieldFillResult]:
        """Deterministic pass only — see `ATSAdapter._fill_known_questions`.
        Subjective/novel custom questions (e.g. "Why do you want to work
        here?") are left unanswered here; Phase 6's `ApplicationAnswerEngine`
        handles those via `automation.interfaces.generate_answer`."""
        return self._fill_known_questions()

    def submit_application(self) -> bool:
        submit_button = find_submit_button(self.page)
        if submit_button is None:
            logger.warning("Greenhouse: no submit button found on page.")
            return False
        try:
            submit_button.click()
            return True
        except PlaywrightError as e:
            logger.warning("Greenhouse: submit click failed: %s", e)
            return False
