"""BambooHRAdapter — Phase 7 (public careers page; see ARCHITECTURE.md). `bamboohr.com/careers`."""

from __future__ import annotations

from automation.ats.base import ATSAdapter, FieldFillResult


class BambooHRAdapter(ATSAdapter):
    name = "bamboohr"

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
