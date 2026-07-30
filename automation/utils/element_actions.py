"""
Element interaction primitives — Phase 8, PART 11.

`safe_click()` centralizes the "scroll into view, wait for visible/enabled,
click, retry if intercepted" dance every `FieldHandler` needs, so a widget
whose real clickable control is temporarily covered by an overlay/animation
(a cookie banner, a sticky header, a menu still transitioning open) gets one
consistent recovery strategy everywhere instead of each handler re-inventing
its own.

`wait_for_dynamic_element()` generalizes the poll-don't-blind-wait pattern
`field_handlers.py` already used for a dropdown's popup (`_wait_for_popup`) —
useful for ANY React-rendering-delay scenario, not just a dropdown menu.
"""

from __future__ import annotations

import logging
from typing import Callable

from playwright.sync_api import Error as PlaywrightError, Locator, Page

logger = logging.getLogger(__name__)

#: Default polling window for `wait_for_dynamic_element` — matches the
#: constants `field_handlers.py` used before this was extracted
#: (`_POPUP_APPEAR_TIMEOUT_MS` / `_POPUP_APPEAR_POLL_MS`).
DEFAULT_WAIT_TIMEOUT_MS = 1000
DEFAULT_WAIT_POLL_MS = 100

DEFAULT_CLICK_ATTEMPTS = 3
#: Deliberately short — Playwright's own default action timeout (30s) is
#: fine for a single "this is the only way" click, but every call site here
#: is one step in a multi-strategy fallback chain (PART 11/12): a click that
#: can never succeed (element never becomes actionable) should fail fast so
#: the caller's NEXT strategy gets tried, not block for 30 real seconds per
#: attempt on a single field.
DEFAULT_CLICK_TIMEOUT_MS = 3000


def safe_click(
    locator: Locator,
    page: Page | None = None,
    *,
    fallback_locator: Locator | None = None,
    max_attempts: int = DEFAULT_CLICK_ATTEMPTS,
    timeout_ms: int = DEFAULT_CLICK_TIMEOUT_MS,
) -> bool:
    """Clicks `locator`, retrying on interception/transient failure before
    giving up. Every attempt: scroll the element into view (best-effort —
    Playwright's own `click()` already does this, but a stale/animating
    layout can need a second nudge), then click. If every attempt on
    `locator` itself fails and a `fallback_locator` was supplied (e.g. a more
    specific nested control — see `field_handlers.py`'s
    `_INNER_CLICK_TARGET_SELECTOR` use of this exact pattern), tries that
    once as a last resort.

    Returns whether a click was actually dispatched without raising — NOT
    whether it had the intended effect (opened a menu, checked a box, ...);
    that's the caller's `verify()` step to confirm, same division of
    responsibility `fill_field()` already uses everywhere else.
    """
    resolved_page = page or locator.page

    for attempt in range(1, max_attempts + 1):
        try:
            locator.scroll_into_view_if_needed()
        except PlaywrightError as e:
            logger.debug("safe_click: scroll_into_view_if_needed failed (attempt %d): %s", attempt, e)
        try:
            locator.click(timeout=timeout_ms)
            return True
        except PlaywrightError as e:
            logger.debug("safe_click: click failed (attempt %d/%d): %s", attempt, max_attempts, e)
            if attempt < max_attempts:
                try:
                    resolved_page.wait_for_timeout(150)
                except PlaywrightError:
                    break

    if fallback_locator is not None:
        try:
            fallback_locator.scroll_into_view_if_needed()
        except PlaywrightError:
            pass
        try:
            fallback_locator.click(timeout=timeout_ms)
            return True
        except PlaywrightError as e:
            logger.debug("safe_click: fallback locator click also failed: %s", e)

    return False


def wait_for_dynamic_element(
    check: Callable[[], bool],
    page: Page,
    *,
    timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
    poll_ms: int = DEFAULT_WAIT_POLL_MS,
) -> bool:
    """Polls `check()` (rather than a single blind wait) up to `timeout_ms`,
    returning as soon as it's truthy instead of always waiting the full
    timeout — handles React/other-framework rendering delays (a menu portal,
    a lazily-mounted panel, ...) that commonly appear a render tick after the
    triggering click rather than synchronously inside its handler.

    `page` is only used to drive the polling wait itself
    (`page.wait_for_timeout`), so this works for any "wait until some
    DOM/state condition becomes true" case, not just a dropdown popup.
    """
    if check():
        return True
    elapsed = 0
    while elapsed < timeout_ms:
        try:
            page.wait_for_timeout(poll_ms)
        except PlaywrightError:
            return False
        elapsed += poll_ms
        if check():
            return True
    return False
