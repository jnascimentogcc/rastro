# syntax=docker/dockerfile:1
# Build from the repository root: docker build -f apps/worker/Dockerfile .
# Also serves as the one-shot migration image (it carries the Alembic config).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY apps ./apps
COPY packages ./packages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package rastro-worker

FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PROCRASTINATE_APP=rastro_worker.app.app
RUN useradd --system --uid 10001 --create-home rastro
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
# Alembic reads alembic.ini and migrations/ from its working directory (not part of the wheel).
COPY packages/db/alembic.ini /app/db/alembic.ini
COPY packages/db/migrations /app/db/migrations
USER rastro
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD ["python", "-m", "rastro_worker.healthcheck"]
CMD ["procrastinate", "worker"]
