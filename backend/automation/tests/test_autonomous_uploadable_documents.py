"""
Tests for the résumé-upload wiring found missing during the
production-readiness review.

`AutonomousTask.uploaded_documents` used to be initialised to `[]` and
populated by nothing, while the decision prompt told the model its
`upload_file` `file_path` "must be one of the uploaded_documents you were
given". With an always-empty list the agent could never attach a résumé — so
it stalled at the file input that essentially every real job application has.
The controlled browser fixture has no file input, which is why this survived
the end-to-end suite.

Covers both halves of the fix:
  * `_build_uploadable_documents` offers the résumé only when it resolves to a
    real LOCAL file (never an `s3://` locator, never a missing file).
  * The loop turns the executor's `upload_path_not_allowed` refusal into an
    honest `MANUAL_ACTION_REQUIRED` pause instead of a silent failure.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import app.api.autonomous_agent as agent_api
import automation.agents.autonomous.loop as loop_mod
from automation.agents.autonomous.actions import AgentAction
from automation.agents.autonomous.decision import Decision
from automation.agents.autonomous.loop import AutonomousAgentLoop, TaskHandle, _uploadable_paths
from automation.tests.test_autonomous_loop import (
    DictDb,
    FakeAuditLogRepo,
    FakeBrowserManager,
    FakeHumanInteractionRepo,
    FakeTask,
    FakeTaskRepo,
    _element,
    _page_state,
)


@dataclass
class FakeResumeRecord:
    resume_id: str = "res_1"
    original_filename: str = "jane_doe_resume.pdf"
    stored_path: str = ""


# ---------------------------------------------------------------------------
# _build_uploadable_documents
# ---------------------------------------------------------------------------

def test_a_real_local_resume_is_offered_for_upload(tmp_path):
    f = tmp_path / "resume.pdf"
    f.write_bytes(b"%PDF-1.7 fake")
    docs = agent_api._build_uploadable_documents(FakeResumeRecord(stored_path=str(f)))

    assert len(docs) == 1
    assert docs[0]["label"] == "resume"
    assert docs[0]["original_filename"] == "jane_doe_resume.pdf"
    # Absolute + resolved, because it becomes the executor's allowlist entry.
    assert docs[0]["file_path"] == str(f.resolve())


def test_no_resume_record_offers_nothing():
    assert agent_api._build_uploadable_documents(None) == []


def test_a_missing_file_is_not_offered(tmp_path):
    """A DB row pointing at a file that isn't there must not be advertised —
    Playwright would raise mid-run instead of pausing cleanly."""
    docs = agent_api._build_uploadable_documents(
        FakeResumeRecord(stored_path=str(tmp_path / "does_not_exist.pdf"))
    )
    assert docs == []


def test_an_s3_locator_is_not_offered():
    """Under `STORAGE_BACKEND=s3`, `stored_path` is an `s3://` URI, which
    `set_input_files` cannot upload. Offering it would turn a clean
    "please attach it yourself" pause into a confusing browser error."""
    docs = agent_api._build_uploadable_documents(
        FakeResumeRecord(stored_path="s3://autogram-bucket/storage/resumes/abc.pdf")
    )
    assert docs == []


def test_empty_stored_path_is_not_offered():
    assert agent_api._build_uploadable_documents(FakeResumeRecord(stored_path="")) == []


# ---------------------------------------------------------------------------
# _uploadable_paths (what the loop hands the executor)
# ---------------------------------------------------------------------------

def test_uploadable_paths_extracts_only_well_formed_entries():
    task = FakeTask()
    task.uploaded_documents = [
        {"label": "resume", "file_path": "/tmp/a.pdf"},
        {"label": "broken", "file_path": ""},   # ignored
        {"label": "no-path"},                    # ignored
        "not-a-dict",                            # ignored
    ]
    assert _uploadable_paths(task) == ["/tmp/a.pdf"]


def test_uploadable_paths_is_empty_when_nothing_was_offered():
    assert _uploadable_paths(FakeTask()) == []


# ---------------------------------------------------------------------------
# The loop's handling of a refused upload
# ---------------------------------------------------------------------------

def test_refused_upload_pauses_for_manual_action_instead_of_failing(monkeypatch):
    """With no uploadable document, an `upload_file` the LLM proposes is
    refused by the allowlist — and the loop must raise an honest
    FILE_UPLOAD_REQUIRED pause asking the human to attach the file, not
    fail the task and not keep guessing at paths (spec §29: a missing
    document gets its own request type rather than the generic
    MANUAL_ACTION_REQUIRED bucket)."""
    task = FakeTask()
    task.uploaded_documents = []  # nothing offered — the pre-fix default
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    loop._ensure_browser(task)

    page_state = _page_state(elements=[_element(2, "Resume/CV")])
    decision = Decision(
        decision_type="EXECUTE_ACTION",
        action=AgentAction(action_type="upload_file", element_ref=2, file_path="C:/Users/someone/.ssh/id_rsa"),
    )

    still_going = loop._handle_execute_action(DictDb(task=task), task, page_state, decision)

    assert still_going is False
    assert task.current_status == "WAITING_FOR_HUMAN"
    assert task.human_intervention["request_type"] == "FILE_UPLOAD_REQUIRED"
    # The refused action is recorded as unsuccessful, with the reason.
    assert task.action_history[-1]["success"] is False
    assert task.action_history[-1]["blocked_reason"] == "upload_path_not_allowed"


def test_offered_document_is_uploaded_through_the_loop(monkeypatch, tmp_path):
    """The positive half: with the résumé offered, the agent CAN attach it —
    which is the whole point of the fix."""
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.7 fake")

    task = FakeTask()
    task.uploaded_documents = [{"label": "resume", "file_path": str(resume.resolve())}]
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
    loop = AutonomousAgentLoop(task.task_id, handle)

    monkeypatch.setattr(loop_mod, "task_repo", FakeTaskRepo())
    monkeypatch.setattr(loop_mod, "human_interaction_repo", FakeHumanInteractionRepo())
    monkeypatch.setattr(loop_mod, "audit_log_repo", FakeAuditLogRepo())
    monkeypatch.setattr(loop_mod, "BrowserManager", FakeBrowserManager)
    loop._ensure_browser(task)

    page_state = _page_state(elements=[_element(2, "Resume/CV")])
    decision = Decision(
        decision_type="EXECUTE_ACTION",
        action=AgentAction(action_type="upload_file", element_ref=2, file_path=str(resume.resolve())),
    )

    still_going = loop._handle_execute_action(DictDb(task=task), task, page_state, decision)

    assert still_going is True
    assert task.current_status != "WAITING_FOR_HUMAN"
    assert task.action_history[-1]["success"] is True
    assert handle.page.locator('[data-agent-ref="2"]').filled_with == str(resume.resolve())
