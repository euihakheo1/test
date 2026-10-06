# jettae API / worker image (same image, different command).
# Build from the repo root:  docker build -f deploy/Dockerfile -t jettae:local .
# NOTE: not built or run on the development machine (no Docker there); see docs/runbook.md.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
# dependencies first (layer cache), then the project itself
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra postgres
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --extra postgres

RUN useradd --system --uid 10001 --home /app jettae \
    && mkdir -p /data/blobs \
    && chown -R jettae /data
USER jettae

ENV JETTAE_ENV=prod \
    JETTAE_BLOB_DIR=/data/blobs \
    JETTAE_MIGRATIONS_DIR=/app/migrations \
    JETTAE_API_HOST=0.0.0.0 \
    JETTAE_API_PORT=8000

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health',timeout=3).status==200 else 1)"

CMD ["jettae", "api", "serve", "--host", "0.0.0.0", "--port", "8000"]
