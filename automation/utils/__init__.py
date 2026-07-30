"""
automation/utils/ — Phase 8: shared, widget-agnostic Playwright interaction
primitives (see PART 11 of the request that created this package).

Extracted out of `automation/forms/field_handlers.py`, which used to define
these as private module-level helpers duplicated in spirit (if not in code)
across `_DropdownHandler`'s open/search/scroll logic. Centralizing them here
means:

- Every `FieldHandler` (existing or new — `ToggleHandler`,
  `VirtualizedListboxHandler`, a hardened `RadioHandler`/`CheckboxHandler`,
  ...) clicks, scrolls, and waits the same way, instead of each
  re-implementing its own "scroll into view, wait, click, retry if
  intercepted" dance.
- A fix to one of these (e.g. a new interception-recovery strategy in
  `safe_click`) benefits every handler and every ATS adapter at once.

Like `field_handlers.py` itself, this package is pure Playwright + stdlib —
no imports of `automation.ats`, `automation.interfaces`, or `app.*` — so it's
usable from any future ATS adapter or handler without pulling in the rest of
the application.
"""
