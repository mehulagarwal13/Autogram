"""
Field handler registry — form-widget interaction layer (see ARCHITECTURE.md).

Sits between "field detection" and "confidence calculation" in the pipeline:

    ApplicationFlowManager -> ATSAdapter -> field detection -> field FILLING -> confidence -> review/submit
                                                                    ^^^^^^^
                                                              this module

`automation/forms/field_mapper.py` (Phase 5) answers "what profile attribute
does this field mean" (label/name/placeholder -> canonical attribute). This
module answers a completely different question: "given a resolved DOM
element and a value to put in it, HOW do I actually interact with this
specific kind of widget, and how do I confirm it worked" — a native
`<select>`, a react-select-style custom combobox, a searchable/virtualized
dropdown, a checkbox, a radio group, a date input, or a file upload all need
different interaction strategies, but every `ATSAdapter` (Greenhouse, Lever,
and any future platform) needs the same set of strategies. Centralizing them
here means adapters never hardcode a widget-interaction strategy themselves
— they just resolve a field and call `fill_field()`.

Design, top to bottom:

- `Field` — a cheap, already-introspected wrapper around one Playwright
  Locator (tag name, input type, ARIA role, resolved profile attribute if
  any) so handlers don't each re-query the DOM to figure out what they're
  looking at. Built once via `describe_field()`.
- `FieldHandler` (ABC) — `supports(field)` / `fill(field, value)` /
  `verify(field, value)`. Thirteen concrete handlers below, checked in order
  by `FieldHandlerRegistry` (most specific first, generic text input last):
  `TextInputHandler`, `TextAreaHandler`, `NativeSelectHandler`,
  `ReactSelectHandler`, `ComboboxHandler`, `CountryPickerHandler`,
  `VirtualizedListboxHandler` (Phase 8), `CheckboxHandler`, `ToggleHandler`
  (Phase 8), `RadioHandler`, `DateHandler`, `FileUploadHandler`.
  The three click-to-open dropdown-flavored handlers (`ReactSelectHandler`,
  `ComboboxHandler`, `CountryPickerHandler`) share one real implementation
  via `_DropdownHandler` — they differ only in *which* widgets they claim
  (`supports`) and, for country fields, alias-aware text matching — not in
  duplicated open/search/scroll/verify logic. `VirtualizedListboxHandler`
  (Phase 8) is a related but distinct fourth shape: an already-expanded
  `role="listbox"` panel needing no click-to-open step at all — see its own
  docstring for why it isn't just folded into `_DropdownHandler`.
- `fill_field()` — the single entry point every `ATSAdapter` fill path
  routes through: resolves the right handler, fills, verifies, retries on a
  failed verification (`DEFAULT_MAX_ATTEMPTS`), logs every stage, and
  returns a `HandlerOutcome` carrying a structured `FieldFailure` instead of
  a bare bool when it ultimately couldn't confirm the value stuck.

This module never imports `automation.ats.base` (that would be a cycle —
`base.py` imports this module) and never imports `automation.interfaces` /
`app.*` — it's pure Playwright + stdlib, and just as usable from a future
ATS adapter as from Greenhouse/Lever today. It DOES import
`automation.utils.*` (Phase 8 — `element_actions.safe_click`/
`wait_for_dynamic_element`, `scrolling.scroll_container_until_option_found`),
which are themselves pure Playwright + stdlib too, so this stays true.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field as _dc_field
from datetime import datetime
from pathlib import Path
from typing import Callable

from playwright.sync_api import Error as PlaywrightError, Locator, Page

from automation.utils.element_actions import safe_click, wait_for_dynamic_element
from automation.utils.scrolling import scroll_container_until_option_found

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Field / result types
# ---------------------------------------------------------------------------

@dataclass
class Field:
    """One resolved on-page control, already introspected once so handlers
    don't each re-query the DOM. `profile_attribute` is optional context
    (e.g. "country") that lets a handler like `CountryPickerHandler` claim a
    field based on its MEANING, not just its DOM shape."""

    locator: Locator
    page: Page
    label: str
    tag_name: str                      # "input" | "select" | "textarea" | "div" | ... | "" if unknown
    input_type: str | None             # input[type], lowercased; None for non-<input> tags
    role: str | None                   # ARIA role attribute, if any
    profile_attribute: str | None = None


@dataclass
class FieldFailure:
    """Structured detail on why a field ultimately couldn't be confirmed
    filled — attached to `automation.ats.base.FieldFillResult.failure`.

    `widget_type`/`context` (Phase 8, PART 13) are additive and default to
    "unknown"/empty — every pre-Phase-8 positional construction site
    (`FieldFailure(label, field_type, expected, actual, reason, retries)`)
    keeps working unchanged. `widget_type` is the resolved handler's class
    name (e.g. `"ComboboxHandler"`) when one was found and tried, or
    `"unknown"` when the registry couldn't classify the widget at all.
    `context` carries whatever the CALLER knows that `field_handlers.py`
    itself doesn't (this module is deliberately ATS-agnostic — see the
    module docstring) — `automation/ats/base.py` fills in `ats_type`/`url`
    on every `fill_field()` call it makes; see `format_failure_report()`."""

    field_label: str
    field_type: str
    expected_value: str
    actual_value: str | None
    failure_reason: str
    retry_count: int
    widget_type: str = "unknown"
    context: dict = _dc_field(default_factory=dict)
    #: Audit additions (still additive/backward-compatible — every existing
    #: positional `FieldFailure(...)` construction site is unaffected):
    #: the last `PlaywrightError` message raised across every fill/verify
    #: attempt (`None` if every attempt completed without raising — a pure
    #: value mismatch, not an exception, which is a meaningfully different
    #: failure mode worth distinguishing), and a best-effort, length-capped
    #: snapshot of the failed element's own `outerHTML` for debugging.
    last_exception: str | None = None
    element_html: str | None = None


#: Cap on `element_html`'s length — a debugging aid, not a full DOM dump;
#: keeps a pathological huge element from bloating logs.
_ELEMENT_HTML_SNAPSHOT_MAX_CHARS = 500


def _capture_element_html(locator: Locator) -> str | None:
    """Best-effort `outerHTML` snapshot of a failed field, truncated. Never
    raises — a snapshot failure (element detached, page navigated away,
    ...) must never mask the real failure it's trying to help debug."""
    try:
        html = locator.evaluate("el => el.outerHTML")
    except PlaywrightError:
        return None
    if not html:
        return None
    html = str(html)
    if len(html) > _ELEMENT_HTML_SNAPSHOT_MAX_CHARS:
        html = html[:_ELEMENT_HTML_SNAPSHOT_MAX_CHARS] + "...(truncated)"
    return html


