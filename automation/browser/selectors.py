"""
Common cross-ATS Playwright selector helpers — Phase 2/3 (see ARCHITECTURE.md).

Shared, low-level DOM query helpers used by every ATS adapter (Phase 4+) so
they don't each re-implement the same generic lookups: the active
"Next"/"Continue"/"Submit" control, a resume file-upload input, or a CAPTCHA
challenge. Platform-specific selectors (e.g. a Greenhouse-specific field ID)
still belong in that adapter's own module (`automation/ats/<name>/`) — this
file only holds patterns generic enough to be useful across ATS platforms.
"""

from __future__ import annotations

import logging
import re

from playwright.sync_api import Error as PlaywrightError, Locator, Page

logger = logging.getLogger(__name__)

# Heuristic text used across ATS platforms to find navigation controls.
# Checked in this order — earlier entries are tried first.
NEXT_BUTTON_TEXT_CANDIDATES = ["Next", "Continue", "Save and Continue", "Save & Continue"]
SUBMIT_BUTTON_TEXT_CANDIDATES = ["Submit Application", "Submit", "Apply", "Send Application", "Send"]
# Some ATS UIs (Greenhouse's "Attach / Dropbox / Google Drive / Enter
# manually" pattern) only reveal the real <input type=file> after a visible
# trigger is clicked — checked when find_file_upload_input() comes up empty.
UPLOAD_TRIGGER_TEXT_CANDIDATES = ["Attach", "Attach Resume", "Upload Resume", "Upload File", "Choose File"]
FILE_INPUT_SELECTOR = "input[type='file']"
# Preferred over a bare first-match when a page has more than one file input
# (a resume field alongside a separate cover-letter/"additional documents"
# one) — generic hints, not any one company's field naming.
RESUME_FILE_INPUT_HINTS = ["resume", "cv", "curriculum"]
CAPTCHA_HINTS = ["captcha", "recaptcha", "hcaptcha", "cf-turnstile", "turnstile"]

# ATS forms mix real <button>s, <a> styled as buttons, and
# <input type=submit/button> — checking all four covers the common cases
# for the loose, text-based fallback search below.
_CLICKABLE_SELECTOR = "button, a, input[type='submit'], input[type='button']"


def _first_visible_enabled(locator: Locator) -> Locator | None:
    """Playwright locators can match multiple elements; calling an action on
    a multi-match locator raises a strict-mode violation. This walks the
    matches in DOM order and returns the first one that's actually visible
    and enabled — skipping hidden duplicate markup (common in ATS templates:
    a mobile-only copy of the same button, a disabled placeholder before JS
    hydrates, etc.) — or `None` if nothing usable was found."""
    count = locator.count()
    for i in range(count):
        candidate = locator.nth(i)
        try:
            if candidate.is_visible() and candidate.is_enabled():
                return candidate
        except PlaywrightError:
            # A racy/detached element mid-render is just "not usable yet",
            # not a reason to fail the whole lookup.
            continue
    return None


def _find_button_by_text(page: Page, candidates: list[str]) -> Locator | None:
    """Tries each candidate text, exact accessible-name match first (most
    reliable — works for a <button>, an <a role="button">, or an
    <input type=submit/button>), then falls back to a substring,
    case-insensitive text match across common clickable elements."""
    for text in candidates:
        exact = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(text)}\s*$", re.IGNORECASE))
        found = _first_visible_enabled(exact)
        if found:
            return found

    for text in candidates:
        loose = page.locator(_CLICKABLE_SELECTOR).filter(has_text=re.compile(re.escape(text), re.IGNORECASE))
        found = _first_visible_enabled(loose)
        if found:
            return found

    return None


def find_next_button(page: Page) -> Locator | None:
    """Returns the page's "Next"/"Continue" control (see
    `NEXT_BUTTON_TEXT_CANDIDATES`), or `None` if this looks like the final
    step of the form. Callers (`ApplicationFlowManager`, Phase 4) treat
    `None` as "no more steps — try submit instead."""
    return _find_button_by_text(page, NEXT_BUTTON_TEXT_CANDIDATES)


def find_submit_button(page: Page) -> Locator | None:
    """Returns the page's final submit control (see
    `SUBMIT_BUTTON_TEXT_CANDIDATES`), or `None` if none is found."""
    return _find_button_by_text(page, SUBMIT_BUTTON_TEXT_CANDIDATES)


def find_file_upload_input(page: Page, *, prefer_hints: list[str] = RESUME_FILE_INPUT_HINTS) -> Locator | None:
    """Returns the resume upload `<input type=file>`, or `None` if the
    current page has none (e.g. a later step of a multi-page form with no
    upload field of its own). When more than one file input exists (a
    separate cover-letter/"additional documents" field alongside the resume
    one), prefers whichever one's `name`/`id`/`aria-label` mentions
    `prefer_hints` over blindly taking the first match — falls back to the
    first input if no candidate matches any hint, same as before this
    existed."""
    all_file_inputs = page.locator(FILE_INPUT_SELECTOR)
    count = all_file_inputs.count()
    if count == 0:
        return None
    if count == 1:
        return all_file_inputs.first

    for i in range(count):
        candidate = all_file_inputs.nth(i)
        try:
            haystack = " ".join(
                filter(None, [
                    candidate.get_attribute("name"),
                    candidate.get_attribute("id"),
                    candidate.get_attribute("aria-label"),
                ])
            ).lower()
        except PlaywrightError:
            continue
        if any(hint in haystack for hint in prefer_hints):
            return candidate

    return all_file_inputs.first


def find_upload_trigger_button(page: Page) -> Locator | None:
    """A visible "Attach"/"Upload" control for ATS UIs that only reveal the
    real `<input type=file>` after it's clicked. Returns `None` if nothing
    matches — callers treat that as "there's just no upload input on this
    page," same as before this existed."""
    return _find_button_by_text(page, UPLOAD_TRIGGER_TEXT_CANDIDATES)


def page_has_captcha(page: Page) -> bool:
    """Heuristic CAPTCHA detection: a captcha-provider iframe, or an element
    carrying a captcha-flavored class/id (see `CAPTCHA_HINTS`). Used to
    trigger human-in-the-loop (§9) instead of blindly attempting to submit —
    ARCHITECTURE.md is explicit that CAPTCHA/OTP must never be bypassed."""
    for hint in CAPTCHA_HINTS:
        selector = f"iframe[src*='{hint}' i], [class*='{hint}' i], [id*='{hint}' i]"
        if page.locator(selector).count() > 0:
            logger.info("CAPTCHA hint '%s' detected on %s", hint, page.url)
            return True
    return False
