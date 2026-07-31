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
from automation.utils.human_input import human_pause_between_fields, human_type
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
    #: Why `tag_name`/`input_type`/`role` are empty, when they are. A failed
    #: introspection used to degrade silently to `tag_name=""`, which no
    #: handler claims (`TextInputHandler` needs `"input"`, `NativeSelectHandler`
    #: needs `"select"`), so the field died as `no_handler_matched` with the
    #: real cause thrown away — the signature of several long-standing suite
    #: failures. `fill_field()` now copies this into the `FieldFailure`.
    introspection_error: str | None = None


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


class FieldFillRefused(Exception):
    """A handler DECLINING to fill, as opposed to failing to.

    `FieldHandler.fill()` returns `None` by contract and `FieldFailure` is
    built by `fill_field()`, so a handler that has positively determined it
    must not touch the field needs a channel out. Raising this is that
    channel: `fill_field()` converts it into a `FieldFailure` carrying
    `reason`/`detail` and — critically — does NOT retry. A refusal is a
    deterministic conclusion about the live DOM ("two options match this
    value equally well"), so re-running it two more times produces the same
    answer and only delays the handoff to a human.

    Distinct from `PlaywrightError`, which means "the interaction itself went
    wrong" and IS worth retrying."""

    def __init__(self, reason: str, detail: str = "", context: dict | None = None):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
        self.context = context or {}


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


#: Wait between the two introspection attempts in `describe_field` — long
#: enough for a just-mounted element to attach, short enough not to matter.
_INTROSPECTION_RETRY_WAIT_MS = 250


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
    introspection_error: str | None = None

    # Retried once: the overwhelmingly common reason this throws is that the
    # element isn't attached yet (a React/Vue form still mounting, a step that
    # just became visible), which a short wait fixes. A second failure is a
    # real problem worth reporting rather than silently flattening into an
    # "unknown shape" that no handler can ever claim.
    for attempt in (1, 2):
        try:
            info = locator.evaluate(
                "el => ({tag: el.tagName ? el.tagName.toLowerCase() : '', "
                "type: (el.getAttribute && el.getAttribute('type') || '').toLowerCase(), "
                "role: el.getAttribute ? el.getAttribute('role') : null})"
            )
            tag_name = info.get("tag") or ""
            input_type = info.get("type") or None
            role = info.get("role")
            introspection_error = None
            break
        except PlaywrightError as e:
            introspection_error = str(e)
            if attempt == 1:
                logger.debug("Introspection of %r failed (%s) — retrying once.", label, e)
                try:
                    resolved_page.wait_for_timeout(_INTROSPECTION_RETRY_WAIT_MS)
                except PlaywrightError:
                    pass

    if introspection_error is not None:
        # WARNING, not debug: this is the difference between "unknown widget"
        # and a diagnosable cause, and it previously vanished at debug level.
        logger.warning(
            "Could not introspect field %r after 2 attempts — no handler will claim it. "
            "Cause: %s. Element: %s",
            label, introspection_error, _capture_element_html(locator),
        )

    return Field(
        locator=locator,
        page=resolved_page,
        label=label or "(unlabeled field)",
        tag_name=tag_name,
        input_type=input_type,
        role=role,
        profile_attribute=profile_attribute,
        introspection_error=introspection_error,
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
        human_type(field.locator, str(value), field.page)

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
        human_type(field.locator, str(value), field.page)

    def verify(self, field: Field, value) -> tuple[bool, str | None]:
        actual = field.locator.input_value()
        return _values_match(value, actual), actual


class DateHandler(FieldHandler):
    def supports(self, field: Field) -> bool:
        return field.tag_name == "input" and field.input_type == "date"

    def fill(self, field: Field, value) -> None:
        # Deliberately NOT human-typed. `input[type=date]` renders as segmented
        # day/month/year spinners whose keystroke handling follows the BROWSER
        # LOCALE, so typing "1990-05-14" character by character lands the parts
        # in a different order per locale, and Escape/Tab between segments
        # varies too. `fill()` sets the ISO value the element actually stores,
        # which is unambiguous — and a date picker has no as-you-type filtering
        # behaviour that per-character input would buy us anyway.
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

#: One round-trip for everything this handler needs to know about a `<select>`:
#: whether it's multi-select, whether it's rendered, and its live option list.
_SELECT_STATE_JS = """
el => ({
  multiple: !!el.multiple,
  rendered: el.offsetParent !== null,
  options: Array.from(el.options || []).map(o => ({
    text: (o.label || o.textContent || '').trim(),
    value: o.value,
    disabled: !!o.disabled,
  })),
  selected: Array.from(el.selectedOptions || []).map(o => (o.label || o.textContent || '').trim()),
})
"""

#: Does a visible custom-dropdown control sit alongside this hidden `<select>`?
#: If so the select is that widget's backing store, and the visible control is
#: what should be driven — mirrors the `is_visible()` reasoning that
#: `VirtualizedListboxHandler.supports()` got in the Phase 8 audit.
_SELECT_PROXY_JS = """
el => {
  const parent = el.parentElement;
  if (!parent) return false;
  const sel = '[role="combobox"],[role="listbox"],[aria-haspopup],'
            + '[class*="select" i],[class*="dropdown" i],button';
  return Array.from(parent.querySelectorAll(sel)).some(n => n !== el && n.offsetParent !== null);
}
"""

#: Separators an upstream answer may use to express several choices for a
#: `<select multiple>`.
_MULTI_VALUE_SEPARATORS = ("\n", ";", ",")


def _multi_targets(value) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value)
    for separator in _MULTI_VALUE_SEPARATORS:
        if separator in text:
            return [part.strip() for part in text.split(separator) if part.strip()]
    return [text.strip()] if text.strip() else []