def format_failure_report(failure: FieldFailure) -> str:
    """Renders the structured "FIELD AUTOMATION FAILURE" block (PART 13) —
    everything worth knowing about one failed field in one place: what
    field, what widget was detected, what was expected vs. actually read
    back, how many attempts were made, the last exception (if any) and a
    snapshot of the element's HTML (if one could be captured), and whatever
    ATS/page context the caller supplied via `fill_field(..., context=...)`.
    Never raises — formatting a failure report must never itself become a
    second failure."""
    lines = [
        "FIELD AUTOMATION FAILURE",
        f"Field: {failure.field_label}",
        f"Field type: {failure.field_type}",
        f"Detected widget: {failure.widget_type}",
        f"Expected value: {failure.expected_value}",
        f"Actual value: {failure.actual_value if failure.actual_value is not None else '(none)'}",
        f"Attempts: {failure.retry_count + 1}",
        f"Failure: {failure.failure_reason}",
    ]
    if failure.last_exception:
        lines.append(f"Exception: {failure.last_exception}")
    for key, value in failure.context.items():
        lines.append(f"{key}: {value}")
    if failure.element_html:
        lines.append(f"Element HTML: {failure.element_html}")
    return "\n".join(lines)


@dataclass
class HandlerOutcome:
    """What `fill_field()` returns — deliberately doesn't know anything
    about `FieldFillResult`'s field_key/profile_path conventions (that's
    ATSAdapter's business); callers translate this into their own result
    type."""

    filled: bool
    actual_value: str | None
    failure: FieldFailure | None = None


# ---------------------------------------------------------------------------
# Shared text-comparison helpers (used for both "find the right option" and
# "verify the result" — same normalization for both keeps them consistent).
# ---------------------------------------------------------------------------

_WHITESPACE = re.compile(r"\s+")


def _normalize_for_compare(text: str) -> str:
    return _WHITESPACE.sub(" ", text.strip().lower())


def _values_match(expected, actual) -> bool:
    """Case/whitespace-insensitive, substring-tolerant in either direction —
    deliberately loose rather than exact-equality, since plenty of real
    widgets reformat what you typed (a phone input mask, a select whose
    visible option text is a slightly longer/shorter variant of the profile
    value: "United States" vs "United States of America")."""
    if expected is None or actual is None:
        return False
    e, a = _normalize_for_compare(str(expected)), _normalize_for_compare(str(actual))
    if not e or not a:
        return False
    return e == a or e in a or a in e


_COUNTRY_ALIASES = {
    "usa": "united states", "us": "united states", "u.s.": "united states",
    "u.s.a.": "united states", "america": "united states",
    "uk": "united kingdom", "u.k.": "united kingdom", "britain": "united kingdom",
    "uae": "united arab emirates",
}


def _canonical_country(text: str) -> str:
    normalized = _normalize_for_compare(text)
    return _COUNTRY_ALIASES.get(normalized, normalized)


def _coerce_date_string(value) -> str:
    """Best-effort normalization to ISO `YYYY-MM-DD` (what native
    `<input type=date>` requires) — passes through unchanged if it already
    looks like one, tries a few common human formats, and otherwise falls
    back to the raw string (an invalid date just fails verification and
    surfaces as a structured failure rather than silently guessing)."""
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def describe_field(
    locator: Locator,
    *,
    label: str = "",
    page: Page | None = None,
    profile_attribute: str | None = None,
) -> Field:
    """Introspects `locator`'s DOM shape once (tag/type/role, batched into a
    single `evaluate` call) and wraps it as a `Field`. Degrades to an
    "unknown shape" `Field` (empty tag_name) rather than raising — every
    handler's `supports()` treats that as "not mine," so an introspection
    failure just means the field ends up unhandled/logged as unknown,
    never a crash."""
    resolved_page = page or locator.page
    tag_name = ""
    input_type: str | None = None
    role: str | None = None
    try:
        info = locator.evaluate(
            "el => ({tag: el.tagName ? el.tagName.toLowerCase() : '', "
            "type: (el.getAttribute && el.getAttribute('type') || '').toLowerCase(), "
            "role: el.getAttribute ? el.getAttribute('role') : null})"
        )
        tag_name = info.get("tag") or ""
        input_type = info.get("type") or None
        role = info.get("role")
    except PlaywrightError as e:
        logger.debug("Could not introspect field %r (%s) — treating as unknown shape.", label, e)
    return Field(
        locator=locator,
        page=resolved_page,
        label=label or "(unlabeled field)",
        tag_name=tag_name,
        input_type=input_type,
        role=role,
        profile_attribute=profile_attribute,
    )


# ---------------------------------------------------------------------------
# Handler contract
# ---------------------------------------------------------------------------

class FieldHandler(ABC):
    """One widget-interaction strategy. `fill`/`verify` may raise
    `PlaywrightError` freely — `fill_field()` catches it per attempt; a
    handler never needs its own try/except scaffolding for that."""

    @abstractmethod
    def supports(self, field: Field) -> bool: ...

    @abstractmethod
    def fill(self, field: Field, value) -> None: ...

    @abstractmethod
    def verify(self, field: Field, value) -> tuple[bool, str | None]:
        """Returns `(matches_expected, actual_value_read_for_logging)`."""
        ...


# ---------------------------------------------------------------------------
# Simple, single-element handlers
# ---------------------------------------------------------------------------

class TextAreaHandler(FieldHandler):
    def supports(self, field: Field) -> bool:
        return field.tag_name == "textarea"

    def fill(self, field: Field, value) -> None:
        field.locator.fill(str(value))

    def verify(self, field: Field, value) -> tuple[bool, str | None]:
        actual = field.locator.input_value()
        return _values_match(value, actual), actual


_TEXT_FILLABLE_INPUT_TYPES = {"", "text", "email", "tel", "number", "url", "search", "password"}


class TextInputHandler(FieldHandler):
    """The final fallback — anything that looks like a plain fillable
    `<input>`. Registered last so every more-specific handler gets first
    refusal."""

    def supports(self, field: Field) -> bool:
        return field.tag_name == "input" and (field.input_type or "") in _TEXT_FILLABLE_INPUT_TYPES

    def fill(self, field: Field, value) -> None:
        field.locator.fill(str(value))

    def verify(self, field: Field, value) -> tuple[bool, str | None]:
        actual = field.locator.input_value()
        return _values_match(value, actual), actual


