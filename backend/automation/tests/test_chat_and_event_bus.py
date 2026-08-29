"""
Chat transcript + live event bus.

The two things worth testing hard here are the two that would be silent
failures in production:

1. **A verification code must never reach `chat_messages.content`.** Unlike
   `TaskHandle.pending_secret`, which lives in memory for milliseconds, a chat
   row is persisted forever and re-served on every page load. The repository
   refuses rather than trusting callers, and that refusal is pinned below.
2. **The bus must never apply backpressure to automation.** `publish` is called
   from the Playwright/agent worker threads mid-run. If a full queue could
   block or raise there, one slow browser tab could stall — or abort — a real
   job application.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models.db_models import SECRET_HUMAN_REQUEST_TYPES
from app.services import chat_repository
from app.services.event_bus import (
    EventBus,
    WorkflowEvent,
    stream_id_for_application,
    stream_id_for_task,
)


# ---------------------------------------------------------------------------
# The secret rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("request_type", sorted(SECRET_HUMAN_REQUEST_TYPES))
def test_a_reply_to_a_secret_request_is_refused_not_stored(request_type):
    """The whole point: `record_user_reply` must make storing an OTP
    impossible, not merely discouraged. It raises before touching the session,
    so no partial row can exist either."""
    with pytest.raises(ValueError, match="verification code"):
        chat_repository.record_user_reply(
            db=None,  # never reached — the guard runs first
            user_id="u1", content="123456",
            autonomous_task_id="t1", request_type=request_type,
        )


def test_the_secret_placeholder_cannot_contain_a_code():
    """`record_secret_submission` takes no value argument at all, so there is
    no parameter through which a code could reach the database even by
    accident. Pinned as a signature check because a future refactor adding a
    `content` argument would reintroduce exactly the leak this prevents."""
    import inspect

    params = set(inspect.signature(chat_repository.record_secret_submission).parameters)
    assert "content" not in params and "value" not in params and "code" not in params
    assert "123456" not in chat_repository.SECRET_SUBMITTED_PLACEHOLDER


def test_a_non_secret_reply_is_allowed():
    """ANSWER_REQUIRED and friends carry ordinary prose ("5 years"), which is
    exactly what the transcript is for — the guard must not over-block."""
    assert "ANSWER_REQUIRED" not in SECRET_HUMAN_REQUEST_TYPES
    # Reaches the DB layer rather than the guard, proving it was not refused.
    with pytest.raises(Exception) as exc:
        chat_repository.record_user_reply(
            db=None, user_id="u1", content="5 years",
            autonomous_task_id="t1", request_type="ANSWER_REQUIRED",
        )
    assert "verification code" not in str(exc.value)


def test_exactly_one_owner_is_required():
    """A transcript row belonging to neither path (or both) would render as a
    mis-attributed conversation, so it is rejected at write time."""
    for kwargs in (
        {"application_id": None, "autonomous_task_id": None},
        {"application_id": "app1", "autonomous_task_id": "t1"},
    ):
        with pytest.raises(ValueError, match="Exactly one"):
            chat_repository.record_agent_message(
                db=None, user_id="u1", content="hi", **kwargs,
            )


def test_an_unknown_role_is_rejected():
    with pytest.raises(ValueError, match="Unknown chat role"):
        chat_repository._add(
            db=None, user_id="u1", role="assistant", content="hi",
            application_id="app1", autonomous_task_id=None,
        )


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

def test_stream_ids_do_not_collide_across_paths():
    """An application id and a task id are both opaque strings, so the scope
    prefix is the only thing stopping one attempt's events being delivered to
    another's subscribers."""
    assert stream_id_for_application("x") != stream_id_for_task("x")


def test_publishing_with_no_subscribers_is_a_no_op():
    """Automation publishes unconditionally; nobody watching is the normal
    case, not an error."""
    EventBus().publish(WorkflowEvent("application:none", "FIELD_FILLED"))


