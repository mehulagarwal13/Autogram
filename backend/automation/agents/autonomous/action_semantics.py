"""
Normalizes a button/link/control's accessible name into the closed,
product-facing action vocabulary the spec defines (§5) — `PageElement
.semantic_action`, populated by `observer.py::observe_page` for every element
it extracts. Purely informational context handed to the LLM decision prompt
(`decision.py`) and recorded in `action_history`; it does not by itself decide
anything the executor wasn't already deciding.

This is deliberately the ONE place a "submit button" pattern lives for the
autonomous-agent path. `executor.py::SUBMIT_BUTTON_PATTERNS` used to be its
own, independently-maintained copy — that duplication is exactly what the
project's "don't create parallel systems" rule warns against, so `executor.py`
now imports `SUBMIT_BUTTON_PATTERNS`/`is_submit_control_name` from here.

Deliberately NOT shared with the deterministic ATS-adapter engine's own
`automation/browser/selectors.py` (`NEXT_BUTTON_TEXT_CANDIDATES` /
`SUBMIT_BUTTON_TEXT_CANDIDATES` / `APPLY_ENTRY_BUTTON_TEXT_CANDIDATES`) — that
module matches exact/loose button TEXT via Playwright's `get_by_role`, tuned
for a different call shape (find-a-button-on-the-page vs
classify-an-already-extracted-element's-name) and a different, independently
tested engine. The phrase choices below are informed by that module's
candidate lists so the two vocabularies can be reconciled later, but this
does not import from or edit it.
"""

from __future__ import annotations

import re

#: The spec's closed, normalized action vocabulary (§5). Anything that
#: doesn't confidently match one of these becomes "UNKNOWN" — never guessed.
SEMANTIC_ACTIONS = frozenset({
    "APPLY", "START_APPLICATION", "CONTINUE", "NEXT", "SAVE",
    "SAVE_AND_CONTINUE", "REVIEW", "VERIFY", "LOGIN", "UPLOAD", "ADD",
    "SUBMIT", "UNKNOWN",
})

#: Final submission is the only irreversible application-flow action and has
#: an independent executor-side approval gate.
IRREVERSIBLE_ACTIONS = frozenset({"SUBMIT"})

# ---------------------------------------------------------------------------
# SUBMIT — moved here verbatim from executor.py (see module docstring). The
# Entry into an application and final submission are intentionally disjoint.
# In particular, "Apply Now" is never a submit synonym: on a listing it means
# start/open the application. Final submission needs explicit finality language.
# ---------------------------------------------------------------------------
SUBMIT_BUTTON_PATTERNS = [
    r"^submit\b", r"submit\s*(my\s*)?application",
    r"finish\s*application", r"complete\s*application", r"send\s*application",
]
_SUBMIT_RE = re.compile("|".join(SUBMIT_BUTTON_PATTERNS), re.IGNORECASE)
_BARE_SUBMIT_RE = re.compile(r"^submit\s*$", re.IGNORECASE)
_APPLICATION_CONTEXT_RE = re.compile(
    r"application|applicant|candidate|resume|résumé|cover\s*letter|employment|final\s*review|job",
    re.IGNORECASE,
)


def is_submit_control_name(name: str) -> bool:
    return bool(_SUBMIT_RE.search(name or ""))


# ---------------------------------------------------------------------------
# Every other verb: (pattern, confidence) pairs, most-specific-first. The
# first pattern that matches wins; a multi-word, unambiguous phrase scores
# HIGH, a single generic word scores MEDIUM (spec's HIGH/MEDIUM/LOW
# confidence literal enum — `db_models.VALID_CONFIDENCE_LEVELS`).
# ---------------------------------------------------------------------------
_PATTERNS: list[tuple[str, str, str]] = [
    # (semantic_action, confidence, regex)
    ("START_APPLICATION", "HIGH", r"start\s*(your\s*)?application|begin\s*application"),
    ("SAVE_AND_CONTINUE", "HIGH", r"save\s*(and|&)\s*continue"),
    ("APPLY", "HIGH", r"\bapply\s*now\b|apply\s*(for\s*this|online|today)|apply\s*for\s*this\s*(job|position)"),
    ("APPLY", "MEDIUM", r"^apply\b"),
    ("SAVE", "MEDIUM", r"^save\b"),
    ("NEXT", "HIGH", r"^next\s*step\b"),
    ("NEXT", "MEDIUM", r"^next\b"),
    ("CONTINUE", "MEDIUM", r"\bcontinue\b|\bproceed\b"),
    ("REVIEW", "MEDIUM", r"\breview\b"),
    ("VERIFY", "HIGH", r"\bverify\b"),
    ("VERIFY", "MEDIUM", r"\bconfirm\b"),
    ("LOGIN", "MEDIUM", r"\blog\s*in\b|\bsign\s*in\b|\blogin\b"),
    ("UPLOAD", "HIGH", r"\bupload\b|\battach\b"),
    ("UPLOAD", "MEDIUM", r"\bchoose\s*file\b"),
    ("ADD", "HIGH", r"\badd\s*(another|more)\b|^\+?\s*add\b"),
    ("ADD", "MEDIUM", r"^add\b"),
]
_COMPILED = [(action, confidence, re.compile(pattern, re.IGNORECASE)) for action, confidence, pattern in _PATTERNS]


def classify_semantic_action(element, *, page_context: str = "") -> tuple[str | None, str | None, bool]:
    """Returns `(semantic_action, action_confidence, irreversible)` for one
    `PageElement`. Only clickable/actionable elements are worth classifying
    (a text input's accessible name is a question, not a command); everything
    else — and anything that matches nothing — comes back as
    `(None, None, False)` rather than a guessed "UNKNOWN", so a caller can
    tell "not applicable" apart from "classified as unrecognized".
    """
    if element.tag not in ("a", "button") and element.type not in ("button", "submit", "option"):
        return None, None, False

    name = f"{element.name or ''} {element.aria_label or ''} {element.title or ''}".strip()
    if not name:
        return None, None, False

    context_parts = [
        page_context,
        getattr(element, "section", "") or "",
        getattr(element, "surrounding_text", "") or "",
    ]
    form = getattr(element, "form", None)
    if isinstance(form, dict):
        context_parts.extend(str(value or "") for value in form.values())
    semantic_context = " ".join(context_parts)

    if is_submit_control_name(name):
        # A bare "Submit" is used by newsletters, searches and contact forms
        # too. It is semantically final submission only when nearby structure
        # identifies an application. Explicit phrases such as "Submit
        # Application" remain unambiguous without extra context.
        if _BARE_SUBMIT_RE.fullmatch(name.strip()) and not _APPLICATION_CONTEXT_RE.search(semantic_context):
            return "UNKNOWN", "LOW", False
        return "SUBMIT", "HIGH", True

    for action, confidence, pattern in _COMPILED:
        if pattern.search(name):
            return action, confidence, action in IRREVERSIBLE_ACTIONS

    return "UNKNOWN", "LOW", False