class DateHandler(FieldHandler):
    def supports(self, field: Field) -> bool:
        return field.tag_name == "input" and field.input_type == "date"

    def fill(self, field: Field, value) -> None:
        field.locator.fill(_coerce_date_string(value))

    def verify(self, field: Field, value) -> tuple[bool, str | None]:
        actual = field.locator.input_value()
        return actual == _coerce_date_string(value), actual


_CHECKBOX_NEGATIVE_TEXT = {"no", "n", "false", "0", "off", "unchecked", "disagree", "decline"}

#: Playwright's default action timeout is 30s — fine for a single "this is
#: the only way to fill this field" attempt, but disastrous for the FIRST
#: attempt of a multi-strategy fallback chain (PART 9/11/12): a genuinely
#: hidden `<input type=checkbox hidden>` will never become actionable, so
#: without a short explicit timeout here, `CheckboxHandler`/`RadioHandler`
#: would block 30 real seconds on every single hidden checkbox/radio before
#: ever reaching the label-click fallback that actually works. This keeps
#: "try the fast native path first" cheap to fail out of.
_FAST_ACTION_TIMEOUT_MS = 1000


def _coerce_checkbox_intent(value) -> bool:
    """What a checkbox should end up as for a given `value`. Booleans pass
    through directly; recognized negative words uncheck; anything else
    non-empty (FieldMapper/AnswerEngine never call in with None/""/[] —
    see `fill_field`) is treated as an affirmative — checked."""
    if isinstance(value, bool):
        return value
    return _normalize_for_compare(str(value)) not in _CHECKBOX_NEGATIVE_TEXT


def _find_label_for(page: Page, input_locator: Locator) -> Locator | None:
    """The `<label>` associated with `input_locator`: via its `id`/`for`
    pairing, or an ancestor `<label>` wrapping it directly — whichever
    resolves first. Shared by `CheckboxHandler` and `RadioHandler` (PART 8/9)
    since both need the exact same "what's the real clickable surface when
    the input itself is visually hidden" lookup."""
    try:
        input_id = input_locator.get_attribute("id")
    except PlaywrightError:
        input_id = None
    if input_id:
        escaped = input_id.replace("\\", "\\\\").replace('"', '\\"')
        try:
            label = page.locator(f'label[for="{escaped}"]')
            if label.count() > 0:
                return label.first
        except PlaywrightError:
            pass
    try:
        parent_label = input_locator.locator("xpath=ancestor::label[1]")
        if parent_label.count() > 0:
            return parent_label.first
    except PlaywrightError:
        pass
    return None


def _element_tag_and_type(locator: Locator) -> tuple[str, str]:
    try:
        tag = locator.evaluate("el => el.tagName ? el.tagName.toLowerCase() : ''")
    except PlaywrightError:
        return "", ""
    input_type = ""
    if tag == "input":
        try:
            input_type = (locator.get_attribute("type") or "").lower()
        except PlaywrightError:
            input_type = ""
    return tag, input_type


class CheckboxHandler(FieldHandler):
    """Handles both a native `<input type=checkbox>` and a custom
    `role="checkbox"` element (PART 9). The common, previously-unhandled
    real-world failure this targets: a required "I agree" checkbox rendered
    as `<input type=checkbox hidden>` next to a `<label>` that's the actual
    clickable surface — native `.check()`/`.uncheck()` need the element
    itself to be visible/actionable, which a `hidden` input never is, so
    this tries the label (and a couple of further fallbacks) before ever
    resorting to a JavaScript-only state flip."""

    def supports(self, field: Field) -> bool:
        if field.tag_name == "input" and field.input_type == "checkbox":
            return True
        return field.role == "checkbox" and field.tag_name != "input"

    def _safe_is_checked(self, field: Field) -> bool:
        # Audit fix: explicit short timeout — `is_checked()` (unlike
        # `get_attribute()`) has an actionability-style wait for the
        # element; without a short override it would fall back to
        # Playwright's 30s default, and this is called on EVERY `verify()`
        # (i.e. every attempt of every fill), not just once.
        try:
            return field.locator.is_checked(timeout=_FAST_ACTION_TIMEOUT_MS)
        except PlaywrightError:
            pass
        try:
            aria = field.locator.get_attribute("aria-checked")
            if aria is not None:
                return aria.lower() == "true"
        except PlaywrightError:
            pass
        return False

    def fill(self, field: Field, value) -> None:
        intent = _coerce_checkbox_intent(value)
        if self._safe_is_checked(field) == intent:
            return  # already in the desired state

        # Attempt 1: native check()/uncheck() — the common, fast path for an
        # ordinary visible checkbox.
        if field.tag_name == "input" and field.input_type == "checkbox":
            try:
                if intent:
                    field.locator.check(timeout=_FAST_ACTION_TIMEOUT_MS)
                else:
                    field.locator.uncheck(timeout=_FAST_ACTION_TIMEOUT_MS)
                return
            except PlaywrightError as e:
                logger.debug("Native check()/uncheck() failed for %r (%s) — trying label/role fallbacks.", field.label, e)

        # Attempt 2: the associated <label> — the real clickable surface for
        # the "<input type=checkbox hidden> + <label>I agree</label>" pattern.
        label = _find_label_for(field.page, field.locator)
        if label is not None and safe_click(label, field.page):
            return

        # Attempt 3: a role="checkbox" element's own click (custom, non-<input>
        # widget), or the resolved element itself if it wasn't already tried above.
        if safe_click(field.locator, field.page):
            return

        # Attempt 4: the nearest wrapping div/span — some ATS UIs put the
        # actual click handler on a styled container around a hidden input
        # and an icon, with no <label> at all.
        try:
            parent = _first_visible(field.locator.locator("xpath=ancestor::*[self::div or self::span][1]"))
        except PlaywrightError:
            parent = None
        if parent is not None and safe_click(parent, field.page):
            return

        # Attempt 5 (last resort, PART 9/11/12): a JavaScript-only state
        # flip. Only reached once every real-interaction strategy above has
        # failed — bypasses whatever client-side handler a genuine click
        # would trigger, so this is deliberately the LEAST preferred
        # strategy, never the default.
        try:
            field.locator.evaluate(
                "(el, checked) => { el.checked = checked; "
                "el.dispatchEvent(new Event('input', {bubbles: true})); "
                "el.dispatchEvent(new Event('change', {bubbles: true})); }",
                intent,
            )
        except PlaywrightError as e:
            logger.debug("JavaScript fallback checkbox flip for %r also failed (%s).", field.label, e)

    def verify(self, field: Field, value) -> tuple[bool, str | None]:
        actual_checked = self._safe_is_checked(field)
        return actual_checked == _coerce_checkbox_intent(value), str(actual_checked)


