"""
Generic retry/recovery helper — Phase 8, PART 11/12.

`automation/forms/field_handlers.py::fill_field()` already has its own
fill-then-verify retry loop (attempt N, retry N-1, structured `FieldFailure`
on exhaustion) — this module does NOT replace that; it's a smaller, more
general building block for the retry steps INSIDE a single handler attempt
(PART 12's "Attempt 3: alternative selector strategy", "Attempt 4: keyboard
interaction", ...) where a handler wants to try several distinct STRATEGIES,
not just repeat the same one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class RetryOutcome:
    """What `retry_strategies()` returns — which strategy (if any) actually
    worked, how many were tried, and every exception hit along the way (for
    structured failure reporting — see `field_handlers.py::FieldFailure`)."""

    success: bool
    result: object = None
    strategy_name: str | None = None
    attempts: int = 0
    errors: list[str] = field(default_factory=list)


def retry_strategies(
    strategies: list[tuple[str, Callable[[], T]]],
    *,
    is_success: Callable[[T], bool] = bool,
) -> RetryOutcome:
    """Tries each `(name, callable)` pair in order — PART 12's "Attempt 1:
    normal interaction, Attempt 2: wait and retry, Attempt 3: alternative
    selector, Attempt 4: keyboard, Attempt 5: JS fallback" pattern,
    generalized: any handler can hand this an ordered list of "ways to try
    accomplishing the same interaction" and get back a single structured
    result instead of hand-rolling its own try/except chain.

    Stops at the first strategy whose result satisfies `is_success` (default:
    truthy). A strategy that raises is recorded as a failed attempt (its
    exception message kept for the eventual structured failure report) and
    the next strategy is tried — one broken strategy never aborts the whole
    chain. Returns a `RetryOutcome` with `success=False` if every strategy
    was exhausted without one succeeding.
    """
    errors: list[str] = []
    for attempt, (name, strategy) in enumerate(strategies, start=1):
        try:
            result = strategy()
        except Exception as e:  # noqa: BLE001 - one bad strategy must never abort the rest
            logger.debug("Strategy %r (attempt %d) raised: %s", name, attempt, e)
            errors.append(f"{name}: {e}")
            continue

        if is_success(result):
            return RetryOutcome(success=True, result=result, strategy_name=name, attempts=attempt, errors=errors)
        errors.append(f"{name}: did not succeed")

    return RetryOutcome(success=False, attempts=len(strategies), errors=errors)
