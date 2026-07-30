"""
JobApplicationAgent — Phase 6, LangGraph (see ARCHITECTURE.md).

Top-level orchestrator: given a job URL and a `CandidateProfileView` (see
`automation/interfaces.py`), choose the ATS adapter (via `ATSDetector`),
drive `ApplicationFlowManager`, handle errors/retries, and hand off to a
human (§9) when confidence is too low or a CAPTCHA/login wall is hit. This
wraps, rather than replaces, the deterministic `ApplicationFlowManager` — the
LangGraph layer is for orchestration and error-recovery decisions, not for
driving the browser directly.

`app/` calls `start(url, profile)` and persists the returned
`ApplicationRunResult` — this class never touches the database itself.
"""

from __future__ import annotations

from automation.interfaces import ApplicationRunResult, CandidateProfileView


class JobApplicationAgent:
    """Phase 6: not yet implemented."""

    def start(self, url: str, profile: CandidateProfileView) -> ApplicationRunResult:
        raise NotImplementedError("Phase 6 — see ARCHITECTURE.md")
