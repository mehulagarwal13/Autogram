"""
Background jobs — APScheduler.

Two independent jobs share one scheduler instance:

- **Job sync** — opt-in: enabled only by setting JOB_SYNC_QUERIES in .env
  (semicolon-separated search terms). No env var, no job, no surprise API
  usage.
- **Retention purge** (§9) — NOT opt-in: retention windows have safe
  defaults (see `app/services/retention_repository.py`), so this job always
  runs once the app starts, regardless of whether job sync is configured.
  Only its INTERVAL is configurable (`RETENTION_PURGE_INTERVAL_HOURS`).

Because of that difference, `start_scheduler()` can no longer early-return
just because job sync is unconfigured — the scheduler object itself is
always created and started; each job is added independently.
"""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import (
    JOB_SYNC_COUNTRY,
    JOB_SYNC_INTERVAL_HOURS,
    JOB_SYNC_QUERIES,
    RETENTION_PURGE_INTERVAL_HOURS,
)
from app.core.database import SessionLocal
from app.services.job_ingestion import embed_pending_jobs, ingest_from_sources

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _sync_all_queries() -> None:
    db = SessionLocal()
    try:
        for query in JOB_SYNC_QUERIES:
            result = ingest_from_sources(db, query, country=JOB_SYNC_COUNTRY)
            logger.info(
                "Job sync '%s': %d ingested, %d deduplicated",
                query, result["total_ingested"], result["total_deduplicated"],
            )
        embedded = embed_pending_jobs(db)
        logger.info("Job sync: embedded %d new jobs", embedded)
    except Exception:
        # A failed cycle must never kill the scheduler; next run may succeed.
        logger.exception("Job sync cycle failed")
    finally:
        db.close()


def _run_retention_purge() -> None:
    # Imported lazily (not at module top level) so a circular-import surprise
    # in retention_service/retention_repository can never take the whole
    # scheduler module down at import time — only this one job's next tick.
    from app.services import retention_service

    db = SessionLocal()
    try:
        results = retention_service.run_purge_for_all_users(db)
        for result in results:
            logger.info(
                "Retention purge (%s): %d record(s), %d file(s) deleted, %d failed%s",
                result["category"], result["records_purged"], result["files_deleted"],
                result["files_failed"], f" — {result['error']}" if result["error"] else "",
            )
    except Exception:
        # Same contract as job sync: a failed cycle must never kill the
        # scheduler, and must never take down app startup either.
        logger.exception("Retention purge cycle failed")
    finally:
        db.close()


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return  # already running (uvicorn --reload re-imports modules)

    _scheduler = BackgroundScheduler(daemon=True)

    if JOB_SYNC_QUERIES:
        _scheduler.add_job(
            _sync_all_queries,
            trigger="interval",
            hours=JOB_SYNC_INTERVAL_HOURS,
            id="job_sync",
            max_instances=1,        # never overlap two sync cycles
            coalesce=True,          # missed runs collapse into one
            next_run_time=datetime.now() + timedelta(seconds=15),  # first sync right after boot
        )
        logger.info(
            "Job sync scheduled: %d queries every %dh (country=%s)",
            len(JOB_SYNC_QUERIES), JOB_SYNC_INTERVAL_HOURS, JOB_SYNC_COUNTRY,
        )
    else:
        logger.info("Job sync disabled (JOB_SYNC_QUERIES not set)")

    _scheduler.add_job(
        _run_retention_purge,
        trigger="interval",
        hours=RETENTION_PURGE_INTERVAL_HOURS,
        id="retention_purge",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now() + timedelta(minutes=1),  # let the app finish booting first
    )
    logger.info("Retention purge scheduled: every %dh", RETENTION_PURGE_INTERVAL_HOURS)

    _scheduler.start()
