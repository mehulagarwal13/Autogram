"""WorkdayAdapter — the long, multi-page ATS.

`myworkdayjobs.com` (and the `myworkdaysite.com` variant). Workday is the
platform every "my automation only handles one page" assumption breaks on: a
single application is typically **4-6 separate pages** — My Information, My
Experience, Application Questions, Voluntary Disclosures, Self Identify,
Review — each an SPA transition rather than a page load, with the résumé upload
on page 2 rather than page 1 and a progress bar that is the only on-page
statement of how long the form actually is.

Almost all of the work that makes that possible is generic and lives elsewhere:
the verified page cycle in `automation/applications/application_flow_manager.py`,
the navigation proof in `automation/applications/page_navigator.py`, and the
field/answer machinery every adapter shares in `automation/ats/base.py`. This
class supplies only what is genuinely Workday-shaped:

- **Its controls are identified by `data-automation-id`, not by text.** That is
  a gift for reliability (these ids are stable across tenants and redesigns in
  a way CSS classes and visible labels are not) and the reason the navigation
  hooks are overridden here at all.
- **Next and Submit are the same button.** `bottom-navigation-next-button`
  reads "Next"/"Save and Continue" throughout the form and "Submit" on the
  review page. So "is this the final page?" is answered by reading that one
  button's label, not by the generic "is there a Next button?" rule — which
  would answer "no, keep going" on the review page forever.

**Login.** Workday makes applicants create an account, and this app never
does that: `find_human_gate` sees the password field and the run stops for a
human (ARCHITECTURE.md, "No password harvesting"). The realistic path is the
one Phase 1 opened up — attaching to the user's own Chrome, where they are
frequently already signed in to the tenant, so the form is reachable without
this app ever touching a credential.

**Submission.** Workday is deliberately absent from
`ApplicationFlowManager.PUBLIC_ATS_PLATFORMS`, so `decide_action` can never
choose AUTO_SUBMIT for it regardless of confidence or the autopilot setting. A
completed Workday application is always handed to a human to submit.
"""

from __future__ import annotations

import logging
import re

from playwright.sync_api import Error as PlaywrightError, Locator

from automation.ats.base import ATSAdapter, FieldFillResult
from automation.browser.selectors import find_submit_button, looks_like_review_page

logger = logging.getLogger(__name__)

#: Workday's own automation ids for the personal-information page, checked in
#: order per attribute. Tenants customise labels and styling freely but these
#: ids come from Workday's shared component library, which is exactly why they
#: are preferred over anything text-based.
FIELD_SELECTORS: dict[str, list[str]] = {
    "first_name": [
        "input[data-automation-id='legalNameSection_firstName']",
        "input[data-automation-id='name--legalName--firstName']",
        "input[data-automation-id*='firstName' i]",
    ],
    "middle_name": [
        "input[data-automation-id='legalNameSection_middleName']",
        "input[data-automation-id*='middleName' i]",
    ],
    "last_name": [
        "input[data-automation-id='legalNameSection_lastName']",
        "input[data-automation-id='name--legalName--lastName']",
        "input[data-automation-id*='lastName' i]",
    ],
    "email": [
        "input[data-automation-id='email']",
        "input[data-automation-id*='email' i]",
        "input[type='email']",
    ],
    "phone": [
        "input[data-automation-id='phone-number']",
        "input[data-automation-id*='phoneNumber' i]",
        "input[type='tel']",
    ],
    "address": [
        "input[data-automation-id='addressSection_addressLine1']",
        "input[data-automation-id*='addressLine1' i]",
    ],
    "city": [
        "input[data-automation-id='addressSection_city']",
        "input[data-automation-id*='_city' i]",
    ],
    "postal_code": [
        "input[data-automation-id='addressSection_postalCode']",
        "input[data-automation-id*='postalCode' i]",
    ],
}

#: The one button that walks the whole application. Same id from page 1 to the
#: review page; only its LABEL changes.
BOTTOM_NAVIGATION_SELECTORS = [
    "button[data-automation-id='bottom-navigation-next-button']",
    "button[data-automation-id='pageFooterNextButton']",
    "[data-automation-id='bottom-navigation-next-button']",
]