class ToggleHandler(FieldHandler):
    """A switch/toggle control (PART 10) — `role="switch"` with
    `aria-checked`, e.g. "Are you willing to relocate?" rendered as a
    YES/NO toggle rather than a checkbox or radio pair.

    Deliberately its own handler, not folded into `CheckboxHandler` even
    though the underlying state is also boolean: a switch is virtually
    never a real `<input type=checkbox>` (no native `.check()`/`.uncheck()`
    to fall back on — every interaction has to be a click), and clicking it
    TOGGLES whatever state it's currently in rather than setting a specific
    one, so filling it correctly requires checking the current state first
    and clicking only when it doesn't already match — clicking an
    already-correct switch would flip it to the WRONG state."""

    def supports(self, field: Field) -> bool:
        return field.role == "switch"

    def _is_on(self, field: Field) -> bool:
        try:
            aria = field.locator.get_attribute("aria-checked")
            if aria is not None:
                return aria.lower() == "true"
        except PlaywrightError:
            pass
        # Audit fix: short explicit timeout — see CheckboxHandler._safe_is_checked.
        try:
            return field.locator.is_checked(timeout=_FAST_ACTION_TIMEOUT_MS)
        except PlaywrightError:
            return False

    def fill(self, field: Field, value) -> None:
        intent = _coerce_checkbox_intent(value)
        if self._is_on(field) == intent:
            return  # already in the desired state — clicking would flip it the wrong way
        safe_click(field.locator, field.page)

    def verify(self, field: Field, value) -> tuple[bool, str | None]:
        actual = self._is_on(field)
        return actual == _coerce_checkbox_intent(value), str(actual)


def _radio_option_text(page: Page, radio: Locator) -> str:
    """A radio's own accessible name: `aria-label`, an associated
    `<label for=id>`, or an ancestor `<label>` wrapping it — whichever
    resolves first. Every lookup is wrapped so a DOM quirk on one candidate
    radio just means "no text for this one," never a crash."""
    try:
        aria_label = radio.get_attribute("aria-label")
        if aria_label:
            return aria_label
    except PlaywrightError:
        pass
    label = _find_label_for(page, radio)
    if label is not None:
        try:
            return (label.text_content() or "").strip()
        except PlaywrightError:
            pass
    return ""


def _option_text(page: Page, option: Locator) -> str:
    """The accessible name of one option in a radio-flavored group,
    regardless of which of the three shapes `RadioHandler` supports it is
    (PART 8): a native radio prefers its associated `<label>` (its own text
    content is usually empty); a `role="radio"` element or a plain `<button>`
    is normally self-labeling, so its own visible text (or `aria-label`) IS
    the option's meaning."""
    tag, input_type = _element_tag_and_type(option)
    if tag == "input" and input_type == "radio":
        return _radio_option_text(page, option)
    try:
        aria_label = option.get_attribute("aria-label")
        if aria_label:
            return aria_label
    except PlaywrightError:
        pass
    try:
        return (option.text_content() or "").strip()
    except PlaywrightError:
        return ""


def _native_radio_group(field: Field) -> Locator:
    try:
        name_attr = field.locator.get_attribute("name")
    except PlaywrightError:
        name_attr = None
    if not name_attr:
        return field.locator
    escaped = name_attr.replace("\\", "\\\\").replace('"', '\\"')
    try:
        group = field.page.locator(f'input[type="radio"][name="{escaped}"]')
        if group.count() > 0:
            return group
    except PlaywrightError:
        pass
    return field.locator


def _aria_radio_group(field: Field) -> Locator:
    """A `role="radio"` element's group: the nearest `role="radiogroup"`
    ancestor's own `[role="radio"]` descendants if one exists (the standard
    accessible pattern), otherwise every `[role="radio"]` sharing the
    resolved element's immediate parent (a reasonable fallback for a custom
    widget that never bothered to add the wrapping `radiogroup` role)."""
    try:
        radiogroup = field.locator.locator("xpath=ancestor::*[@role='radiogroup'][1]")
        if radiogroup.count() > 0:
            options = radiogroup.first.locator('[role="radio"]')
            if options.count() > 0:
                return options
    except PlaywrightError:
        pass
    try:
        siblings = field.locator.locator("xpath=parent::*").locator('[role="radio"]')
        if siblings.count() > 0:
            return siblings
    except PlaywrightError:
        pass
    return field.locator


def _button_choice_group(field: Field) -> Locator:
    """A plain `<button>` used as a single-choice option (PART 8's
    "button-based choices" — e.g. `<button>Yes</button><button>No</button>`
    with no ARIA role at all): every sibling `<button>` under the resolved
    element's immediate parent."""
    try:
        siblings = field.locator.locator("xpath=parent::*").locator("button")
        if siblings.count() > 0:
            return siblings
    except PlaywrightError:
        pass
    return field.locator