class NativeSelectHandler(FieldHandler):
    """A real `<select>`. Everything is driven off ONE live read of the
    element's own option list (`_SELECT_STATE_JS`), rather than handing a
    candidate string to Playwright and hoping it matches something.

    The reason is that the previous implementation's last-resort tier looped
    the live options, took the FIRST whose text satisfied `_values_match`, and
    selected it. `_values_match` is substring-tolerant in BOTH directions, so
    filling "No" against `["Not applicable", "None"]` silently selected
    "Not applicable" — wrong data, on a real application, verified as success
    because `verify()` uses the same loose comparison. It also had nothing
    stopping it landing on a placeholder ("Select...").

    That made this path LOOSER than `answer_engine._match_option`, which
    refuses when two options match a value equally well. This handler now
    applies the same discipline: exact text, then exact value, then
    case/whitespace-normalized text, then loose — taking the first tier that
    yields exactly ONE non-placeholder option, and REFUSING (via
    `FieldFillRefused`, no retries) on either zero matches or a tie.

    Note that a tie is only reachable when no earlier, tighter tier matched.
    "No" against `["Not applicable", "No"]` resolves cleanly at the exact-text
    tier and is NOT a refusal — treating it as ambiguous would break every
    Yes/No select that also offers "Not applicable", which is a very common
    shape."""

    def _state(self, field: Field) -> dict | None:
        try:
            return field.locator.evaluate(_SELECT_STATE_JS)
        except PlaywrightError as e:
            logger.debug("Could not read <select> state for %r (%s).", field.label, e)
            return None

    def supports(self, field: Field) -> bool:
        if field.tag_name != "select":
            return False
        try:
            if field.locator.is_visible():
                return True
        except PlaywrightError:
            return True  # can't tell — behave exactly as before this guard
        # Hidden. Decline only if something visible is clearly standing in for
        # it; otherwise still claim it (see `fill`, which forces the selection
        # rather than burning every attempt on actionability timeouts).
        try:
            has_proxy = bool(field.locator.evaluate(_SELECT_PROXY_JS))
        except PlaywrightError:
            has_proxy = False
        if has_proxy:
            logger.info(
                "%r is a hidden <select> with a visible control beside it — leaving it to that widget's handler.",
                field.label,
            )
            return False
        return True

    @staticmethod
    def _resolve_one(field_label: str, options: list[dict], target: str) -> dict:
        """The tiered, ambiguity-checked match. Raises `FieldFillRefused`
        rather than guessing."""
        selectable = [
            option for option in options
            if not option.get("disabled") and option.get("text")
            and not _is_placeholder_option(option["text"])
        ]
        if not selectable:
            raise FieldFillRefused(
                "no_selectable_options",
                f"<select> for {field_label!r} offers no selectable, non-placeholder options.",
            )

        normalized_target = _normalize_for_compare(target)
        tiers = (
            ("exact_text", [o for o in selectable if o["text"] == target]),
            ("exact_value", [o for o in selectable if o.get("value") == target]),
            ("normalized_text", [o for o in selectable if _normalize_for_compare(o["text"]) == normalized_target]),
            ("loose_text", [o for o in selectable if _values_match(target, o["text"])]),
        )
        for tier, matches in tiers:
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                texts = [o["text"] for o in matches]
                raise FieldFillRefused(
                    "ambiguous_option_match",
                    f"{target!r} matches {len(matches)} options equally well at the {tier} tier: {texts!r}. "
                    "Refusing rather than picking one.",
                    context={"ambiguous_options": texts, "match_tier": tier},
                )
        raise FieldFillRefused(
            "no_matching_option",
            f"No option matches {target!r}. Available: {[o['text'] for o in selectable]!r}.",
            context={"available_options": [o["text"] for o in selectable]},
        )

    def _select(self, field: Field, values: list[str], *, hidden: bool) -> None:
        try:
            field.locator.select_option(value=values, force=hidden)
        except PlaywrightError as e:
            if hidden:
                # Don't spend the remaining attempts re-timing-out on an
                # element that isn't rendered — say so, distinguishably.
                raise FieldFillRefused(
                    "hidden_native_select",
                    f"{field.label!r} is a <select> that isn't rendered and could not be set even with force: {e}",
                    context={"hidden_native_select": True},
                ) from e
            raise

    def fill(self, field: Field, value) -> None:
        state = self._state(field)
        if state is None:
            # Couldn't read the element at all — fall back to the original
            # blind attempts rather than refusing outright, so a select we
            # merely failed to introspect still has a chance of being filled.
            text_value = str(value)
            try:
                field.locator.select_option(label=text_value)
                return
            except PlaywrightError:
                field.locator.select_option(value=text_value)
                return

        options = state.get("options") or []
        hidden = not state.get("rendered", True)

        if state.get("multiple"):
            targets = _multi_targets(value)
            resolved = [self._resolve_one(field.label, options, target) for target in targets]
            self._select(field, [o.get("value", "") for o in resolved], hidden=hidden)
            return

        chosen = self._resolve_one(field.label, options, str(value))
        self._select(field, [chosen.get("value", "")], hidden=hidden)

    def verify(self, field: Field, value) -> tuple[bool, str | None]:
        state = self._state(field)
        if state is not None and state.get("multiple"):
            selected = [text for text in (state.get("selected") or []) if text]
            targets = _multi_targets(value)
            actual = ", ".join(selected)
            if len(selected) != len(targets):
                return False, actual
            # Every target must be represented exactly once in the selection.
            remaining = list(selected)
            for target in targets:
                match = next((s for s in remaining if _values_match(target, s)), None)
                if match is None:
                    return False, actual
                remaining.remove(match)
            return True, actual

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


