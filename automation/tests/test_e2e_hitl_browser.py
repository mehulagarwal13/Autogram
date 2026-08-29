"""
REAL end-to-end Human-in-the-Loop browser validation.

Unlike every other `test_autonomous_*` / `test_human_interaction_*` module —
which fake the browser, the LLM, and the DB session to test logic in
isolation — **nothing is faked here**:

* a real Playwright **Chromium** process (`AUTOMATION_BROWSER_MODE=launch`,
  headless, so no dependency on the developer's own Chrome or an open CDP
  port — that's the mode `browser_manager.py` documents for exactly this
  "CI/headless server" case),
* a real local HTTP site (`fixtures/hitl_test_site.py`) whose markup and
  accept/reject behavior reproduce each blocker,
* the real **Postgres** database (skipped cleanly if unreachable),
* the real FastAPI app, routes, JWT auth, and ownership checks, driven over
  real HTTP through `TestClient`,
* the real `runner.py` background thread, `AutonomousAgentLoop`,
  `observe_page`/`detect_blocker`, and `ActionExecutor`.

The only thing stubbed — and only in the tests that need it — is the LLM
`decide_next_step` call, for determinism. `test_01_*` deliberately does NOT
stub it: it exercises the genuine model-driven observe→decide→act path so a
regression in the non-blocker flow can't hide behind a stub.

Because these drive real browsers over real HTTP, they are slow (tens of
seconds each) and are marked `e2e`. Run them explicitly:

    pytest automation/tests/test_e2e_hitl_browser.py -v -s

They must run in their OWN pytest process (see `conftest.py`'s
`requires_chromium` docstring: a session-scoped `sync_playwright()` opened by
another test file collides with the separate one `BrowserManager` starts).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

# --- MUST precede any `app.*` / `automation.*` import ---------------------
# `app/core/config.py` reads these at module load. We use `cdp` — the
# PRODUCTION DEFAULT mode — rather than `launch`, for two reasons:
#
#  1. It exercises the real default code path (`BrowserManager._attach_over_cdp`
#     -> `chrome_attach.attach_or_launch_chrome` -> `connect_to_chrome` ->
#     `contexts[0]`), which is what actually ships.
#  2. Playwright's *sync* API is thread-affine: a `Page` created on the loop's
#     background thread cannot be driven from the test's main thread
#     ("greenlet.error: Cannot switch to a different thread"). In production
#     that never matters, because only the loop thread ever touches the page.
#     But several scenarios here require a HUMAN to act in the browser (log
#     in, clear a challenge, navigate away) while the automation holds its own
#     connection. Over CDP the test opens its OWN independent connection to
#     the same browser — which is exactly the real-world situation the `cdp`
#     mode exists for, and the honest way to simulate a human sharing it.
#
# `_cdp_browser` (below) starts the browser as a plain subprocess with the
# debug port open, so no Playwright instance is ever created on the main
# thread just to launch it.
_CDP_PORT = socket.socket()
_CDP_PORT.bind(("127.0.0.1", 0))
CDP_PORT = _CDP_PORT.getsockname()[1]
_CDP_PORT.close()

os.environ["AUTOMATION_BROWSER_MODE"] = "cdp"
os.environ["AUTOMATION_CDP_URL"] = f"http://127.0.0.1:{CDP_PORT}"
# Never let a failed attach silently fall back to launching real Chrome (which
# would be detached, headful, and outlive the test run) — we want a hard,
# visible failure instead.
os.environ["AUTOMATION_CDP_AUTOLAUNCH"] = "false"
os.environ["AUTOMATION_HEADLESS"] = "true"
os.environ["AUTOMATION_HUMAN_PACING"] = "0"  # no jittered per-character typing delays

import pytest
from sqlalchemy import text

# `app.core.database` builds the engine but does not connect; `app.main` DOES
# connect eagerly at import (create_all + pgvector bootstrap). So probe
# reachability with the former and bail out BEFORE importing the latter,
# otherwise an unreachable DB is a collection error instead of a clean skip.
#
# NOTE: `automation/tests/conftest.py` does
# `os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")`
# so the whole suite can import without a database. That default wins over a
# `.env` value, so these tests need a real DATABASE_URL exported in the
# environment — see this module's docstring for the exact command.
from app.core.database import engine as _probe_engine


def _db_available() -> bool:
    try:
        with _probe_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


if not _db_available():  # pragma: no cover - environment guard
    pytest.skip(
        "No reachable Postgres. These are real end-to-end tests; export a real "
        "DATABASE_URL (conftest.py's test default points at localhost).",
        allow_module_level=True,
    )

from fastapi.testclient import TestClient

import app.main
import automation.agents.autonomous.loop as loop_mod
from app.core.auth import create_access_token, hash_password
from app.core.database import SessionLocal
from app.models.db_models import (
    ApplicationAuditLog,
    AutonomousTask,
    HumanInteractionRequest,
    ResumeRecord,
    User,
)
from app.services import autonomous_task_repository as task_repo
from app.services import human_interaction_repository as human_interaction_repo
from automation.agents.autonomous.actions import AgentAction
from automation.agents.autonomous.decision import Decision
from automation.agents.autonomous.runner import _REGISTRY, _REGISTRY_LOCK
from automation.tests.fixtures.hitl_test_site import INVALID_OTP, VALID_OTP, HitlTestSite

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def cdp_browser():
    """Starts a headless Chromium with the DevTools port open, as a plain
    subprocess (no Playwright on this thread — see the module-top comment).
    Both the automation loop and the `human` fixture attach to it over CDP as
    independent clients, which is exactly what `cdp` mode is designed for."""
    from playwright.sync_api import sync_playwright

    # Ask Playwright where its bundled Chromium lives, then close that
    # instance immediately — we only wanted the path, not a live driver.
    with sync_playwright() as p:
        executable = p.chromium.executable_path

    # A UNIQUE profile directory per run. Chrome's ProcessSingleton means a
    # second Chrome pointed at a profile that is already in use just hands its
    # command line to the running instance and exits — WITHOUT opening the
    # debug port (see `chrome_attach.launch_chrome_with_remote_debugging`'s
    # own note on this). A fixed directory therefore made every run after a
    # crashed/leaked one skip with "never opened the CDP port". A fresh
    # directory can never collide.
    profile = Path(tempfile.mkdtemp(prefix="autogram_e2e_cdp_"))
    proc = subprocess.Popen(
        [
            executable,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={profile}",
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    from automation.browser.chrome_attach import wait_for_devtools

    version = wait_for_devtools(f"http://127.0.0.1:{CDP_PORT}", timeout_s=60)
    if version is None:  # pragma: no cover - environment guard
        proc.kill()
        pytest.skip(f"Chromium never opened the CDP port {CDP_PORT} (profile: {profile}).")
    print(f"\n[CDP] {version.get('Browser')} on 127.0.0.1:{CDP_PORT} (profile {profile.name})")

    yield f"http://127.0.0.1:{CDP_PORT}"

    # Kill the whole browser process tree, then drop the throwaway profile.
    # Chromium spawns renderer/GPU children that outlive a bare `kill()` of
    # the parent and would keep the profile locked.
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture
def human(cdp_browser):
    """The test's OWN Playwright connection to the same browser — i.e. "the
    person sitting in front of it". A separate `sync_playwright()` on the test
    thread, so it never collides with the loop thread's own instance.

    **Reconnects on demand.** A `connect_over_cdp` client only enumerates the
    targets that exist when it connects; a tab another client opens *later*
    does not appear in an already-open connection's `context.pages`. Since the
    automation opens its tab after this fixture is set up, `page_for()` makes a
    fresh connection on each poll until it finds the tab. (Verified: a
    connection made after the tab exists sees it immediately.)"""
    from playwright.sync_api import sync_playwright
    from automation.browser.chrome_attach import connect_to_chrome

    pw = sync_playwright().start()
    cdp_url = cdp_browser

    class _Human:
        """Holds AT MOST ONE live CDP connection at a time.

        This matters a great deal: an earlier version reconnected on every
        poll iteration and never closed the previous connection, leaking
        60-120 CDP clients per test. Chrome then started refusing/stalling new
        DevTools clients, which surfaced as `connect_over_cdp: Timeout`,
        `Page.goto: Timeout`, and `greenlet.error: Cannot switch to a
        different thread` in *other* tests. So: reuse the current connection,
        and when a reconnect IS needed (to observe a tab opened after we
        connected), close the old one first.

        `browser.close()` on a CDP-attached browser only disconnects this
        client — it never stops the browser (see
        `chrome_attach.connect_to_chrome`'s docstring), so the module-scoped
        browser and the automation's own connection are unaffected."""

        def __init__(self):
            self._attached = None

        def _disconnect(self):
            if self._attached is None:
                return
            try:
                self._attached.browser.close()
            except Exception:
                pass
            self._attached = None

        def _connect(self):
            self._disconnect()
            self._attached = connect_to_chrome(pw, cdp_url)
            return self._attached.context

        def _pages(self):
            if self._attached is None:
                self._connect()
            try:
                return list(self._attached.context.pages)
            except Exception:
                self._connect()
                return list(self._attached.context.pages)

        def _urls(self, pages) -> list[str]:
            out = []
            for pg in pages:
                try:
                    out.append(pg.url)
                except Exception:
                    pass
            return out

        def _match(self, suffix: str):
            for pg in self._pages():
                try:
                    if pg.url.rstrip("/").endswith(suffix.rstrip("/")):
                        return pg
                except Exception:
                    continue
            return None

        def open_tab_urls(self) -> list[str]:
            self._connect()  # fresh view
            return self._urls(self._pages())

        def page_for(self, suffix: str, *, timeout: float = 40.0):
            """The open tab whose URL ends with `suffix`. Checks the existing
            connection first, and only reconnects (once per poll) when the tab
            isn't visible yet — a tab opened after we connected is invisible to
            an already-open client."""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                found = self._match(suffix)
                if found is not None:
                    return found
                self._connect()
                found = self._match(suffix)
                if found is not None:
                    return found
                time.sleep(1.0)
            pytest.fail(
                f"No open tab ending in {suffix!r} within {timeout}s. "
                f"Open tabs: {self._urls(self._pages())}"
            )

        def wait_until_some_tab_ends_with(self, suffix: str, *, timeout: float = 40.0) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._match(suffix) is not None:
                    return True
                self._connect()
                if self._match(suffix) is not None:
                    return True
                time.sleep(1.0)
            return False

    h = _Human()
    yield h

    # Never close the browser itself (the module fixture owns it) — the same
    # ownership rule `BrowserManager.close()` follows in cdp mode.
    h._disconnect()
    pw.stop()


@pytest.fixture(scope="module")
def site():
    with HitlTestSite() as s:
        yield s


@pytest.fixture(scope="module")
def client():
    return TestClient(app.main.app)


@pytest.fixture
def user_token():
    """A real user row + a real JWT, cleaned up afterwards along with every
    task/request/audit row the test created for them.

    Also seeds a real `ResumeRecord` with extracted text. Without it,
    `_build_candidate_profile_snapshot` hands the agent an empty profile AND
    empty resume text — at which point the correct behavior of a
    "never invent personal information" agent is to immediately request human
    intervention rather than fill anything, so a "normal automation" test
    would have nothing to observe. Seeding real source data is what lets the
    agent legitimately act."""
    db = SessionLocal()
    uid = f"e2e_{uuid.uuid4().hex[:10]}"
    db.add(User(user_id=uid, email=f"{uid}@example.com", password_hash=hash_password("pw")))
    db.commit()
    # A REAL file on disk, because `_build_uploadable_documents` only offers a
    # résumé whose `stored_path` actually resolves — so a placeholder path
    # would silently disable the whole upload path under test.
    resume_dir = Path(tempfile.mkdtemp(prefix="autogram_e2e_resume_"))
    resume_file = resume_dir / "jane_doe_resume.pdf"
    resume_file.write_bytes(b"%PDF-1.7\n% minimal fake resume for the E2E suite\n")
    db.add(ResumeRecord(
        resume_id=f"res_{uuid.uuid4().hex[:10]}",
        user_id=uid,
        original_filename="jane_doe_resume.pdf",
        stored_path=str(resume_file),
        file_hash=uuid.uuid4().hex,
        status="parsed",
        extracted_text=(
            "Jane Doe\njane.doe@example.com\n+1 555 0100\n"
            "Senior Software Engineer with 7 years of experience in Python and distributed systems.\n"
            "Experience: Acme Corp (2019-2026), Senior Software Engineer.\n"
            "Education: BSc Computer Science, State University, 2018."
        ),
        parsed_data={
            "first_name": "Jane", "last_name": "Doe",
            "email": "jane.doe@example.com", "phone": "+1 555 0100",
            "years_of_experience": 7,
            "skills": {"technical": ["Python", "distributed systems"]},
        },
    ))
    db.commit()
    token = create_access_token(uid)
    yield uid, {"Authorization": f"Bearer {token}"}

    shutil.rmtree(resume_dir, ignore_errors=True)

    task_ids = [t.task_id for t in db.query(AutonomousTask).filter(AutonomousTask.user_id == uid).all()]
    for tid in task_ids:
        with _REGISTRY_LOCK:
            handle = _REGISTRY.get(tid)
        if handle is not None:
            handle.cancel_requested.set()
            handle.resume_event.set()
    # Wait for every loop thread to actually EXIT before deleting its rows.
    # Deleting underneath a live loop makes its next commit raise
    # `StaleDataError` ("expected to update 1 row(s); 0 were matched") on the
    # background thread — pure teardown noise that buries real failures.
    # `runner._run_and_cleanup` pops the handle in a `finally`, so the handle
    # disappearing from the registry is the signal that the thread is done.
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        with _REGISTRY_LOCK:
            still_live = [t for t in task_ids if t in _REGISTRY]
        if not still_live:
            break
        time.sleep(0.5)
    db.query(ApplicationAuditLog).filter(ApplicationAuditLog.user_id == uid).delete()
    db.query(HumanInteractionRequest).filter(HumanInteractionRequest.user_id == uid).delete()
    db.query(AutonomousTask).filter(AutonomousTask.user_id == uid).delete()
    db.query(ResumeRecord).filter(ResumeRecord.user_id == uid).delete()
    db.query(User).filter(User.user_id == uid).delete()
    db.commit()
    db.close()


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _start_task(client, headers, job_url: str) -> str:
    resp = client.post("/agent/tasks", json={"job_url": job_url}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["task_id"]


def _get_task(client, headers, task_id: str) -> dict:
    resp = client.get(f"/agent/tasks/{task_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _wait_for_status(client, headers, task_id: str, *statuses: str, timeout: float = 60.0) -> dict:
    """Polls the REAL status endpoint — the same thing the frontend polls."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _get_task(client, headers, task_id)
        if last["current_status"] in statuses:
            return last
        time.sleep(0.4)
    pytest.fail(
        f"Task {task_id} never reached {statuses} within {timeout}s "
        f"(last status={last and last['current_status']}, error={last and last.get('error')})"
    )


def _wait_for_new_request(client, headers, task_id: str, *, not_request_id: str | None = None, timeout: float = 60.0) -> dict:
    """Waits for an active HumanInteractionRequest whose id differs from
    `not_request_id` — how a "rejected code raises a BRAND-NEW request" claim
    is verified rather than assumed.

    Also waits for the TASK to have caught up to `WAITING_FOR_HUMAN`.
    `loop.py::_pause_for_human` creates the request row first and flips
    `current_status` immediately after, so for a few milliseconds a PENDING
    request coexists with a task that still reads `RUNNING`. That ordering is
    deliberate (a task claiming to wait for a human before the request exists
    would be the worse of the two windows — the UI would have nothing to
    render), so the test waits for the pair to settle rather than asserting on
    the transient intermediate state."""
    deadline = time.monotonic() + timeout
    seen: dict | None = None
    while time.monotonic() < deadline:
        resp = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers)
        if resp.status_code == 200:
            body = resp.json()
            if not_request_id is None or body["request_id"] != not_request_id:
                seen = body
                if _get_task(client, headers, task_id)["current_status"] == "WAITING_FOR_HUMAN":
                    return body
        time.sleep(0.4)
    pytest.fail(
        f"No new human-request + WAITING_FOR_HUMAN for {task_id} within {timeout}s "
        f"(excluding {not_request_id}; last seen request={seen and seen.get('request_id')})"
    )


def _wait_for_request_status(client, headers, request_id: str, *statuses: str, timeout: float = 90.0) -> dict:
    """Polls `GET /human-requests/{id}` until it reaches one of `statuses`.

    Needed because consuming a delivered code is genuinely slow against a real
    browser + a cloud database: re-observe the page, fill, click, wait for the
    navigation, then several commits to Neon. Measured ~17s end-to-end, so any
    fixed `sleep()` here is a flaky test waiting to happen."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.get(f"/human-requests/{request_id}", headers=headers).json()
        if last["status"] in statuses:
            return last
        time.sleep(0.5)
    pytest.fail(f"Request {request_id} never reached {statuses} within {timeout}s (last={last and last['status']})")


def _live_handle(task_id: str):
    with _REGISTRY_LOCK:
        return _REGISTRY.get(task_id)


def _audit_events(db, task_id: str) -> list[str]:
    rows = (
        db.query(ApplicationAuditLog)
        .filter(ApplicationAuditLog.autonomous_task_id == task_id)
        .order_by(ApplicationAuditLog.created_at)
        .all()
    )
    return [r.event_type for r in rows]


def _stub_llm(monkeypatch, *decisions: Decision):
    """Replaces ONLY the LLM call. The browser, executor, DB, routes, and
    loop all stay real. Returns a list that records how many times the model
    was consulted, so a test can assert the LLM was never called at all."""
    calls: list[dict] = []
    queue = list(decisions)

    def fake_decide(**kwargs):
        calls.append(kwargs)
        if queue:
            return queue.pop(0)
        # Anything past the scripted decisions: stop cleanly rather than
        # looping forever burning browser time.
        return Decision(decision_type="TASK_FAILED", evidence="(stub) no further scripted decisions")

    monkeypatch.setattr(loop_mod, "decide_next_step", fake_decide)
    return calls


# ===========================================================================
# TEST 1 — Normal automation, REAL LLM, real browser
# ===========================================================================

def test_01_normal_automation_with_real_llm(client, site, user_token, db):
    """No stubs at all: real Chromium, real page, real LLM decisions, real
    ActionExecutor. Proves the HITL changes didn't regress the ordinary
    observe→decide→act path."""
    uid, headers = user_token
    task_id = _start_task(client, headers, site.url("/apply"))

    # The plain form has no blocker, so the loop must reach the LLM and act.
    # It should end up either pausing for something it can't answer, ready for
    # approval, or completed — all legitimate; what must NOT happen is FAILED
    # or getting stuck in CREATED.
    task = _wait_for_status(
        client, headers, task_id,
        "WAITING_FOR_HUMAN", "WAITING_FOR_APPROVAL", "COMPLETED", "FAILED",
        timeout=180.0,
    )

    print(f"\n[TEST 1] final status={task['current_status']} actions={len(task['action_history'])}")
    for a in task["action_history"][:8]:
        print(f"   - {a['action_type']:10} {str(a.get('element_name'))[:34]:34} success={a['success']}")

    assert task["current_status"] != "FAILED", f"real-LLM run failed: {task.get('error')}"
    # The agent actually did something in the browser.
    assert task["action_history"], "no actions were dispatched at all"
    # The browser state was really observed and persisted. NOTE: don't assert
    # on `elements` here — a run that reaches the post-submit confirmation page
    # correctly observes a page with no interactive elements at all.
    assert task["current_browser_state"]["url"].startswith(site.base_url)
    # At least one action actually targeted a real element the observer found,
    # which is the meaningful proof that observation → decision → execution
    # worked against live DOM (not that the last page happened to have inputs).
    targeted = [a for a in task["action_history"] if a.get("element_ref") is not None and a["success"]]
    assert targeted, f"no successful element-targeted action: {task['action_history']}"
    assert "automation_started" in _audit_events(db, task_id)
    # No HITL blocker should have been raised on a plain form — this is the
    # no-regression half of the test.
    assert "blocker_detected" not in _audit_events(db, task_id)


# ===========================================================================
# TEST 2 — OTP detection happens BEFORE the LLM
# ===========================================================================

def test_02_otp_detected_deterministically_without_any_llm_call(client, site, user_token, db, human, monkeypatch):
    uid, headers = user_token
    llm_calls = _stub_llm(monkeypatch)  # any call at all would be a failure

    task_id = _start_task(client, headers, site.url("/otp"))
    task = _wait_for_status(client, headers, task_id, "WAITING_FOR_HUMAN")

    # (1)+(2) detected before the LLM — the model was never consulted.
    assert llm_calls == [], f"LLM was called {len(llm_calls)}x for a deterministic blocker"

    # (3)+(4) a real request row exists, with the right type.
    req = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers).json()
    assert req["request_type"] == "OTP_REQUIRED"
    assert req["status"] == "PENDING"
    assert req["expires_at"] is not None
    # Masked destination made it through from the real DOM, un-unmasked.
    assert req["safe_metadata"]["masked_destination"] == "j***@gmail.com"
    assert req["safe_metadata"]["detection_layer"] == "deterministic"

    # (5) task status.
    assert task["current_status"] == "WAITING_FOR_HUMAN"
    assert task["human_intervention"]["request_type"] == "OTP_REQUIRED"

    # (6)+(7) the browser is still open and the SAME tab is still usable —
    # verified from the human's own independent CDP connection.
    otp_tab = human.page_for("/otp")
    assert not otp_tab.is_closed()
    assert otp_tab.locator("#code").count() == 1, "verification field gone from the live tab"
    assert otp_tab.locator("#verify-btn").count() == 1

    # The observer really did read this page (not a cached/blank state).
    assert task["current_browser_state"]["url"].endswith("/otp")
    refs = {e["ref"]: e for e in task["current_browser_state"]["elements"]}
    otp_ref = req["safe_metadata"]["otp_field_ref"]
    assert refs[otp_ref]["autocomplete"] if "autocomplete" in refs[otp_ref] else True
    assert "code" in refs[otp_ref]["name"].lower() or refs[otp_ref]["name"] == "Verification code"

    # (8) what the frontend polls in order to render VerificationModal.
    assert task["human_intervention"]["request_id"] == req["request_id"]

    events = _audit_events(db, task_id)
    print(f"\n[TEST 2] audit trail: {events}")
    print(f"[TEST 2] otp_field_ref={otp_ref} submit_ref={req['safe_metadata']['submit_ref']}")
    assert "blocker_detected" in events
    assert "human_request_created" in events
    assert "automation_paused" in events


# ===========================================================================
# TEST 3 — Wrong OTP -> rejected, brand-new request, never retried
# ===========================================================================

def test_03_wrong_otp_creates_a_fresh_request_and_never_retries(client, site, user_token, db, monkeypatch):
    uid, headers = user_token
    llm_calls = _stub_llm(monkeypatch)

    task_id = _start_task(client, headers, site.url("/otp"))
    _wait_for_status(client, headers, task_id, "WAITING_FOR_HUMAN")
    first = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers).json()

    resp = client.post(
        f"/human-requests/{first['request_id']}/respond",
        json={"action": "OTP_SUBMITTED", "value": INVALID_OTP}, headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"request_id": first["request_id"], "status": "accepted"}
    assert INVALID_OTP not in resp.text  # never echoed

    # The site rejects it; a BRAND-NEW request must appear.
    second = _wait_for_new_request(client, headers, task_id, not_request_id=first["request_id"])
    assert second["request_type"] == "OTP_REQUIRED"
    assert second["request_id"] != first["request_id"]

    task = _get_task(client, headers, task_id)
    assert task["current_status"] == "WAITING_FOR_HUMAN"

    # The old request is closed out, not left dangling in PENDING.
    old = client.get(f"/human-requests/{first['request_id']}", headers=headers).json()
    assert old["status"] in ("RESOLVED", "FAILED"), old["status"]

    # No guessing: exactly ONE fill of the code field ever happened.
    fills = [a for a in task["action_history"] if a["action_type"] == "fill"]
    assert len(fills) == 1, f"expected exactly one code fill, got {len(fills)}"
    assert fills[0]["value"] == "[REDACTED]"
    assert INVALID_OTP not in json.dumps(task["action_history"])
    assert llm_calls == [], "the LLM must never be consulted on the OTP path"

    events = _audit_events(db, task_id)
    print(f"\n[TEST 3] audit trail: {events}")
    assert "verification_submitted" in events
    assert "verification_rejected" in events


# ===========================================================================
# TEST 4 — Correct OTP -> accepted, automation resumes and continues
# ===========================================================================

def test_04_correct_otp_resumes_and_continues(client, site, user_token, db, human, monkeypatch):
    uid, headers = user_token
    # After the code is accepted the page becomes /next-step (no blocker), so
    # the loop WILL reach the LLM — script it to stop cleanly so the test is
    # deterministic and fast.
    llm_calls = _stub_llm(
        monkeypatch,
        Decision(decision_type="APPLICATION_READY_FOR_SUBMISSION", evidence="Work-history page reached after verification."),
    )

    task_id = _start_task(client, headers, site.url("/otp"))
    _wait_for_status(client, headers, task_id, "WAITING_FOR_HUMAN")
    req = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers).json()
    # On the expected verification page before submitting anything.
    human.page_for("/otp")

    resp = client.post(
        f"/human-requests/{req['request_id']}/respond",
        json={"action": "OTP_SUBMITTED", "value": VALID_OTP}, headers=headers,
    )
    assert resp.status_code == 200
    assert VALID_OTP not in resp.text

    # Verification accepted -> the REAL browser navigated onward, observed
    # from the human's independent connection.
    # Called for the WAIT, not the value: this blocks until the task leaves
    # the verification step, which is what makes the browser assertion below
    # meaningful rather than racing the loop.
    _wait_for_status(client, headers, task_id, "WAITING_FOR_APPROVAL", "COMPLETED", "RUNNING")
    assert human.wait_until_some_tab_ends_with("/next-step", timeout=40), \
        f"browser never reached /next-step; tabs={human.open_tab_urls()}"

    # The transient secret slot was cleared.
    handle = _live_handle(task_id)
    assert handle is None or handle.pending_secret is None

    # Wait for the loop to finish consuming the code and mark the request
    # terminal — this really does take ~15-20s (real page + cloud DB), so poll
    # rather than sleep a fixed amount.
    final_req = _wait_for_request_status(client, headers, req["request_id"], "RESOLVED", "FAILED")
    # Then wait for the loop to (a) re-observe the NEW page — which is what
    # flips the audit trail to verification_accepted — AND (b) actually reach
    # its next normal decision. (b) happens strictly after (a), so waiting
    # only for the URL races the decision and `llm_calls` can still be empty.
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        observed = (_get_task(client, headers, task_id)["current_browser_state"] or {}).get("url", "")
        if observed.endswith("/next-step") and llm_calls:
            break
        time.sleep(0.5)

    final_task = _get_task(client, headers, task_id)
    events = _audit_events(db, task_id)
    print(f"\n[TEST 4] request {req['request_id']} -> {final_req['status']} "
          f"(responded={final_req['responded_at']} resolved={final_req['resolved_at']})")
    print(f"[TEST 4] task status={final_task['current_status']} browser_state.url={final_task['current_browser_state']['url']}")
    print(f"[TEST 4] audit trail: {events}")

    # The request reached a terminal state, and the automation genuinely
    # progressed past verification (the LLM was reached only AFTER the OTP was
    # cleared).
    assert final_req["status"] == "RESOLVED"
    assert final_req["resolved_at"] is not None
    assert len(llm_calls) >= 1, "loop should have resumed into a normal decision after verification"
    # The persisted browser state proves the loop re-observed the NEW page.
    assert final_task["current_browser_state"]["url"].endswith("/next-step")
    assert "verification_submitted" in events
    assert "verification_accepted" in events
    assert "automation_resuming" in events
    assert "verification_rejected" not in events


