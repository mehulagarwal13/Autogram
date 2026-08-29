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

import logging
import re
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError, Page

logger = logging.getLogger(__name__)

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
_EXTRACT_ELEMENTS_JS = """
() => {
  const SELECTOR = [
    'a[href]', 'button', 'input', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="checkbox"]',
    '[role="radio"]', '[role="combobox"]', '[contenteditable="true"]',
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
    out.push({
      ref: idx,
      tag,
      type,
      name: accessibleName(el),
      // Password values are masked at the source — a raw password must never
      // reach a PageState (persisted to the DB, sent to the LLM prompt, or
      // returned via the status API). Non-secret fields keep their real value.
      value: (type === 'password') ? null : ('value' in el ? String(el.value || '').slice(0, 300) : null),
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      disabled: !!el.disabled,
      checked: 'checked' in el ? !!el.checked : null,
      options,
      autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
      inputmode: (el.getAttribute('inputmode') || '').toLowerCase(),
      maxlength: el.getAttribute('maxlength') ? parseInt(el.getAttribute('maxlength'), 10) : null,
    });
  });
  return out.slice(0, __MAX_ELEMENTS__);
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

    def as_dict(self) -> dict:
        return {
            "ref": self.ref, "tag": self.tag, "type": self.type, "name": self.name,
            "value": self.value, "required": self.required, "disabled": self.disabled,
            "checked": self.checked, "options": self.options,
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

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "visible_text": self.visible_text,
            "elements": [e.as_dict() for e in self.elements],
            "detected_ats_platform": self.detected_ats_platform,
        }


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

    try:
        script = _EXTRACT_ELEMENTS_JS.replace("__MAX_ELEMENTS__", str(MAX_ELEMENTS))
        raw_elements = page.evaluate(script) or []
    except PlaywrightError as e:
        logger.warning("observe_page: element extraction failed: %s", e)

    elements = [
        PageElement(
            ref=el["ref"], tag=el["tag"], type=el["type"], name=el["name"] or "",
            value=el.get("value"), required=bool(el.get("required")),
            disabled=bool(el.get("disabled")), checked=el.get("checked"),
            options=el.get("options"), autocomplete=el.get("autocomplete") or "",
            inputmode=el.get("inputmode") or "", maxlength=el.get("maxlength"),
        )
        for el in raw_elements
    ]

    try:
        blocker_hint = detect_blocker(url, visible_text, raw_elements)
    except Exception as e:  # noqa: BLE001 — detection failing must never crash the loop iteration
        logger.warning("observe_page: blocker detection failed: %s", e)
        blocker_hint = None

    return PageState(
        url=url, title=title, visible_text=visible_text, elements=elements,
        detected_ats_platform=ats_hint, blocker_hint=blocker_hint,
    )


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