def _matches_selector(locator: Locator, selector: str) -> bool:
    try:
        return bool(locator.evaluate("(el, sel) => el.matches(sel)", selector))
    except PlaywrightError:
        return False


def _field_search_input(field: Field) -> Locator | None:
    """This field's OWN text input, if its widget has one — checked before
    falling back to `_find_search_input(page)`.

    That page-wide fallback picks the first visible match anywhere, which is
    correct on a form with one dropdown and wrong on a real one. The live
    Greenhouse application has NINE elements matching `_SEARCH_INPUT_SELECTOR`
    (every react-select question is `input[role='combobox']
    [aria-autocomplete='list']`, plus the phone widget), all visible, so the
    page-wide lookup typed this field's answer into the first question's box
    every time. The element the label pass resolved IS the right input on that
    markup — so look at the field itself, and inside it, before looking at the
    page."""
    if _matches_selector(field.locator, _SEARCH_INPUT_SELECTOR):
        return field.locator
    nested = _first_visible(field.locator.locator(_SEARCH_INPUT_SELECTOR))
    if nested is not None:
        return nested
    # A react-select-style widget keeps its input as a SIBLING of the control
    # the label points at, under a shared shell — one hop up, then look down.
    try:
        parent = field.locator.locator("xpath=ancestor::*[self::div or self::fieldset][1]")
        if parent.count() > 0:
            return _first_visible(parent.first.locator(_SEARCH_INPUT_SELECTOR))
    except PlaywrightError:
        pass
    return None


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


