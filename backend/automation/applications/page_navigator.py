"""
Page navigation for multi-page applications — "did the form actually advance?"

`ApplicationFlowManager`'s step loop used to be:

    next_control = find_next_button(page)
    next_control.click()
    # ...immediately fill again

Three things are missing from that, and each one on its own is enough to break
a 5-page Workday application:

1. **No wait.** `click()` returns the moment the click lands, not when the next
   step has rendered. On any SPA-driven ATS the next fill pass then runs against
   the previous page's DOM (or a half-mounted one), so page 2 is filled with
   page 1's fields and page 2's own fields are never seen.

2. **No proof.** If validation silently blocks the click — the overwhelmingly
   common outcome on a long form — the loop happily fills the same page again
   and clicks again, burning every iteration of its safety cap before declaring
   the page it never left to be "the final step". The application is then
   scored, and possibly handed over, on the basis of page 1 of 5.

3. **No recovery.** A cookie banner covering the button, a loading overlay
   swallowing the pointer, or a detached element mid-rerender all surface as a
   Playwright error that aborts the run, when each is individually retryable.

This module supplies the missing piece: a `PageSignature` — cheap, structural
evidence of *which* page is on screen — plus `advance_to_next_page()`, which
clicks, waits for the page to settle, and then compares signatures to decide
whether the form genuinely moved. Nothing here is ATS-specific: the signature is
built from a page's URL, title, heading, its own progress indicator, and the
identities of its visible controls, all of which every ATS has.

Deliberately NOT part of the signature: any field's *value*, and the page's body
text. Filling a form changes both, and a navigation check that fires on "the
page changed because we typed into it" would report success without ever leaving
page 1 — the exact failure this module exists to catch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as _dc_field

from playwright.sync_api import Error as PlaywrightError, Locator, Page

from automation.browser.selectors import (
    dismiss_overlays,
    find_page_heading,
    find_step_indicator,
    find_validation_errors,
    wait_for_form_ready,
    wait_for_overlays_to_clear,
)

logger = logging.getLogger(__name__)

#: How long to keep polling for the page signature to change after a Next
#: click. Generous because it covers a real server round-trip on a slow ATS;
#: the poll exits the moment the signature differs, so a fast page pays ~0.
SIGNATURE_CHANGE_TIMEOUT_MS = 20_000
_SIGNATURE_POLL_INTERVAL_MS = 250

#: Playwright's own click timeout. Shorter than its 30s default on purpose: a
#: click that hasn't landed in 10s is being intercepted by something, and the
#: recovery path below (dismiss overlays, then a direct JS click) resolves that
#: far faster than waiting out the default ever would.
_CLICK_TIMEOUT_MS = 10_000
_SCROLL_TIMEOUT_MS = 5_000

#: Substrings of the Playwright error message that mean "something is in front
#: of the button", as opposed to "the button isn't there".
_INTERCEPTION_HINTS = (
    "intercepts pointer events", "not stable", "element is not visible",
    "element is outside of the viewport", "subtree intercepts",
)

#: One JS round-trip for every visible control's identity. Identity, never
#: value: `name`/`id`/`data-automation-id`/ARIA name are what distinguish one
#: page's form from another's, and none of them change when a field is filled.
#:
#: Occurrences are numbered (`...#0`, `...#1`) so a radio group's members stay
#: distinguishable — otherwise a group growing from two options to four would
#: collapse to the same identity and read as "nothing changed".
_CONTROL_IDENTITIES_JS = """
() => {
  const selector = [
    'input:not([type="hidden"])', 'select', 'textarea',
    '[role="combobox"]', '[role="listbox"]', '[role="radiogroup"]',
    '[contenteditable="true"]',
  ].join(', ');
  const counts = new Map();
  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 1 && rect.height < 1) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const identity = el.getAttribute('data-automation-id')
      || el.getAttribute('name') || el.id
      || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
    const key = el.tagName.toLowerCase()
      + ':' + (el.getAttribute('type') || '')
      + ':' + identity;
    const seen = counts.get(key) || 0;
    counts.set(key, seen + 1);
    out.push(key + '#' + seen);
  }
  return out;
}
"""


#: A real job-application URL is short. A `data:` URL — which is what the
#: multi-page tests navigate to, and what a few ATS previews use — is the whole
#: document, and logging one per page turns a run's log into something nobody
#: will read. Truncation is purely cosmetic; comparison always uses the full URL.
_MAX_LOGGED_URL_LEN = 120


def short_url(url: str) -> str:
    """`url` trimmed to something worth putting in a log line."""
    if len(url) <= _MAX_LOGGED_URL_LEN:
        return url
    return f"{url[:_MAX_LOGGED_URL_LEN]}… ({len(url)} chars)"


@dataclass(frozen=True)
class PageSignature:
    """Structural evidence of which page of an application is on screen.

    Two signatures that compare equal mean the form did not move. That is the
    only question this type answers, and it answers it without knowing anything
    about the ATS, the page count, or the shape of the form."""

    url: str = ""
    title: str = ""
    heading: str = ""
    step_indicator: str = ""
    controls: tuple[str, ...] = ()

    @property
    def control_count(self) -> int:
        return len(self.controls)

    def differs_from(self, other: "PageSignature") -> bool:
        return (
            self.url != other.url
            or self.title != other.title
            or self.heading != other.heading
            or self.step_indicator != other.step_indicator
            or self.controls != other.controls
        )

    def newly_visible_controls(self, previous: "PageSignature") -> tuple[str, ...]:
        """Controls present here that weren't in `previous` — how a conditional
        follow-up question ("If yes, which visa do you hold?") announces itself
        after an earlier answer reveals it."""
        before = set(previous.controls)
        return tuple(control for control in self.controls if control not in before)

    def describe(self) -> str:
        """A one-line, log-friendly identification of this page."""
        parts = [part for part in (self.step_indicator, self.heading or self.title) if part]
        label = " — ".join(parts) if parts else (short_url(self.url) or "(unidentified page)")
        return f"{label} [{self.control_count} field(s)]"


@dataclass
class NavigationOutcome:
    """What one attempt to move to the next page achieved."""

    advanced: bool
    reason: str
    before: PageSignature = _dc_field(default_factory=PageSignature)
    after: PageSignature = _dc_field(default_factory=PageSignature)
    validation_errors: list[str] = _dc_field(default_factory=list)
    #: True when the click itself could not be delivered at all, as opposed to
    #: landing and being rejected by the form. The two need different responses:
    #: an undelivered click is worth retrying, a rejected one needs the
    #: validation errors fixed (or a human) first.
    click_failed: bool = False


def visible_control_identities(page: Page) -> tuple[str, ...]:
    """Identities of every visible form control, in DOM order. `()` if the page
    can't currently be read — a detached/navigating document is a transient
    state, not an error worth propagating."""
    try:
        identities = page.evaluate(_CONTROL_IDENTITIES_JS)
    except PlaywrightError as e:
        logger.debug("Could not read control identities (%s) — treating as none.", e)
        return ()
    return tuple(str(item) for item in (identities or []))


def capture_page_signature(page: Page) -> PageSignature:
    """A snapshot of which page is currently on screen. Every component is
    best-effort: a page mid-navigation yields a partial signature rather than
    raising, and a partial signature still compares correctly."""
    try:
        url = page.url or ""
    except PlaywrightError:
        url = ""
    try:
        title = page.title() or ""
    except PlaywrightError:
        title = ""
    return PageSignature(
        url=url,
        title=title,
        heading=find_page_heading(page),
        step_indicator=find_step_indicator(page),
        controls=visible_control_identities(page),
    )


def wait_for_page_settled(page: Page, *, timeout_ms: int = 15_000) -> None:
    """Waits for the page to be worth reading again: document loaded, network
    quiet, no loading overlay on top. Never raises — every wait in here is a
    bounded optimization (see `wait_for_form_ready`)."""
    wait_for_form_ready(page, timeout_ms=timeout_ms)
    wait_for_overlays_to_clear(page, timeout_ms=timeout_ms)


def click_control(page: Page, control: Locator) -> tuple[bool, str]:
    """Clicks `control`, working around the things that stop a click landing on
    a real ATS page. Returns `(clicked, detail)`.

    Escalates rather than giving up: a normal click, then — if something is
    covering the button — dismiss overlays and retry, then a direct DOM
    `.click()`, which ignores interception entirely. The last one is a genuine
    fallback and not the default because it also bypasses the checks that catch
    a disabled or off-screen button."""
    try:
        control.scroll_into_view_if_needed(timeout=_SCROLL_TIMEOUT_MS)
    except PlaywrightError as e:
        logger.debug("Could not scroll the navigation control into view (%s) — clicking anyway.", e)

    try:
        control.click(timeout=_CLICK_TIMEOUT_MS)
        return True, "clicked"
    except PlaywrightError as e:
        first_error = str(e)

    if any(hint in first_error.lower() for hint in _INTERCEPTION_HINTS):
        dismissed = dismiss_overlays(page)
        if dismissed:
            logger.info("Navigation click was blocked; dismissed %s and retrying.", ", ".join(dismissed))
        try:
            control.click(timeout=_CLICK_TIMEOUT_MS)
            return True, "clicked after dismissing an overlay"
        except PlaywrightError as e:
            first_error = str(e)

    try:
        control.evaluate("el => el.click()")
        return True, "clicked via JS fallback"
    except PlaywrightError as e:
        return False, f"click failed ({first_error}); JS fallback also failed ({e})"


def advance_to_next_page(
    page: Page,
    control: Locator,
    *,
    before: PageSignature | None = None,
    timeout_ms: int = SIGNATURE_CHANGE_TIMEOUT_MS,
) -> NavigationOutcome:
    """Clicks `control` and reports whether the application actually moved on.

    The return value distinguishes the three outcomes a caller must handle
    differently: the form advanced; the click landed but the form refused to
    advance (`validation_errors` says why, when the page says); or the click
    could not be delivered at all (`click_failed`, worth retrying).

    `before` should be captured AFTER the page has been filled — a signature
    taken before filling would include the conditional fields that filling
    reveals, and their appearance would then be misread as having navigated."""
    before = before if before is not None else capture_page_signature(page)

    clicked, detail = click_control(page, control)
    if not clicked:
        return NavigationOutcome(
            advanced=False, reason=detail, before=before, after=before, click_failed=True,
        )

    after = _wait_for_signature_change(page, before, timeout_ms=timeout_ms)
    if after.differs_from(before):
        return NavigationOutcome(
            advanced=True,
            reason=f"{detail}; advanced to {after.describe()}",
            before=before, after=after,
        )

    # The click landed and nothing moved. On a long application that is almost
    # always the form rejecting the page — ask it why before reporting back.
    errors = find_validation_errors(page)
    reason = (
        f"the form did not advance ({detail}); {len(errors)} validation error(s) reported"
        if errors else
        f"the form did not advance ({detail}) and reported no validation errors"
    )
    return NavigationOutcome(
        advanced=False, reason=reason, before=before, after=after, validation_errors=errors,
    )


def _wait_for_signature_change(
    page: Page, before: PageSignature, *, timeout_ms: int,
) -> PageSignature:
    """Polls until the page's signature differs from `before`, or the timeout
    expires. Returns whatever the signature is at that point, changed or not.

    Polling rather than waiting on a load event because both shapes have to
    work: a classic form POST (real navigation) and an SPA step transition
    (no navigation at all, just a re-render). A load-state wait alone is blind
    to the second, which is the shape every modern ATS uses."""
    wait_for_page_settled(page, timeout_ms=min(timeout_ms, 15_000))

    for _ in range(max(1, timeout_ms // _SIGNATURE_POLL_INTERVAL_MS)):
        current = capture_page_signature(page)
        if current.differs_from(before):
            # One more settle: the signature changes as soon as the new step
            # starts rendering, and reading fields mid-render is how a partially
            # mounted page gets mistaken for a short one.
            wait_for_page_settled(page, timeout_ms=min(timeout_ms, 15_000))
            return capture_page_signature(page)
        try:
            page.wait_for_timeout(_SIGNATURE_POLL_INTERVAL_MS)
        except PlaywrightError:
            break

    return capture_page_signature(page)
