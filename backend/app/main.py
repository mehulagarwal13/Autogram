import logging

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import applications, auth, automation, autonomous_agent, chat, human_interaction, metrics, resumes, jobs, profile
from app.core.auth import get_current_user
from app.core.config import CORS_ORIGINS, CORS_ORIGIN_REGEX
from app.core.database import Base, SessionLocal, engine
from app.core.middleware import register_middleware
from app.core.pgvector_setup import ensure_pgvector_extension, ensure_vector_schema
from app.core.scheduler import start_scheduler
from app.models import db_models  # noqa: F401 — registers models on Base.metadata
from app.services.automation_recovery import reconcile_orphaned_automation_on_startup

# Basic structured-ish logging so warnings (LLM retries, extraction failures) surface.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# --- Database bootstrap -----------------------------------------------------
# NOTE: Alembic is the source of truth for schema (alembic upgrade head).
# create_all is kept as a convenience for first-run local setups; it only
# creates missing tables and never alters existing ones.
try:
    ensure_pgvector_extension()   # must precede create_all (vector column type)
    Base.metadata.create_all(bind=engine)
    ensure_vector_schema()        # idempotent column backfill + HNSW index
except Exception:
    logger.critical(
        "Database bootstrap failed. Check DATABASE_URL in .env — it must be a "
        "valid Neon connection string (postgresql://...neon.tech/...?sslmode=require)."
    )
    raise

start_scheduler()                 # no-op unless JOB_SYNC_QUERIES is set in .env

# --- Orphaned automation-ownership reconciliation ---------------------------
# Both paths drive automation from process memory (`runner.py::_REGISTRY` for
# the autonomous agent, `BackgroundTasks` + `_OPEN_REVIEW_SESSIONS` for the
# deterministic one) but persist ownership as a STATUS that
# `automation_ownership.find_active_automation` reads with no liveness check.
# Both registries are empty right here — this process just started — so any
# attempt still in an executing status was abandoned by a previous process, and
# would otherwise block its job with 409 forever.
#
# See `app/services/automation_recovery.py` for the full reasoning, in
# particular why a crashed `processing` attempt is recovered to `needs_review`
# and never to `failed` (it may already have clicked Submit).
try:
    with SessionLocal() as _startup_db:
        _reconciled = reconcile_orphaned_automation_on_startup(_startup_db)
        if any(_reconciled.values()):
            logger.warning(
                "Reconciled orphaned automation at startup: %d autonomous task(s), %d application(s).",
                _reconciled["autonomous_tasks"], _reconciled["applications"],
            )
except Exception:
    logger.exception("Orphaned-automation reconciliation failed at startup — continuing anyway.")

# --- App --------------------------------------------------------------------
app = FastAPI(title="AI Job Application Agent", version="1.0.0")

register_middleware(app)          # rate limiting, request logging, error safety net

if CORS_ORIGINS or CORS_ORIGIN_REGEX:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_origin_regex=CORS_ORIGIN_REGEX,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Auth endpoints are public; all business routes require a logged-in user (JWT).
app.include_router(auth.router)
app.include_router(resumes.router)  # endpoints take the user dependency individually
app.include_router(profile.router)  # endpoints take the user dependency individually
app.include_router(applications.router)  # endpoints take the user dependency individually
app.include_router(automation.router)  # browser-extension field mapping; endpoints take the user dependency individually
app.include_router(autonomous_agent.router)  # general-purpose autonomous agent; endpoints take the user dependency individually
app.include_router(chat.router)  # HITL chat transcript + live WebSocket event stream
app.include_router(human_interaction.router)  # HITL OTP/MFA/CAPTCHA/login requests; endpoints take the user dependency individually
app.include_router(metrics.router)  # cross-engine success-metrics summary; endpoints take the user dependency individually
app.include_router(jobs.router, dependencies=[Depends(get_current_user)])


@app.get("/health")
def health_check():
    """Liveness + DB reachability."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "unreachable"
    return {"status": "ok" if db_status == "ok" else "degraded", "database": db_status}
