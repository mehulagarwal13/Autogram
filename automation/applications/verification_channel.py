"""
Transient delivery of a human-supplied verification code to a RUNNING
deterministic application.

## Why this exists

The deterministic path detects an OTP/MFA gate and pauses
(`ApplicationFlowManager._wait_for_human`), polling until a human clears it.
Until now the only way to clear it was to type the code into the automation's
own browser window — which works, but assumes the user is sitting in front of
that window. There was no way to enter a code in Autogram itself.

This module is the missing channel, and nothing more: a place to hand one code
to one running application, exactly once.

## The security contract, and how it is enforced rather than promised

A verification code is the single most sensitive value this system ever touches.
The rules are the same ones `runner.py::deliver_secret` follows for the
autonomous path, and they are enforced by construction here:

* **Never persisted.** The code lives in a module-level dict in this process's
  memory. There is no column, no JSONB field, and no file it can reach — this
  module imports no database session and no storage backend at all.
* **Never logged.** Nothing in this file interpolates a code into a log line.
  Every log statement records the application id and an outcome, never a value.
* **Consumed exactly once.** `take` pops. A second read gets `None`, so a code
  cannot be replayed against a later gate even within the same run.
* **Cleared on timeout.** A code nobody consumed is discarded when the wait
  ends (`discard`), so an unused value cannot sit in memory for the life of the
  process.
* **Never returned by any API.** No getter is exposed that returns a value to a
  caller — `take` is for the automation thread only, and the HTTP layer calls
  `deliver`, which returns a bool.

## Threading

`deliver` is called from a FastAPI request thread; `take` is called from the
Playwright worker thread that owns the page. A plain lock is enough — there is
one writer and one reader per application, and the dict operations under it are
trivial.

Process-local, like every other in-memory registry in this codebase, and for
the same reason: the browser being driven lives in this process. See the
single-worker note in README.md.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

#: application_id -> the code awaiting pickup. Deliberately NOT a dataclass
#: with extra context: the less that travels with a secret, the fewer places it
#: can be accidentally logged or serialized from.
_PENDING: dict[str, str] = {}
_LOCK = threading.Lock()


def deliver(application_id: str, code: str) -> bool:
    """Hand a code to the run for `application_id`, replacing any earlier
    undelivered one.

    Returns False for an empty/blank code so a stray submit cannot clear a
    genuinely pending slot. Deliberately does NOT verify that a run is actually
    waiting: the API layer checks the application's status, and racing that
    check against the run's own state here would just add a second, weaker
    gate. A code delivered to a run that has already moved on is simply never
    taken, and is discarded when the wait ends.
    """
    code = (code or "").strip()
    if not code:
        return False
    with _LOCK:
        _PENDING[application_id] = code
    # The id and the fact — never the value.
    logger.info("application %s: a verification code was delivered for pickup.", application_id)
    return True


def take(application_id: str) -> str | None:
    """Pop the pending code, if any. The ONLY reader, and it consumes.

    Called from the automation thread on each poll of the human-gate wait.
    Returning `None` is the overwhelmingly common case (nobody has typed
    anything yet) and is not noteworthy, so it is not logged.
    """
    with _LOCK:
        return _PENDING.pop(application_id, None)


def has_pending(application_id: str) -> bool:
    """Whether a code is waiting to be picked up — the FACT, never the value.

    Used only so the API can tell a user "that code hasn't been read yet"
    rather than silently accepting a second submission.
    """
    with _LOCK:
        return application_id in _PENDING


def discard(application_id: str) -> None:
    """Drop any uncollected code for this application.

    Called when a run stops waiting — whether it resumed, timed out, or failed.
    Without this, a code the automation never got round to reading would stay
    in memory until the process restarted.
    """
    with _LOCK:
        existed = _PENDING.pop(application_id, None) is not None
    if existed:
        logger.info("application %s: discarded an uncollected verification code.", application_id)
