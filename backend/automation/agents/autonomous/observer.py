"""
PageState observer — turns a live Playwright `Page` into a compact,
structured snapshot the LLM decision step can reason over.

This is NOT raw HTML and NOT arbitrary LLM-authored JavaScript: the
extraction script below is fixed code we wrote and control, run the same way
on every page, that walks the DOM for interactive/readable elements and tags
each one with a stable `data-agent-ref` attribute. The LLM never sees a CSS
selector or writes one — it picks an action against one of the numbered
`element_ref` values this module already found, and `executor.py` resolves
that ref back to the real element via `[data-agent-ref="<n>"]`.

Kept independent of `automation/browser/selectors.py`
(ATS-field-name-pattern matching, used by the deterministic adapters) and
`automation/forms/vision_fallback.py` (screenshot-based, LLM-vision) —
neither is a general "describe whatever this page currently looks like"
primitive, which is what the autonomous loop needs every iteration.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError, Page

from automation.agents.autonomous.action_semantics import classify_semantic_action
from automation.browser.selectors import REVIEW_PAGE_TEXT_PATTERNS

logger = logging.getLogger(__name__)

#: Plausible post-submit confirmation phrases — moved here (from
#: `loop.py::_page_shows_confirmation`, which now imports this) so
#: `classify_page_type` below can reuse the exact same check rather than
#: keeping a second, driftable copy.
CONFIRMATION_PHRASES = [
    "application submitted", "successfully submitted", "thank you for applying",
    "thank you for your application", "application received", "your application has been",
    "we have received your application", "application complete",
]

#: Cap on how many interactive elements go into one PageState — a long
#: multi-page application form can have hundreds of DOM nodes; the LLM only
#: needs the ones actually visible/relevant to decide the next step, and an
#: unbounded list would blow the context budget for no benefit.
MAX_ELEMENTS = 120

#: Cap on the plain-text summary of the page (title + visible body text),
#: separate from MAX_ELEMENTS — keeps the prompt bounded even on a page whose
#: interactive-element count is small but whose body text is huge (a long
#: job description embedded in the application page itself).
MAX_TEXT_CHARS = 4000

# A single, fixed extraction script — never templated with LLM output, never
# regenerated per page. It tags every candidate element with a stable
# `data-agent-ref` index (in DOM order) and returns a plain-data description
# of each: tag, role/type, accessible name, current value, and whether it
# looks required/disabled.
_EXTRACT_ELEMENTS_JS = r"""
() => {
  const SELECTOR = [
    'a[href]', 'button', 'input', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="checkbox"]',
    '[role="radio"]', '[role="combobox"]', '[contenteditable="true"]',
    // 'option': the individual choices inside an OPENED custom dropdown
    // (react-select-style, or a WAI-ARIA listbox/grid like Amex's
    // cx-select-input). Without this, a custom dropdown's options never
    // become element_refs at all once the trigger is clicked open, so the
    // agent has nothing to click to actually pick a value.
    '[role="option"]',
  ].join(',');

  function accessibleName(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const parts = labelledBy.split(/\\s+/).map(id => {
        const node = document.getElementById(id);
        return node ? node.innerText || node.textContent || '' : '';
      });
      const joined = parts.join(' ').trim();
      if (joined) return joined;
    }
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label && (label.innerText || label.textContent)) {
        return (label.innerText || label.textContent).trim();
      }
    }
    const closestLabel = el.closest('label');
    if (closestLabel) {
      const text = (closestLabel.innerText || closestLabel.textContent || '').trim();
      if (text) return text;
    }
    const placeholder = el.getAttribute('placeholder');
    if (placeholder) return placeholder.trim();
    const title = el.getAttribute('title');
    if (title) return title.trim();
    const text = (el.innerText || el.textContent || '').trim();
    if (text) return text.slice(0, 200);
    const name = el.getAttribute('name');
    if (name) return name;
    return '';
  }

  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  }

  function textOf(el) {
    return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function nearbyText(el) {
    const container = el.closest('fieldset, [role="group"], form, section, article, .field, .form-field');
    return container ? textOf(container).slice(0, 350) : '';
  }

  function sectionName(el) {
    const section = el.closest('fieldset, section, [role="group"], form');
    if (!section) return '';
    const title = section.querySelector('legend,h1,h2,h3,h4,h5,h6,[role="heading"]');
    return title ? textOf(title).slice(0, 160) : '';
  }

  const nodes = Array.from(document.querySelectorAll(SELECTOR)).filter(isVisible);
  const out = [];
  nodes.forEach((el, idx) => {
    el.setAttribute('data-agent-ref', String(idx));
    const tag = el.tagName.toLowerCase();
    let type = el.getAttribute('type') || el.getAttribute('role') || tag;
    let options = null;
    if (tag === 'select') {
      options = Array.from(el.options).map(o => o.textContent.trim());
    }
    const rect = el.getBoundingClientRect();
    const form = el.form || el.closest('form');
    out.push({
      ref: idx,
      tag,
      type,
      name: accessibleName(el),
      aria_label: (el.getAttribute('aria-label') || '').trim(),
      // Distinct from `type`, which prefers the native HTML `type`
      // ATTRIBUTE when present — a real custom combobox is very commonly an
      // `<input type="text" role="combobox">` (react-select, MUI Autocomplete,
      // Amex's cx-select-input), where `type` resolves to "text", not
      // "combobox". Widget-semantics code (see `loop.py::_widget_type`) reads
      // `role` for that reason; `type` keeps its existing meaning everywhere
      // else (native form-control kind).
      role: (el.getAttribute('role') || '').toLowerCase(),
      title: (el.getAttribute('title') || '').trim(),
      href: el.href || null,
      position: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)},
      form: form ? {id: form.id || '', name: form.getAttribute('name') || '', action: form.action || ''} : null,
      surrounding_text: nearbyText(el),
      section: sectionName(el),
      // Password values are masked at the source — a raw password must never
      // reach a PageState (persisted to the DB, sent to the LLM prompt, or
      // returned via the status API). Non-secret fields keep their real value.
      value: (type === 'password') ? null : ('value' in el ? String(el.value || '').slice(0, 300) : null),
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      disabled: !!el.disabled,
      checked: 'checked' in el ? !!el.checked : null,
      // Tri-state: true/false when the element actually declares
      // aria-selected (an option inside an opened listbox/grid), null when
      // it doesn't apply (most elements). This is the WAI-ARIA-authoritative
      // "is this option currently committed" signal — surfaced to the LLM
      // directly so it can see an already-selected option without guessing.
      aria_selected: el.hasAttribute('aria-selected') ? (el.getAttribute('aria-selected') === 'true') : null,
      options,
      selected_options: tag === 'select' ? Array.from(el.selectedOptions || []).map(o => textOf(o)) : null,
      autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
      inputmode: (el.getAttribute('inputmode') || '').toLowerCase(),
      maxlength: el.getAttribute('maxlength') ? parseInt(el.getAttribute('maxlength'), 10) : null,
      validation_message: ('validationMessage' in el ? String(el.validationMessage || '') : '').slice(0, 300),
      invalid: el.getAttribute('aria-invalid') === 'true' || ('validity' in el && !el.validity.valid),
    });
  });
  const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]'))
    .filter(isVisible).map(textOf).filter(Boolean).slice(0, 40);
  const dialogs = Array.from(document.querySelectorAll('dialog,[role="dialog"],[role="alertdialog"]'))
    .filter(isVisible).map(textOf).filter(Boolean).map(t => t.slice(0, 500)).slice(0, 10);
  const validation_messages = Array.from(document.querySelectorAll(
    '[role="alert"], [aria-live="assertive"], .error, .errors, .field-error, .validation-error, [data-error]'
  )).filter(isVisible).map(textOf).filter(Boolean).map(t => t.slice(0, 300)).slice(0, 30);
  const forms = Array.from(document.forms).filter(isVisible).map(form => ({
    id: form.id || '', name: form.getAttribute('name') || '', action: form.action || '',
    method: (form.method || 'get').toLowerCase(),
  })).slice(0, 20);
  return {elements: out.slice(0, __MAX_ELEMENTS__), headings, dialogs, validation_messages, forms};
}
"""


@dataclass
class PageElement:
    ref: int
    tag: str
    type: str
    name: str
    value: str | None
    required: bool
    disabled: bool
    checked: bool | None
    options: list[str] | None
    autocomplete: str = ""
    inputmode: str = ""
    maxlength: int | None = None
    aria_label: str = ""
    #: Raw ARIA `role`, distinct from `type` (which prefers a native HTML
    #: `type` ATTRIBUTE when present — see `_EXTRACT_ELEMENTS_JS`). A real
    #: custom combobox is very commonly `<input type="text" role="combobox">`,
    #: where `type` resolves to "text"; widget-semantics code that needs to
    #: recognize a combobox/option regardless of a native type attribute reads
    #: this field instead (see `loop.py::_widget_type`).
    role: str = ""
    title: str = ""
    href: str | None = None
    position: dict | None = None
    form: dict | None = None
    surrounding_text: str = ""
    section: str = ""
    selected_options: list[str] | None = None
    validation_message: str = ""
    invalid: bool = False
    #: Tri-state WAI-ARIA `aria-selected` reading — see `_EXTRACT_ELEMENTS_JS`.
    #: Only meaningful for `type == "option"` (an item inside an opened
    #: listbox/grid); `None` for everything else.
    aria_selected: bool | None = None
    semantic_action: str | None = None
    action_confidence: str | None = None
    irreversible: bool = False

    def as_dict(self) -> dict:
        return {
            "ref": self.ref, "tag": self.tag, "type": self.type, "name": self.name,
            "value": self.value, "required": self.required, "disabled": self.disabled,
            "enabled": not self.disabled,
            "checked": self.checked, "options": self.options,
            "aria_label": self.aria_label, "role": self.role, "title": self.title, "href": self.href,
            "position": self.position, "form": self.form,
            "surrounding_text": self.surrounding_text, "section": self.section,
            "selected_options": self.selected_options,
            "validation_message": self.validation_message, "invalid": self.invalid,
            "aria_selected": self.aria_selected,
            "semantic_action": self.semantic_action,
            "action_confidence": self.action_confidence,
            "irreversible": self.irreversible,
        }


@dataclass
class PageState:
    url: str
    title: str
    visible_text: str
    elements: list[PageElement] = field(default_factory=list)
    #: Optional ATS-adapter hint — see `automation/ats/detector.py`. Purely
    #: informational context for the LLM prompt; never used for control flow
    #: here (no per-ATS branching in the autonomous loop).
    detected_ats_platform: str | None = None
    #: Deterministic (non-LLM) human-blocker detection — see `detect_blocker`
    #: below. Never contains a secret value, only which field/button was
    #: found and non-sensitive context (masked destination text). When set,
    #: `loop.py` requests human intervention WITHOUT calling the LLM decision
    #: step at all for that iteration (Layer 1/2 of the spec's detection
    #: strategy; the LLM is Layer 3, used only when this is None).
    blocker_hint: dict | None = None
    page_type: str = "unknown"
    workflow_state: str = "ANALYZING"
    headings: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)
    validation_messages: list[str] = field(default_factory=list)
    forms: list[dict] = field(default_factory=list)
    required_fields: list[int] = field(default_factory=list)
    blocking_messages: list[str] = field(default_factory=list)
    page_signature: str = ""

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "visible_text": self.visible_text,
            "elements": [e.as_dict() for e in self.elements],
            "detected_ats_platform": self.detected_ats_platform,
            "page_type": self.page_type,
            "workflow_state": self.workflow_state,
            "headings": self.headings,
            "dialogs": self.dialogs,
            "validation_messages": self.validation_messages,
            "forms": self.forms,
            "required_fields": self.required_fields,
            "blocking_messages": self.blocking_messages,
            "page_signature": self.page_signature,
        }


@dataclass(frozen=True)
class PageCompletion:
    ready: bool
    missing_required_refs: list[int] = field(default_factory=list)
    validation_messages: list[str] = field(default_factory=list)
    blocking_messages: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        parts = []
        if self.missing_required_refs:
            parts.append(f"{len(self.missing_required_refs)} required field(s) are incomplete")
        if self.validation_messages:
            parts.append(f"{len(self.validation_messages)} validation error(s) are visible")
        if self.blocking_messages:
            parts.append(f"{len(self.blocking_messages)} blocking dialog/message(s) remain")
        return "; ".join(parts) or "page is complete"


def compute_page_completion(page_state: PageState) -> PageCompletion:
    """Spec §17's page-completion gate: is this page actually done, or would
    moving on (or offering it up for final-submit approval) skip a required
    field / leave a validation error on screen?

    A required text/select field counts as missing when empty; a required
    checkbox/radio counts as missing when unchecked. Disabled controls are
    never "missing" — a field the page itself has turned off isn't something
    the candidate can fill."""
    missing_refs: list[int] = []
    for element in page_state.elements:
        if not element.required or element.disabled:
            continue
        if element.type in ("checkbox", "radio"):
            if not element.checked:
                missing_refs.append(element.ref)
        elif element.tag == "select":
            if not element.value and not element.selected_options:
                missing_refs.append(element.ref)
        elif not (element.value or "").strip():
            missing_refs.append(element.ref)

    return PageCompletion(
        ready=not missing_refs and not page_state.validation_messages and not page_state.dialogs,
        missing_required_refs=missing_refs,
        validation_messages=list(page_state.validation_messages),
        blocking_messages=list(page_state.dialogs),
    )


#: Maps a deterministic Layer-1/2 blocker (`detect_blocker`, below) straight
#: to the spec's page_type vocabulary — checked first in `classify_page_type`
#: since it's the strongest, non-text-heuristic signal available.
_BLOCKER_TO_PAGE_TYPE = {
    "OTP_REQUIRED": "verification",
    "MFA_REQUIRED": "verification",
    "CAPTCHA_REQUIRED": "captcha",
    "LOGIN_REQUIRED": "login",
}


def classify_page_type(page_state: "PageState") -> str:
    """One of the application-state vocabulary: JOB_LISTING, application_page, login,
    verification, captcha, review, confirmation, unknown. Built entirely from
    signals `observe_page` already computed — no separate LLM call, and never
    a source of truth on its own (`AutonomousTask.current_status`, driven by
    the loop's actual control flow, remains authoritative; this is contextual
    labeling for the LLM prompt and the status API)."""
    if page_state.blocker_hint:
        mapped = _BLOCKER_TO_PAGE_TYPE.get(page_state.blocker_hint.get("request_type"))
        if mapped:
            return mapped

    haystack = f"{page_state.title} {page_state.visible_text}".lower()
    if any(phrase in haystack for phrase in CONFIRMATION_PHRASES):
        return "confirmation"
    if any(phrase in haystack for phrase in REVIEW_PAGE_TEXT_PATTERNS):
        return "review"

    has_form_fields = any(
        el.tag in ("input", "textarea", "select") or el.type == "combobox"
        for el in page_state.elements
    )
    has_apply_entry = any(el.semantic_action in ("APPLY", "START_APPLICATION") for el in page_state.elements)
    # Listing pages frequently contain search/filter/email-alert fields, so
    # the mere presence of an input must not make them application forms. A
    # visible apply-entry control plus ordinary job metadata is the stronger
    # signal. None of these phrases or structures is tied to an ATS/vendor.
    job_metadata_phrases = (
        "job description", "responsibilities", "qualifications", "requirements",
        "about the role", "about the job", "employment type", "job id",
        "requisition", "salary", "compensation", "benefits", "location",
    )
    metadata_hits = sum(1 for phrase in job_metadata_phrases if phrase in haystack)
    meaningful_heading = any(
        len(" ".join(str(heading).split())) >= 5
        for heading in page_state.headings
    ) or len((page_state.title or "").strip()) >= 8
    application_field_count = sum(
        1 for el in page_state.elements
        if el.tag in ("input", "textarea", "select") or el.type == "combobox"
    )
    if has_apply_entry and (meaningful_heading or metadata_hits > 0 or application_field_count < 3):
        return "JOB_LISTING"

    if has_form_fields:
        return "application_page"

    return "unknown"


def field_identity(element: PageElement) -> str:
    """A stable identity for one logical field, independent of its
    DOM-order-based `ref` (which can shift across re-observations of the same
    page — a field re-rendered after a validation error, for instance, is
    still "the same field" a human would recognize, even if its ref index
    moved). Used by `AutonomousTask.field_attempt_ledger` (spec §16) to
    remember a field failed across resumes/process-restarts, which `ref`
    alone cannot do."""
    normalized_name = " ".join((element.name or "").split()).strip().lower()
    key = f"{element.tag}|{normalized_name}|{element.section}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def observe_page(page: Page, *, ats_hint: str | None = None) -> PageState:
    """Runs the fixed extraction script and returns a `PageState`. Best-effort
    on every sub-step (a slow-loading widget, a transient navigation) so one
    flaky read never crashes the whole loop iteration — an empty/partial
    `PageState` just means the next LLM call sees less than ideal context,
    which is a `REQUEST_HUMAN_INTERVENTION`-worthy situation the LLM itself
    can react to, not a Python exception."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except PlaywrightError as e:
        logger.debug("observe_page: load-state wait failed (continuing): %s", e)

    url = ""
    title = ""
    visible_text = ""
    raw_elements: list[dict] = []

    try:
        url = page.url
    except PlaywrightError:
        pass
    try:
        title = page.title()
    except PlaywrightError:
        pass
    try:
        body_text = page.inner_text("body")
        visible_text = " ".join(body_text.split())[:MAX_TEXT_CHARS]
    except PlaywrightError as e:
        logger.debug("observe_page: could not read body text: %s", e)

    extraction: dict = {}
    try:
        script = _EXTRACT_ELEMENTS_JS.replace("__MAX_ELEMENTS__", str(MAX_ELEMENTS))
        extraction = page.evaluate(script) or {}
    except PlaywrightError as e:
        logger.warning("observe_page: element extraction failed: %s", e)

    # The extraction script returns ONE object — {elements, headings, dialogs,
    # validation_messages, forms} — not a bare array. `raw_elements` must be
    # its "elements" key, never the object itself (iterating the object
    # would walk its string keys, not element dicts).
    raw_elements: list[dict] = extraction.get("elements") or []

    elements = [
        PageElement(
            ref=el["ref"], tag=el["tag"], type=el["type"], name=el["name"] or "",
            value=el.get("value"), required=bool(el.get("required")),
            disabled=bool(el.get("disabled")), checked=el.get("checked"),
            options=el.get("options"), autocomplete=el.get("autocomplete") or "",
            inputmode=el.get("inputmode") or "", maxlength=el.get("maxlength"),
            aria_label=el.get("aria_label") or "", role=el.get("role") or "", title=el.get("title") or "",
            href=el.get("href"), position=el.get("position"), form=el.get("form"),
            surrounding_text=el.get("surrounding_text") or "", section=el.get("section") or "",
            selected_options=el.get("selected_options"),
            validation_message=el.get("validation_message") or "",
            invalid=bool(el.get("invalid")),
            aria_selected=el.get("aria_selected"),
        )
        for el in raw_elements
    ]

    for element in elements:
        semantic_action, action_confidence, irreversible = classify_semantic_action(element)
        element.semantic_action = semantic_action
        element.action_confidence = action_confidence
        element.irreversible = irreversible

    try:
        blocker_hint = detect_blocker(url, visible_text, raw_elements)
    except Exception as e:  # noqa: BLE001 — detection failing must never crash the loop iteration
        logger.warning("observe_page: blocker detection failed: %s", e)
        blocker_hint = None

    page_state = PageState(
        url=url, title=title, visible_text=visible_text, elements=elements,
        detected_ats_platform=ats_hint, blocker_hint=blocker_hint,
        headings=extraction.get("headings") or [],
        dialogs=extraction.get("dialogs") or [],
        validation_messages=extraction.get("validation_messages") or [],
        forms=extraction.get("forms") or [],
    )
    page_state.page_type = classify_page_type(page_state)
    return page_state


