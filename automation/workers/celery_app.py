"""
Celery/ARQ app bootstrap — Phase 4+ (see ARCHITECTURE.md).

One queued job per `POST /application/start` call, backed by Redis. Kept in
`automation/` (not `app/workers/`) so the FastAPI process (`app/`) never has
to import Playwright/browser-automation code — the API process and the
browser-automation worker process are deployed and scaled independently (see
ARCHITECTURE.md, Phase 7 Docker services: backend, worker, redis, postgres).
`app/` only ever talks to this worker through the queue (a job ID and a
plain-data payload), never through a Python import.
"""

from __future__ import annotations

# Deferred: instantiate the Celery/ARQ app here once REDIS_URL exists (Phase 4).
# Left unimplemented to avoid a hard dependency on a running Redis instance
# during Phase 1 (profile system) development/tests.