# ===========================================================================
# TEST 5 — Secret-leakage audit against the REAL database
# ===========================================================================

def test_05_secret_leakage_audit_against_real_postgres(client, site, user_token, db, monkeypatch):
    """Runs a full OTP acceptance, then greps the real Postgres rows, the real
    API responses, and the captured application logs for the exact code."""
    uid, headers = user_token
    _stub_llm(monkeypatch, Decision(decision_type="APPLICATION_READY_FOR_SUBMISSION", evidence="reached next step"))

    task_id = _start_task(client, headers, site.url("/otp"))
    _wait_for_status(client, headers, task_id, "WAITING_FOR_HUMAN")
    req = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers).json()
    client.post(
        f"/human-requests/{req['request_id']}/respond",
        json={"action": "OTP_SUBMITTED", "value": VALID_OTP}, headers=headers,
    )
    # Wait for the code to be fully consumed (request terminal) AND for the
    # loop to have written the post-verification browser state — that's the
    # point at which every persisted surface this test greps actually exists.
    _wait_for_request_status(client, headers, req["request_id"], "RESOLVED", "FAILED")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        st = (_get_task(client, headers, task_id)["current_browser_state"] or {}).get("url", "")
        if st.endswith("/next-step"):
            break
        time.sleep(0.5)

    # --- 1/2/6/7: the whole AutonomousTask row, every JSONB column ---------
    db.expire_all()
    row = db.execute(
        text("""SELECT task_id, current_status, current_browser_state::text, action_history::text,
                       human_intervention::text, confirmed_answers::text, application_progress::text,
                       final_result::text, error, candidate_profile::text, job_information::text
                FROM autonomous_tasks WHERE task_id = :t"""),
        {"t": task_id},
    ).mappings().one()
    for column, value in row.items():
        assert VALID_OTP not in str(value or ""), f"OTP leaked into autonomous_tasks.{column}"

    # --- 3: confirmed_answers specifically (the legacy leak vector) --------
    assert row["confirmed_answers"] in ("{}", None) or VALID_OTP not in row["confirmed_answers"]
    assert json.loads(row["confirmed_answers"] or "{}") == {}, "OTP path must not write confirmed_answers at all"

    # --- 4: action_history contains ONLY the redaction --------------------
    history = json.loads(row["action_history"])
    fills = [a for a in history if a["action_type"] == "fill"]
    assert fills and all(a["value"] == "[REDACTED]" for a in fills)
    assert VALID_OTP not in row["action_history"]

    # --- 3(table): every HumanInteractionRequest row for this task --------
    hreq_rows = db.execute(
        text("SELECT request_id, request_type, status, title, message, safe_metadata::text "
             "FROM human_interaction_requests WHERE task_id = :t"),
        {"t": task_id},
    ).mappings().all()
    assert hreq_rows
    for r in hreq_rows:
        for column, value in r.items():
            assert VALID_OTP not in str(value or ""), f"OTP leaked into human_interaction_requests.{column}"

    # --- 8: audit log ------------------------------------------------------
    audit_rows = db.execute(
        text("SELECT event_type, actor, event_metadata::text FROM application_audit_log "
             "WHERE autonomous_task_id = :t"),
        {"t": task_id},
    ).mappings().all()
    assert audit_rows
    for r in audit_rows:
        assert VALID_OTP not in str(r["event_metadata"] or ""), "OTP leaked into an audit event"

    # --- A brute-force sweep: EVERY text-ish column of both tables --------
    # Catches a future column nobody remembered to add above.
    for table in ("autonomous_tasks", "human_interaction_requests", "application_audit_log"):
        dumped = db.execute(text(f"SELECT to_jsonb(t)::text FROM {table} t")).scalars().all()
        assert not any(VALID_OTP in (d or "") for d in dumped), f"OTP found somewhere in {table}"

    # --- 10/11: the GET APIs the frontend actually calls -------------------
    task_json = client.get(f"/agent/tasks/{task_id}", headers=headers).text
    assert VALID_OTP not in task_json
    req_json = client.get(f"/human-requests/{req['request_id']}", headers=headers).text
    assert VALID_OTP not in req_json
    list_json = client.get("/agent/tasks", headers=headers).text
    assert VALID_OTP not in list_json

    print(f"\n[TEST 5] swept {len(hreq_rows)} request row(s), {len(audit_rows)} audit row(s), "
          f"{len(history)} action(s) — no occurrence of {VALID_OTP}")


