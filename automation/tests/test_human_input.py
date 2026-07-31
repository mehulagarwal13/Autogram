"""
Human-paced input (`automation/utils/human_input.py`).

`automation/tests/conftest.py` disables pacing for the whole suite, so these
tests turn it back on explicitly via monkeypatched env — and deliberately use
SHORT values, since the point is to assert the mechanism, not to sit through
realistic delays.

The behaviour that actually matters is the keystroke stream: a widget that
filters/validates as you type sees per-character events instead of one bulk
mutation. The fixtures below record real `keydown`/`input` events to prove it,
rather than asserting on wall-clock timing (which would be flaky).
"""

from __future__ import annotations

import pytest

from automation.forms.field_handlers import describe_field, fill_field
from automation.utils import human_input


@pytest.fixture
def paced(monkeypatch):
    monkeypatch.setenv(human_input.HUMAN_PACING_ENV, "1")
    # Keep the assertions fast — the mechanism is what's under test.
    monkeypatch.setattr(human_input, "SCROLL_SETTLE_MS", (1, 2))
    monkeypatch.setattr(human_input, "PRE_TYPE_MS", (1, 2))
    monkeypatch.setattr(human_input, "PER_CHAR_MS", (1, 2))
    monkeypatch.setattr(human_input, "INTER_FIELD_MS", 1)


#: Records every keystroke and input event so a test can tell real typing from
#: a single bulk `fill()`.
_KEYSTROKE_RECORDER = """
<label for="q">Preferred name</label>
<input id="q" type="text">
<script>
  window.__keys = [];
  window.__inputs = 0;
  const el = document.getElementById('q');
  el.addEventListener('keydown', e => window.__keys.push(e.key));
  el.addEventListener('input', () => window.__inputs++);
</script>
"""


def _render(page, html: str):
    page.set_content(f"<!DOCTYPE html><html><body>{html}</body></html>")


def test_typing_produces_one_keystroke_per_character(page, paced):
    _render(page, _KEYSTROKE_RECORDER)
    field = describe_field(page.locator("#q"), label="Preferred name", page=page)

    outcome = fill_field(field, "Ada")

    assert outcome.filled is True
    # The leading "Delete" is the clear step (`fill("")`) — see
    # `test_an_existing_value_is_replaced_not_appended` for why it's needed.
    keys = page.evaluate("window.__keys")
    assert [k for k in keys if k != "Delete"] == ["A", "d", "a"]


def test_typing_fires_an_input_event_per_character_not_one_bulk_mutation(page, paced):
    """This is the correctness argument for the whole module: an autocomplete
    or character counter listening on `input` must see progressive events."""
    _render(page, _KEYSTROKE_RECORDER)
    field = describe_field(page.locator("#q"), label="Preferred name", page=page)

    fill_field(field, "Ada")

    assert page.evaluate("window.__inputs") >= 3


def test_pacing_disabled_uses_a_single_bulk_fill(page, monkeypatch):
    monkeypatch.setenv(human_input.HUMAN_PACING_ENV, "0")
    _render(page, _KEYSTROKE_RECORDER)
    field = describe_field(page.locator("#q"), label="Preferred name", page=page)

    outcome = fill_field(field, "Ada")

    assert outcome.filled is True
    assert page.locator("#q").input_value() == "Ada"
    assert page.evaluate("window.__keys") == []  # no keystrokes at all


def test_an_existing_value_is_replaced_not_appended(page, paced):
    """`press_sequentially` appends, so the field has to be cleared first —
    otherwise re-filling a pre-populated field produces "AdaAda"."""
    _render(page, '<input id="q" type="text" value="Grace">')
    field = describe_field(page.locator("#q"), label="Preferred name", page=page)

    fill_field(field, "Ada")

    assert page.locator("#q").input_value() == "Ada"


def test_long_text_falls_back_to_fill_rather_than_typing_for_minutes(page, paced, monkeypatch):
    """A cover-letter-length answer has no keystroke-sensitive behaviour to
    satisfy, and typing it would cost minutes — so above MAX_TYPED_CHARS the
    value is set in one shot. The value must still land correctly."""
    monkeypatch.setattr(human_input, "MAX_TYPED_CHARS", 10)
    _render(page, _KEYSTROKE_RECORDER)
    field = describe_field(page.locator("#q"), label="Preferred name", page=page)

    long_value = "a" * 25
    outcome = fill_field(field, long_value)

    assert outcome.filled is True
    assert page.locator("#q").input_value() == long_value
    assert page.evaluate("window.__keys") == []


def test_a_textarea_is_typed_too(page, paced):
    _render(page, """
        <label for="why">Why us?</label><textarea id="why"></textarea>
        <script>
          window.__keys = [];
          document.getElementById('why').addEventListener('keydown', e => window.__keys.push(e.key));
        </script>
    """)
    field = describe_field(page.locator("#why"), label="Why us?", page=page)

    fill_field(field, "Hi")

    assert page.locator("#why").input_value() == "Hi"
    assert [k for k in page.evaluate("window.__keys") if k != "Delete"] == ["H", "i"]


def test_a_date_input_is_still_filled_not_typed(page, paced):
    """Locale-dependent segmented spinners — typing lands the parts in a
    different order per browser locale, so `fill()` with the ISO value stays."""
    _render(page, '<input id="d" type="date">')
    field = describe_field(page.locator("#d"), label="Start date", page=page)

    outcome = fill_field(field, "1990-05-14")

    assert outcome.filled is True
    assert page.locator("#d").input_value() == "1990-05-14"


def test_typing_failure_falls_back_to_fill_so_the_value_still_lands(page, paced, monkeypatch):
    """If the keystroke path raises for any reason, the field must still end
    up with the right value — `fill_field`'s verify step is what matters."""
    from playwright.sync_api import Error as PlaywrightError

    def boom(self, *a, **kw):
        raise PlaywrightError("synthetic press_sequentially failure")

    monkeypatch.setattr("playwright.sync_api._generated.Locator.press_sequentially", boom)
    _render(page, _KEYSTROKE_RECORDER)
    field = describe_field(page.locator("#q"), label="Preferred name", page=page)

    outcome = fill_field(field, "Ada")

    assert outcome.filled is True
    assert page.locator("#q").input_value() == "Ada"