def _option_containers(page: Page) -> list[Locator]:
    """Every visible `_LISTBOX_SELECTOR` match that actually holds visible
    options right now, in DOM order.

    The distinction from `_find_listbox_container()` (first visible match,
    full stop) is load-bearing on real forms. On the live Greenhouse
    application the first visible match is the intl-tel-input phone-country
    wrapper — `div.iti--inline-dropdown`, caught by `_LISTBOX_SELECTOR`'s
    `div[class*='dropdown' i]` branch, permanently visible, with its 244
    country options hidden inside. Anything that stopped at that first match
    looked into a container with zero visible options and concluded the
    dropdown had none, while the real `select__menu-list` sat two nodes
    further down the same query."""
    try:
        candidates = page.locator(_LISTBOX_SELECTOR).all()
    except PlaywrightError:
        return []
    containers = []
    for candidate in candidates:
        try:
            if not candidate.is_visible():
                continue
        except PlaywrightError:
            continue
        if _iter_visible_options(candidate):
            containers.append(candidate)
    return containers


def _find_matching_option(page: Page, value, matches: Callable[[str, object], bool]) -> Locator | None:
    """Searches every open option container, not just the first one found —
    see `_option_containers`. Falls back to scanning `body` when no container
    resolves at all, which is the original behavior and is safe here because
    this is looking for ONE known string rather than enumerating choices."""
    scopes = _option_containers(page) or [page.locator("body")]
    for scope in scopes:
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


#: Where a custom dropdown renders the value it is currently showing.
_DISPLAY_VALUE_SELECTOR = (
    "[class*='single-value' i], [class*='singlevalue' i], "
    "[class*='selected-value' i], [class*='selected-option' i]"
)


def _display_value_scopes(field: Field) -> list[Locator]:
    """`field.locator` first, then the widget's own control/container
    ancestor.

    The ancestors matter because the element the label pass resolves is often
    not the element that displays the value. On react-select — Greenhouse's
    boards, and the shape that exposed this — `<label for=...>` points at the
    `<input class="select__input">`, which has NO children, is `opacity: 0`
    once a value is chosen, and has its `value` attribute cleared by the
    library on selection. So all three of the original lookups (descendant
    selectors, `inner_text()`, `value`) came back empty on a dropdown that
    had in fact been filled correctly, `verify()` failed, and `fill_field()`
    burned all 3 attempts re-selecting an option that was already selected
    before reporting a bogus `verification_failed`. The text is one hop up, in
    a sibling `select__single-value`.

    Scoped to `control`/`container` ancestors ONLY, and used ONLY with the
    explicit `_DISPLAY_VALUE_SELECTOR` — deliberately never a broad
    `inner_text()` of an ancestor. On this very form `select__container`
    includes the question's own `<label>`, and `_values_match` is
    substring-tolerant in both directions, so reading a whole container's
    text would let the label "...If not, are you willing to relocate..."
    satisfy a check for the value "No"."""
    scopes = [field.locator]
    for xpath in (
        "xpath=ancestor::*[contains(@class,'control')][1]",
        "xpath=ancestor::*[contains(@class,'container')][1]",
    ):
        try:
            candidate = field.locator.locator(xpath)
            if candidate.count() > 0:
                scopes.append(candidate.first)
        except PlaywrightError:
            continue
    return scopes