def test_05b_otp_never_appears_in_application_logs(client, site, user_token, caplog, monkeypatch):
    """Separate test so `caplog` captures the whole flow at DEBUG level."""
    import logging

    uid, headers = user_token
    _stub_llm(monkeypatch, Decision(decision_type="APPLICATION_READY_FOR_SUBMISSION", evidence="ok"))

    with caplog.at_level(logging.DEBUG):
        task_id = _start_task(client, headers, site.url("/otp"))
        _wait_for_status(client, headers, task_id, "WAITING_FOR_HUMAN")
        req = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers).json()
        client.post(
            f"/human-requests/{req['request_id']}/respond",
            json={"action": "OTP_SUBMITTED", "value": VALID_OTP}, headers=headers,
        )
        # Stay inside the caplog context until the code has been fully
        # consumed, so the capture covers the fill/submit path itself.
        _wait_for_request_status(client, headers, req["request_id"], "RESOLVED", "FAILED")

    assert VALID_OTP not in caplog.text, "OTP appeared in captured application logs"
    print(f"\n[TEST 5b] captured {len(caplog.text)} chars of DEBUG logs — no occurrence of {VALID_OTP}")


# ===========================================================================
# TEST 6 — LOGIN_REQUIRED
# ===========================================================================

def test_06_login_required_pauses_and_resumes_after_manual_login(client, site, user_token, human, monkeypatch):
    uid, headers = user_token
    llm_calls = _stub_llm(
        monkeypatch,
        Decision(decision_type="APPLICATION_READY_FOR_SUBMISSION", evidence="ordinary form reached after login"),
    )

    task_id = _start_task(client, headers, site.url("/login"))
    task = _wait_for_status(client, headers, task_id, "WAITING_FOR_HUMAN")

    req = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers).json()
    assert req["request_type"] == "LOGIN_REQUIRED"
    assert llm_calls == [], "a password field is a deterministic blocker; no LLM call expected"

    # No password is ever requested through an API, and the observer must not
    # have captured the password field's value.
    state = _get_task(client, headers, task_id)["current_browser_state"]
    pw_elements = [e for e in state["elements"] if e["type"] == "password"]
    assert pw_elements, "fixture should expose a password field"
    assert all(e["value"] is None for e in pw_elements), "a password value was captured into PageState"
    # The secret-bearing actions are not offered for this request type.
    bad = client.post(
        f"/human-requests/{req['request_id']}/respond",
        json={"action": "OTP_SUBMITTED", "value": "hunter2"}, headers=headers,
    )
    assert bad.status_code == 400

    # The human logs in by hand, in the real browser session — through their
    # OWN CDP connection, exactly as a person at the keyboard would.
    login_tab = human.page_for("/login")
    login_tab.fill("#user", "someone@example.com")
    login_tab.fill("#pw", "a-password-autogram-never-sees")
    login_tab.click("button[type=submit]")
    login_tab.wait_for_load_state("domcontentloaded")
    assert login_tab.url.rstrip("/").endswith("/apply"), f"login did not proceed: {login_tab.url}"

    # ...then signals completion through the existing architecture.
    resume = client.post(
        f"/human-requests/{req['request_id']}/respond",
        json={"action": "USER_APPROVED"}, headers=headers,
    )
    assert resume.status_code == 200, resume.text

    task = _wait_for_status(client, headers, task_id, "WAITING_FOR_APPROVAL", "COMPLETED", "RUNNING")
    print(f"\n[TEST 6] resumed to status={task['current_status']} tabs={human.open_tab_urls()}")
    # The loop re-observed and moved past the login wall.
    deadline = time.monotonic() + 60
    while time.monotonic() + 0 < deadline and not len(llm_calls):
        time.sleep(0.5)
    assert len(llm_calls) >= 1
    final_state = _get_task(client, headers, task_id)["current_browser_state"]
    assert not any(e["type"] == "password" for e in final_state["elements"]), "still on the login page"
    # No password anywhere in the task row.
    assert "a-password-autogram-never-sees" not in json.dumps(_get_task(client, headers, task_id))


