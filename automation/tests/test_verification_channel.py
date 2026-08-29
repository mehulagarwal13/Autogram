"""
The deterministic path's verification-code channel.

This is the newest place in the codebase where a real one-time passcode lives,
so it gets the same scrutiny as `TaskHandle.pending_secret` on the autonomous
side. Every test here is about a property whose violation would be silent:

  - a code that survives one pickup could be replayed against a later gate;
  - a code that outlives a timed-out wait sits in memory indefinitely;
  - a code offered at a CAPTCHA would imply Autogram was trying to answer one;
  - a code that reaches a log or a database is an unrecoverable leak.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from automation.applications import verification_channel as vc

CODE = "482913"


@pytest.fixture(autouse=True)
def _clean():
    """The channel is a module-level dict, so leakage between tests would make
    these pass for the wrong reason."""
    vc._PENDING.clear()
    yield
    vc._PENDING.clear()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_a_delivered_code_is_taken_exactly_once():
    """Replay protection. `take` pops, so a second read cannot re-use a code
    against a different gate later in the same run."""
    assert vc.deliver("app1", CODE) is True
    assert vc.take("app1") == CODE
    assert vc.take("app1") is None


def test_taking_from_an_application_with_nothing_pending_is_the_normal_case():
    """The wait loop calls `take` on every poll; almost every call returns
    None and must be cheap and silent."""
    assert vc.take("never-delivered") is None


def test_codes_are_isolated_per_application():
    """Two runs can be paused at once. A code typed for one must never be
    typed into the other's form."""
    vc.deliver("app1", "111111")
    vc.deliver("app2", "222222")
    assert vc.take("app2") == "222222"
    assert vc.take("app1") == "111111"


def test_a_second_delivery_replaces_the_first():
    """The user mistyped and re-sent. The newest code is the one they mean."""
    vc.deliver("app1", "111111")
    vc.deliver("app1", "222222")
    assert vc.take("app1") == "222222"
    assert vc.take("app1") is None


def test_a_blank_code_is_refused_and_cannot_clear_a_pending_one():
    """An empty submit must not wipe a code the user already entered."""
    vc.deliver("app1", CODE)
    assert vc.deliver("app1", "   ") is False
    assert vc.deliver("app1", "") is False
    assert vc.take("app1") == CODE


def test_surrounding_whitespace_is_stripped():
    """Codes get pasted from an email with a trailing newline."""
    vc.deliver("app1", f"  {CODE}\n")
    assert vc.take("app1") == CODE


def test_discard_removes_an_uncollected_code():
    """Called when a wait ends however it ends. Without it, a code the run
    never got to would stay in memory for the life of the process."""
    vc.deliver("app1", CODE)
    vc.discard("app1")
    assert vc.take("app1") is None


def test_discarding_nothing_is_harmless():
    """`discard` runs in a `finally`, including on paths where no code was
    ever delivered."""
    vc.discard("app-that-never-existed")


def test_has_pending_reports_the_fact_and_never_the_value():
    """The API uses this to tell a user their previous code has not been read
    yet. It must not become a way to read a code back out."""
    assert vc.has_pending("app1") is False
    vc.deliver("app1", CODE)
    assert vc.has_pending("app1") is True
    assert isinstance(vc.has_pending("app1"), bool)


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------

def test_no_function_returns_a_code_except_the_single_consumer():
    """`take` is the only reader, and it is called from the automation thread.
    A future convenience getter would be a way to read a live code out of the
    process, so the absence of one is pinned."""
    readers = [
        name for name, fn in vars(vc).items()
        if callable(fn)
        and not name.startswith("_")
        and name != "take"  # the sanctioned single consumer
        and "_PENDING.pop" in inspect.getsource(fn)
        and "return" in inspect.getsource(fn)
    ]
    assert readers == [], (
        f"these functions read a code out of the store besides `take`: {readers}"
    )
    # And `take` itself must CONSUME, not peek — otherwise a code could be
    # read repeatedly and replayed.
    assert "_PENDING.pop" in inspect.getsource(vc.take)


def test_delivering_a_code_never_writes_it_to_the_log(caplog):
    """The most likely accidental leak: an f-string in a log line. The channel
    logs the application id and the fact, never the value."""
    with caplog.at_level(logging.DEBUG, logger="automation.applications.verification_channel"):
        vc.deliver("app1", CODE)
        vc.take("app1")
        vc.deliver("app1", CODE)
        vc.discard("app1")
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert CODE not in logged, f"the code reached a log line: {logged!r}"
    assert "app1" in logged, "the application id should still be logged for traceability"