def _read_dropdown_displayed_value(field: Field) -> str | None:
    for scope in _display_value_scopes(field):
        try:
            near = scope.locator(_DISPLAY_VALUE_SELECTOR)
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

        search_input = _field_search_input(field) or _find_search_input(field.page)
        if search_input is not None:
            logger.info("Searching option %r for %r.", value, field.label)
            # `click_first=False`: the search box is focused the moment the
            # menu opens, and clicking it again toggles the menu shut. This is
            # also the path where per-character typing matters most — a
            # searchable dropdown filters on keystroke events, and a bulk
            # `fill()` can leave the option list unfiltered.
            human_type(search_input, str(value), field.page, click_first=False)
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
# Option introspection
# ---------------------------------------------------------------------------

#: A native `<select>`'s first entry is usually a prompt, not a choice
#: ("Select...", "-- Choose one --", ""). Offering it to the answer engine as
#: a legitimate answer invites picking it; every real ATS treats it as
#: "nothing selected."
_PLACEHOLDER_OPTION_PATTERN = re.compile(
    r"^(|-+\s*)?(please\s+)?(select|choose|pick)\b.*$|^-+$|^\s*$", re.IGNORECASE
)


def _is_placeholder_option(text: str) -> bool:
    return bool(_PLACEHOLDER_OPTION_PATTERN.match(text.strip()))


def _close_popup(page: Page) -> None:
    """Best-effort restore after `_probe_dropdown_options` opened a menu.
    Escape is what closes every dropdown library in practice; the visibility
    re-check exists so a widget that ignores Escape doesn't silently leave an
    open menu covering the next field the sweep tries to touch."""
    try:
        page.keyboard.press("Escape")
    except PlaywrightError:
        return
    if not _popup_is_open(page):
        return
    try:
        page.locator("body").click(position={"x": 0, "y": 0}, timeout=_FAST_ACTION_TIMEOUT_MS)
    except PlaywrightError:
        logger.debug("Could not close a probed dropdown popup — leaving it to the fill pass.")


def _visible_options_container(page: Page) -> Locator | None:
    """A popup container that actually has visible options in it right now.

    Deliberately NOT `_popup_is_open()`. That predicate is satisfied by
    `_find_search_input()`, which matches `input[role='combobox']` /
    `input[aria-autocomplete='list']` — and on react-select (which is what
    Greenhouse's boards render) THAT INPUT IS THE WIDGET ITSELF, present and
    visible whether the menu is open or closed. Waiting on it returns
    instantly and always, so a probe built on it reads the DOM a render tick
    before the menu mounts and concludes there are no options. Waiting for a
    container that holds visible options is the condition that actually
    distinguishes open from closed.

    Also deliberately NOT `_find_listbox_container()`, which returns the first
    VISIBLE match and stops. On the live Greenhouse form that first match is
    the intl-tel-input phone-country wrapper (`div.iti--inline-dropdown`,
    which `_LISTBOX_SELECTOR`'s `div[class*='dropdown' i]` branch matches, and
    which is permanently visible with its 244 country options hidden inside),
    so the real `select__menu-list` two nodes later was never examined. Every
    candidate gets checked here, and the first one holding visible options
    wins — which also means the hidden-until-opened phone list can't be
    mistaken for this field's choices."""
    try:
        candidates = page.locator(_LISTBOX_SELECTOR).all()
    except PlaywrightError:
        return None
    for candidate in candidates:
        try:
            if not candidate.is_visible():
                continue
        except PlaywrightError:
            continue
        if _iter_visible_options(candidate):
            return candidate
    return None


