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

import json
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


# --- Overlays: cookie banners, consent walls, loading spinners ---------------
# `_is_inside_distraction` already stops a cookie banner's "Continue" from being
# mistaken for the form's own Next button. That is not enough on a multi-page
# application: a banner that merely SITS THERE also intercepts pointer events
# over whatever it covers, so the real Next button becomes unclickable
# ("element is not stable"/"intercepts pointer events" from Playwright) and the
# run stalls on page 1 of 5. Avoiding the banner is a read-time concern;
# dismissing it is a write-time one, and both are needed.
#
# Text is matched against the SAME distraction containers `_is_inside_distraction`
# recognises, never the page at large. That restriction is what makes it safe to
# look for words as generic as "Accept" and "I agree": the form's own required
# "I agree to the Privacy Policy" checkbox (see `automation/ats/base.py`'s
# `_fill_consent_checkboxes`) lives in the form, not in a cookie container, so
# nothing here can ever reach it.
OVERLAY_DISMISS_TEXT_CANDIDATES = [
    "Accept all", "Accept All Cookies", "Accept cookies", "Allow all", "Accept",
    "I agree", "Agree", "Got it", "Understood", "OK", "Okay",
    "No thanks", "Decline", "Reject all", "Close", "Dismiss",
]

#: Full-page "please wait" overlays. Between steps, a Workday-style SPA covers
#: the form with one of these while it fetches the next page; clicking or
#: reading through it produces either an interception error or a snapshot of the
#: OLD page, which is precisely how a multi-page run silently refills page 1.
LOADING_OVERLAY_HINTS = [
    "loading", "spinner", "progress-overlay", "busy-indicator", "please-wait",
]
_LOADING_OVERLAY_SELECTOR = ", ".join(
    f"[class*='{hint}' i], [id*='{hint}' i], [data-automation-id*='{hint}' i]"
    for hint in LOADING_OVERLAY_HINTS
)
#: A cookie banner is one container with many nested elements, every one of
#: which matches `_DISTRACTION_CLOSEST_SELECTOR`. Only the outermost few are
#: worth inspecting — this caps the work, it isn't a limit on how many distinct
#: banners can be dismissed across repeated calls.
_MAX_OVERLAY_CONTAINERS = 12
_OVERLAY_CLICK_TIMEOUT_MS = 2_000


def dismiss_overlays(page: Page) -> list[str]:
    """Clicks the accept/close control of any visible cookie/consent/newsletter/
    chat overlay, returning a short description of each one dismissed.

    Best-effort and never raises: an overlay that can't be dismissed is left
    alone, and the caller's next click falls back to a JS click that ignores
    interception anyway. Safe to call repeatedly — once a banner is gone it is
    no longer visible, so subsequent calls are no-ops."""
    dismissed: list[str] = []
    try:
        containers = page.locator(_DISTRACTION_CLOSEST_SELECTOR).all()
    except PlaywrightError:
        return dismissed

    for container in containers[:_MAX_OVERLAY_CONTAINERS]:
        try:
            if not container.is_visible():
                continue
        except PlaywrightError:
            continue

        for text in OVERLAY_DISMISS_TEXT_CANDIDATES:
            try:
                button = container.locator(_CLICKABLE_SELECTOR).filter(
                    has_text=re.compile(rf"^\s*{re.escape(text)}\s*$", re.IGNORECASE)
                )
                if button.count() == 0:
                    continue
                target = button.first
                if not (target.is_visible() and target.is_enabled()):
                    continue
                target.click(timeout=_OVERLAY_CLICK_TIMEOUT_MS)
            except PlaywrightError:
                continue
            logger.info("Dismissed an overlay by clicking %r.", text)
            dismissed.append(text)
            break  # this container is handled; don't click it twice

    return dismissed


def has_loading_overlay(page: Page) -> bool:
    """Whether a visible "loading"/spinner overlay is currently covering the
    page. A hidden one (every SPA ships one permanently in the DOM) does not
    count — same visibility-not-presence rule as `page_has_captcha`."""
    try:
        candidates = page.locator(_LOADING_OVERLAY_SELECTOR).all()
    except PlaywrightError:
        return False
    for node in candidates:
        try:
            if node.is_visible():
                return True
        except PlaywrightError:
            continue
    return False


