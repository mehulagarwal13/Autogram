"""GenericAdapter — the fallback that makes "apply to any job portal by
link" real, not just for Greenhouse/Lever/Workday.

When `ATSDetector` can't confidently match a known, registered platform (an
arbitrary company's own custom-built careers page, or a platform this
deployment has no dedicated adapter for yet — see `ats/registry.py`),
`ApplicationFlowManager._resolve_adapter_from_listing_page` constructs THIS
adapter instead of giving up. It deliberately reuses the exact same
ATS-agnostic machinery every other adapter is built on
(`ATSAdapter._fill_known_questions`, `automation/forms/field_mapper.py`,
`automation/forms/field_handlers.py`, `automation/browser/selectors.py`) —
there is no separate "generic form-filling engine" to write, because that
machinery was never actually Greenhouse/Lever-specific to begin with; it
already resolves a field from nothing more than its `<label>` text, `name`/
`id` attribute, or placeholder, which is exactly what's available on an
unrecognized page too.

`detect()` always returns a low, constant confidence (0.1): this adapter
never competes with a real adapter's own `detect()` for the SAME page (see
`ATSDetector`'s tiered strategy) and its runs never qualify for `AUTO_SUBMIT`
regardless of how well the form fills, because `"custom"` (and any other
platform without a dedicated adapter) is never a member of
`ApplicationFlowManager.PUBLIC_ATS_PLATFORMS` — every run through this
adapter ends at `NEEDS_REVIEW` or `COPILOT_REVIEW`, never a fully autonomous
submission. A human always reviews (and, in copilot mode, clicks submit on)
an application this adapter filled.

If the page needs something no adapter — generic or specialized — should
ever be filling around (a CAPTCHA, a login/account-creation wall, an OTP),
`ApplicationFlowManager` already stops for a human before this adapter's
methods are ever called; see `_process_page`'s human-gate/CAPTCHA checks.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Error as PlaywrightError

from automation.ats.base import ATSAdapter, FieldFillResult
from automation.browser.selectors import find_submit_button

logger = logging.getLogger(__name__)


class GenericAdapter(ATSAdapter):
    name = "custom"

    def detect(self) -> float:
        # Always the fallback — never wins over a specialized adapter's own
        # detect() (see ATSDetector's tiered strategy and
        # `_resolve_adapter_from_listing_page`, which only ever constructs
        # this adapter after every registered platform's detection has
        # already failed on this page).
        return 0.1

    def fill_personal_information(self) -> list[FieldFillResult]:
        # An unrecognized page has no known selector table to build a
        # Greenhouse/Lever-style `FIELD_SELECTORS` dict from — there is
        # nothing platform-specific to key off of. Rather than duplicate
        # `_fill_known_questions()`'s label/name/placeholder sweep here
        # (it already resolves `first_name`/`last_name`/`email`/`phone`/
        # etc. — those are ordinary `FIELD_SYNONYMS` entries, not
        # screening-question-specific), personal information is left to
        # that single sweep in `answer_questions()`, which runs immediately
        # after this on every page anyway (see
        # `ApplicationFlowManager._fill_page`). Returning `[]` here costs
        # nothing: no field goes unfilled, and none gets double-counted.
        return []

    def upload_resume(self) -> bool:
        # Inherited from ATSAdapter (automation/ats/base.py) — identical to
        # every other adapter's, built on find_file_upload_input/
        # find_upload_trigger_button, which know nothing about any specific
        # ATS. No override needed.
        return super().upload_resume()

    def answer_questions(self) -> list[FieldFillResult]:
        """The entire fill strategy for an unrecognized page: the same
        label/name/placeholder sweep + answer-engine batch + checkbox-group
        + consent-checkbox handling every real adapter's `answer_questions()`
        also just delegates to. See `ATSAdapter._fill_known_questions`."""
        return self._fill_known_questions()

    def submit_application(self) -> bool:
        submit_button = find_submit_button(self.page)
        if submit_button is None:
            logger.warning("GenericAdapter: no submit button found on page.")
            return False
        try:
            submit_button.click()
            return True
        except PlaywrightError as e:
            logger.warning("GenericAdapter: submit click failed: %s", e)
            return False