def _wait_for_options(page: Page) -> Locator | None:
    """Polls for `_visible_options_container`, returning it as soon as one
    appears. Uses the same budget as `_wait_for_popup`."""
    found = wait_for_dynamic_element(
        lambda: _visible_options_container(page) is not None,
        page,
        timeout_ms=_POPUP_APPEAR_TIMEOUT_MS,
        poll_ms=_POPUP_APPEAR_POLL_MS,
    )
    return _visible_options_container(page) if found else None


def _probe_dropdown_options(field: Field) -> tuple[str, ...]:
    """Opens a custom dropdown just far enough to read its options, then
    closes it again. NEVER clicks an option — the trigger only — so this
    cannot change the field's value.

    This is a deliberate, narrow exception to "introspection doesn't touch
    the page." It was originally left out on the principle that a pre-answer
    read should be side-effect-free, but on a real Greenhouse posting EVERY
    screening dropdown is this kind of widget, so refusing to open them meant
    the engine answered every one of them blind — writing prose at a control
    that only accepts one of two words. A click-open/Escape-closed probe is
    exactly what a human applicant does before deciding, and `_DropdownHandler`
    re-opens the widget from scratch at fill time regardless.

    Returns `()` — and leaves the page as it found it — whenever the menu
    can't be opened or no real option container appears."""
    page = field.page
    try:
        page.keyboard.press("Escape")  # clear a stray popup from an earlier field
    except PlaywrightError:
        pass

    safe_click(field.locator, page)
    container = _wait_for_options(page)
    if container is None:
        # Same nested-control retry `_DropdownHandler.fill` needs: the
        # element resolved from a <label for=...> is often a wrapper, not the
        # library's actual clickable control.
        inner = _first_visible(field.locator.locator(_INNER_CLICK_TARGET_SELECTOR))
        if inner is not None and safe_click(inner, page):
            container = _wait_for_options(page)
    if container is None:
        # Deliberately NOT falling back to scanning `body` the way
        # `_find_matching_option` does: it's looking for one known string, so
        # a too-broad scope is harmless, whereas this reads EVERY match and
        # would happily return the page's nav links as if they were choices.
        _close_popup(page)
        logger.debug("Could not open %r to read its options — treating as free-text.", field.label)
        return ()

    options: list[str] = []
    for option in _iter_visible_options(container):
        try:
            text = (option.text_content() or "").strip()
        except PlaywrightError:
            continue
        if text and not _is_placeholder_option(text) and text not in options:
            options.append(text)

    _close_popup(page)
    logger.info("Read %d option(s) from the dropdown for %r.", len(options), field.label)
    return tuple(options)