class RadioHandler(FieldHandler):
    """`field.locator` may be any ONE option in its group (however the
    adapter resolved it) — this re-derives the whole group and picks
    whichever option's own accessible text matches `value`, rather than
    assuming the resolved element is the right option. Supports three real
    on-page shapes (PART 8): a native `<input type=radio>` group (matched by
    shared `name`), an ARIA `role="radio"` group (matched by a
    `role="radiogroup"` ancestor or shared parent), and a plain `<button>`
    button-group (matched by shared parent) — selection priority is native
    `.check()` first, then the option's own associated `<label>`, then a
    direct click on the option itself, then a keyboard-navigation fallback
    (focus + Space) for a widget whose real handler only listens for
    keyboard interaction."""

    def supports(self, field: Field) -> bool:
        if field.tag_name == "input" and field.input_type == "radio":
            return True
        if field.role == "radio":
            return True
        # A bare <button> is only ever claimed here if the caller already
        # resolved it as a radio-flavored choice via aria-pressed (the
        # standard accessible toggle-button pattern) — this deliberately
        # does NOT blanket-claim every <button> on a page (a submit/"Next"
        # button must never be routed through RadioHandler).
        if field.tag_name == "button":
            try:
                return field.locator.get_attribute("aria-pressed") is not None
            except PlaywrightError:
                return False
        return False

    def _group(self, field: Field) -> Locator:
        if field.tag_name == "input" and field.input_type == "radio":
            return _native_radio_group(field)
        if field.role == "radio":
            return _aria_radio_group(field)
        return _button_choice_group(field)

    def _matching_option(self, field: Field, value) -> Locator | None:
        group = self._group(field)
        try:
            count = group.count()
        except PlaywrightError:
            count = 0
        for i in range(count):
            option = group.nth(i)
            text = _option_text(field.page, option)
            if text and _values_match(value, text):
                return option
        return None

    def _is_selected(self, option: Locator) -> bool:
        # Audit fix: short explicit timeout — see CheckboxHandler._safe_is_checked.
        try:
            return option.is_checked(timeout=_FAST_ACTION_TIMEOUT_MS)
        except PlaywrightError:
            pass
        for attribute in ("aria-checked", "aria-pressed"):
            try:
                raw = option.get_attribute(attribute)
                if raw is not None:
                    return raw.lower() == "true"
            except PlaywrightError:
                continue
        return False

    def fill(self, field: Field, value) -> None:
        option = self._matching_option(field, value)
        if option is None:
            return

        tag, input_type = _element_tag_and_type(option)

        # Attempt 1: native .check() for a real radio input.
        if tag == "input" and input_type == "radio":
            try:
                option.check(timeout=_FAST_ACTION_TIMEOUT_MS)
                return
            except PlaywrightError as e:
                logger.debug("Native radio check() failed for %r (%s) — trying label/click fallbacks.", field.label, e)
            # Attempt 2 (native radio only): its own associated <label> —
            # the same hidden-input-behind-a-styled-label pattern
            # CheckboxHandler handles.
            label = _find_label_for(field.page, option)
            if label is not None and safe_click(label, field.page):
                return

        # Attempt 3: click the option element itself — the primary strategy
        # for both the ARIA role=radio and button-group shapes.
        if safe_click(option, field.page):
            return

        # Attempt 4: keyboard-navigation fallback, for a widget whose real
        # selection handler only listens for keyboard interaction (a common
        # accessible-widget implementation detail for role=radiogroup).
        try:
            option.focus()
            field.page.keyboard.press("Space")
        except PlaywrightError as e:
            logger.debug("Keyboard fallback for %r failed (%s).", field.label, e)

    def verify(self, field: Field, value) -> tuple[bool, str | None]:
        option = self._matching_option(field, value)
        if option is None:
            return False, None
        return self._is_selected(option), _option_text(field.page, option)


class FileUploadHandler(FieldHandler):
    """`set_input_files()` works on a hidden `<input type=file>` — Playwright
    doesn't require visibility for it — so this deliberately never checks
    `is_visible()`, unlike every other handler."""

    def supports(self, field: Field) -> bool:
        return field.tag_name == "input" and field.input_type == "file"

    def fill(self, field: Field, value) -> None:
        field.locator.set_input_files(str(value))

    def verify(self, field: Field, value) -> tuple[bool, str | None]:
        try:
            file_count = field.locator.evaluate("el => el.files ? el.files.length : 0")
        except PlaywrightError:
            file_count = 0
        if file_count:
            try:
                filename = field.locator.evaluate("el => el.files[0] ? el.files[0].name : null")
            except PlaywrightError:
                filename = None
            return True, filename

        # Some ATS "Attach / Dropbox / Google Drive / Enter manually" UIs
        # render the real <input> off-screen but still show the filename
        # somewhere nearby once JS processes the upload — best-effort
        # fallback so verification doesn't just assume failure.
        basename = Path(str(value)).name
        try:
            visible_hint = field.page.get_by_text(basename, exact=False)
            if visible_hint.count() > 0:
                return True, basename
        except PlaywrightError:
            pass
        return False, None


# ---------------------------------------------------------------------------
# Native <select>
# ---------------------------------------------------------------------------

class NativeSelectHandler(FieldHandler):
    def supports(self, field: Field) -> bool:
        return field.tag_name == "select"

    def fill(self, field: Field, value) -> None:
        text_value = str(value)
        try:
            field.locator.select_option(label=text_value)
            return
        except PlaywrightError:
            pass
        try:
            field.locator.select_option(value=text_value)
            return
        except PlaywrightError:
            pass
        # Last resort: case-insensitive/substring match against each
        # <option>'s visible text (handles "United States" not exactly
        # matching an option literally titled "United States of America").
        try:
            options = field.locator.locator("option").all()
        except PlaywrightError:
            options = []
        for option in options:
            try:
                option_text = (option.text_content() or "").strip()
            except PlaywrightError:
                continue
            if _values_match(text_value, option_text):
                try:
                    option_value = option.get_attribute("value")
                    if option_value is not None:
                        field.locator.select_option(value=option_value)
                        return
                except PlaywrightError:
                    continue

    def verify(self, field: Field, value) -> tuple[bool, str | None]:
        try:
            selected_value = field.locator.input_value()
        except PlaywrightError:
            return False, None
        selected_text = selected_value
        try:
            escaped = selected_value.replace("\\", "\\\\").replace('"', '\\"')
            selected_option = field.locator.locator(f'option[value="{escaped}"]')
            if selected_option.count() > 0:
                selected_text = (selected_option.first.text_content() or "").strip() or selected_value
        except PlaywrightError:
            pass
        return _values_match(value, selected_text), selected_text


# ---------------------------------------------------------------------------
# Dropdown family — react-select / generic combobox / country picker.
# One shared implementation (open -> search-or-scroll -> select -> verify);
# subclasses only differ in which widgets they claim and how they match text.
# ---------------------------------------------------------------------------

_SEARCH_INPUT_SELECTOR = (
    "input[role='combobox'], input[aria-autocomplete='list'], "
    "input[type='search'], input[class*='search' i]"
)
_LISTBOX_SELECTOR = (
    "[role='listbox'], [role='menu'], ul[class*='menu' i], div[class*='menu' i], "
    "div[class*='dropdown' i], div[class*='options' i]"
)
_OPTION_SELECTOR = "[role='option'], li, div[class*='option' i]"


def _first_visible(locator: Locator) -> Locator | None:
    try:
        count = locator.count()
    except PlaywrightError:
        return None
    for i in range(count):
        candidate = locator.nth(i)
        try:
            if candidate.is_visible():
                return candidate
        except PlaywrightError:
            continue
    return None


def _find_search_input(page: Page) -> Locator | None:
    return _first_visible(page.locator(_SEARCH_INPUT_SELECTOR))


def _find_listbox_container(page: Page) -> Locator | None:
    return _first_visible(page.locator(_LISTBOX_SELECTOR))


def _iter_visible_options(container: Locator) -> list[Locator]:
    try:
        candidates = container.locator(_OPTION_SELECTOR).all()
    except PlaywrightError:
        return []
    visible = []
    for candidate in candidates:
        try:
            if candidate.is_visible():
                visible.append(candidate)
        except PlaywrightError:
            continue
    return visible


