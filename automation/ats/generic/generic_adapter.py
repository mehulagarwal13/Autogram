"""GenericAdapter — Phase 6/7 fallback for unrecognized/custom career portals.

When `ATSDetector` can't match a known platform, applications route here: an
LLM-driven agent reads the page's accessibility tree (not screenshots — far
fewer tokens) and reasons about field purposes directly, at lower confidence
than a specialized adapter. Always routes through `NEEDS_REVIEW` unless
confidence clears the auto-submit bar (see ARCHITECTURE.md, decision table) —
which for a generic/unknown portal in practice means never autopilot.
"""

from __future__ import annotations

from automation.ats.base import ATSAdapter, FieldFillResult


class GenericAdapter(ATSAdapter):
    name = "custom"

    def detect(self) -> float:
        # Always the fallback — never wins over a specialized adapter's own detect().
        return 0.1

    def fill_personal_information(self) -> list[FieldFillResult]:
        raise NotImplementedError("Phase 6/7 — see ARCHITECTURE.md")

    def upload_resume(self) -> bool:
        raise NotImplementedError("Phase 6/7 — see ARCHITECTURE.md")

    def answer_questions(self) -> list[FieldFillResult]:
        raise NotImplementedError("Phase 6/7 — see ARCHITECTURE.md")

    def submit_application(self) -> bool:
        raise NotImplementedError("Phase 6/7 — see ARCHITECTURE.md")
