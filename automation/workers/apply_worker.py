"""
apply_worker — Phase 4+ (see ARCHITECTURE.md).

The queued task behind `POST /application/start`: looks up the application
(via a plain-data payload, not a DB session — `app/` resolves the row and
passes in a `CandidateProfileView`), runs `ATSDetector` -> adapter selection
-> `ApplicationFlowManager`, and returns an `ApplicationRunResult` (see
`automation/interfaces.py`) for `app/` to persist.
"""

from __future__ import annotations

from automation.interfaces import ApplicationRunResult, CandidateProfileView


def run_application(application_id: str, job_url: str, profile: CandidateProfileView) -> ApplicationRunResult:
    raise NotImplementedError("Phase 4 — see ARCHITECTURE.md")
