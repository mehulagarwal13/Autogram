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

# Some postings (Lever and others) show a one-click "Apply with LinkedIn"/
# "Continue with LinkedIn" autofill shortcut alongside — often ABOVE, in DOM
# order — the real Submit/Next control. This app deliberately never uses
# that shortcut (manual-form-only, no third-party autofill/OAuth — see
# ARCHITECTURE.md's "no password harvesting" principle), but "Apply" and
# "Continue" are both real substrings of it, so without this exclusion the
# loose fallback tier in `_find_button_by_text` below could happily click
# "Apply with LinkedIn" instead of the real control whenever the real one's
# accessible name doesn't happen to match a more-specific candidate first.
# `has_not_text` filters it out at the query level, before DOM order/
# visibility ever gets a chance to pick it.
#
# The same reasoning extends to every other third-party-auth and
# account-creation control ("Apply with Indeed", "Continue with Google",
# "Sign in to apply"): each embeds a word this module actively searches for,
# and clicking one abandons the manual form — or worse, creates an account,
# which is a thing this app must never do automatically.
#
# Only text that can co-occur with a Next/Continue/Submit/Apply/Send/Attach
# candidate needs to be here; a bare "Register" button is never a candidate
# in the first place, so it can't be reached regardless.
_THIRD_PARTY_AUTOFILL_TEXT = re.compile(
    r"linkedin|indeed|glassdoor|\bgoogle\b|\bgithub\b|\bfacebook\b|\bapple\b"
    r"|sign\s*in|sign\s*up|log\s*in|login|create\s+(an\s+)?account|register",
    re.IGNORECASE,
)

# Text-matching alone can't catch a cookie banner or newsletter modal whose
# button says exactly "Continue" — indistinguishable from a real form step by
# label, but clicking it dismisses an overlay (or navigates away) while the
# flow manager counts it as having advanced a form step. So candidates are
# additionally rejected structurally, by the container they live in.
#
# Deliberately excludes a bare "ad" (it would match "header", "shadow",
# "loading") and a bare "consent" (a form's OWN privacy-consent section
# legitimately uses that word, and its submit button may sit inside it).
DISTRACTION_CONTAINER_HINTS = [
    "cookie", "gdpr", "newsletter", "subscribe", "chat-widget", "chat-bubble",
    "intercom", "drift-", "zendesk", "advertisement", "ad-banner", "ad-slot",
]
_DISTRACTION_CLOSEST_SELECTOR = ", ".join(
    f"[class*='{hint}' i], [id*='{hint}' i]" for hint in DISTRACTION_CONTAINER_HINTS
)


def _is_inside_distraction(candidate: Locator) -> bool:
    """Whether `candidate` sits inside a cookie/newsletter/chat/ad container.
    Best-effort: any DOM error just means "not a distraction", so a broken
    `closest()` can never make a real submit button unreachable."""
    try:
        return bool(candidate.evaluate(
            "(el, sel) => !!el.closest(sel)", _DISTRACTION_CLOSEST_SELECTOR,
        ))
    except PlaywrightError:
        return False


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
            if not (candidate.is_visible() and candidate.is_enabled()):
                continue
        except PlaywrightError:
            # A racy/detached element mid-render is just "not usable yet",
            # not a reason to fail the whole lookup.
            continue
        if _is_inside_distraction(candidate):
            logger.debug("Skipping a control inside a cookie/newsletter/chat/ad container.")
            continue
        return candidate
    return None


def _find_button_by_text(page: Page, candidates: list[str]) -> Locator | None:
    """Tries each candidate text, exact accessible-name match first (most
    reliable — works for a <button>, an <a role="button">, or an
    <input type=submit/button>), then falls back to a substring,
    case-insensitive text match across common clickable elements. Never
    matches a LinkedIn autofill control either way — see
    `_THIRD_PARTY_AUTOFILL_TEXT`."""
    for text in candidates:
        exact = page.get_by_role(
            "button", name=re.compile(rf"^\s*{re.escape(text)}\s*$", re.IGNORECASE)
        ).filter(has_not_text=_THIRD_PARTY_AUTOFILL_TEXT)
        found = _first_visible_enabled(exact)
        if found:
            return found

    for text in candidates:
        loose = page.locator(_CLICKABLE_SELECTOR).filter(
            has_text=re.compile(re.escape(text), re.IGNORECASE)
        ).filter(has_not_text=_THIRD_PARTY_AUTOFILL_TEXT)
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


