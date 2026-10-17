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

## GitHub App setup (phase 4)

1. **Create the App** at <https://github.com/settings/apps/new>:
   - Homepage URL: any (e.g. the repository URL).
   - Webhook URL: a public URL that reaches your API (for local work use <https://smee.io> and
     `npx smee-client --url https://smee.io/XXXX --target http://localhost:8000/webhooks/github`).
   - Webhook secret: a long random string (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
   - Repository permissions: Contents: read-only, Pull requests: read and write, Metadata: read-only.
   - Subscribe to events: Push, Pull request (the `installation` events are always sent).
   - Where can this App be installed: only on your account.
2. **Generate a private key** (App settings, "Private keys") and save the `.pem` under `secrets/`
   (git-ignored). Note the **App ID**.
3. **Fill `.env`** (copy `.env.example`): `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`,
   `GITHUB_WEBHOOK_SECRET`.
4. **Run** the services with the same environment:

   ```bash
   uv run --env-file .env python -m rastro_api --reload   # selects a Windows-safe event loop
   PROCRASTINATE_APP=rastro_worker.app.app uv run --env-file .env procrastinate worker
   ```

5. **Install the App** on a test repository. GitHub sends `installation`; the API registers the
   repository and the worker runs `index_repository` (symbols and call graph of the default
   branch). Check "Advanced > Recent deliveries" in the App settings for `202` responses.
6. **Push a commit**: the API stores it and queues `analyze_commit`, which joins it with the
   session captured by `rastro` (same SHA) and refreshes the symbols of the files it touched.

Webhooks: `ping`, `installation`, `installation_repositories` and `push` are handled;
`pull_request` is accepted and ignored until phase 7.

## Rule extraction (phase 5)

For every commit with a captured session, the worker's `rule_extraction` job sends the diff, the
user requests, the changed symbols and the rules already anchored to them to the model, validates
the answer and stores rules, origins and anchors (plus exact/embedding duplicates). Set
`OPENAI_API_KEY`, `LLM_MODEL` and `EMBEDDING_MODEL` in `.env` (embeddings must support the
`dimensions` parameter; the database uses 1024). Without them the job is skipped.

Measure quality against the planted rules of `fixtures/clinica-demo` (needs the compose Postgres):

```bash
uv run --env-file .env python -m rastro_worker.evaluate fixtures/clinica-demo            # real model
uv run --env-file .env python -m rastro_worker.evaluate fixtures/clinica-demo --oracle   # harness check, no model
uv run --env-file .env python -m rastro_worker.evaluate fixtures/clinica-demo --runs 3   # models vary between runs
```

`--verbose` prints every rule the model produced next to what was expected.

Measured on `fixtures/clinica-demo` (3 runs each, `text-embedding-3-small`): `gpt-6-luna` 94% mean (worst
91%, 1 false-positive commit in 2 of 3 runs), `gpt-6.1-sol` 100% in every run with no false positives and
about half the output tokens. `gpt-6.1-sol` is the recommended `LLM_MODEL`.

## Pull request impact analysis (phase 7)

On `pull_request` events (`opened`, `synchronize`, `reopened`, `ready_for_review`) the API queues
`analyze_pr`. The worker maps the PR onto the symbols it changes, picks the candidate rules
(anchored to changed or deleted symbols, within two calls of them, or with unchanged copies), asks
the model whether the change can break or change each one, and posts **one** comment that it edits
in place on every push (and turns into an "all clear" if the warning no longer applies). Only
findings above `IMPACT_THRESHOLD` (default 0.7) are reported, and a finding needs a real change of
behaviour plus a concrete counterexample from the model; renames and moves are left to the anchor
resync. `IMPACT_COMMENT_LANGUAGE` is `en` or `pt`; `IMPACT_MAX_CANDIDATES` caps model calls per PR.
The GitHub App needs *Pull requests: read and write* (already in the setup above).

`fixtures/clinica-demo/prs/` holds five pull requests (two that must warn, three that must not)
that the evaluation runs after the commits; the criterion is fewer than 1 irrelevant comment per 5
PRs. `--runs 3` reports it per run.

## Querying the rules (phase 8)

```bash
rastro rules                                   # active rules of this repository, with the reason behind each
rastro rules --file src/agenda.ts              # rules anchored in a file (or a folder); path relative to where you are
rastro rules --search "can implants be booked on fridays"   # semantic search; plain text match without an embedding model
rastro rules --status orphaned                 # active (default), orphaned, superseded or all
rastro rules --limit 50 --json                 # raw server response
rastro rule 4f3e2d1c                           # one rule: code anchors (primary/duplicate) and the commits behind it
```

A rule whose conversation never said why shows "unknown origin" instead of a guessed reason.
The server exposes the same data at `GET /v1/repos/{id}/rules` (filters `file`, `status`, `q`,
`limit`, `offset`) and `GET /v1/repos/{id}/rules/{rule_id}`, authenticated with the repository
token. Semantic search embeds the query with the configured `EMBEDDING_MODEL` and keeps matches
above 0.3 cosine similarity; if the server has no embedding model, or the call fails, it falls
back to a case-insensitive text match on the statement and the reason.

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