def _find_matching_option(page: Page, value, matches: Callable[[str, object], bool]) -> Locator | None:
    container = _find_listbox_container(page)
    scope = container if container is not None else page.locator("body")
    for option in _iter_visible_options(scope):
        try:
            text = (option.text_content() or "").strip()
        except PlaywrightError:
            continue
        if text and matches(text, value):
            return option
    return None


def _search_with_scrolling(
    field: Field, value, matches: Callable[[str, object], bool], max_scroll_attempts: int
) -> Locator | None:
    """Only for widgets with no search box — delegates the actual
    scroll-and-retry mechanics to `automation.utils.scrolling` (PART 11):
    scrolls the LISTBOX CONTAINER itself (never the page) between attempts,
    re-querying currently-visible options fresh each time rather than
    caching one snapshot, which is what makes this work against a
    virtualized list whose DOM nodes get recycled as you scroll."""
    container = _find_listbox_container(field.page)
    if container is None:
        return None
    return scroll_container_until_option_found(
        field.page, container,
        lambda: _find_matching_option(field.page, value, matches),
        max_attempts=max_scroll_attempts,
        label=field.label,
    )


#: How long, and how often, to poll for a dropdown's popup (search input or
#: listbox) to actually appear after a click before giving up on that
#: attempt. A custom dropdown's popup (react-select's menu portal, an ARIA
#: combobox's listbox, ...) routinely mounts on the next render tick rather
#: than synchronously inside the click handler — querying for it with zero
#: wait is a real, common source of "clicked but nothing found" flakiness,
#: distinct from the widget genuinely not opening at all.
_POPUP_APPEAR_TIMEOUT_MS = 1000
_POPUP_APPEAR_POLL_MS = 100

#: Selector for a more specific, likely-clickable inner element to retry
#: against when clicking `field.locator` itself didn't open anything — covers
#: the case where a `<label for=...>` (or another wrapping ancestor) resolves
#: to a container `<div>` whose own click handler does nothing, because the
#: library actually wires its listener onto a nested child (react-select's
#: `*-control`/`*-value-container`, or the widget's own search input).
_INNER_CLICK_TARGET_SELECTOR = (
    "[class*='control' i], [class*='value-container' i], "
    "input, [role='combobox'], [role='button']"
)


def _popup_is_open(page: Page) -> bool:
    return _find_search_input(page) is not None or _find_listbox_container(page) is not None


def _wait_for_popup(
    page: Page, *, timeout_ms: int = _POPUP_APPEAR_TIMEOUT_MS, poll_ms: int = _POPUP_APPEAR_POLL_MS
) -> bool:
    """Polls (rather than a single blind wait) for a search input or listbox
    to appear anywhere on the page, up to `timeout_ms`, returning as soon as
    one is found instead of always waiting the full timeout. Delegates the
    generic poll loop to `automation.utils.element_actions.wait_for_dynamic_element`
    (PART 11) — this function's own job is purely "what counts as open"
    (`_popup_is_open`)."""
    return wait_for_dynamic_element(lambda: _popup_is_open(page), page, timeout_ms=timeout_ms, poll_ms=poll_ms)


def _read_dropdown_displayed_value(field: Field) -> str | None:
    for selector in (
        "[class*='single-value' i]", "[class*='singlevalue' i]",
        "[class*='selected-value' i]", "[class*='selected-option' i]",
    ):
        try:
            near = field.locator.locator(selector)
            if near.count() > 0:
                text = (near.first.text_content() or "").strip()
                if text:
                    return text
        except PlaywrightError:
            continue
    try:
        # Audit fix: `inner_text()`, not `text_content()` — `text_content()`
        # concatenates EVERY descendant's text regardless of CSS visibility,
        # so if the widget's popup/menu is a hidden (display:none) CHILD of
        # `field.locator` rather than a sibling (some custom widgets nest it
        # that way, unlike the react-select convention this module's other
        # fixtures use), every unselected option's text would be included
        # here too — and this loose, substring-tolerant match (`_values_match`)
        # could then report success for a fill that never actually happened,
        # simply because the target text existed somewhere in the still-
        # closed menu. `inner_text()` only returns rendered/visible text.
        text = (field.locator.inner_text() or "").strip()
        if text:
            return text
    except PlaywrightError:
        pass
    try:
        return field.locator.get_attribute("value")
    except PlaywrightError:
        return None


class _DropdownHandler(FieldHandler):
    """Shared algorithm for every non-native, JS-driven dropdown:

        click to open (retrying against a more specific nested element if
            the resolved locator's own click didn't open anything, and
            polling — not a single blind wait — for the popup to appear,
            since it commonly mounts a render tick after the click)
        if a search input appears:
            type the value, wait, search visible options
        else:
            search visible options; if not found, scroll the CONTAINER
            (not the page) and keep searching until found or exhausted
        click the matching option
        verify the widget now displays the expected value
    """

    max_scroll_attempts = 12

    def _matches(self, actual: str, expected) -> bool:
        return _values_match(expected, actual)

    def fill(self, field: Field, value) -> None:
        try:
            field.page.keyboard.press("Escape")  # best-effort: close a stray popup from a prior retry
        except PlaywrightError:
            pass

        safe_click(field.locator, field.page)
        opened = _wait_for_popup(field.page)

        if not opened:
            # The element the adapter resolved (often via a <label for=...>)
            # can be a non-interactive wrapper around the widget's real
            # clickable control rather than the control itself — before
            # giving up, retry against a more specific nested element.
            inner = _first_visible(field.locator.locator(_INNER_CLICK_TARGET_SELECTOR))
            if inner is not None:
                if safe_click(inner, field.page):
                    logger.debug("Retried opening dropdown for %r via a nested control.", field.label)
                else:
                    logger.debug("Nested-control retry click for %r did not succeed.", field.label)
                opened = _wait_for_popup(field.page)

        logger.info("Dropdown opened for %r." if opened else "Dropdown did not appear to open for %r.", field.label)

        search_input = _find_search_input(field.page)
        if search_input is not None:
            logger.info("Searching option %r for %r.", value, field.label)
            search_input.fill(str(value))
            field.page.wait_for_timeout(300)  # debounce — most searchable dropdowns filter async
            option = _find_matching_option(field.page, value, self._matches)
        else:
            option = _search_with_scrolling(field, value, self._matches, self.max_scroll_attempts)

        if option is None:
            logger.debug("No matching option found in dropdown for %r.", field.label)
            return

        # Audit fix: was a bare `option.click()` — Playwright's default
        # action timeout is 30s, so an option that's momentarily covered
        # (an animating menu, a sticky header) would block a FULL 30
        # seconds before this raised (caught one level up by `fill_field`'s
        # per-attempt try/except, but only after that full wait). `safe_click`
        # fails fast and retries instead.
        safe_click(option, field.page)
        logger.info("Option selected for %r.", field.label)

    def verify(self, field: Field, value) -> tuple[bool, str | None]:
        actual = _read_dropdown_displayed_value(field)
        return self._matches(actual or "", value), actual