def test_publish_from_a_worker_thread_never_raises_when_the_loop_is_gone():
    """`publish` runs on the Playwright/agent thread. A closed or missing loop
    must degrade to a dropped notification, never an exception that could
    propagate into a live job application."""
    bus = EventBus()
    bus._subscribers["application:1"] = {asyncio.Queue()}
    bus._loop = None
    bus.publish(WorkflowEvent("application:1", "FIELD_FILLED"))  # must not raise


def test_a_saturated_subscriber_drops_the_oldest_event_and_keeps_the_newest():
    """A slow client must lose history, never stall the publisher — and what it
    keeps must be the MOST RECENT state, since that is what the UI renders."""
    from app.services.event_bus import _MAX_QUEUED_EVENTS

    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    for i in range(2):
        queue.put_nowait(WorkflowEvent("s", f"OLD_{i}"))
    assert queue.full()

    EventBus._offer(queue, WorkflowEvent("s", "NEWEST"))

    assert queue.qsize() == 2
    remaining = [queue.get_nowait().event_type for _ in range(2)]
    assert remaining == ["OLD_1", "NEWEST"], "the oldest event should have been dropped"
    assert _MAX_QUEUED_EVENTS > 0


def test_unsubscribe_removes_the_stream_entry_entirely():
    """A long-lived process must not accumulate one empty set per application
    it has ever run."""
    bus = EventBus()
    queue: asyncio.Queue = asyncio.Queue()
    bus._subscribers["application:1"] = {queue}
    bus.unsubscribe("application:1", queue)
    assert "application:1" not in bus._subscribers
    assert bus.subscriber_count("application:1") == 0


def test_unsubscribing_something_never_subscribed_is_harmless():
    """The socket handler unsubscribes in a `finally`, which can run after an
    error that happened before subscribing."""
    EventBus().unsubscribe("application:missing", asyncio.Queue())


def test_event_serializes_to_the_documented_shape():
    payload = {"field": "email", "source": "user_profile"}
    body = WorkflowEvent("application:1", "FIELD_FILLED", payload).to_json()
    assert body["event"] == "FIELD_FILLED"
    assert body["payload"] == payload
    assert body["stream_id"] == "application:1"
    assert isinstance(body["timestamp"], str)


# `asyncio.run` rather than `@pytest.mark.asyncio`: this project's venv ships
# pytest + anyio and NOT pytest-asyncio, and under that runner an
# `async def` test is reported as a FAILURE ("async def functions are not
# natively supported") rather than being skipped. Driving the loop explicitly
# keeps these tests runnable with the interpreter the repo actually uses, and
# adds no plugin dependency to run one coroutine.

def test_a_subscriber_receives_a_published_event_end_to_end():
    """The real path: subscribe on the loop, publish (as a worker thread would),
    receive."""

    async def scenario():
        bus = EventBus()
        queue = bus.subscribe("application:1")
        bus.publish(WorkflowEvent("application:1", "APPLICATION_SUBMITTED", {"ok": True}))
        event = await asyncio.wait_for(queue.get(), timeout=5)
        bus.unsubscribe("application:1", queue)
        return event

    event = asyncio.run(scenario())
    assert event.event_type == "APPLICATION_SUBMITTED"
    assert event.payload == {"ok": True}


def test_events_are_not_delivered_to_another_attempts_subscriber():
    """Cross-delivery here would show one user's application activity inside
    another's chat panel."""

    async def scenario():
        bus = EventBus()
        mine = bus.subscribe("application:1")
        theirs = bus.subscribe("application:2")
        bus.publish(WorkflowEvent("application:1", "FIELD_FILLED"))
        received = await asyncio.wait_for(mine.get(), timeout=5)
        return received, theirs.empty()

    received, others_empty = asyncio.run(scenario())
    assert received.event_type == "FIELD_FILLED"
    assert others_empty, "an event reached a different attempt's subscriber"
