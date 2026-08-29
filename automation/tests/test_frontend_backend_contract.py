"""
The frontend and backend must agree on their shared vocabulary.

This is a static contract check, not a UI test: it reads the real frontend
source and asserts that every action name, HITL request type, and status string
it uses actually exists in the backend's own definitions.

Why it earns its place: during this project I introduced three action names —
`USER_CONFIRMED`, `CAPTCHA_COMPLETED`, `LOGIN_COMPLETED` — that do not exist in
`_VALID_ACTIONS`. Nothing caught it, because a wrong action name is not a syntax
error on either side; it becomes a 400 at runtime, in the middle of a real job
application, at the exact moment a human is trying to unblock it. The backend
enums are the single source of truth, and drift away from them is silent until
it is expensive.

Deliberately one-directional. The frontend must not use a name the backend does
not define; the backend MAY define names no UI surfaces yet (a request type the
agent can raise but no control renders for is a gap, not a contract violation).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.api.human_interaction import _SECRET_ACTIONS, _VALID_ACTIONS
from app.models.db_models import (
    SECRET_HUMAN_REQUEST_TYPES,
    VALID_AUTONOMOUS_TASK_STATUSES,
    VALID_HUMAN_REQUEST_TYPES,
)
from app.services.application_repository import DISPLAY_STATUS_MAP
from app.services.event_bus import WORKFLOW_EVENTS

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"

#: Anything ALL_CAPS_WITH_UNDERSCORES in a string literal or as a bare object
#: key. Broad on purpose: the point is to catch a name the frontend uses at all,
#: however it is written.
_TOKEN = re.compile(r"""["']?\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b["']?""")


def _frontend_files():
    """Application source only.

    `__tests__` is excluded deliberately: a frontend test legitimately mentions
    event and status names in fixtures and assertions, and scanning it produced
    a false positive on `APPLICATION_SUBMITTED` — an EVENT name appearing in a
    test, flagged as an unknown action. The contract that matters is what the
    shipped UI sends, not what a fixture names.
    """
    if not FRONTEND_SRC.is_dir():
        pytest.skip(f"frontend source not found at {FRONTEND_SRC}")
    files = sorted(FRONTEND_SRC.rglob("*.jsx")) + sorted(FRONTEND_SRC.rglob("*.js"))
    return [f for f in files if "__tests__" not in f.parts]


def _tokens_in_frontend() -> set[str]:
    found: set[str] = set()
    for path in _frontend_files():
        found.update(_TOKEN.findall(path.read_text(encoding="utf-8")))
    return found


def test_the_frontend_sends_only_actions_the_backend_accepts():
    """The regression that motivated this file. An action the backend rejects
    surfaces as a 400 at the worst possible moment — mid-application, while a
    human is trying to clear a blocker."""
    # Only names that LOOK like response actions; the token scan is deliberately
    # broad, so narrow here rather than in the regex.
    action_shaped = {
        t for t in _tokens_in_frontend()
        if t.endswith(("_SUBMITTED", "_APPROVED", "_PROVIDED_VALUE", "_COMPLETED", "_CONFIRMED"))
    }
    # `_COMPLETED`/`_CONFIRMED` are included above precisely to catch the
    # invented names; anything matching must be a real action.
    unknown = {t for t in action_shaped if t not in _VALID_ACTIONS}
    # Status strings legitimately end in _COMPLETED-ish shapes too, so subtract
    # everything the backend defines as a status or request type.
    unknown -= VALID_AUTONOMOUS_TASK_STATUSES
    unknown -= VALID_HUMAN_REQUEST_TYPES
    unknown -= set(DISPLAY_STATUS_MAP.values())
    # Live-event names share the _SUBMITTED/_COMPLETED shape with actions but
    # are a different vocabulary entirely — `APPLICATION_SUBMITTED` is something
    # the backend announces, never something the frontend sends.
    unknown -= WORKFLOW_EVENTS
    assert not unknown, (
        f"frontend uses action name(s) the backend does not accept: {sorted(unknown)}. "
        f"Valid actions are {sorted(_VALID_ACTIONS)}."
    )


def test_the_frontend_references_only_real_request_types():
    request_shaped = {t for t in _tokens_in_frontend() if t.endswith("_REQUIRED") or t == "UNKNOWN_BLOCKER"}
    unknown = request_shaped - VALID_HUMAN_REQUEST_TYPES - set(DISPLAY_STATUS_MAP.values())
    unknown -= VALID_AUTONOMOUS_TASK_STATUSES  # WAITING_FOR_HUMAN etc.
    assert not unknown, (
        f"frontend references unknown HITL request type(s): {sorted(unknown)}. "
        f"Valid types are {sorted(VALID_HUMAN_REQUEST_TYPES)}."
    )


def test_the_frontends_secret_type_set_matches_the_backends():
    """The frontend decides whether to render a MASKED input from its own copy
    of this set. If it drifts, a verification code gets typed into a plain
    text box — visible, and on the non-redacted response path."""
    source = (FRONTEND_SRC / "pages" / "AutonomousAgent.jsx").read_text(encoding="utf-8")
    match = re.search(r"SECRET_REQUEST_TYPES\s*=\s*new Set\(\[([^\]]*)\]\)", source)
    assert match, "SECRET_REQUEST_TYPES not found — has the frontend been restructured?"
    frontend_secrets = set(re.findall(r'"([A-Z_]+)"', match.group(1)))
    assert frontend_secrets == set(SECRET_HUMAN_REQUEST_TYPES), (
        f"frontend masks {sorted(frontend_secrets)} but the backend treats "
        f"{sorted(SECRET_HUMAN_REQUEST_TYPES)} as secret-bearing"
    )


def test_secret_actions_and_secret_request_types_stay_aligned():
    """Backend-internal, but the frontend depends on it: the action it sends for
    a masked request must be the one that takes the redacted path."""
    assert {a.replace("_SUBMITTED", "_REQUIRED") for a in _SECRET_ACTIONS} == set(SECRET_HUMAN_REQUEST_TYPES)


def test_the_frontend_uses_only_real_display_statuses():
    """`display_status` is the presentation vocabulary the UI switches on. A
    status the backend never emits means a branch that silently never renders."""
    source = (FRONTEND_SRC / "pages" / "ApplicationDetail.jsx").read_text(encoding="utf-8")
    compared = set(re.findall(r'status\s*===\s*"([A-Z_]+)"', source))
    compared |= set(re.findall(r'\[\s*((?:"[A-Z_]+"\s*,?\s*)+)\]\.includes\(status\)', source))
    flat: set[str] = set()
    for entry in compared:
        flat.update(re.findall(r'"([A-Z_]+)"', entry) or [entry])
    unknown = flat - set(DISPLAY_STATUS_MAP.values())
    assert not unknown, (
        f"ApplicationDetail branches on display status(es) the backend never emits: {sorted(unknown)}. "
        f"Emitted values are {sorted(set(DISPLAY_STATUS_MAP.values()))}."
    )


def test_the_frontend_handles_only_events_the_backend_can_emit():
    """The other direction of the same contract: a UI branch keyed on an event
    name nothing publishes is dead code that looks alive.

    Scoped to names used in an EVENT COMPARISON (`msg.event === "..."`,
    `case "..."`), not to anything event-shaped. An earlier version keyed off
    name prefixes and flagged `FIELD_GROUPS` — a UI display constant that has
    nothing to do with the event bus. A contract test that cries wolf gets
    muted, which costs more than the check is worth.
    """
    comparisons: set[str] = set()
    for path in _frontend_files():
        text = path.read_text(encoding="utf-8")
        comparisons.update(re.findall(r'\.event\s*===\s*"([A-Z_]+)"', text))
        comparisons.update(re.findall(r'case\s+"([A-Z_]+)"', text))

    unknown = comparisons - WORKFLOW_EVENTS
    assert not unknown, (
        f"frontend branches on event name(s) the backend never emits: {sorted(unknown)}. "
        f"Emitted events are {sorted(WORKFLOW_EVENTS)}."
    )


def test_every_event_the_backend_publishes_is_in_the_declared_vocabulary():
    """`WORKFLOW_EVENTS` is only a contract if the publish sites obey it. This
    scans the real call sites rather than trusting the constant."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    published: set[str] = set()
    for path in list((root / "app").rglob("*.py")) + list((root / "automation").rglob("*.py")):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        published.update(re.findall(r'publish_(?:application|task)_event\(\s*[^,]+,\s*"([A-Z_]+)"', text))
        published.update(re.findall(r'_emit\(\s*[^,]+,\s*"([A-Z_]+)"', text))

    undeclared = published - WORKFLOW_EVENTS
    assert not undeclared, (
        f"these event names are published but not declared in WORKFLOW_EVENTS: {sorted(undeclared)}"
    )
    assert published, "no publish sites found — has the event bus been renamed?"