# ===========================================================================
# TEST 7 — CAPTCHA_REQUIRED (simulated; never solved or bypassed)
# ===========================================================================

def test_07_captcha_pauses_and_resumes_only_after_the_human_clears_it(client, site, user_token, human, monkeypatch):
    uid, headers = user_token
    llm_calls = _stub_llm(
        monkeypatch,
        Decision(decision_type="APPLICATION_READY_FOR_SUBMISSION", evidence="challenge cleared"),
    )

    task_id = _start_task(client, headers, site.url("/captcha"))
    _wait_for_status(client, headers, task_id, "WAITING_FOR_HUMAN")

    req = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers).json()
    assert req["request_type"] == "CAPTCHA_REQUIRED"
    assert llm_calls == []

    captcha_tab = human.page_for("/captcha")
    # The automation must NOT have touched the challenge.
    assert captcha_tab.locator("#challenge").count() == 1, "the challenge was interacted with by automation"
    task = _get_task(client, headers, task_id)
    assert not any(
        a["action_type"] == "click" and "robot" in str(a.get("element_name", "")).lower()
        for a in task["action_history"]
    ), "automation attempted to click the CAPTCHA"

    # The human completes it in the real browser; the gate then lets them
    # through to the ordinary form (what a real anti-bot gate does).
    captcha_tab.click("#captcha-btn")
    captcha_tab.wait_for_url("**/apply", timeout=15000)
    assert captcha_tab.locator("#challenge").count() == 0

    resume = client.post(
        f"/human-requests/{req['request_id']}/respond",
        json={"action": "USER_APPROVED"}, headers=headers,
    )
    assert resume.status_code == 200, resume.text

    # The loop must re-observe, find the blocker gone, and proceed to a normal
    # decision. Poll for that rather than asserting immediately — a real
    # observe + Neon round-trip takes seconds.
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not llm_calls:
        time.sleep(0.5)
    task = _get_task(client, headers, task_id)
    print(f"\n[TEST 7] resumed after human cleared the challenge -> {task['current_status']}, "
          f"llm_calls={len(llm_calls)}")
    assert len(llm_calls) >= 1, "loop never reached a normal decision after the challenge was cleared"
    # And the re-observation saw the cleared page, not the stale challenge.
    observed = (task["current_browser_state"] or {}).get("visible_text", "")
    assert "not a robot" not in observed.lower(), f"loop resumed on a stale observation: {observed[:120]}"


