"""LeverAdapter — Phase 4 (see ARCHITECTURE.md).

Public, no-login ATS (`jobs.lever.co`), single-page `.application-form`.
Unlike Greenhouse, Lever's classic posting form uses one "Full Name" field
rather than separate first/last inputs — a small but real illustration of
why each ATS gets its own adapter instead of one shared field map.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Error as PlaywrightError

from automation.ats.base import ATSAdapter, FieldFillResult
from automation.ats.detector import DOM_FINGERPRINTS
from automation.browser.selectors import find_submit_button

logger = logging.getLogger(__name__)

FIELD_SELECTORS: dict[str, list[str]] = {
    "full_name": ["input[name='name']", "#name-input"],
    "email": ["input[name='email']", "#email-input"],
    "phone": ["input[name='phone']", "#phone-input"],
    "current_company": ["input[name='org']", "#org-input"],
}


class LeverAdapter(ATSAdapter):
    name = "lever"

    def detect(self) -> float:
        for selector in DOM_FINGERPRINTS.get("lever", []):
            try:
                if self.page.locator(selector).count() > 0:
                    return 0.9
            except PlaywrightError:
                continue
        return 0.0

    def fill_personal_information(self) -> list[FieldFillResult]:
        # Lever has no separate first/last name fields — fall back to
        # combining them if profile.full_name itself wasn't set.
        full_name = self.profile.full_name or " ".join(
            part for part in (self.profile.first_name, self.profile.last_name) if part
        ) or None

        values = {
            "full_name": full_name,
            "email": self.profile.email,
            "phone": self.phone,
            "current_company": self.profile.current_company,
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
    # identical to Greenhouse's, so it's implemented once, on the base class.

    def answer_questions(self) -> list[FieldFillResult]:
        return self._fill_known_questions()

    def submit_application(self) -> bool:
        submit_button = find_submit_button(self.page)
        if submit_button is None:
            logger.warning("Lever: no submit button found on page.")
            return False
        try:
            submit_button.click()
            return True
        except PlaywrightError as e:
            logger.warning("Lever: submit click failed: %s", e)
            return False