def test_the_module_cannot_reach_a_database_or_storage():
    """Structural guarantee: this module imports nothing that could persist a
    value. A future import of a session or a storage backend would make
    'never persisted' a promise rather than a property."""
    # Inspect the IMPORTS, not the prose — the module docstring legitimately
    # mentions "storage" and "database" while explaining what it must never
    # touch, and an earlier version of this test failed on its own
    # documentation.
    import ast

    tree = ast.parse(inspect.getsource(vc))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= {"__future__", "logging", "threading"}, (
        f"verification_channel gained an import that could persist or transmit a code: "
        f"{sorted(imported - {'__future__', 'logging', 'threading'})}"
    )


def test_the_api_request_model_carries_only_the_code():
    """A model that also accepted, say, a `note` field would invite someone to
    log the whole body."""
    from app.models.application import VerificationCodeRequest

    assert set(VerificationCodeRequest.model_fields) == {"code"}


# ---------------------------------------------------------------------------
# The API route's guards
# ---------------------------------------------------------------------------

def _fake_app(status="manual_required", reason="one-time passcode / multi-factor authentication."):
    from types import SimpleNamespace

    return SimpleNamespace(
        application_id="app1", user_id="user1", status=status, failure_reason=reason,
    )


def _call(monkeypatch, application, code="482913"):
    from types import SimpleNamespace

    import app.api.applications as api

    monkeypatch.setattr(api, "_get_owned_application", lambda db, aid, user: application)
    monkeypatch.setattr(api, "_record_audit_event", lambda db, **kw: None)
    monkeypatch.setattr(api, "_emit", lambda *a, **kw: None)
    return api.submit_verification_code(
        "app1", api.VerificationCodeRequest(code=code),
        user=SimpleNamespace(user_id="user1"), db=None,
    )


def test_a_code_is_accepted_when_the_run_is_waiting_on_a_passcode(monkeypatch):
    result = _call(monkeypatch, _fake_app())
    assert vc.take("app1") == "482913", "the code must reach the channel"
    assert "482913" not in result.message, "the response must never echo the code back"


@pytest.mark.parametrize("status", ["pending", "processing", "copilot_review", "applied", "failed"])
def test_a_code_is_refused_unless_the_run_is_actually_paused(monkeypatch, status):
    """Parking a code against a run that is not asking for one would leave it
    sitting in memory with nothing to consume it."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _call(monkeypatch, _fake_app(status=status))
    assert exc.value.status_code == 409
    assert vc.take("app1") is None


def test_a_code_is_refused_at_a_CAPTCHA_pause(monkeypatch):
    """THE important guard. A CAPTCHA also sits in `manual_required`. Offering
    to type a code at one would imply Autogram was trying to answer it — which
    this project refuses to do, in the browser and in the UI alike."""
    from fastapi import HTTPException

    captcha = _fake_app(reason="A CAPTCHA was detected on this page.")
    with pytest.raises(HTTPException) as exc:
        _call(monkeypatch, captcha)
    assert exc.value.status_code == 409
    assert "different kind of human step" in exc.value.detail
    assert vc.take("app1") is None


def test_an_empty_code_is_rejected_before_anything_is_stored(monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _call(monkeypatch, _fake_app(), code="   ")
    assert exc.value.status_code == 422
    assert vc.take("app1") is None


def test_the_ui_gate_and_the_api_gate_use_the_same_keywords():
    """`ApplicationDetail.jsx` decides whether to SHOW the code box using its
    own regex. If it drifts from the route's guard, the UI renders an input
    whose submission is refused with a 409 — the user types a code from their
    phone and is told no."""
    import re
    from pathlib import Path

    jsx = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "ApplicationDetail.jsx").read_text(encoding="utf-8")
    match = re.search(r"NEEDS_VERIFICATION_CODE\s*=\s*/([^/]+)/i", jsx)
    assert match, "the UI gate regex was renamed or removed"
    ui_keywords = set(match.group(1).split("|"))

    route = (Path(__file__).resolve().parents[2] / "app" / "api" / "applications.py").read_text(encoding="utf-8")
    api_match = re.search(r'for k in \(([^)]*)\)', route)
    assert api_match, "the route guard keywords were restructured"
    api_keywords = set(re.findall(r'"([a-z0-9-]+)"', api_match.group(1)))

    assert ui_keywords == api_keywords, (
        f"UI shows the code box for {sorted(ui_keywords)} but the API accepts {sorted(api_keywords)}"
    )
