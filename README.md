# Rastro

Rastro preserves the *why* behind AI-generated code: it captures developer intent from coding-agent sessions, extracts business rules, anchors them to code, and warns on pull requests when a change may break one. See [docs/spec-rastro-mvp.md](docs/spec-rastro-mvp.md) for the full MVP spec.

## Development

Requirements: [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync --all-packages        # install every workspace package
cp .env.example .env          # optional; defaults match docker-compose.yml
docker compose up -d --wait   # PostgreSQL 16 + pgvector
(cd packages/db && uv run alembic upgrade head)
PROCRASTINATE_APP=rastro_worker.app.app uv run procrastinate schema --apply
```

Run the services:

```bash
uv run uvicorn rastro_api.main:app --reload
PROCRASTINATE_APP=rastro_worker.app.app uv run procrastinate worker
```

Checks (the same ones CI runs):

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy apps packages
uv run pytest                              # all tests; DB tests need the compose Postgres
uv run pytest apps/api/tests/test_health.py::test_health   # a single test
uv run pytest -m "not db"                  # skip tests that need PostgreSQL
```

Create a migration after changing `packages/db/src/rastro_db/models.py`:

```bash
cd packages/db && uv run alembic revision --autogenerate -m "describe change"
```

Review the generated file: Alembic does not detect everything (for example `CREATE EXTENSION`).
