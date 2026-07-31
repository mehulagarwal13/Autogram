"""
Human-paced input primitives — scroll, settle, click, type character by
character, pause before the next field.

Lives alongside the other interaction primitives in `automation/utils/`
(`element_actions.py`, `scrolling.py`) and is called from
`automation/forms/field_handlers.py`, so every handler and every ATS adapter
inherits the behaviour rather than each fill path re-implementing it.

**Why this exists, in order of how much it actually matters.**

1. *Correctness.* Playwright's `fill()` sets the value and dispatches one
   `input` event. Plenty of ATS fields are fine with that, but a control that
   listens for real keystrokes — an autocomplete that filters as you type, a
   react-select search box, a masked phone/date input, a character counter
   that gates the Submit button — sees a single bulk mutation and either
   ignores it or ends up in an inconsistent state. `press_sequentially()`
   produces a genuine `keydown`/`keypress`/`input`/`keyup` sequence per
   character, which is what those widgets are written against.
2. *Not tripping rate limits.* Filling thirty fields in under a second is a
   traffic pattern no applicant produces, and some boards throttle or soft-block
   on it. Pacing keeps a run inside normal human bounds.

Everything here is a no-op when pacing is off (see `human_pacing_enabled`), so
the test suite stays fast: ~400 tests each paying 2s per field would add hours.
Production runs default to ON.
"""

from __future__ import annotations

import logging
import os
import random

from playwright.sync_api import Error as PlaywrightError, Locator, Page

logger = logging.getLogger(__name__)

#: Set to 0/false/no/off to disable pacing entirely (tests, CI, debugging).
#: Read at call time, not import time, so a test can flip it per-case.
HUMAN_PACING_ENV = "AUTOMATION_HUMAN_PACING"

_FALSEY = {"0", "false", "no", "off", ""}

#: After scrolling a field into view, before touching it.
SCROLL_SETTLE_MS = (500, 1200)
#: Between clicking a field and starting to type.
PRE_TYPE_MS = (200, 500)
#: Per-character typing delay.
PER_CHAR_MS = (30, 90)
#: After a field is done, before the next one is touched.
INTER_FIELD_MS = 2000

#: Characters typed per re-rolled delay. Playwright's `press_sequentially`
#: takes ONE delay for the whole string, so a literal per-character random
#: delay would mean one round-trip per character — ~500 round-trips for a
#: cover-letter answer, which costs far more in IPC than it buys in realism.
#: Typing in short chunks with a freshly rolled delay each time gives genuinely
#: varying cadence at a fraction of the overhead.
_CHUNK_CHARS = 8

#: Above this length, type nothing and fall back to `fill()`. A 2,000-character
#: cover letter at 60ms/char is two minutes of typing for a `<textarea>` that
#: has no keystroke-sensitive behaviour to satisfy — the correctness argument
#: above simply doesn't apply to long-form prose fields, and the cost is real.
MAX_TYPED_CHARS = 400


def human_pacing_enabled() -> bool:
    return os.getenv(HUMAN_PACING_ENV, "1").strip().lower() not in _FALSEY


def _jitter(bounds: tuple[int, int]) -> int:
    return random.randint(*bounds)


def _pause(page: Page, bounds: tuple[int, int]) -> None:
    try:
        page.wait_for_timeout(_jitter(bounds))
    except PlaywrightError:
        pass


def scroll_into_view(locator: Locator, page: Page) -> None:
    """Best-effort — a field that can't be scrolled to is still very often
    fillable (already in view, or in a scroll container Playwright handles
    on its own at action time), so this never raises."""
    if not human_pacing_enabled():
        return
    try:
        locator.scroll_into_view_if_needed(timeout=2000)
    except PlaywrightError as e:
        logger.debug("Could not scroll a field into view (%s) — continuing.", e)
        return
    _pause(page, SCROLL_SETTLE_MS)


def human_type(locator: Locator, text: str, page: Page, *, click_first: bool = True) -> None:
    """Scroll → settle → click → settle → clear → type in jittered chunks.

    Falls back to a plain `fill()` when pacing is disabled, when the text is
    longer than `MAX_TYPED_CHARS`, or if typing raises — in every one of those
    cases the value still lands, which is what `fill_field()`'s verify step
    cares about.

    `click_first=False` is for a field that is ALREADY focused and where a
    click would be actively harmful — notably a custom dropdown's search box,
    which is focused the moment the menu opens and whose trigger would toggle
    the menu shut if clicked again.
    """
    text = str(text)
    if not human_pacing_enabled() or len(text) > MAX_TYPED_CHARS:
        locator.fill(text)
        return

    scroll_into_view(locator, page)
    if click_first:
        try:
            locator.click(timeout=3000)
            _pause(page, PRE_TYPE_MS)
        except PlaywrightError as e:
            logger.debug("Could not click a field before typing (%s) — typing anyway.", e)

    try:
        locator.fill("")  # press_sequentially appends; clear whatever was there
        for start in range(0, len(text), _CHUNK_CHARS):
            locator.press_sequentially(text[start:start + _CHUNK_CHARS], delay=_jitter(PER_CHAR_MS))
    except PlaywrightError as e:
        logger.debug("Character-by-character typing failed (%s) — falling back to fill().", e)
        locator.fill(text)


def human_pause_between_fields(page: Page) -> None:
    """Called once per field by `fill_field()` — after the retry loop has
    settled, not per attempt."""
    if not human_pacing_enabled():
        return
    try:
        page.wait_for_timeout(INTER_FIELD_MS)
    except PlaywrightError:
        pass
