"""
ProfileAgent — Phase 6 (see ARCHITECTURE.md).

Suggests profile completeness improvements and picks the best-fit resume
document for a given job description (using `job_type_tag` plus semantic
similarity). Embeddings are computed by `app/` (this module never imports
`app.services.embedding_service` directly) and injected as an
`automation.interfaces.EmbedCallable` at construction time.
"""

from __future__ import annotations

from automation.interfaces import CandidateProfileView, EmbedCallable, ResumeDocumentView


class ProfileAgent:
    """Phase 6: not yet implemented."""

    def __init__(self, embed: EmbedCallable):
        self.embed = embed

    def select_resume_for_job(
        self, profile: CandidateProfileView, documents: list[ResumeDocumentView], job_description: str
    ) -> ResumeDocumentView:
        raise NotImplementedError("Phase 6 — see ARCHITECTURE.md")
