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

Capture a session for a commit (phase 3). Until the GitHub App creates repositories (phase 4), issue a
repository token by hand and point the CLI at the API:

```bash
uv run python -m rastro_api.admin issue-token --github-id 1 --full-name owner/repo   # prints the token once
cd /path/to/your/repo
rastro init --api-url http://localhost:8000     # prompts for the token, installs the post-commit hook
git commit ...                                  # the hook runs `rastro capture`, which returns immediately
rastro flush                                    # resend captures that could not be uploaded
```

The token goes to the system keychain (or `RASTRO_TOKEN` on headless machines), never to `.rastro/config.json`.
Captures wait in `.rastro/pending/` (git-ignored through `.git/info/exclude`) until the upload succeeds;
failures are logged to `.rastro/capture.log`.

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