def read_field_options(field: Field) -> tuple[str, ...]:
    """Every choice `field` offers, in DOM order, as the exact strings the
    page displays — or `()` if it isn't a fixed-choice control.

    - **Native `<select>`** — read directly from `el.options`. No interaction.
    - **Radio-flavored groups** (native radio, `role="radio"`, button
      choices) — reuses `RadioHandler`'s own group resolution, so the options
      the engine sees are exactly the ones the handler will later select
      among. No interaction.
    - **Custom dropdowns** (react-select, ARIA combobox/listbox) — opened,
      read, and closed by `_probe_dropdown_options`. The value is never
      touched; only the trigger is clicked.

    Two honest limits on the probed path. A **searchable** dropdown that
    loads its options from the server as you type shows only its initial page
    of results here, and a **virtualized** list only ever materializes its
    visible window — so for a long list (countries, universities) these
    options are a sample, not the full set. That's fine for the screening
    questions this exists for (Yes/No, seniority bands, notice periods) and
    is why `_match_option` treats "not in the list" as a reason to ask a
    human rather than proof the answer is wrong. `ComboboxHandler`/
    `VirtualizedListboxHandler` still do the real, complete option matching
    at fill time with the menu genuinely open and scrollable.
    """
    if field.tag_name == "select":
        try:
            texts = field.locator.evaluate(
                "el => Array.from(el.options || []).map(o => (o.label || o.textContent || '').trim())"
            )
        except PlaywrightError as e:
            logger.debug("Could not read <select> options for %r (%s) — treating as free-text.", field.label, e)
            return ()
        if not isinstance(texts, list):
            return ()
        return tuple(t for t in (str(x) for x in texts) if t and not _is_placeholder_option(t))

    radio_handler = RadioHandler()
    try:
        is_radio = radio_handler.supports(field)
    except PlaywrightError:
        return ()
    if is_radio:
        group = radio_handler._group(field)  # noqa: SLF001 - same module; deliberately the handler's own grouping
        try:
            count = group.count()
        except PlaywrightError:
            return ()
        options = []
        for i in range(count):
            text = _option_text(field.page, group.nth(i)).strip()
            if text and text not in options:
                options.append(text)
        return tuple(options)

    # Anything a dropdown handler would claim at fill time gets probed, so
    # the engine sees the same choices the handler will later select among.
    try:
        is_dropdown = ComboboxHandler().supports(field) or ReactSelectHandler().supports(field)
    except PlaywrightError:
        return ()
    if is_dropdown:
        return _probe_dropdown_options(field)
    return ()


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
        # Distinguish "we don't support this widget" from "we never managed to
        # look at it." Both used to surface as a bare `no_handler_matched`
        # with the real cause discarded, which is why a class of failures here
        # stayed unexplained for so long.
        introspection_failed = field.introspection_error is not None
        reason = "introspection_failed" if introspection_failed else "no_handler_matched"
        if introspection_failed:
            logger.info(
                "No handler for %r because its shape was never readable (%s).",
                field.label, field.introspection_error,
            )
            failure_context = {**failure_context, "introspection_failed": True}
        else:
            logger.info("Unknown field type for %r — skipping.", field.label)
        failure = FieldFailure(
            field.label, field.tag_name or "unknown", str(value), None, reason, 0,
            widget_type="unknown", context=failure_context,
            last_exception=field.introspection_error,
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
        except FieldFillRefused as refusal:
            # A positive decision not to touch this field — see FieldFillRefused.
            # Deterministic, so the remaining attempts would reach the same
            # conclusion; report it now and hand the field to a human.
            logger.warning("Declining to fill %r: %s", field.label, refusal.detail or refusal.reason)
            failure = FieldFailure(
                field.label, field.tag_name or "unknown", str(value), None, refusal.reason, 0,
                widget_type=type(handler).__name__,
                context={**failure_context, **refusal.context},
                last_exception=None,
                element_html=_capture_element_html(field.locator),
            )
            logger.warning(format_failure_report(failure))
            human_pause_between_fields(field.page)
            return HandlerOutcome(filled=False, actual_value=None, failure=failure)
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
            # Once per field, after the retry loop settles — never per attempt,
            # which would make a 3-attempt field pause for 6 seconds.
            human_pause_between_fields(field.page)
            return HandlerOutcome(filled=True, actual_value=actual_value, failure=None)

        logger.info("Verification failed for %r (attempt %d).", field.label, attempt)

    logger.warning("Giving up on %r after %d attempt(s).", field.label, max_attempts)
    human_pause_between_fields(field.page)
    failure = FieldFailure(
        field.label, field.tag_name or "unknown", str(value), actual_value, "verification_failed", max_attempts - 1,
        widget_type=type(handler).__name__, context=failure_context,
        last_exception=last_exception, element_html=_capture_element_html(field.locator),
    )
    logger.warning(format_failure_report(failure))
    return HandlerOutcome(filled=False, actual_value=actual_value, failure=failure)
