"""iCIMSAdapter — Phase 7 (login-gated, multi-step; see ARCHITECTURE.md). `icims.com`."""

from __future__ import annotations

from automation.ats.base import ATSAdapter, FieldFillResult


class ICIMSAdapter(ATSAdapter):
    name = "icims"

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
