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

## Running with Docker

The compose file runs the whole stack: `postgres` (pgvector), a one-shot `migrate` service
(`alembic upgrade head`, then `python -m rastro_worker.schema`, which applies the Procrastinate schema
only when it is missing, so re-running is safe), `api` and `worker`. Both images are multi-stage
uv builds that run as a non-root user.

```bash
cp .env.example .env          # optional; GitHub App and LLM settings are read from it
mkdir -p secrets              # put github-app.private-key.pem here (mounted read-only at /secrets)
docker compose up -d --build --wait
curl localhost:8000/health    # {"status":"ok"}
```

`DATABASE_URL` and the private-key path are set by compose itself; everything else comes from `.env`.
Host ports can be changed with `POSTGRES_PORT` and `API_PORT` (defaults 5432 and 8000). To issue a
repository token: `docker compose exec api python -m rastro_api.admin issue-token --github-id 1 --full-name owner/repo`.
After pulling new code run `docker compose up -d --build` again; `migrate` runs before `api` and `worker` restart.

## Releasing the CLI

The CLI is published as the distribution `rastro-cli` (the command is still `rastro`) and depends on
`rastro-session-parsers`, which must be published first. The version lives in each package's
`pyproject.toml`; bump both together when the parser API changes, and keep the `rastro-session-parsers`
range in `packages/cli/pyproject.toml` in sync. The license field is a placeholder
(`LicenseRef-Proprietary`) to be replaced before the first public release. The distribution is called
`rastro-cli` because `rastro` is already taken on PyPI by another project; `rastro-cli` and
`rastro-session-parsers` were free when checked (2026-10-17), so confirm they still are right before the
first upload. The product name is provisional (spec), so renaming later only changes the distribution
name: keep the `rastro` script or update the hook text in `rastro_cli/hook.py` too.

```bash
rm -rf dist
uv build --package rastro-session-parsers -o dist
uv build --package rastro-cli -o dist
# smoke test in a throwaway tool dir, resolving the sibling wheel from dist/
UV_TOOL_DIR=$(mktemp -d) UV_TOOL_BIN_DIR=$(mktemp -d) uv tool install --find-links dist ./dist/rastro_cli-<version>-py3-none-any.whl
# publish (maintainers only): parsers first, then the CLI
uv publish dist/rastro_session_parsers-* && uv publish dist/rastro_cli-*
```

Users then run `pipx install rastro-cli` or `uvx --from rastro-cli rastro init` (`uvx` needs `--from`
because the package and the command have different names). The git hook calls `rastro` by name, so the
tool's bin directory must be on the `PATH` git sees (`pipx ensurepath` / `uv tool update-shell`);
`rastro init` warns when it is not.

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

Webhooks: `ping`, `installation`, `installation_repositories`, `push` and `pull_request` are
handled. Each delivery id (`X-GitHub-Delivery`) is stored in `webhook_deliveries`; a replay is
answered `202` with `"duplicate": true` and does nothing (a delivery whose processing failed is
not remembered, so GitHub's redelivery works).

### App lifecycle and data retention

| GitHub event | Repository `status` |
| --- | --- |
| `installation` created / `installation_repositories` added | `active` (initial index queued) |
| `installation` suspend | `suspended` |
| `installation` unsuspend | `active` (re-index queued: pushes were missed) |
| `installation` deleted / `installation_repositories` removed | `uninstalled` |

A repository that is not `active` is not analysed: push and pull request webhooks are ignored,
queued jobs return immediately, and `POST /v1/sessions` answers `403` (the CLI keeps the capture
pending). **Retention choice: nothing is deleted.** Rules, symbols, sessions and analyses are
kept so a reinstall picks up where it left off, and `rastro rules` keeps working with the repo
token. Remove a repository's data manually (delete the `repositories` row and its dependents) if
a customer asks for erasure.

GitHub truncates recursive trees of very large repositories (100,000 entries / 7 MB); the client
detects `truncated` and walks the tree one directory per request instead.

## Security: excerpts at rest and secret redaction

`agent_sessions.excerpts` (pieces of private conversations) is encrypted by the application with
Fernet before it reaches PostgreSQL. The column stays `jsonb`: an encrypted value is
`{"v": 1, "ct": "..."}`, a plain list is a legacy row and is still readable, and the ORM always
hands readers a list of dicts. Keys come from `EXCERPTS_ENCRYPTION_KEYS` (comma separated, the
first encrypts, all decrypt). Without a key the API logs a warning and stores plaintext, which is
only acceptable for local development. Reading encrypted data with a missing or wrong key raises
`DecryptionError`.

```bash
uv run python -m rastro_db.crypto generate-key      # a new key
uv run python -m rastro_db.crypto reencrypt         # encrypt plaintext rows / move rows to the primary key
```

Rotation: prepend the new key (`new,old`), deploy, run `reencrypt`, then drop the old key.
Migration `0006` encrypts existing rows when a key is set while migrating.

Before leaving the developer machine, text is scrubbed by `rastro_session_parsers.redact`: our
own patterns plus the `detect-secrets` detectors (cloud, VCS, payment and messaging providers).

## Reliability and cost control

Jobs are retried with exponential backoff (30 s, 1 min, 2 min... capped at 30 min, 5 retries)
only for transient failures: GitHub 5xx, 429 and rate limits (honouring `Retry-After`), network
errors, model rate limits/timeouts/5xx and lost database connections. Permanent errors (GitHub
401/403/404/422, model refusals, bad payloads) fail at once. Every decision is logged by
`rastro_worker.retry` (`retry 2/5 in 60s` / `giving up` / `failed permanently`).

`LLM_MONTHLY_TOKEN_BUDGET` caps the tokens (input + output, embeddings included) one repository
may use per calendar month (UTC), summed from `llm_calls`. It is checked before a job starts:
rule extraction marks the commit `skipped_budget` (re-run `rule_extraction` with `force` once
there is budget), impact analysis returns `skipped_budget` without commenting, and `rastro rules
--query` falls back to text search. A job already running can overshoot by what it spends itself.
Unset or `0` means unlimited.

If the OpenAI account runs out of credit the jobs do not retry (it cannot help): the affected commits
get the status `skipped_quota` and the worker logs an error. After topping up, run
`uv run --env-file .env python -m rastro_worker.requeue` to analyse them (it also re-queues
`skipped_budget` commits; `--status` and `--repository-id` narrow it). A pull request analysed while
the balance was empty gets no comment at all, so push to it again once credit is back.

## Rule extraction (phase 5)

Rules describe the default branch. Extraction runs only for commits that are on it: a commit with
a session pushed to a feature branch is marked `awaiting_merge`, and the push that merges it (a merge
commit pushes the PR's commits again) releases it. This way a pull request is analysed against the
rules as they were before it, and a PR that is never merged never changes them. Squash and rebase
merges create new commit SHAs without a session, so their rules are not learned yet.

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