# ---------------------------------------------------------------------------
# Deterministic (non-LLM) human-blocker detection — spec Layers 1 & 2.
# ---------------------------------------------------------------------------

#: Layer 2: phrases in labels/placeholders/headings/button text/surrounding
#: visible text that indicate a verification-code or 2FA challenge.
_OTP_TEXT_RE = re.compile(
    r"one[-\s]?time\s*(password|code|pass)|verification\s*code|security\s*code|"
    r"authenticat(?:or|ion)\s*code|two[-\s]?factor|2fa\b|\botp\b|"
    r"verify\s*your\s*identity|enter\s*the\s*code|code\s*(?:sent|we\s*sent)|"
    r"check\s*your\s*(?:email|inbox|phone|messages)\s*for\s*(?:a|the)?\s*code",
    re.IGNORECASE,
)
#: Layer 1: HTML-native OTP signals (name/id/placeholder patterns) — a
#: fallback for pages that don't set `autocomplete="one-time-code"`.
_OTP_FIELD_NAME_RE = re.compile(r"(?:one.?time|verification|security).{0,15}(?:code|pass)|\botp\b|\b2fa\b|\bmfa\b", re.IGNORECASE)
_MFA_TEXT_RE = re.compile(r"two[-\s]?factor|2fa\b|\bmfa\b|authenticator\s*app", re.IGNORECASE)
_CAPTCHA_TEXT_RE = re.compile(
    r"captcha|i'?m\s*not\s*a\s*robot|verify\s*you\s*are\s*human|prove\s*you'?re\s*human|security\s*check\b",
    re.IGNORECASE,
)
_LOGIN_TEXT_RE = re.compile(
    r"sign\s*in\s*to\s*(?:continue|your\s*account)|log\s*in\s*to\s*(?:continue|apply)|please\s*(?:sign|log)\s*in\b",
    re.IGNORECASE,
)
_VERIFY_CONTROL_RE = re.compile(r"\b(verify|confirm|continue|submit)\b", re.IGNORECASE)
#: Best-effort, cosmetic-only "masked destination" strings for the UI
#: (e.g. "j***@gmail.com", "ending in 1234") — never a full/raw address, and
#: never anything secret; purely a courtesy so the user knows which inbox/
#: phone to check.
_MASKED_EMAIL_RE = re.compile(r"[\w.+-]*\*{2,}[\w.+-]*@[\w.-]+\.\w+")
_MASKED_PHONE_RE = re.compile(r"(?:ending in|ending with)\s*[\d\s-]{2,6}\d{2,4}", re.IGNORECASE)