# Selects every field that could plausibly be "required" in the profile-
# mapping sense — same exclusions `automation/ats/base.py`'s name/placeholder
# pass uses (hidden/submit/button aren't real form fields; file inputs are
# handled by upload_resume, not this generic scan).
_REQUIRED_CANDIDATE_SELECTOR = (
    "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='file']), "
    "select, textarea"
)


def _looks_required(field: Locator) -> bool:
    try:
        return (
            field.get_attribute("required") is not None
            or (field.get_attribute("aria-required") or "").lower() == "true"
        )
    except PlaywrightError:
        return False


def _describe_unfilled_field(field: Locator) -> str:
    """Best-effort human-readable name for a `failure_reason`/error-log
    message — prefers whatever the page itself calls the field (aria-label,
    placeholder, name, id, in that order of how likely each is to make sense
    to a person reading it later) over a raw selector."""
    for attribute in ("aria-label", "placeholder", "name", "id"):
        try:
            value = field.get_attribute(attribute)
        except PlaywrightError:
            value = None
        if value:
            return value
    return "an unnamed field"


def find_unfilled_required_fields(page: Page) -> list[str]:
    """Scans the page's CURRENT DOM state — after every fill pass an adapter
    runs has already had its turn — for any visible, required
    input/select/textarea/checkbox/radio that's still empty/unchecked.
    Returns a human-readable name per such field (empty if none). Used by
    `ApplicationFlowManager` to decide `manual_required` (with a specific
    reason) instead of guessing from an aggregate confidence score alone: a
    required field with genuinely no value in the candidate's profile isn't
    "low confidence," it's "automation cannot proceed without a human."

    Deliberately a fresh DOM scan rather than something every adapter/fill
    pass has to separately track and report — this is what lets the check
    work identically across every ATS platform and every fill path with no
    per-adapter code at all."""
    missing: list[str] = []
    try:
        candidates = page.locator(_REQUIRED_CANDIDATE_SELECTOR).all()
    except PlaywrightError:
        return missing

    for field in candidates:
        try:
            if not field.is_visible() or not _looks_required(field):
                continue
            input_type = (field.get_attribute("type") or "").lower()
        except PlaywrightError:
            continue

        try:
            if input_type in ("checkbox", "radio"):
                has_value = field.is_checked()
            else:
                has_value = bool((field.input_value() or "").strip())
        except PlaywrightError:
            continue

        if not has_value:
            missing.append(_describe_unfilled_field(field))

    return missing


# --- Submission confirmation (§10 of both specs) ------------------------------
# Clicking submit succeeding is NOT the same as the application being accepted:
# the click can land, the page can POST, and the ATS can still reject it
# server-side (seen live on a real Lever posting: "There was an error verifying
# your application. Please try again."). Reporting that run as "applied" is the
# worst available outcome, because the durable record then says the candidate
# applied when they did not — and idempotency will refuse to try again.
#
# So a submission is only ever reported as applied on POSITIVE evidence: the URL
# moved to a confirmation route, a recognizable success phrase appeared, or the
# ATS handed back an application/reference identifier.
SUBMISSION_CONFIRMATION_URL_HINTS = ["thank", "confirmation", "confirmed", "/success", "submitted"]
SUBMISSION_CONFIRMATION_TEXT_PATTERNS = [
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "thank you for your interest",
    "application received",
    "we have received your application",
    "we've received your application",
    "your application has been received",
    "application submitted",
    "your application has been submitted",
    "successfully submitted",
    "application complete",
]
# "Application ID: 4f21c" / "Reference number - 88213" / "Confirmation # ABC-9"
_APPLICATION_REFERENCE_RE = re.compile(
    r"(application|reference|confirmation)\s*(id|number|no\.?|#)\s*[:\-#]?\s*[a-z0-9][a-z0-9\-]{2,}",
    re.IGNORECASE,
)


def _visible_page_text(page: Page) -> str:
    """The page's rendered text, whitespace-collapsed and lower-cased. Uses
    `inner_text` (not `text_content`) deliberately: `inner_text` reflects what
    is actually VISIBLE, so a hidden success template or a `display:none`
    error node baked into the markup can't produce a false match."""
    try:
        raw = page.locator("body").inner_text(timeout=5_000)
    except PlaywrightError:
        return ""
    return " ".join(raw.split()).lower()