# ===========================================================================
# TEST 8 — Page navigated away before the OTP is submitted
# ===========================================================================

def test_08_page_changed_before_submission_is_reclassified_not_blindly_injected(
    client, site, user_token, db, human, monkeypatch
):
    uid, headers = user_token
    llm_calls = _stub_llm(monkeypatch)

    task_id = _start_task(client, headers, site.url("/otp"))
    _wait_for_status(client, headers, task_id, "WAITING_FOR_HUMAN")
    req = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers).json()

    # The user wanders off to a DIFFERENT recognizable blocker, in the same tab.
    otp_tab = human.page_for("/otp")
    otp_tab.goto(site.url("/captcha"), wait_until="domcontentloaded")
    assert otp_tab.locator("#code").count() == 0

    client.post(
        f"/human-requests/{req['request_id']}/respond",
        json={"action": "OTP_SUBMITTED", "value": VALID_OTP}, headers=headers,
    )

    # A fresh request must appear, describing what is ACTUALLY on screen.
    new_req = _wait_for_new_request(client, headers, task_id, not_request_id=req["request_id"])
    assert new_req["request_type"] == "CAPTCHA_REQUIRED", new_req
    print(f"\n[TEST 8] reclassified to {new_req['request_type']} (was OTP_REQUIRED)")

    # The secret was cleared and NOT typed into anything.
    handle = _live_handle(task_id)
    assert handle is None or handle.pending_secret is None
    task = _get_task(client, headers, task_id)
    assert VALID_OTP not in json.dumps(task)
    assert not [a for a in task["action_history"] if a["action_type"] == "fill"], \
        "a fill happened even though the verification field was gone"
    assert llm_calls == [], "the OTP path must never reach the LLM"

    old = client.get(f"/human-requests/{req['request_id']}", headers=headers).json()
    assert old["status"] == "FAILED"
    assert "verification_field_lost" in _audit_events(db, task_id)