def _find_masked_destination(text: str) -> str | None:
    m = _MASKED_EMAIL_RE.search(text) or _MASKED_PHONE_RE.search(text)
    return m.group(0) if m else None


def _find_submit_ref(raw_elements: list[dict]) -> int | None:
    """Best-effort: the first button/link whose accessible name looks like a
    verify/continue/submit control. Only ever used so a deterministic OTP
    auto-fill (`loop.py`) can click "Verify" after typing the code — the
    click still goes through the normal `ActionExecutor`, subject to its
    usual safety gates, exactly like any other click."""
    for el in raw_elements:
        if el.get("tag") in ("button", "a") or (el.get("type") in ("button", "submit")):
            if _VERIFY_CONTROL_RE.search(el.get("name") or ""):
                return el["ref"]
    return None


def detect_blocker(url: str, visible_text: str, raw_elements: list[dict]) -> dict | None:
    """Layers 1 + 2 of the human-blocker detection strategy: HTML-native
    signals (password fields, `autocomplete="one-time-code"`, input
    attributes) and DOM/accessibility text matching (labels, headings,
    button text, surrounding copy) — no LLM call. Returns a dict describing
    the blocker (request_type + non-secret context) or `None` if nothing
    was confidently detected here, in which case `loop.py` falls back to the
    LLM's own classification (Layer 3) for this iteration.

    Never returns or references a secret value. Checked most-specific,
    strongest-signal first: an actual OTP/MFA code field or a password field
    (Layer 1, HTML-native) both outrank plain text matching (Layer 2) — a
    page that happens to mention "code" in unrelated copy while also showing
    a real password field is a login wall, not a false OTP match. CAPTCHA/
    login TEXT matching is the least specific and checked last.
    """
    haystack = (visible_text or "")[:4000]

    otp_ref = None
    password_ref = None
    for el in raw_elements:
        el_type = (el.get("type") or "").lower()
        if el_type == "password":
            if password_ref is None:
                password_ref = el["ref"]
            continue  # a password field is a LOGIN_REQUIRED signal, not OTP
        autocomplete = el.get("autocomplete") or ""
        name = el.get("name") or ""
        maxlength = el.get("maxlength")
        looks_like_code_field = (
            autocomplete == "one-time-code"
            or bool(_OTP_FIELD_NAME_RE.search(name))
            or (el.get("inputmode") == "numeric" and maxlength and 0 < maxlength <= 8)
        )
        if looks_like_code_field and otp_ref is None:
            otp_ref = el["ref"]

    if otp_ref is not None:
        return {
            "request_type": "MFA_REQUIRED" if _MFA_TEXT_RE.search(haystack) else "OTP_REQUIRED",
            "reason": "A verification-code input field was detected on the page.",
            "otp_field_ref": otp_ref,
            "submit_ref": _find_submit_ref(raw_elements),
            "masked_destination": _find_masked_destination(haystack),
        }

    if password_ref is not None:
        return {
            "request_type": "LOGIN_REQUIRED",
            "reason": "A password field was detected — Autogram never enters credentials on the user's behalf.",
            "otp_field_ref": None, "submit_ref": None, "masked_destination": None,
        }

    if _OTP_TEXT_RE.search(haystack):
        return {
            "request_type": "MFA_REQUIRED" if _MFA_TEXT_RE.search(haystack) else "OTP_REQUIRED",
            "reason": "Verification-code language was detected on the page.",
            "otp_field_ref": None,
            "submit_ref": _find_submit_ref(raw_elements),
            "masked_destination": _find_masked_destination(haystack),
        }

    if _CAPTCHA_TEXT_RE.search(haystack):
        return {
            "request_type": "CAPTCHA_REQUIRED",
            "reason": "The page appears to be showing a CAPTCHA / anti-bot challenge.",
            "otp_field_ref": None, "submit_ref": None, "masked_destination": None,
        }

    if _LOGIN_TEXT_RE.search(haystack):
        return {
            "request_type": "LOGIN_REQUIRED",
            "reason": "The page's text asks the user to sign in / log in to continue.",
            "otp_field_ref": None, "submit_ref": None, "masked_destination": None,
        }

    return None