class ReactSelectHandler(_DropdownHandler):
    """react-select's own convention: the control carries a class like
    `*-control`/`react-select__control`, or an id like `react-select-2-input`
    on its inner search input — matched generically (a class-name pattern
    the library itself uses everywhere it's rendered), never a specific
    company's markup."""

    _CLASS_HINT = re.compile(r"(react-select|select__control|css-[\w-]+-control)", re.IGNORECASE)

    def supports(self, field: Field) -> bool:
        if field.tag_name == "select":
            return False
        try:
            class_attr = field.locator.get_attribute("class") or ""
        except PlaywrightError:
            class_attr = ""
        if self._CLASS_HINT.search(class_attr):
            return True
        try:
            control_id = field.locator.get_attribute("id") or ""
        except PlaywrightError:
            control_id = ""
        return bool(re.search(r"react-select", control_id, re.IGNORECASE))


class CountryPickerHandler(_DropdownHandler):
    """Registered ahead of `ReactSelectHandler`/`ComboboxHandler` so a
    country field gets alias-aware matching ("USA" <-> "United States")
    regardless of which underlying widget flavor renders it — but always
    behind `NativeSelectHandler`, since a plain `<select name=country>`
    needs none of this."""

    def supports(self, field: Field) -> bool:
        return field.profile_attribute == "country" and field.tag_name != "select"

    def _matches(self, actual: str, expected) -> bool:
        if _canonical_country(actual) == _canonical_country(str(expected)):
            return True
        return _values_match(expected, actual)

    def fill(self, field: Field, value) -> None:
        super().fill(field, value)
        logger.info("Country selected: %r for %r.", value, field.label)


class VirtualizedListboxHandler(FieldHandler):
    """A directly-embedded, always-expanded `role="listbox"` panel (PART 6)
    — e.g. `<div role="listbox"><div role="option">...</div></div>`
    rendered inline on the page — as opposed to `_DropdownHandler`'s
    click-to-open trigger whose popup mounts elsewhere in the DOM. Handles
    country/location/years selectors and custom ATS questions rendered as a
    long, lazily-rendered (virtualized) scrollable list that's already
    visible, not hidden behind a click.

    Registered ahead of `ComboboxHandler` (which would otherwise also claim
    a bare `role="listbox"` element) so a field the caller resolved AS the
    listbox itself — not a separate trigger button — is scrolled and
    searched directly against that exact container, with no click-to-open
    step and no risk of ambiguity if more than one listbox happens to be on
    the page at once (`_DropdownHandler`'s `_find_listbox_container` instead
    searches the whole page for the first visible one). Still behind
    `CountryPickerHandler` — a country field keeps its alias-aware matching
    regardless of which of these two shapes renders it.

    Reuses `automation.utils.scrolling.scroll_container_until_option_found`
    (PART 11) for the same virtualization-safe "re-query visible options
    fresh after each scroll" behavior `_DropdownHandler` uses elsewhere —
    the container here is simply `field.locator` itself."""

    #: Real virtualized lists (100+ countries, etc.) can need more scroll
    #: iterations than a small react-select-style dropdown to reach a late
    #: option — see automation/tests/ for a 100+-option fixture.
    max_scroll_attempts = 20

    def supports(self, field: Field) -> bool:
        # Audit fix: a role="listbox" element that is NOT currently visible
        # is a click-to-open combobox's popup before it's been opened, not
        # an already-expanded panel — this handler's whole premise (search
        # directly against `field.locator`, no click-to-open step). Without
        # this check, a hidden listbox resolved directly (e.g. nested inside
        # a <label>, before any trigger click) would be claimed here and
        # then correctly-but-uselessly fail (every option reads as
        # not-visible), instead of being left for `ComboboxHandler`, which
        # DOES know how to open it first. `is_visible()` is a cheap
        # attribute/geometry read, not an action, so this stays a pure
        # predicate like every other `supports()` in this module.
        if field.role != "listbox":
            return False
        try:
            return field.locator.is_visible()
        except PlaywrightError:
            return False

    def _find_option(self, field: Field, value) -> Locator | None:
        for option in _iter_visible_options(field.locator):
            try:
                text = (option.text_content() or "").strip()
            except PlaywrightError:
                continue
            if text and _values_match(value, text):
                return option
        return None

    def fill(self, field: Field, value) -> None:
        option = self._find_option(field, value) or scroll_container_until_option_found(
            field.page, field.locator, lambda: self._find_option(field, value),
            max_attempts=self.max_scroll_attempts, label=field.label,
        )
        if option is None:
            logger.debug("No matching option found in virtualized listbox for %r.", field.label)
            return
        safe_click(option, field.page)
        logger.info("Option selected in virtualized listbox for %r.", field.label)

    def verify(self, field: Field, value) -> tuple[bool, str | None]:
        # Prefer an option explicitly marked selected — the standard way a
        # listbox communicates its current selection, and still readable
        # even if that option has since scrolled out of the currently-
        # rendered virtualized window.
        try:
            selected = field.locator.locator('[aria-selected="true"]')
            if selected.count() > 0:
                text = (selected.first.text_content() or "").strip()
                return _values_match(value, text), text
        except PlaywrightError:
            pass
        # Fall back to re-finding (and, if necessary, re-scrolling to) the
        # matching option for widgets that don't expose aria-selected at all.
        option = self._find_option(field, value) or scroll_container_until_option_found(
            field.page, field.locator, lambda: self._find_option(field, value),
            max_attempts=self.max_scroll_attempts, label=field.label,
        )
        if option is None:
            return False, None
        try:
            text = (option.text_content() or "").strip()
        except PlaywrightError:
            text = None
        return bool(text) and _values_match(value, text), text


class ComboboxHandler(_DropdownHandler):
    """The generic fallback for any searchable/virtualized dropdown that
    isn't specifically a react-select control or a country field — an ARIA
    `combobox`/`listbox` role, or a trigger exposing `aria-haspopup`/
    `aria-expanded` (the standard accessible pattern for a custom dropdown
    trigger, regardless of which library rendered it)."""

    def supports(self, field: Field) -> bool:
        if field.tag_name == "select":
            return False
        if field.role in ("combobox", "listbox"):
            return True
        try:
            has_popup = field.locator.get_attribute("aria-haspopup")
        except PlaywrightError:
            has_popup = None
        if has_popup:
            return True
        try:
            expanded = field.locator.get_attribute("aria-expanded")
        except PlaywrightError:
            expanded = None
        return expanded is not None