#: Fingerprints tight enough to be worth asserting on. `DOM_FINGERPRINTS`'
#: bare `[data-automation-id]` is fine as a cheap routing hint but far too
#: loose to confirm with — plenty of non-Workday pages carry that attribute.
DETECTION_SELECTORS = [
    "[data-automation-id='bottom-navigation-next-button']",
    "[data-automation-id='pageHeader']",
    "[data-automation-id='progressBar']",
    "[data-automation-id='applyFlowPage']",
]
_WORKDAY_URL_HINTS = ("myworkdayjobs.com", "myworkdaysite.com", "/wday/")

#: A bottom-navigation button whose label matches this is the end of the line.
_SUBMIT_LABEL_RE = re.compile(r"\bsubmit\b", re.IGNORECASE)
_LABEL_READ_TIMEOUT_MS = 2_000


class WorkdayAdapter(ATSAdapter):
    name = "workday"

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect(self) -> float:
        """Confidence that this page is a Workday application. `ATSDetector`
        has already run its URL tier by the time an adapter exists, so this is
        the DOM-side confirmation."""
        try:
            url = (self.page.url or "").lower()
        except PlaywrightError:
            url = ""
        if any(hint in url for hint in _WORKDAY_URL_HINTS):
            return 0.98

        for selector in DETECTION_SELECTORS:
            try:
                if self.page.locator(selector).count() > 0:
                    return 0.9
            except PlaywrightError:
                continue
        return 0.0

    # ------------------------------------------------------------------
    # Multi-page navigation (see ATSAdapter's "Multi-page navigation")
    # ------------------------------------------------------------------

    def _bottom_navigation_button(self) -> Locator | None:
        """Workday's single next/submit control, or `None` if this page has
        none (a rendering that hasn't finished, or a non-apply page)."""
        for selector in BOTTOM_NAVIGATION_SELECTORS:
            try:
                candidates = self.page.locator(selector)
                count = candidates.count()
            except PlaywrightError:
                continue
            for index in range(count):
                candidate = candidates.nth(index)
                try:
                    if candidate.is_visible() and candidate.is_enabled():
                        return candidate
                except PlaywrightError:
                    continue
        return None

    @staticmethod
    def _button_label(button: Locator) -> str:
        for read in (
            lambda: button.inner_text(timeout=_LABEL_READ_TIMEOUT_MS),
            lambda: button.get_attribute("aria-label"),
            lambda: button.get_attribute("title"),
        ):
            try:
                text = read()
            except PlaywrightError:
                continue
            if text:
                return " ".join(text.split())
        return ""

    def find_next_control(self) -> Locator | None:
        """The button that advances to the next page — deliberately `None` when
        the bottom-navigation button says "Submit", because submitting is not
        navigating and the flow manager must never reach a submit through the
        navigation path."""
        button = self._bottom_navigation_button()
        if button is None:
            return super().find_next_control()
        if _SUBMIT_LABEL_RE.search(self._button_label(button)):
            return None
        return button

    def find_submit_control(self) -> Locator | None:
        button = self._bottom_navigation_button()
        if button is not None and _SUBMIT_LABEL_RE.search(self._button_label(button)):
            return button
        return find_submit_button(self.page)

    def is_final_page(self) -> bool:
        """The review page: the bottom-navigation button now says "Submit", or
        the page identifies itself as a review/summary step.

        The generic rule ("no Next button means the last page") is wrong here in
        both directions — Workday always has a bottom-navigation button, so the
        generic rule would keep navigating off the review page, and a page whose
        button is momentarily disabled mid-render would look final when it
        isn't."""
        button = self._bottom_navigation_button()
        if button is None:
            # No usable navigation control at all. Fall back to the generic
            # question rather than assuming: a page still rendering shouldn't
            # be declared the end of a five-page application.
            return super().is_final_page()
        return bool(_SUBMIT_LABEL_RE.search(self._button_label(button))) or looks_like_review_page(self.page)

    def page_label(self) -> str:
        """Workday names every step in its own header ("My Experience"), which
        is the single most useful thing a multi-page run can log."""
        for selector in ("[data-automation-id='pageHeader']", "[data-automation-id='jobPostingHeader']"):
            try:
                header = self.page.locator(selector).first
                if header.count() == 0:
                    continue
                text = " ".join((header.inner_text(timeout=_LABEL_READ_TIMEOUT_MS) or "").split())
            except PlaywrightError:
                continue
            if text:
                return text
        return super().page_label()

    # ------------------------------------------------------------------
    # Filling
    # ------------------------------------------------------------------

    def fill_personal_information(self) -> list[FieldFillResult]:
        """Fills whichever of the known personal-information fields are
        actually ON THIS PAGE.

        A field that isn't here produces no result at all — not a failed one.
        The flow manager calls this once per page, and on a 5-page application
        at most one of those pages has name/email/phone fields; recording eight
        failures for each of the other four would put 32 fields that were never
        there to fill into the confidence score. Measured on a five-page form:
        8/46 fields "filled" (0.17 confidence, an automatic `needs_review`)
        before this skipped absent fields, against 8/14 after.

        Presence is judged by VISIBILITY, not by `count()`. Plenty of wizards —
        including Workday's own accordion sections — keep every step in the DOM
        and toggle `display`, so a page-1 field is still `count() == 1` while
        page 4 is on screen. `_fill_first_match` already refuses to type into a
        hidden control, so counting them would produce exactly the phantom
        failures this exists to prevent."""
        values = {
            "first_name": self.profile.first_name,
            "middle_name": getattr(self.profile, "middle_name", None),
            "last_name": self.profile.last_name,
            "email": self.profile.email,
            "phone": self.phone,
            "address": self.address,
            "city": getattr(self.profile, "city", None),
            "postal_code": getattr(self.profile, "postal_code", None),
        }

        results: list[FieldFillResult] = []
        for attribute, value in values.items():
            if value in (None, "", []):
                continue
            selectors = FIELD_SELECTORS[attribute]
            if not self._has_visible_field(selectors):
                continue  # not on this page — nothing happened, so nothing to report
            filled, failure = self._fill_first_match(selectors, value)
            results.append(FieldFillResult(
                field_key=attribute,
                profile_path=attribute,
                value_used=value,
                confidence=0.95 if filled else 0.0,
                filled=filled,
                failure=failure,
            ))
        return results

    def _has_visible_field(self, selectors: list[str]) -> bool:
        """Whether any of `selectors` matches a control the user can actually
        see right now."""
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                count = locator.count()
            except PlaywrightError:
                continue
            for index in range(count):
                try:
                    if locator.nth(index).is_visible():
                        return True
                except PlaywrightError:
                    continue
        return False

    def answer_questions(self) -> list[FieldFillResult]:
        """The shared cross-ATS sweep (`ATSAdapter._fill_known_questions`) —
        labels, unlabeled questions recovered from nearby text, the answer
        engine, checkbox groups, consent checkboxes.

        Nothing Workday-specific is needed here, and that is the point: the
        Application Questions and Voluntary Disclosures pages are ordinary
        labeled forms, and every pass that already works on Greenhouse and
        Lever works on them unchanged."""
        return self._fill_known_questions()

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def submit_application(self) -> bool:
        """Clicks the review page's Submit button.

        Only ever reached through `decide_action`, which cannot return
        AUTO_SUBMIT for Workday (it is not in `PUBLIC_ATS_PLATFORMS`) — so in
        practice this runs only when a human has explicitly driven the run to
        submission."""
        button = self.find_submit_control()
        if button is None:
            logger.warning("%s: no submit control found on the review page.", self.name)
            return False
        try:
            button.scroll_into_view_if_needed(timeout=5_000)
        except PlaywrightError:
            pass
        try:
            button.click()
            return True
        except PlaywrightError as e:
            logger.warning("%s: submit click failed: %s", self.name, e)
            return False
