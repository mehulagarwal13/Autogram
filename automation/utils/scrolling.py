"""
Dropdown/listbox scroll-search — Phase 8, PART 5/6/11.

Extracted from `automation/forms/field_handlers.py::_DropdownHandler`'s
original `_search_with_scrolling` — same algorithm, now shared by every
handler that needs it (`ComboboxHandler`, `CountryPickerHandler`,
`ReactSelectHandler` via `_DropdownHandler`, and the new
`VirtualizedListboxHandler`).

The one rule this exists to enforce everywhere: scroll the dropdown's own
CONTAINER, never the page (`page.mouse.wheel()` scrolls the page, which does
nothing for a widget with its own internal scrollable panel — see PART 5 of
the request that created this). A virtualized/lazily-rendered list only ever
renders a small window of options at a time and recycles its DOM nodes as
you scroll, which is why this re-queries the currently-visible options fresh
on every attempt instead of caching one snapshot up front.
"""

from __future__ import annotations

import logging
from typing import Callable

from playwright.sync_api import Error as PlaywrightError, Locator, Page

logger = logging.getLogger(__name__)

DEFAULT_MAX_SCROLL_ATTEMPTS = 12
DEFAULT_SCROLL_WAIT_MS = 120


def scroll_container_until_option_found(
    page: Page,
    container: Locator,
    find_option: Callable[[], Locator | None],
    *,
    max_attempts: int = DEFAULT_MAX_SCROLL_ATTEMPTS,
    scroll_wait_ms: int = DEFAULT_SCROLL_WAIT_MS,
    label: str = "",
) -> Locator | None:
    """Repeatedly calls `find_option()` (which the caller supplies — it
    knows how to search whatever's currently rendered inside `container` for
    a match) and, if nothing matches yet, scrolls `container` itself down by
    roughly one "page" and tries again, up to `max_attempts` times.

    `find_option` takes no arguments so callers can close over whatever
    matching logic (text comparison, alias resolution, ...) they need
    without this function knowing anything about it — this module is purely
    the "scroll and retry" mechanics, not the "does this option match" logic.
    """
    for attempt in range(max_attempts):
        option = find_option()
        if option is not None:
            return option
        logger.debug("Scrolling dropdown container for %r (attempt %d).", label, attempt + 1)
        try:
            container.evaluate("el => el.scrollBy(0, Math.max(el.clientHeight * 0.8, 40))")
        except PlaywrightError:
            break
        try:
            page.wait_for_timeout(scroll_wait_ms)
        except PlaywrightError:
            break
    return None
