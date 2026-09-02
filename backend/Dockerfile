FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HF_HOME=/home/autogram/.cache/huggingface

WORKDIR /app

COPY requirements.txt .

# sentence-transformers needs PyTorch, but this service performs CPU inference.
# Installing from PyTorch's CPU index first prevents pip from adding unused
# NVIDIA/CUDA runtime packages to the Linux image.
RUN useradd --create-home --uid 1000 autogram

RUN pip install \
        --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple \
        torch && \
    pip install -r requirements.txt && \
    playwright install --with-deps --only-shell chromium && \
    mkdir -p /ms-playwright /app/storage /app/logs /home/autogram/.cache/huggingface && \
    chown -R autogram:autogram /ms-playwright /app /home/autogram && \
    rm -rf /var/lib/apt/lists/*

COPY --chown=autogram:autogram . .

USER autogram
EXPOSE 10000

# One process is required while task handles, browsers and event subscribers
# remain in process memory. See README.md's single-worker limitation.
# Migrations run before the web process in the same container (no separate
# pre-deploy step): a migration failure then prevents a broken revision from
# ever passing the health check.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