# ===========================================================================
# TEST 9 — Verification field disappears
# ===========================================================================

def test_09_verification_field_disappearing_is_handled_safely(client, site, user_token, db, human, monkeypatch):
    uid, headers = user_token
    llm_calls = _stub_llm(monkeypatch)

    task_id = _start_task(client, headers, site.url("/otp"))
    _wait_for_status(client, headers, task_id, "WAITING_FOR_HUMAN")
    req = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers).json()

    # Remove the field from the live DOM, leaving nothing else recognizable —
    # done from the human's connection, on the automation's real tab.
    otp_tab = human.page_for("/otp")
    otp_tab.evaluate("document.getElementById('code').remove()")
    otp_tab.evaluate("document.querySelectorAll('p,h1,label,button').forEach(e => e.remove())")
    assert otp_tab.locator("#code").count() == 0

    client.post(
        f"/human-requests/{req['request_id']}/respond",
        json={"action": "OTP_SUBMITTED", "value": VALID_OTP}, headers=headers,
    )

    new_req = _wait_for_new_request(client, headers, task_id, not_request_id=req["request_id"])
    print(f"\n[TEST 9] field vanished -> {new_req['request_type']}")
    assert new_req["request_type"] == "UNKNOWN_BLOCKER"

    task = _get_task(client, headers, task_id)
    handle = _live_handle(task_id)
    assert handle is None or handle.pending_secret is None
    assert VALID_OTP not in json.dumps(task)
    # No arbitrary field received the secret.
    assert not [a for a in task["action_history"] if a["action_type"] == "fill"]
    assert task["current_status"] == "WAITING_FOR_HUMAN", "must not silently continue"
    assert llm_calls == []
    assert "verification_field_lost" in _audit_events(db, task_id)