# ---------------------------------------------------------------------------
# Registry + orchestration
# ---------------------------------------------------------------------------

class FieldHandlerRegistry:
    def __init__(self, handlers: list[FieldHandler]):
        self._handlers = list(handlers)

    def get_all_matches(self, field: Field) -> list[FieldHandler]:
        """Every handler whose `supports(field)` is true, in registry order
        — a diagnostic used by `get_handler()` to detect ambiguity (audit
        finding: multiple handlers legitimately CAN claim the same field,
        e.g. both `VirtualizedListboxHandler` and `ComboboxHandler` claim a
        bare `role="listbox"` element). `supports()` implementations are
        pure predicates (attribute/role reads, no DOM mutation) everywhere
        in this module, so evaluating all of them is safe."""
        matches = []
        for handler in self._handlers:
            try:
                if handler.supports(field):
                    matches.append(handler)
            except PlaywrightError as e:
                logger.debug("Handler %s.supports() raised (%s) — trying the next one.", type(handler).__name__, e)
                continue
        return matches

    def get_handler(self, field: Field) -> FieldHandler | None:
        """Selection is deterministic BY REGISTRY ORDER: the first handler
        (in `DEFAULT_HANDLER_REGISTRY`'s list order) whose `supports()` is
        true always wins — same handler, every call, for the same field
        shape. When more than one handler matches (expected for some widget
        shapes — see `get_all_matches()`), that's logged so the ambiguity is
        visible in production logs instead of silently depending on list
        order with no diagnostic trail; the WINNING handler is unchanged
        either way."""
        matches = self.get_all_matches(field)
        if not matches:
            return None
        if len(matches) > 1:
            logger.debug(
                "Field %r matched by %d handlers (%s) — using %s (first by registry priority).",
                field.label, len(matches), [type(h).__name__ for h in matches], type(matches[0]).__name__,
            )
        return matches[0]


# Order matters: most specific/unambiguous first, generic text input last.
# CountryPickerHandler sits ahead of ReactSelectHandler/ComboboxHandler/
# VirtualizedListboxHandler so a country field gets alias-aware matching
# regardless of widget flavor, but behind NativeSelectHandler since a plain
# <select> needs no special casing. VirtualizedListboxHandler sits ahead of
# ComboboxHandler so a resolved role="listbox" element (already expanded) is
# scrolled/searched directly rather than routed through _DropdownHandler's
# click-to-open flow. ToggleHandler (role="switch") and the widened
# CheckboxHandler (role="checkbox") are structurally unambiguous with
# everything else and can sit anywhere before the generic text fallback.
DEFAULT_HANDLER_REGISTRY = FieldHandlerRegistry([
    FileUploadHandler(),
    CheckboxHandler(),
    ToggleHandler(),
    RadioHandler(),
    DateHandler(),
    NativeSelectHandler(),
    CountryPickerHandler(),
    VirtualizedListboxHandler(),
    ReactSelectHandler(),
    ComboboxHandler(),
    TextAreaHandler(),
    TextInputHandler(),
])

#: Initial attempt + this many retries before giving up and returning a
#: structured FieldFailure. Matches the "Retry 1" / "Retry 2" log shape.
DEFAULT_MAX_ATTEMPTS = 3


def fill_field(
    field: Field,
    value,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    registry: FieldHandlerRegistry | None = None,
    context: dict | None = None,
) -> HandlerOutcome:
    """The one place every `ATSAdapter` fill path routes through: resolves
    the right `FieldHandler` for `field`, fills, verifies, retries on a
    failed verification, and returns a `HandlerOutcome` — never raises for
    an ordinary fill/verify failure (a handler's own `PlaywrightError` is
    caught per attempt), so one broken field never aborts the rest of a
    form's sweep.

    `context` (Phase 8, PART 13) is optional, caller-supplied extra detail —
    typically `{"ats_type": ..., "url": ..., "company": ...}` from
    `automation/ats/base.py` — attached to a `FieldFailure` verbatim so
    `format_failure_report()` can render it; this module stays ATS-agnostic
    and never inspects or requires any particular key in it."""
    active_registry = registry or DEFAULT_HANDLER_REGISTRY
    failure_context = dict(context) if context else {}

    if value in (None, "", []):
        logger.debug("Skipped %r — nothing to fill.", field.label)
        return HandlerOutcome(filled=False, actual_value=None, failure=None)

    logger.info(
        "Detected field: label=%r type=%s role=%s",
        field.label, field.tag_name or field.input_type or "unknown", field.role,
    )

    handler = active_registry.get_handler(field)
    if handler is None:
        logger.info("Unknown field type for %r — skipping.", field.label)
        failure = FieldFailure(
            field.label, field.tag_name or "unknown", str(value), None, "no_handler_matched", 0,
            widget_type="unknown", context=failure_context,
            element_html=_capture_element_html(field.locator),
        )
        logger.warning(format_failure_report(failure))
        return HandlerOutcome(filled=False, actual_value=None, failure=failure)

    logger.info("Handler selected for %r: %s", field.label, type(handler).__name__)

    actual_value: str | None = None
    last_exception: str | None = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            logger.info("Retry %d for %r.", attempt - 1, field.label)
        try:
            handler.fill(field, value)
        except PlaywrightError as e:
            logger.debug("Fill attempt %d for %r raised (%s).", attempt, field.label, e)
            last_exception = str(e)

        try:
            ok, actual_value = handler.verify(field, value)
        except PlaywrightError as e:
            logger.debug("Verify attempt %d for %r raised (%s).", attempt, field.label, e)
            last_exception = str(e)
            ok = False

        if ok:
            logger.info("Verification passed for %r (attempt %d). Filled.", field.label, attempt)
            return HandlerOutcome(filled=True, actual_value=actual_value, failure=None)

        logger.info("Verification failed for %r (attempt %d).", field.label, attempt)

    logger.warning("Giving up on %r after %d attempt(s).", field.label, max_attempts)
    failure = FieldFailure(
        field.label, field.tag_name or "unknown", str(value), actual_value, "verification_failed", max_attempts - 1,
        widget_type=type(handler).__name__, context=failure_context,
        last_exception=last_exception, element_html=_capture_element_html(field.locator),
    )
    logger.warning(format_failure_report(failure))
    return HandlerOutcome(filled=False, actual_value=actual_value, failure=failure)