def wait_for_overlays_to_clear(page: Page, *, timeout_ms: int = 15_000) -> bool:
    """Waits for every visible loading overlay to disappear. Returns whether the
    page came clear within `timeout_ms` — `False` is informational (the caller
    proceeds anyway), never an error: a permanently-visible element that merely
    has "loading" in its class name must not be able to stall a run forever."""
    deadline_polls = max(1, timeout_ms // 250)
    for _ in range(deadline_polls):
        if not has_loading_overlay(page):
            return True
        try:
            page.wait_for_timeout(250)
        except PlaywrightError:
            return False
    logger.debug("A loading overlay was still visible after %dms — continuing anyway.", timeout_ms)
    return False


# --- Where am I in a multi-page application? ---------------------------------
#: "Step 2 of 5", "Page 3 of 4", "2/5". Read from the page's own progress UI —
#: the only place that knows how long an application actually is. Never used to
#: DECIDE anything (the number of pages is not assumed anywhere); it is a
#: navigation signal — a step indicator that changed proves the form advanced —
#: and the single most useful thing to have in a run's log.
_STEP_INDICATOR_RE = re.compile(
    r"(?:step|page|section)\s*(\d{1,2})\s*(?:of|/)\s*(\d{1,2})", re.IGNORECASE
)
_BARE_FRACTION_RE = re.compile(r"\b(\d{1,2})\s*/\s*(\d{1,2})\b")
_STEP_INDICATOR_SELECTOR = (
    "[aria-current='step'], [data-automation-id*='progressBar' i], "
    "[class*='progress' i], [class*='step' i], [role='progressbar']"
)
_MAX_STEP_INDICATOR_TEXT_LEN = 120


def find_step_indicator(page: Page) -> str:
    """The page's own "Step 2 of 5"-style progress text, or `""` when the form
    doesn't show one (most single-page ATS forms don't)."""
    try:
        nodes = page.locator(_STEP_INDICATOR_SELECTOR).all()
    except PlaywrightError:
        nodes = []

    for node in nodes[:20]:
        try:
            if not node.is_visible():
                continue
            text = " ".join((node.inner_text(timeout=1_000) or "").split())
        except PlaywrightError:
            continue
        if not text or len(text) > _MAX_STEP_INDICATOR_TEXT_LEN:
            continue
        match = _STEP_INDICATOR_RE.search(text) or _BARE_FRACTION_RE.search(text)
        if match:
            return match.group(0)

    match = _STEP_INDICATOR_RE.search(_visible_page_text(page))
    return match.group(0) if match else ""


#: The last page before submission on a long application: everything already
#: entered, shown back for a final look. Recognising it matters because it is
#: where the run must STOP and hand over — an application is never submitted
#: from here without explicit authorization (see `decide_action`).
REVIEW_PAGE_TEXT_PATTERNS = [
    "review your application", "review and submit", "review & submit",
    "please review your", "review the information", "review your information",
    "almost done", "summary of your application",
]
_HEADING_SELECTOR = "h1, h2, [role='heading'], legend, [data-automation-id='pageHeader']"
_MAX_HEADING_LEN = 200


def find_page_heading(page: Page) -> str:
    """The current page's own heading — what a human would call this step ("My
    Experience", "Voluntary Disclosures"). `""` when the page has none.

    Worth having for two reasons: it is the most legible thing a multi-page run
    can put in its log, and a heading that changed is strong evidence the form
    actually advanced (see `automation/applications/page_navigator.py`)."""
    try:
        nodes = page.locator(_HEADING_SELECTOR).all()
    except PlaywrightError:
        return ""
    for node in nodes[:10]:
        try:
            if not node.is_visible():
                continue
            text = " ".join((node.inner_text(timeout=1_000) or "").split())
        except PlaywrightError:
            continue
        if text and len(text) <= _MAX_HEADING_LEN:
            return text
    return ""


def looks_like_review_page(page: Page) -> bool:
    """Whether the current page is the final review/summary step."""
    heading = find_page_heading(page).lower()
    text = _visible_page_text(page)
    for pattern in REVIEW_PAGE_TEXT_PATTERNS:
        if pattern in heading or pattern in text:
            return True
    return heading.strip() in ("review", "review your application", "summary")


def find_submit_button(page: Page) -> Locator | None:
    """Returns the page's final submit control (see
    `SUBMIT_BUTTON_TEXT_CANDIDATES`), or `None` if none is found."""
    return _find_button_by_text(page, SUBMIT_BUTTON_TEXT_CANDIDATES)


# --- "Apply from Job Link": entering an application from a job LISTING page --
# A pasted job URL is often a job DESCRIPTION page (a company careers page, an
# aggregator listing) that sits in front of the real application form —
# reached only by clicking its own "Apply"/"Apply Now"/"Start Application"
# control. Distinct from `find_submit_button` above: that one finds the FINAL
# control on an already-open application FORM (`SUBMIT_BUTTON_TEXT_CANDIDATES`
# even includes the bare word "Apply" for that reason — some ATS's last button
# literally says "Apply"). This one finds the ENTRY control on a page that has
# no form fields yet. The two are never searched for on the same page in the
# same call, so the text overlap between them causes no ambiguity in practice.
#
# More specific phrases first — same ordering rule `NEXT_BUTTON_TEXT_CANDIDATES`
# uses, since `_find_button_by_text`'s loose (substring) tier stops at the
# first candidate that matches anything, and a bare "Apply" would otherwise
# take priority over the page's own more specific wording.
APPLY_ENTRY_BUTTON_TEXT_CANDIDATES = [
    "Apply Now", "Apply for this Job", "Apply for this job", "Apply for this Position",
    "Apply for this position", "Start Application", "Start Your Application",
    "Begin Application", "Apply Online", "Apply Today", "Apply",
]


def find_apply_entry_button(page: Page) -> Locator | None:
    """The "Apply"/"Apply Now"/"Start Application" control on a job LISTING
    page — the first click of an "Apply from Job Link" run, before any ATS
    has even been identified. `None` if this doesn't look like a listing page
    at all (already an application form, or neither)."""
    return _find_button_by_text(page, APPLY_ENTRY_BUTTON_TEXT_CANDIDATES)


# --- Job posting metadata: title/company, for a pasted link with no hints ---
# Read-only, best-effort, and NEVER a guess: only structured signals a real
# posting itself publishes are used, in order of reliability. A field that
# can't be confidently read stays `None` — the same "never fabricate personal
# facts" discipline the answer engine applies, extended to job metadata.
_JOB_POSTING_JSONLD_SELECTOR = 'script[type="application/ld+json"]'
_MAX_JSONLD_SCRIPTS_CHECKED = 10


def _job_posting_from_json_ld(page: Page) -> tuple[str | None, str | None]:
    """Tier 1: schema.org `JobPosting` structured data
    (https://schema.org/JobPosting) — the same markup Google for Jobs relies
    on, and the single most reliable signal since it's machine-readable by
    design. Most real ATS/job-board postings (Greenhouse, Lever, Workday,
    LinkedIn, Indeed, and most company careers pages) emit it purely for SEO."""
    try:
        scripts = page.locator(_JOB_POSTING_JSONLD_SELECTOR).all()
    except PlaywrightError:
        return None, None

    for node in scripts[:_MAX_JSONLD_SCRIPTS_CHECKED]:
        try:
            raw = node.text_content(timeout=1_000) or ""
        except PlaywrightError:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue

        candidates = data if isinstance(data, list) else [data]
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("@type")
            types = entry_type if isinstance(entry_type, list) else [entry_type]
            if not any(str(t).lower() == "jobposting" for t in types if t):
                continue
            title = entry.get("title")
            org = entry.get("hiringOrganization")
            company = org.get("name") if isinstance(org, dict) else org
            title = title.strip() if isinstance(title, str) and title.strip() else None
            company = company.strip() if isinstance(company, str) and company.strip() else None
            if title or company:
                return title, company

    return None, None


def _job_posting_from_open_graph(page: Page) -> tuple[str | None, str | None]:
    """Tier 2: Open Graph meta tags (`og:title`/`og:site_name`) — a common
    fallback for pages without JobPosting structured data, though less
    reliable (`og:title` is often the page's marketing title, not
    necessarily the exact role name, and `og:site_name` is the SITE's brand,
    which usually but not always matches the hiring company)."""
    title = company = None
    try:
        title_meta = page.locator('meta[property="og:title"]').first
        if title_meta.count() > 0:
            title = (title_meta.get_attribute("content") or "").strip() or None
    except PlaywrightError:
        pass
    try:
        site_meta = page.locator('meta[property="og:site_name"]').first
        if site_meta.count() > 0:
            company = (site_meta.get_attribute("content") or "").strip() or None
    except PlaywrightError:
        pass
    return title, company


def find_job_posting_title_and_company(page: Page) -> tuple[str | None, str | None]:
    """Best-effort `(title, company)` for whatever job posting `page` shows —
    used to fill in `Application.company`/`.position` when the caller pasted
    a bare job URL with no hints of its own (§"Apply from Job Link"). Tries
    schema.org JobPosting data first, then Open Graph tags; returns
    `(None, None)` fields it can't confidently read rather than ever
    guessing one from the page's plain `<title>` (too unreliable — company
    names in a browser tab title are inconsistently formatted across sites)."""
    title, company = _job_posting_from_json_ld(page)
    if title and company:
        return title, company
    fallback_title, fallback_company = _job_posting_from_open_graph(page)
    return title or fallback_title, company or fallback_company


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


# --- Waiting for a client-rendered form to be ready ---------------------------
# `page.goto(..., wait_until="domcontentloaded")` returns as soon as the
# server's HTML is parsed, which on a modern ATS is BEFORE the React/Remix app
# has hydrated it. Filling in that window is not merely early, it can be
# silently undone: Greenhouse's `job-boards.*` board logs React hydration
# errors ("React recovered from an error during hydration") and re-creates part
# of the form's DOM as it recovers, discarding anything already put into the
# affected elements.
#
# That was observed live, not theorized: on a real Greenhouse posting the
# résumé was attached at t=4.3s and verified attached (`input.files.length ==
# 1`); hydration recovered at t=4.9s; from t=6.3s onward the same `#resume`
# input was empty again and the form showed no attachment, so the run finished
# with a "resume uploaded" checkpoint and no résumé on the application.
#
# Hence: settle first, and (see `ATSAdapter.ensure_resume_attached`) re-check
# the résumé at the END rather than trusting a verification made seconds
# before hydration ran.
FORM_READY_TIMEOUT_MS = 15_000


def wait_for_form_ready(page: Page, *, timeout_ms: int = FORM_READY_TIMEOUT_MS) -> None:
    """Best-effort wait for a client-rendered application form to finish
    loading and hydrating, before anything is typed into it.

    Deliberately never raises and never fails a run: every wait here is a
    bounded optimization, and a page that legitimately keeps a connection open
    (analytics beacons, a chat widget's websocket, a polling request) would
    otherwise time out on `networkidle` forever. Timing out just means filling
    starts anyway — exactly the pre-existing behavior."""
    for state in ("load", "networkidle"):
        try:
            page.wait_for_load_state(state, timeout=timeout_ms)
        except PlaywrightError as e:
            logger.debug("wait_for_form_ready: %r state not reached (%s) — continuing.", state, e)


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


def find_unfilled_required_field_locators(page: Page) -> list[tuple[str, Locator]]:
    """`find_unfilled_required_fields` (below), but keeping each field's
    `Locator` alongside its human-readable name.

    Same scan, two callers with different needs: the flow manager only ever
    wanted the names for its `manual_required` reason string, while the vision
    fallback pass (`automation/forms/vision_fallback.py`) has to actually
    screenshot and then FILL these fields, which needs the locator. Kept as
    one scan rather than two so "what counts as an unfilled required field"
    can never drift between the check that reports a problem and the pass that
    tries to fix it."""
    missing: list[tuple[str, Locator]] = []
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
            missing.append((_describe_unfilled_field(field), field))

    return missing


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
    per-adapter code at all.

    Note this reads the CONTROL's own value, which is why a custom widget
    whose visible selection lives outside its backing input (a react-select
    combobox, a country picker) can appear here while the page plainly shows a
    value. That's deliberately not "fixed" by loosening the check — under-
    reporting a genuinely empty required field is the dangerous direction —
    and it's part of why the vision fallback pass is told to answer `null`
    for any field whose screenshot already shows a value."""
    return [name for name, _locator in find_unfilled_required_field_locators(page)]


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
#
# ---------------------------------------------------------------------------
# UNVALIDATED AGAINST A REAL POST-SUBMIT PAGE — resolve before enabling
# autopilot against live Greenhouse/Lever postings.
# ---------------------------------------------------------------------------
# The 12 text patterns and 5 URL hints below have NEVER been checked against a
# real Greenhouse or Lever confirmation page. They were derived from the specs
# and from ATS documentation/wording conventions, not from a captured live run:
# development deliberately never submitted a live application, so no genuine
# post-submit DOM was ever recorded. Every test that exercises this code
# supplies fixture HTML written to match these patterns, which means the tests
# prove the MATCHING works — not that the patterns are the wording real ATSs
# actually use.
#
# The specific risk: Greenhouse's `job-boards.*` board is a SPA and may render
# its confirmation WITHOUT changing the URL, in which case the URL hints never
# fire and the text patterns are the only signal left. If the real wording
# isn't one of the 12 (or is an image/aria-only banner), a genuinely successful
# autopilot submit lands in `needs_review` with the "cannot prove it landed"
# warning — a false negative. That direction is the safe one by design (it
# never claims `applied` without evidence), but at scale it would make
# autopilot look broken and push every real success through manual review.
#
# How to resolve this properly: capture a small number of REAL confirmation
# pages — with the user's consent, on the user's own genuine applications —
# and check the actual wording, URL, and DOM against this list. Do NOT resolve
# it by guessing extra patterns now: unvalidated additions widen the surface
# for a FALSE POSITIVE (reporting `applied` when the ATS rejected the
# submission server-side), which is the far worse failure per this section's
# header comment, and adds no real coverage.
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
