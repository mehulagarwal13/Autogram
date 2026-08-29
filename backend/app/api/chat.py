"""
Chat transcript + live event stream.

Two surfaces over one workflow:

* `GET /chat/{scope}/{id}` — the durable transcript (`ChatMessage`), the source
  of truth the UI renders on load and after any reconnect.
* `WS  /chat/{scope}/{id}/stream` — a live tap on `event_bus`, so the panel
  updates without polling.

The socket is an ACCELERATOR, never an authority. Every event it delivers is a
hint to look at the database, and the transcript endpoint is what actually
answers "what happened". That is why a dropped event, a full queue, or a
missed reconnect can never desync the UI into a wrong state — it can only make
it briefly stale, which the client resolves by refetching.

Answering a human-in-the-loop prompt is deliberately NOT implemented here.
`POST /human-requests/{id}/respond` remains the single chokepoint for that,
because it owns the atomic `try_claim_for_resume` guard that stops two
concurrent responses from resuming one task twice. This module only records
the conversational echo of that action; adding a second write path would
duplicate the guard and eventually diverge from it.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import SessionLocal, get_db
from app.models.db_models import Application, AutonomousTask, User
from app.services import chat_repository
from app.services.event_bus import bus, stream_id_for_application, stream_id_for_task

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

#: How long the socket waits for an event before sending a keepalive. Well under
#: the 60s idle timeout most proxies impose, so an application that is quietly
#: filling a long form does not look dead and get disconnected.
_KEEPALIVE_SECONDS = 25.0


class ChatMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    message_id: str
    role: str
    content: str
    human_request_id: str | None = None
    safe_metadata: dict | None = None
    created_at: object | None = None


def _resolve_scope(db: Session, scope: str, resource_id: str, user: User) -> tuple[str, str]:
    """Authorize `(scope, id)` and return `(stream_id, ownership_kind)`.

    Ownership is checked HERE, once, for both the transcript and the socket —
    a stream is a live feed of someone's job application, so an unauthorized
    subscriber would be a direct data leak. Returns 404 rather than 403 for
    another user's id, matching `_get_owned_application`/`_get_owned_task`, so
    the API never confirms that an id exists to someone who cannot see it.
    """
    if scope == "applications":
        row = db.query(Application).filter(Application.application_id == resource_id).first()
        if row is None or row.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="Application not found.")
        return stream_id_for_application(resource_id), "application"
    if scope == "tasks":
        row = db.query(AutonomousTask).filter(AutonomousTask.task_id == resource_id).first()
        if row is None or row.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="Task not found.")
        return stream_id_for_task(resource_id), "task"
    raise HTTPException(status_code=404, detail=f"Unknown chat scope {scope!r}.")


@router.get("/chat/{scope}/{resource_id}", response_model=list[ChatMessageResponse])
def get_transcript(
    scope: str,
    resource_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The full conversation, oldest first. Safe to call on every reconnect —
    it is the authoritative view the live stream only accelerates."""
    _stream_id, kind = _resolve_scope(db, scope, resource_id, user)
    if kind == "application":
        return chat_repository.list_for_application(db, resource_id)
    return chat_repository.list_for_task(db, resource_id)


@router.websocket("/chat/{scope}/{resource_id}/stream")
async def stream_events(websocket: WebSocket, scope: str, resource_id: str, token: str = ""):
    """Live workflow events for one attempt.

    The token arrives as a QUERY PARAMETER because the browser `WebSocket` API
    cannot set an `Authorization` header — there is no option to pass one. It is
    still verified with exactly the same `get_current_user` logic as every HTTP
    route; only the transport of the credential differs.

    Consequence worth knowing: query strings are more likely to be written to
    access logs than headers are. This project's own request logging
    (`app/core/middleware.py`) does not log query strings, but a reverse proxy
    in front of it might, so short-lived tokens matter more on this route than
    elsewhere.
    """
    # Authenticate BEFORE accepting: a rejected socket should never complete a
    # handshake, or a client cannot tell "denied" from "connected then silent".
    db = SessionLocal()
    try:
        try:
            user = _authenticate_socket(db, token)
            stream_id, _kind = _resolve_scope(db, scope, resource_id, user)
        except HTTPException as exc:
            # 1008 = policy violation. Closing before accept() is not permitted
            # by the ASGI spec, so accept then immediately close with a reason.
            await websocket.accept()
            await websocket.close(code=1008, reason=str(exc.detail))
            return
    finally:
        db.close()

    await websocket.accept()
    queue = bus.subscribe(stream_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                # Proves liveness in both directions: if the peer has gone away,
                # this send raises and we clean up instead of leaking a
                # subscriber for the life of the process.
                await websocket.send_json({"event": "KEEPALIVE"})
                continue
            await websocket.send_json(event.to_json())
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - a dead socket must not take down anything else
        logger.debug("Chat stream %s closed unexpectedly.", stream_id, exc_info=True)
    finally:
        # Unconditional: every exit path must unsubscribe, or the bus keeps
        # handing events to a queue nobody reads.
        bus.unsubscribe(stream_id, queue)


def _authenticate_socket(db: Session, token: str) -> User:
    """Reuses the HTTP dependency's own implementation rather than re-decoding
    the JWT here, so the two can never drift on algorithm, expiry, or the
    user-exists check."""
    from app.core.auth import get_current_user as _get_current_user

    if not token:
        raise HTTPException(status_code=401, detail="Missing token.")
    return _get_current_user(token=token, db=db)
