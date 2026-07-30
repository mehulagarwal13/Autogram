"""
AnswerGenerationAgent — Phase 6 (see ARCHITECTURE.md).

Thin LangGraph wrapper around `automation/forms/answer_engine.py::ApplicationAnswerEngine`
for the subjective-question path (cover-letter-style answers, "why this role")
— reuses the job-description + profile context already assembled by
`ApplicationFlowManager`. Like `answer_engine.py`, this takes an
`automation.interfaces.LLMCallable` injected by `app/`, never importing
`app.ai.*` directly.
"""

from __future__ import annotations

from automation.interfaces import CandidateProfileView, LLMCallable


class AnswerGenerationAgent:
    """Phase 6: not yet implemented."""

    def __init__(self, llm: LLMCallable):
        self.llm = llm

    def generate(self, question: str, job_description: str, profile: CandidateProfileView) -> str:
        raise NotImplementedError("Phase 6 — see ARCHITECTURE.md")
