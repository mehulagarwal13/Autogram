"""
GET /metrics/summary — the four success metrics named in the original
Autogram planning doc (median time-to-outcome, HITL resolution rate, a
submission-accuracy proxy, and a field-mapping-confidence proxy), computed
per user.

A dedicated router rather than folded into `applications.py`: the
deterministic and autonomous paths are separate systems with separate
tables, and this endpoint deliberately reports on both — it doesn't belong
to either one's own router. See `app/services/metrics_repository.py` for
exactly what each number measures and the one metric ("submission accuracy")
that isn't computable as the plan literally defined it.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.db_models import User
from app.models.metrics import MetricsSummaryResponse
from app.services import metrics_repository

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/summary", response_model=MetricsSummaryResponse)
def get_metrics_summary(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return MetricsSummaryResponse(
        deterministic=metrics_repository.deterministic_metrics(db, user.user_id),
        autonomous=metrics_repository.autonomous_metrics(db, user.user_id),
    )