def find_submission_confirmation(page: Page) -> str | None:
    """Positive evidence that a submitted application was actually accepted,
    as a short human-readable string for the run log — or `None` if no such
    evidence is present. `None` must never be treated as success; see this
    section's header comment.

    Only meaningful when called AFTER a submit click: several of these phrases
    ("thank you for your interest") can legitimately appear in a job posting's
    own body text, so the caller — not this function — owns the ordering."""
    try:
        url = (page.url or "").lower()
    except PlaywrightError:
        url = ""
    for hint in SUBMISSION_CONFIRMATION_URL_HINTS:
        if hint in url:
            return f"confirmation URL (matched {hint!r})"

    text = _visible_page_text(page)
    if not text:
        return None

    for pattern in SUBMISSION_CONFIRMATION_TEXT_PATTERNS:
        if pattern in text:
            return f"success message ({pattern!r})"

    reference = _APPLICATION_REFERENCE_RE.search(text)
    if reference:
        return f"application reference ({reference.group(0)!r})"

    return None


def wait_for_submission_confirmation(page: Page, *, timeout_ms: int = 15_000) -> str | None:
    """`find_submission_confirmation` with a bounded wait, since a submit
    usually triggers navigation or an async POST that resolves a moment later.
    Polls rather than waiting on a specific selector because every ATS renders
    its confirmation differently — and a fixed sleep would either be too short
    for a slow POST or waste time on a fast one."""
    deadline_polls = max(1, timeout_ms // 500)
    for _ in range(deadline_polls):
        confirmation = find_submission_confirmation(page)
        if confirmation:
            return confirmation
        try:
            page.wait_for_timeout(500)
        except PlaywrightError:
            break
    return None


# --- Validation errors (§10 / VALIDATION of both specs) -----------------------
# Both specs gate submission on "zero visible validation errors". Class/id
# substrings rather than one ATS's exact markup, same tiering idea as
# CAPTCHA_HINTS, plus the two standard accessible signals.
VALIDATION_ERROR_HINTS = ["error", "invalid", "field-error", "has-error", "helper-text-error"]
_ACCESSIBLE_ERROR_SELECTOR = "[role='alert'], [aria-invalid='true']"
# Guards against treating a whole error-styled page wrapper as one message.
_MAX_VALIDATION_ERROR_TEXT_LEN = 300


def find_validation_errors(page: Page) -> list[str]:
    """Visible validation-error messages currently on the page (empty if the
    form is clean). Requires each candidate to be BOTH visible and to carry
    non-empty text: real ATS markup routinely ships permanently-present,
    empty error containers that would otherwise register as failures on every
    single run.

    An `aria-invalid="true"` field carries no text of its own, so it's
    reported by field name via `_describe_unfilled_field` instead."""
    messages: list[str] = []
    seen: set[str] = set()

    selector = ", ".join(f"[class*='{hint}' i]" for hint in VALIDATION_ERROR_HINTS)
    try:
        candidates = page.locator(selector).all()
    except PlaywrightError:
        candidates = []

    for node in candidates:
        try:
            if not node.is_visible():
                continue
            text = " ".join((node.inner_text(timeout=2_000) or "").split())
        except PlaywrightError:
            continue
        if not text or len(text) > _MAX_VALIDATION_ERROR_TEXT_LEN:
            continue
        if text.lower() not in seen:
            seen.add(text.lower())
            messages.append(text)

    try:
        accessible = page.locator(_ACCESSIBLE_ERROR_SELECTOR).all()
    except PlaywrightError:
        accessible = []

    for node in accessible:
        try:
            if not node.is_visible():
                continue
            text = " ".join((node.inner_text(timeout=2_000) or "").split())
            if not text or len(text) > _MAX_VALIDATION_ERROR_TEXT_LEN:
                # An invalid INPUT has no text — name the field instead.
                text = f"{_describe_unfilled_field(node)} is marked invalid"
        except PlaywrightError:
            continue
        if text.lower() not in seen:
            seen.add(text.lower())
            messages.append(text)

    return messages


# --- Human-in-the-loop gates beyond CAPTCHA (§9 / HUMAN-IN-THE-LOOP) ---------
# Both specs require pausing for a human on OTP/MFA, email or SMS
# verification, identity verification, payment, and any login/registration
# wall — not just CAPTCHA, which was the only one previously detected. None of
# these may ever be auto-answered or routed around; the run stops and a person
# takes over.
#
# Each entry is (gate_name, css_selector, text_patterns). A gate fires on a
# visible structural match OR a visible text match, so it works whether the
# ATS marks the field up semantically or only labels it in prose.
_HUMAN_GATES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "login or registration required",
        # A genuine application form never needs a password. Its presence
        # means an account wall, which this app must never transact with.
        "input[type='password']",
        ("sign in to apply", "log in to apply", "sign in to continue",
         "create an account to apply", "please sign in", "please log in"),
    ),
    (
        "one-time passcode / multi-factor authentication",
        "input[autocomplete='one-time-code'], input[name*='otp' i], input[id*='otp' i], "
        "input[name*='verification_code' i], input[name*='verificationcode' i]",
        ("one-time password", "one time passcode", "verification code",
         "two-factor", "two factor", "2fa", "authenticator app",
         "enter the code we sent", "enter the 6-digit"),
    ),
    (
        "email or SMS verification required",
        "",
        ("verify your email", "verify your phone", "verify your mobile",
         "we sent a code to", "we've sent a verification",
         "confirm your email address to continue"),
    ),
    (
        "identity or document verification required",
        "",
        ("identity verification", "verify your identity",
         "government-issued id", "government issued id",
         "upload a photo of your id", "proof of identity"),
    ),
    (
        "payment or billing details requested",
        "input[autocomplete='cc-number'], input[name*='card_number' i], input[name*='cardnumber' i]",
        ("credit card number", "payment details", "billing information",
         "enter your card", "subscription required"),
    ),
)