# ===========================================================================
# TEST 10 — Duplicate submission via REAL concurrent HTTP requests
# ===========================================================================

def test_10_concurrent_duplicate_otp_submissions_only_one_wins(client, site, user_token, monkeypatch):
    uid, headers = user_token
    _stub_llm(monkeypatch, Decision(decision_type="APPLICATION_READY_FOR_SUBMISSION", evidence="ok"))

    task_id = _start_task(client, headers, site.url("/otp"))
    _wait_for_status(client, headers, task_id, "WAITING_FOR_HUMAN")
    req = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers).json()

    # Two genuinely concurrent HTTP POSTs through the real ASGI stack.
    results: list[tuple[int, str]] = []
    barrier = threading.Barrier(2)

    def _submit():
        barrier.wait()  # maximize the overlap
        r = client.post(
            f"/human-requests/{req['request_id']}/respond",
            json={"action": "OTP_SUBMITTED", "value": VALID_OTP}, headers=headers,
        )
        results.append((r.status_code, r.text))

    threads = [threading.Thread(target=_submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    codes = sorted(c for c, _ in results)
    print(f"\n[TEST 10] concurrent /respond status codes: {codes}")
    assert len(results) == 2
    assert codes == [200, 409], f"expected exactly one winner, got {codes}"
    # Neither response echoed the code.
    assert all(VALID_OTP not in body for _, body in results)

    # Exactly one resume: exactly one fill of the code field.
    _wait_for_request_status(client, headers, req["request_id"], "RESOLVED", "FAILED")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        st = (_get_task(client, headers, task_id)["current_browser_state"] or {}).get("url", "")
        if st.endswith("/next-step"):
            break
        time.sleep(0.5)
    task = _get_task(client, headers, task_id)
    fills = [a for a in task["action_history"] if a["action_type"] == "fill"]
    print(f"[TEST 10] fills recorded: {len(fills)} -> {[a['value'] for a in fills]}")
    assert len(fills) == 1, f"secret delivered/typed {len(fills)}x — duplicate resume"
    assert fills[0]["value"] == "[REDACTED]"


# ===========================================================================
# TEST 11 — Cancellation racing an OTP submission
# ===========================================================================

def test_11_cancellation_race_terminal_state_wins(client, site, user_token, monkeypatch):
    uid, headers = user_token
    _stub_llm(monkeypatch, Decision(decision_type="APPLICATION_READY_FOR_SUBMISSION", evidence="ok"))

    task_id = _start_task(client, headers, site.url("/otp"))
    _wait_for_status(client, headers, task_id, "WAITING_FOR_HUMAN")
    req = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers).json()

    outcomes: dict[str, tuple[int, str]] = {}
    barrier = threading.Barrier(2)

    def _respond():
        barrier.wait()
        r = client.post(
            f"/human-requests/{req['request_id']}/respond",
            json={"action": "OTP_SUBMITTED", "value": VALID_OTP}, headers=headers,
        )
        outcomes["respond"] = (r.status_code, r.text)

    def _cancel():
        barrier.wait()
        r = client.post(f"/agent/tasks/{task_id}/cancel", headers=headers)
        outcomes["cancel"] = (r.status_code, r.text)

    threads = [threading.Thread(target=_respond), threading.Thread(target=_cancel)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    print(f"\n[TEST 11] respond={outcomes['respond'][0]} cancel={outcomes['cancel'][0]}")
    assert outcomes["cancel"][0] == 200

    # Let everything settle, then assert the task ended in a legitimate
    # terminal-or-paused state and, above all, that no secret was persisted.
    time.sleep(3.0)
    task = _get_task(client, headers, task_id)
    print(f"[TEST 11] final status={task['current_status']}")
    assert task["current_status"] in ("CANCELLED", "WAITING_FOR_HUMAN", "RUNNING", "WAITING_FOR_APPROVAL")
    assert VALID_OTP not in json.dumps(task)

    # If the cancellation won, the task must be CANCELLED and stay there.
    if task["current_status"] == "CANCELLED":
        time.sleep(2.0)
        again = _get_task(client, headers, task_id)
        assert again["current_status"] == "CANCELLED", "a cancelled task resumed"


# ===========================================================================
# TEST 12 — Backend restart semantics
# ===========================================================================

def test_12a_waiting_for_human_task_survives_restart_but_handle_does_not(client, site, user_token, monkeypatch):
    """Scenario A: a paused task's DB row survives; its in-memory handle does
    not. Submitting a code afterwards must NOT claim a false resume."""
    uid, headers = user_token
    _stub_llm(monkeypatch)

    task_id = _start_task(client, headers, site.url("/otp"))
    _wait_for_status(client, headers, task_id, "WAITING_FOR_HUMAN")
    req = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers).json()

    # Simulate the restart: the registry is what does NOT survive.
    handle = _live_handle(task_id)
    with _REGISTRY_LOCK:
        _REGISTRY.pop(task_id, None)
    assert _live_handle(task_id) is None

    # DB state is intact and still reports the pause honestly.
    task = _get_task(client, headers, task_id)
    assert task["current_status"] == "WAITING_FOR_HUMAN"
    assert task["human_intervention"]["request_type"] == "OTP_REQUIRED"

    # Submitting the code now must fail loudly, not silently "resume".
    resp = client.post(
        f"/human-requests/{req['request_id']}/respond",
        json={"action": "OTP_SUBMITTED", "value": VALID_OTP}, headers=headers,
    )
    print(f"\n[TEST 12a] post-restart /respond -> {resp.status_code}: {resp.json().get('detail')}")
    assert resp.status_code == 409
    assert VALID_OTP not in resp.text

    # The original request is FAILED, and a fresh LOGIN_REQUIRED explains what
    # the human must do — the UI never claims automation resumed.
    old = client.get(f"/human-requests/{req['request_id']}", headers=headers).json()
    assert old["status"] == "FAILED"
    fresh = client.get(f"/agent/tasks/{task_id}/human-request", headers=headers).json()
    assert fresh["request_type"] == "LOGIN_REQUIRED"
    assert "no longer available" in fresh["message"].lower()

    # No secret anywhere.
    assert VALID_OTP not in client.get(f"/agent/tasks/{task_id}", headers=headers).text

    # Cleanup: release the orphaned Chromium we detached from the registry.
    if handle is not None:
        handle.cancel_requested.set()
        handle.resume_event.set()


def test_12b_orphaned_running_task_is_reconciled_at_startup(client, site, user_token, db):
    """Scenario B: a RUNNING/RESUMING task with no live handle (what a crash
    leaves behind) is failed explicitly by startup reconciliation — never
    silently continued."""
    from automation.agents.autonomous.runner import reconcile_orphaned_tasks_on_startup

    uid, headers = user_token
    task = task_repo.create_task(
        db, user_id=uid, job_url=site.url("/apply"), original_objective="orphan test",
    )
    task_repo.set_status(db, task, "RUNNING")
    req = human_interaction_repo.create_request(
        db, user_id=uid, task_id=task.task_id, request_type="OTP_REQUIRED", message="stale",
    )
    assert _live_handle(task.task_id) is None  # exactly the post-crash situation

    count = reconcile_orphaned_tasks_on_startup(db)
    print(f"\n[TEST 12b] reconciled {count} orphaned task(s)")
    assert count >= 1

    after = _get_task(client, headers, task.task_id)
    assert after["current_status"] == "FAILED"
    assert "restart" in after["error"].lower()
    assert "start a new task" in after["error"].lower()

    db.expire_all()
    stale = human_interaction_repo.get_by_id(db, req.request_id)
    assert stale.status == "EXPIRED"
    assert "automation_failed" in _audit_events(db, task.task_id)
    assert "human_request_expired" in _audit_events(db, task.task_id)


# ===========================================================================
# TEST 13 — Résumé upload against a REAL file input
# ===========================================================================

def test_13_resume_is_uploaded_to_a_real_file_input(client, site, user_token, db, human, monkeypatch):
    """The gap the original fixture could not catch: `uploaded_documents` was
    always `[]`, so the agent could never attach a résumé — and essentially
    every real application has a file input. Drives the whole path for real:
    the task is created with the résumé offered, the LLM proposes an
    `upload_file`, the executor's allowlist permits THAT path, and the file
    actually lands on the input."""
    uid, headers = user_token

    # Install the stub BEFORE starting the task. A stub applied afterwards
    # races the loop's very first decision, which then goes to the real LLM —
    # a flaky test that depends on whether OPENAI_API_KEY happens to be set.
    # The stub is dynamic because the element ref for the file input isn't
    # known until the page is actually observed; `decide_next_step` is handed
    # the live `page_state`, so the stub can just look it up itself.
    seen_upload = {"done": False}

    def _decide(**kwargs):
        if seen_upload["done"]:
            return Decision(decision_type="APPLICATION_READY_FOR_SUBMISSION", evidence="Résumé attached.")
        page_state = kwargs["page_state"]
        file_ref = next((e.ref for e in page_state.elements if e.type == "file"), None)
        if file_ref is None:
            return Decision(decision_type="TASK_FAILED", evidence="(stub) no file input observed")
        docs = kwargs["uploaded_documents"]
        assert docs, "the loop was given no uploadable documents"
        seen_upload["done"] = True
        return Decision(
            decision_type="EXECUTE_ACTION",
            action=AgentAction(action_type="upload_file", element_ref=file_ref,
                               file_path=docs[0]["file_path"]),
            reasoning="Attach the candidate's résumé.",
        )

    monkeypatch.setattr(loop_mod, "decide_next_step", _decide)

    # The task must have been created with the résumé offered.
    task_id = _start_task(client, headers, site.url("/upload"))
    task = _get_task(client, headers, task_id)
    docs = task["uploaded_documents"]
    print(f"\n[TEST 13] uploaded_documents = {docs}")
    assert len(docs) == 1, "no uploadable résumé was offered to the task"
    assert docs[0]["label"] == "resume"
    assert Path(docs[0]["file_path"]).is_file()

    deadline = time.monotonic() + 150
    uploads = []
    while time.monotonic() < deadline:
        uploads = [
            a for a in _get_task(client, headers, task_id)["action_history"]
            if a["action_type"] == "upload_file"
        ]
        if uploads:
            break
        time.sleep(0.5)

    print(f"[TEST 13] upload actions: {uploads}")
    assert uploads, "the agent never attempted an upload"
    assert uploads[-1]["success"] is True, uploads[-1]["detail"]
    assert uploads[-1]["blocked_reason"] is None
    assert uploads[-1]["element_name"] == "Resume/CV"
    # And the file really reached the input in the live browser. Read from THIS
    # task's own tab: `page_for("/upload")` alone can match a leftover tab from
    # another test in this module-scoped browser, so confirm the attached
    # filename rather than merely that some /upload tab has a file.
    upload_tab = human.page_for("/upload")
    attached = upload_tab.evaluate(
        "document.getElementById('resume').files.length "
        "? document.getElementById('resume').files[0].name : null"
    )
    assert attached is not None, "no file attached on the observed /upload tab"
    assert "resume" in attached.lower(), attached


def test_13b_upload_of_a_non_offered_path_is_refused_and_pauses(client, site, user_token, db, monkeypatch):
    """The security half, end-to-end: an LLM naming a local file it was NOT
    offered must be refused by the executor allowlist, and the task must pause
    for a human rather than upload it."""
    uid, headers = user_token

    # Dynamic stub, installed BEFORE the task starts — see test_13's comment on
    # why a stub applied afterwards races the loop's first real LLM call.
    forbidden = str(Path(".env").resolve())

    def _decide(**kwargs):
        page_state = kwargs["page_state"]
        file_ref = next((e.ref for e in page_state.elements if e.type == "file"), None)
        if file_ref is None:
            return Decision(decision_type="TASK_FAILED", evidence="(stub) no file input observed")
        return Decision(
            decision_type="EXECUTE_ACTION",
            action=AgentAction(action_type="upload_file", element_ref=file_ref, file_path=forbidden),
            reasoning="(adversarial) attach an arbitrary local file",
        )

    monkeypatch.setattr(loop_mod, "decide_next_step", _decide)

    task_id = _start_task(client, headers, site.url("/upload"))
    task = _wait_for_status(client, headers, task_id, "WAITING_FOR_HUMAN", timeout=150)
    print(f"\n[TEST 13b] refused -> {task['human_intervention']['request_type']}")
    assert task["human_intervention"]["request_type"] == "MANUAL_ACTION_REQUIRED"

    refused = [a for a in task["action_history"] if a["action_type"] == "upload_file"]
    assert refused, "the agent never attempted the adversarial upload"
    # NOTHING was attached: every upload this task attempted was refused by the
    # allowlist, and `_do_upload_file` returns before it ever resolves a
    # locator — so `set_input_files` was provably never reached.
    #
    # Asserted from `action_history` rather than by reading the DOM: several
    # `/upload` tabs are open across this module-scoped browser (test_13 leaves
    # its own, with a résumé legitimately attached), so a `page_for("/upload")`
    # DOM probe can and does match the wrong tab.
    assert all(a["success"] is False for a in refused)
    assert all(a["blocked_reason"] == "upload_path_not_allowed" for a in refused)
    # And the refusal message never echoes the path it rejected back as if it
    # had been used.
    assert "Refused" in refused[-1]["detail"]
