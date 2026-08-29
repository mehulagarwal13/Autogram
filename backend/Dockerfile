# API image for Autogram.
#
# The browser binary IS installed here (see the `playwright install` line
# below). Local dev via docker-compose still defaults to
# AUTOMATION_BROWSER_MODE=cdp, attaching to the developer's own running
# Chrome on the host, so the installed browser sits unused there. But the
# Cloudflare Containers deploy (see ../worker/wrangler.toml) runs headless
# with AUTOMATION_BROWSER_MODE=launch — there is no developer Chrome to
# attach to in the cloud — so the image needs its own Chromium plus the OS
# libraries `--with-deps` pulls in.

FROM python:3.10-slim

# `PYTHONUNBUFFERED` so logs reach `docker logs` as they happen rather than
# sitting in a block buffer, which matters when watching a live automation run.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, as their own layer: requirements.txt changes far less
# often than application code, so edits to app/ do not reinstall the world.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Needed for AUTOMATION_BROWSER_MODE=launch (headless browser in-container).
RUN playwright install --with-deps chromium

COPY . .

# Writable at runtime, and bind-mounted in compose so they survive the
# container: résumés, encrypted automation sessions, run screenshots/traces.
RUN mkdir -p storage logs

EXPOSE 8000

# Single worker, on purpose and not for lack of ambition: the pause/resume
# design keeps each task's `TaskHandle` (and its open browser) in THIS process's
# memory, and `app/services/event_bus.py` fans out live events in-process. A
# second worker would route a resume — or a WebSocket subscriber — to a process
# that does not own the task. See `automation/agents/autonomous/runner.py`'s
# docstring for the Celery/Redis migration that would lift this.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
