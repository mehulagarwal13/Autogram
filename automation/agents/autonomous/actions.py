"""
The constrained action vocabulary the autonomous agent's LLM decision step
may choose from. This is the entire surface the LLM can affect the browser
through — there is no "run this JavaScript" or "run this Playwright code"
action, by design (see `AUTONOMOUS_AGENT.md` compliance section). Every
action here is a small, typed, logged operation dispatched by
`executor.py::ActionExecutor`, never raw code the model wrote.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The full, closed set of action verbs the LLM may use. Anything else in a
#: decision payload is a hard validation failure in `decision.py`, not a
#: best-effort attempt to interpret it.
ALLOWED_ACTION_TYPES = frozenset({
    "navigate", "click", "fill", "select", "check", "uncheck", "scroll",
    "press_key", "upload_file", "extract_text", "wait", "go_back",
    "get_page_state",
})

#: Actions that require an on-screen target element, identified by the
#: `element_ref` the observer assigned it (see `observer.py`) — never a raw
#: CSS/XPath selector the LLM invented, since the observer is the only thing
#: that has actually looked at the DOM.
TARGETED_ACTIONS = frozenset({"click", "fill", "select", "check", "uncheck", "upload_file"})


class InvalidActionError(ValueError):
    """Raised when a decision payload names an action outside
    `ALLOWED_ACTION_TYPES`, or omits a parameter that action requires."""


@dataclass(frozen=True)
class AgentAction:
    """One instruction for the executor to carry out. `element_ref` is the
    stable index the observer's fixed extraction script tagged an element
    with (`data-agent-ref="<n>"`) — the ONLY way an action targets an
    element, so the model is choosing among elements we already found and
    described, never authoring a selector blind."""

    action_type: str
    element_ref: int | None = None
    value: str | None = None          # fill text / select option / press_key key name
    url: str | None = None            # navigate
    direction: str | None = None      # scroll: "up" | "down"
    amount: int | None = None         # scroll: pixels (default a reasonable viewport-ish chunk)
    file_path: str | None = None      # upload_file: path to an already-uploaded document
    wait_ms: int | None = None        # wait
    extra: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(payload: dict) -> "AgentAction":
        action_type = payload.get("action_type")
        if action_type not in ALLOWED_ACTION_TYPES:
            raise InvalidActionError(
                f"Unknown or disallowed action_type {action_type!r}. "
                f"Allowed: {sorted(ALLOWED_ACTION_TYPES)}"
            )
        if action_type in TARGETED_ACTIONS and payload.get("element_ref") is None:
            raise InvalidActionError(f"Action '{action_type}' requires an element_ref.")
        if action_type == "navigate" and not payload.get("url"):
            raise InvalidActionError("Action 'navigate' requires a url.")
        if action_type in ("fill", "select") and payload.get("value") is None:
            raise InvalidActionError(f"Action '{action_type}' requires a value.")
        if action_type == "press_key" and not payload.get("value"):
            raise InvalidActionError("Action 'press_key' requires a value (key name).")
        if action_type == "upload_file" and not payload.get("file_path"):
            raise InvalidActionError("Action 'upload_file' requires a file_path.")

        return AgentAction(
            action_type=action_type,
            element_ref=payload.get("element_ref"),
            value=payload.get("value"),
            url=payload.get("url"),
            direction=payload.get("direction"),
            amount=payload.get("amount"),
            file_path=payload.get("file_path"),
            wait_ms=payload.get("wait_ms"),
            extra={k: v for k, v in payload.items() if k not in {
                "action_type", "element_ref", "value", "url", "direction",
                "amount", "file_path", "wait_ms",
            }},
        )


@dataclass(frozen=True)
class ActionResult:
    """What executing one `AgentAction` produced — logged verbatim into
    `AutonomousTask.action_history` (see
    `app/services/autonomous_task_repository.py::append_action`)."""

    success: bool
    action_type: str
    detail: str
    extracted_text: str | None = None
    blocked_reason: str | None = None  # set when a safety gate refused to run this action at all

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "action_type": self.action_type,
            "detail": self.detail,
            "extracted_text": self.extracted_text,
            "blocked_reason": self.blocked_reason,
        }
