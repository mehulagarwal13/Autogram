from pydantic import BaseModel
from typing import Optional


class JobMatchResponse(BaseModel):
    match_id: str
    job_id: str
    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    apply_url: Optional[str] = None
    blended_score: float
    vector_similarity: float
    skill_overlap_ratio: float
    matched_skills: list[str]
    missing_skills: list[str]
    explanation: Optional[str] = None
    ats_score: float
    ats_found_keywords: list[str]
    ats_missing_keywords: list[str]
    ats_format_score: float
    status: str

class MatchesResponse(BaseModel):
    resume_id: str
    result_count: int
    results: list[JobMatchResponse]