def find_human_gate(page: Page) -> str | None:
    """The first human-only gate present on the page (see `_HUMAN_GATES`), as
    a human-readable reason — or `None` if none is present. CAPTCHA is
    handled separately by `page_has_captcha`, so callers check both.

    Structural matches require visibility, for the same reason
    `page_has_captcha` does: ATS markup routinely ships hidden inputs (a
    dormant password field on a combined sign-in/apply template, for
    instance) that a presence-only check would misread as a live wall,
    blocking every run against that posting."""
    text = _visible_page_text(page)

    for gate_name, selector, patterns in _HUMAN_GATES:
        if selector:
            try:
                candidates = page.locator(selector).all()
            except PlaywrightError:
                candidates = []
            for node in candidates:
                try:
                    if node.is_visible():
                        logger.info("Human gate detected (%s) via markup on %s", gate_name, page.url)
                        return gate_name
                except PlaywrightError:
                    continue

        for pattern in patterns:
            if pattern in text:
                logger.info("Human gate detected (%s) via text %r", gate_name, pattern)
                return gate_name

    return None


def page_has_captcha(page: Page) -> bool:
    """Heuristic CAPTCHA detection: an actually VISIBLE captcha-provider
    iframe/widget carrying a captcha-flavored class/id/src (see
    `CAPTCHA_HINTS`) — not merely one present in the DOM. Used to trigger
    human-in-the-loop (§9) instead of blindly attempting to submit —
    ARCHITECTURE.md is explicit that CAPTCHA/OTP must never be bypassed.

    Visibility is the deciding factor on purpose: most ATS postings (Lever
    included) embed an invisible/passive hCaptcha or reCAPTCHA purely as a
    background bot-deterrent — zero-size, `display:none`-equivalent, never
    shown to a normal applicant — that only ever challenges a visitor its
    risk engine flags as suspicious. Treating that dormant widget's mere
    presence as a blocking CAPTCHA (checking DOM presence alone, as this
    used to) stops every run against such a page before it ever tries to
    fill anything, even though there is nothing for a human to see or solve
    either — a false positive, not §9 caution."""
    for hint in CAPTCHA_HINTS:
        selector = f"iframe[src*='{hint}' i], [class*='{hint}' i], [id*='{hint}' i]"
        locator = page.locator(selector)
        try:
            count = locator.count()
        except PlaywrightError:
            continue
        for i in range(count):
            try:
                if locator.nth(i).is_visible():
                    logger.info("CAPTCHA hint '%s' detected (visible) on %s", hint, page.url)
                    return True
            except PlaywrightError:
                continue
    return False
