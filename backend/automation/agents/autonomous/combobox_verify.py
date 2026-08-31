"""
Committed-state verification for custom (non-native) dropdowns, for the
autonomous agent's `ActionExecutor`.

This does NOT reimplement the algorithm — it reuses the exact functions the
deterministic ATS-adapter engine already proved out against real custom
widgets (Amex's `cx-select-input` among them), imported straight from
`automation.forms.field_handlers`. That module's helpers are keyed only on a
`Field`'s `.locator`/`.page`, so a throwaway `Field` wraps whatever locator
the autonomous agent is looking at — no duplication of the actual signal
logic, per the project's "don't build parallel systems" rule.

Mirrors `field_handlers.py::_DropdownHandler.verify()`'s exact priority
order and its most important lesson: an inspectable popup reporting nothing
selected is an AUTHORITATIVE NEGATIVE, overriding whatever text happens to
still be sitting in the input (a failed search leaves its filter text behind,
which must never read as a successful match). False-positive verification is
worse than false-negative (spec §15/§20).
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page

from automation.forms.field_handlers import (
    Field,
    _committed_value_from_hidden_twin,
    _has_inspectable_options,
    _read_dropdown_displayed_value,
    _selected_option_via_active_descendant,
    _selected_option_via_aria_controls,
    _values_match,
)


def verify_combobox_commit(locator: Locator, page: Page, expected_value: str | None) -> tuple[bool | None, str | None]:
    """Returns `(committed, observed_value)`:

    - `committed=True` when one of the signals confirms `expected_value` was
      actually selected.
    - `committed=False` when a signal positively contradicts it (a different
      value was committed, or an inspectable popup shows nothing selected).
    - `committed=None` when nothing here was conclusive either way — the
      caller (`executor.py`) falls back to its own plain text-input check
      rather than treating silence as either a pass or a fail.

    `observed_value` is whatever was actually seen (even on a mismatch or
    inconclusive read) — useful for `ActionResult.detail` the same way
    `field_handlers.py`'s own failure reports use it.
    """
    if not expected_value:
        return True, None

    field = Field(locator=locator, page=page, label="", tag_name="", input_type=None, role="combobox")

    observed_any: str | None = None
    for probe in (
        _selected_option_via_active_descendant,
        _selected_option_via_aria_controls,
        _committed_value_from_hidden_twin,
    ):
        observed = probe(field)
        if observed is None:
            continue
        if _values_match(expected_value, observed):
            return True, observed
        observed_any = observed_any or observed

    if _has_inspectable_options(field):
        # The widget's own markup says nothing is selected — authoritative,
        # even if the input still displays leftover search text.
        return False, observed_any

    actual = _read_dropdown_displayed_value(field)
    if _values_match(expected_value, actual or ""):
        return True, actual
    if actual or observed_any:
        return False, actual or observed_any
    return None, None
