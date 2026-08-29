"""
In-process publish/subscribe for live workflow events.

## Why in-process, and not Redis/a queue

The same reason `runner.py::_REGISTRY` and `app/core/middleware.py`'s rate
limiter are in-process: this project runs a single uvicorn worker. That is a
verified property, not an assumption — `requirements.txt` ships
`uvicorn[standard]` and no other HTTP server, there is no
Dockerfile/Procfile/k8s manifest/`--workers`/`WEB_CONCURRENCY` anywhere, and
the pause/resume design already REQUIRES it (a paused task can only be resumed
by the process holding its `TaskHandle`). Adding a broker here would introduce
infrastructure the rest of the system cannot yet use, to solve a problem this
deployment does not have.

If a second worker is ever introduced, this degrades the same way `_REGISTRY`
does: a client connected to worker A stops seeing events published by worker B.
It never corrupts state — every durable fact still lives in Postgres, and the
frontend's existing polling remains the fallback — but the live stream would
need the same Celery/Redis migration `runner.py`'s docstring tracks.

## Delivery guarantees, stated plainly

This is a live tap, NOT a durable log:

* events published while nobody is subscribed are dropped;
* a subscriber whose queue is full drops the OLDEST event rather than blocking
  the publisher — a slow browser tab must never be able to stall the automation
  thread that is publishing;
* nothing is replayed on reconnect.

That is deliberate, and safe, because the stream is an accelerator rather than
a source of truth: `ApplicationAuditLog` holds the compliance record,
`ChatMessage` holds the conversation, and the status endpoints hold current
state. A client that misses an event and refetches sees the truth. Nothing may
ever depend on an event *arriving* — treat a delivered event only as a hint to
look at the database.

## Threading

Events are published from the automation's own worker THREADS (the deterministic
one-off Playwright thread, the autonomous daemon thread), while subscribers live
on the asyncio event loop serving the WebSocket. `publish` is therefore
thread-safe and non-async, and hands each event to the loop with
`call_soon_threadsafe`; calling an asyncio primitive directly from those threads
would be a race at best and a silent no-op at worst.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock

logger = logging.getLogger(__name__)

#: The complete live-event vocabulary. This is a CONTRACT, not documentation:
#: the frontend switches on these names, and a typo in a publish call would
#: silently produce an event nobody handles — invisible, because publishing is
#: fire-and-forget by design.
#:
#: Defined here so there is one authority. `automation/tests/test_frontend_backend_contract.py`
#: checks the frontend against it, and `WorkflowEvent` validates against it in
#: development (see `__post_init__`).
WORKFLOW_EVENTS = frozenset({
    # Lifecycle
    "APPLICATION_STARTED",
    "PAGE_ANALYZED",
    "FIELD_FILLED",
    "SUBMISSION_STARTED",
    "APPLICATION_SUBMITTED",
    "APPLICATION_FAILED",
    "APPLICATION_READY_FOR_SUBMISSION",
    "REVIEW_REQUIRED",
    # Human-in-the-loop
    "HUMAN_ACTION_REQUIRED",
    "HUMAN_ACTION_COMPLETED",
    "REQUEST_HUMAN_INTERVENTION",
    # Transport-level, never a state change — the socket sends this to keep an
    # idle connection alive, and every consumer must ignore it.
    "KEEPALIVE",
})


#: Per-subscriber buffer. Small on purpose: these are UI notifications, and a
#: client that has fallen 64 events behind is better served by refetching state
#: than by replaying a backlog.
_MAX_QUEUED_EVENTS = 64


@dataclass(frozen=True)
class WorkflowEvent:
    """One thing that happened, addressed to one automation attempt.

    `event_type` is the vocabulary the frontend switches on (APPLICATION_STARTED,
    FIELD_FILLED, OTP_REQUIRED, CAPTCHA_DETECTED, HUMAN_ACTION_REQUIRED,
    REVIEW_REQUIRED, APPLICATION_SUBMITTED, APPLICATION_FAILED, ...).

    `payload` is display context ONLY and must never carry a verification code,
    password, session cookie, or token — it is serialized straight to a browser
    over the socket. Same rule as `HumanInteractionRequest.safe_metadata`.
    """

    stream_id: str
    event_type: str
    payload: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Catch a mistyped event name at the publish site.

        A warning rather than an exception: publishing happens mid-run on the
        automation thread, and a notification bug must never be able to abort a
        real job application. The log line is enough to find it, and the
        contract test above fails the build for anything the frontend uses.
        """
        if self.event_type not in WORKFLOW_EVENTS:
            logger.warning(
                "Publishing unknown event type %r — add it to WORKFLOW_EVENTS or fix the typo. "
                "No frontend handler will match it.",
                self.event_type,
            )

    def to_json(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "event": self.event_type,
            "payload": self.payload,
            "timestamp": self.created_at.isoformat(),
        }


def stream_id_for_application(application_id: str) -> str:
    return f"application:{application_id}"


def stream_id_for_task(task_id: str) -> str:
    return f"task:{task_id}"


class EventBus:
    """Fan-out from automation threads to WebSocket subscribers."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        # A plain threading.Lock, not an asyncio one: `publish` is called from
        # non-async worker threads that have no event loop of their own.
        self._lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the serving event loop so `publish` can hop onto it from a
        worker thread. Called once from the first subscriber, which is
        guaranteed to be running on that loop."""
        self._loop = loop

    def subscribe(self, stream_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUED_EVENTS)
        try:
            self.bind_loop(asyncio.get_running_loop())
        except RuntimeError:  # pragma: no cover - subscribe is always called from async code
            pass
        with self._lock:
            self._subscribers.setdefault(stream_id, set()).add(queue)
        return queue

    def unsubscribe(self, stream_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            subscribers = self._subscribers.get(stream_id)
            if not subscribers:
                return
            subscribers.discard(queue)
            if not subscribers:
                # Drop the empty set so a long-lived process does not accumulate
                # one entry per application it has ever run.
                self._subscribers.pop(stream_id, None)

    def subscriber_count(self, stream_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(stream_id, ()))

    def publish(self, event: WorkflowEvent) -> None:
        """Thread-safe, non-blocking, and never raises.

        Callers are automation code mid-run. A failure to notify a browser tab
        must never propagate into — let alone abort — a real job application, so
        every delivery problem is swallowed and logged rather than raised.
        """
        with self._lock:
            queues = list(self._subscribers.get(event.stream_id, ()))
            loop = self._loop
        if not queues or loop is None:
            return
        for queue in queues:
            try:
                loop.call_soon_threadsafe(self._offer, queue, event)
            except RuntimeError:
                # The loop is closing/closed (shutdown, or a test that finished).
                logger.debug("Event bus: loop unavailable for %s.", event.event_type)

    @staticmethod
    def _offer(queue: asyncio.Queue, event: WorkflowEvent) -> None:
        """Runs ON the event loop. Drops the oldest event when a subscriber is
        full, so one slow client can never apply backpressure to automation."""
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - drained concurrently
                pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - we just made room
            logger.debug("Event bus: dropped %s for a saturated subscriber.", event.event_type)


#: Module-level singleton — the process-wide bus, mirroring how `_REGISTRY` is
#: a module-level singleton in `runner.py`.
bus = EventBus()


def publish_application_event(application_id: str, event_type: str, **payload) -> None:
    bus.publish(WorkflowEvent(stream_id_for_application(application_id), event_type, payload))


def publish_task_event(task_id: str, event_type: str, **payload) -> None:
    bus.publish(WorkflowEvent(stream_id_for_task(task_id), event_type, payload))
