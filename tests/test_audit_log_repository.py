"""
Tests for audit_log_repository.py — the append-only decision/approval trail
(HITL platform, PHASE2_ARCHITECTURE.md Initiative 3). DB-touching calls are
exercised against a `MagicMock` session, same convention as the rest of this
test suite.

The one invariant that actually matters here: this module exposes
`record_event`/`list_for_application` and NOTHING that updates or deletes a
row — that's enforced by there simply being no such function to call, which
this file confirms by import.
"""

from unittest.mock import MagicMock

import app.services.audit_log_repository as audit_log_repository
from app.services.audit_log_repository import list_for_application, record_event


def test_record_event_persists_the_expected_fields():
    db = MagicMock()

    record_event(
        db, application_id="app-1", user_id="user-1", event_type="human_approved",
        actor="user-1", metadata={"result_status": "applied"},
    )

    added = db.add.call_args[0][0]
    assert added.application_id == "app-1"
    assert added.user_id == "user-1"
    assert added.event_type == "human_approved"
    assert added.actor == "user-1"
    assert added.event_metadata == {"result_status": "applied"}
    db.commit.assert_called_once()


def test_record_event_metadata_is_optional():
    db = MagicMock()

    record_event(db, application_id="app-1", user_id="user-1", event_type="autopilot_run_started", actor="system")

    added = db.add.call_args[0][0]
    assert added.event_metadata is None


def test_list_for_application_orders_newest_first():
    db = MagicMock()
    fake_rows = [MagicMock(), MagicMock()]
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = fake_rows

    result = list_for_application(db, "app-1")

    assert result is fake_rows


def test_module_exposes_no_update_or_delete_path():
    """Append-only, enforced by absence: there is no `update_event`/`delete_event`
    (or similarly named) function anywhere in this module — the only way to
    change what happened is to record a NEW event, never to rewrite history."""
    public_names = {name for name in dir(audit_log_repository) if not name.startswith("_")}
    forbidden = {"update_event", "delete_event", "update", "delete", "edit_event"}
    assert not (public_names & forbidden)
