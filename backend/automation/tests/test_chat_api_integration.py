"""
Chat transcript + WebSocket stream, exercised through the real ASGI app
against real Postgres.

These are the tests the unit suite cannot give you: that the routes are wired,
that ownership is actually enforced (a live feed of someone's job application
is a direct data leak if it is not), and that an event published from a WORKER
THREAD — which is how automation really publishes — arrives at a subscribed
socket.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from sqlalchemy import text

from app.core.auth import create_access_token
from app.core.database import SessionLocal, engine
from app.models.db_models import Application, ChatMessage, User
from app.services import application_repository, chat_repository
from app.services.event_bus import publish_application_event

# Imported at MODULE scope, not inside the `client` fixture. Importing
# `app.main` runs the schema bootstrap (`Base.metadata.create_all` plus
# `pgvector_setup`'s idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`),
# which needs an AccessExclusiveLock. Doing that from a fixture means it
# fires mid-session, while other tests already hold RowExclusiveLocks on the
# same tables — observed as a real `deadlock detected` that then cascaded
# into errors across every later DB test in the run. At import time nothing
# else has opened a transaction yet, so the DDL is uncontended.
import app.main as main_app

JOB_URL = "https://careers.example.com/jobs/chat-api/apply"


def _db_available() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


db_required = pytest.mark.skipif(not _db_available(), reason="No reachable Postgres.")


@pytest.fixture
def client():
    with TestClient(main_app.app) as c:
        yield c


@pytest.fixture
def two_users():
    """Two real users, because the interesting authorization case is another
    user's id — not an absent token."""
    db = SessionLocal()
    made = []
    for _ in range(2):
        uid = f"chatapi_{uuid.uuid4().hex[:10]}"
        db.add(User(user_id=uid, email=f"{uid}@example.com", password_hash="x"))
        made.append(uid)
    db.commit()
    yield db, made
    db.rollback()
    for uid in made:
        db.query(ChatMessage).filter(ChatMessage.user_id == uid).delete()
        db.query(Application).filter(Application.user_id == uid).delete()
        db.query(User).filter(User.user_id == uid).delete()
    db.commit()
    db.close()


def _make_application(db, uid, url=JOB_URL):
    return application_repository.create_application(
        db, user_id=uid, job_url=url, autopilot_enabled=False,
        company="Acme", position="SWE", source="server_automation",
    )


def _auth(uid):
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

@db_required
def test_the_transcript_returns_messages_oldest_first(client, two_users):
    db, (uid, _other) = two_users
    app_row = _make_application(db, uid)

    chat_repository.record_agent_message(
        db, user_id=uid, application_id=app_row.application_id,
        content="Starting your application.",
    )
    chat_repository.record_user_reply(
        db, user_id=uid, application_id=app_row.application_id,
        content="5 years", request_type="ANSWER_REQUIRED",
    )

    res = client.get(f"/chat/applications/{app_row.application_id}", headers=_auth(uid))
    assert res.status_code == 200
    body = res.json()
    assert [m["role"] for m in body] == ["agent", "user"]
    assert body[0]["content"] == "Starting your application."


@db_required
def test_another_users_transcript_is_404_not_403(client, two_users):
    """404, matching `_get_owned_application`: the API must not confirm that an
    id exists to someone who cannot see it."""
    db, (uid, other) = two_users
    app_row = _make_application(db, uid)

    res = client.get(f"/chat/applications/{app_row.application_id}", headers=_auth(other))
    assert res.status_code == 404


@db_required
def test_the_transcript_requires_authentication(client, two_users):
    db, (uid, _other) = two_users
    app_row = _make_application(db, uid)
    assert client.get(f"/chat/applications/{app_row.application_id}").status_code == 401


@db_required
def test_an_unknown_scope_is_rejected(client, two_users):
    _db, (uid, _other) = two_users
    res = client.get("/chat/wharrgarbl/xyz", headers=_auth(uid))
    assert res.status_code == 404
    assert "scope" in res.json()["detail"]


@db_required
def test_a_secret_reply_never_reaches_the_transcript_through_the_api(client, two_users):
    """End-to-end version of the repository guard: even with a real session and
    a real application, an OTP cannot be written to `chat_messages`."""
    db, (uid, _other) = two_users
    app_row = _make_application(db, uid)

    with pytest.raises(ValueError, match="verification code"):
        chat_repository.record_user_reply(
            db, user_id=uid, application_id=app_row.application_id,
            content="482913", request_type="OTP_REQUIRED",
        )

    chat_repository.record_secret_submission(
        db, user_id=uid, application_id=app_row.application_id,
        human_request_id=None, request_type="OTP_REQUIRED",
    )
    body = client.get(f"/chat/applications/{app_row.application_id}", headers=_auth(uid)).json()
    assert len(body) == 1
    assert "482913" not in body[0]["content"]
    assert body[0]["content"] == chat_repository.SECRET_SUBMITTED_PLACEHOLDER
    assert body[0]["safe_metadata"]["secret_redacted"] is True


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@db_required
def test_the_socket_streams_an_event_published_from_a_worker_thread(client, two_users):
    """The real shape: automation publishes from a non-async worker thread while
    the socket waits on the event loop."""
    db, (uid, _other) = two_users
    app_row = _make_application(db, uid)
    token = create_access_token(uid)

    url = f"/chat/applications/{app_row.application_id}/stream?token={token}"
    with client.websocket_connect(url) as ws:
        threading.Thread(
            target=publish_application_event,
            args=(app_row.application_id, "FIELD_FILLED"),
            kwargs={"field": "email", "source": "user_profile"},
            daemon=True,
        ).start()

        # KEEPALIVE may arrive first on a slow machine; take the first real one.
        for _ in range(3):
            message = ws.receive_json()
            if message["event"] != "KEEPALIVE":
                break
        assert message["event"] == "FIELD_FILLED"
        assert message["payload"] == {"field": "email", "source": "user_profile"}
        assert message["stream_id"] == f"application:{app_row.application_id}"


@db_required
def test_the_socket_rejects_a_missing_token(client, two_users):
    db, (uid, _other) = two_users
    app_row = _make_application(db, uid)
    with client.websocket_connect(f"/chat/applications/{app_row.application_id}/stream") as ws:
        # Authentication failures close with 1008 (policy violation) rather
        # than completing the session, so the client can tell "denied" from
        # "connected then silent". Asserting the SPECIFIC disconnect — not a
        # bare Exception — is what makes this a test of the rejection rather
        # than of "something went wrong".
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 1008


@db_required
def test_the_socket_rejects_another_users_application(client, two_users):
    db, (uid, other) = two_users
    app_row = _make_application(db, uid)
    token = create_access_token(other)
    url = f"/chat/applications/{app_row.application_id}/stream?token={token}"
    with client.websocket_connect(url) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 1008, "another user's stream must be refused by policy"


@db_required
def test_a_disconnected_socket_unsubscribes_itself(client, two_users):
    """A leaked subscriber would keep receiving events for the life of the
    process, and the bus would grow one entry per application ever streamed."""
    from app.services.event_bus import bus, stream_id_for_application

    db, (uid, _other) = two_users
    app_row = _make_application(db, uid)
    stream_id = stream_id_for_application(app_row.application_id)
    token = create_access_token(uid)

    url = f"/chat/applications/{app_row.application_id}/stream?token={token}"
    with client.websocket_connect(url):
        pass  # closes on exit
    assert bus.subscriber_count(stream_id) == 0
