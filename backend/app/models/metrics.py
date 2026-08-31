"""Response models for GET /metrics/summary — see app/services/metrics_repository.py
for exactly what each field measures and why "submission accuracy" is a
proxy rather than the plan's literal definition."""

from pydantic import BaseModel


class DeterministicMetrics(BaseModel):
    total: int
    median_hours_to_outcome: float | None
    clean_submission_rate: float | None
    auto_answered_question_rate: float | None


class AutonomousMetrics(BaseModel):
    total: int
    median_hours_to_outcome: float | None
    hitl_resolution_rate: float | None
    fully_autonomous_completion_rate: float | None


class MetricsSummaryResponse(BaseModel):
    deterministic: DeterministicMetrics
    autonomous: AutonomousMetrics
