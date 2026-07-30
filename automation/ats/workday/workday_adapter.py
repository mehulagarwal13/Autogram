"""WorkdayAdapter — Phase 7 (login-gated, multi-step; see ARCHITECTURE.md).

`myworkdayjobs.com`. Requires an authenticated `SessionStore` entry (Phase 2)
and a multi-step `ApplicationFlowManager` (Phase 4) — build after the
public/no-login adapters are solid.
"""

from __future__ import annotations

from automation.ats.base import ATSAdapter, FieldFillResult


class WorkdayAdapter(ATSAdapter):
    name = "workday"

    def detect(self) -> float:
        raise NotImplementedError("Phase 7 — see ARCHITECTURE.md")

    def fill_personal_information(self) -> list[FieldFillResult]:
        raise NotImplementedError("Phase 7 — see ARCHITECTURE.md")

    def upload_resume(self) -> bool:
        raise NotImplementedError("Phase 7 — see ARCHITECTURE.md")

    def answer_questions(self) -> list[FieldFillResult]:
        raise NotImplementedError("Phase 7 — see ARCHITECTURE.md")

    def submit_application(self) -> bool:
        raise NotImplementedError("Phase 7 — see ARCHITECTURE.md")
