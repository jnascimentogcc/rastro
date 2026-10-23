# Contributing to Rastro

Thank you for helping. Rastro is young, so for anything bigger than a small fix please open an issue first to
agree on the direction. By contributing you agree that your contribution is licensed under the [MIT](LICENSE)
license.

All code, comments, commit messages, docs and pull request text are written in **English**.

## Development setup

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

Capture a session for a commit. To try it without the GitHub App creating the repository (see
[docs/reference.md](docs/reference.md)), issue a repository token by hand and point the CLI at the API:

```bash
uv run python -m rastro_api.admin issue-token --github-id 1 --full-name owner/repo   # prints the token once
cd /path/to/your/repo
rastro init --api-url http://localhost:8000     # prompts for the token, installs the post-commit hook
git commit ...                                  # the hook runs `rastro capture`, which returns immediately
rastro flush                                    # resend captures that could not be uploaded
```

The token goes to the system keychain (or `RASTRO_TOKEN` on headless machines), never to `.rastro/config.json`.
Captures wait in `.rastro/pending/` (git-ignored through `.git/info/exclude`) until the upload succeeds;
failures are logged to `.rastro/capture.log`, and so is a commit for which a session file exists but nothing was captured
(with the time window and the reason). Edits made through Claude Code's Edit/Write/MultiEdit tools are recognised, as are Bash
commands that write a changed file (`sed -i`, `tee`, `>`, `mv`, `git apply`, scripts calling `write_text`...); the command text is never uploaded.

A commit's session window starts at its parent's commit time, but never later than the last commit already captured
(`.rastro/captured.json`, at most 24 h back), so a squash-merge commit created by GitHub after the conversation does not hide it.
If a wider window (24 h) would have captured something the automatic one missed, `capture.log` says so and prints the recovery command:

```bash
rastro capture --sha <sha> --since 2026-10-05T11:30    # recapture with an explicit window start and upload (idempotent)
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

## Conventions

- One branch and one pull request per change, against `develop`. Keep a change small enough to review.
- Add or update tests for what you change, and run the checks above before pushing (CI runs the same ones).
- A change to the database needs an Alembic migration (see above) and the models and migrations must not drift:
  `cd packages/db && uv run alembic upgrade head && uv run alembic check`.
- The hook path of the CLI (`rastro_cli.rules_cache`, `rules_block`, `hook_entry`) must stay standard library only
  and free of heavy imports; a test enforces it.
- Architecture notes for contributors, including the decisions behind each part, are in [CLAUDE.md](CLAUDE.md) and
  in the specs under [docs/](docs/). Reference material about how the server behaves is in
  [docs/reference.md](docs/reference.md).

## Releasing the packages

For maintainers.

Three distributions are published, in this order because each depends on the previous one:
`rastro-session-parsers`, `rastro-cli` (the command is still `rastro`) and `rastro-mcp` (the read-only MCP
server). The version lives in each package's `pyproject.toml`; bump them together when an API between them
changes and keep the `rastro-session-parsers` range in `packages/cli/pyproject.toml` and the `rastro-cli`
range in `packages/mcp/pyproject.toml` in sync. The license is MIT (`LICENSE` at the root and in each
package). The distribution
is called `rastro-cli` because `rastro` is already taken on PyPI by another project; the three names were free
when checked (2026-10-21), so confirm they still are right before the first upload. The product name is
provisional (spec), so renaming later only changes the distribution names: keep the `rastro` script or update
the hook text in `rastro_cli/hook.py` too. A published version can never be replaced, so rehearse on TestPyPI.

```bash
rm -rf dist
uv build --package rastro-session-parsers -o dist
uv build --package rastro-cli -o dist
uv build --package rastro-mcp -o dist
uvx twine check dist/*
# smoke test in a throwaway tool dir, resolving the sibling wheel from dist/ (add --no-cache --reinstall if
# a version was built before with different content: uv caches wheels by version)
UV_TOOL_DIR=$(mktemp -d) UV_TOOL_BIN_DIR=$(mktemp -d) uv tool install --find-links dist ./dist/rastro_cli-<version>-py3-none-any.whl
# publish (maintainers only; the token comes from the environment, never from a file in the repository):
export UV_PUBLISH_TOKEN=...        # a TestPyPI token for a rehearsal, a PyPI token for the real thing
uv publish --publish-url https://test.pypi.org/legacy/ dist/rastro_session_parsers-*   # rehearsal; drop --publish-url for PyPI
uv publish --publish-url https://test.pypi.org/legacy/ dist/rastro_cli-*
uv publish --publish-url https://test.pypi.org/legacy/ dist/rastro_mcp-*
```

After the real upload, tag the exact commit the packages were built from and create a pre-release on GitHub (one tag for the three packages while they share a version; use per-package tags if they ever diverge). The target must be the full commit SHA:

```bash
gh release create v<version> --target "$(git rev-parse HEAD)" --prerelease --title "v<version>" --notes-file <notes.md>
```

Users then run `pipx install rastro-cli` or `uvx --from rastro-cli rastro init` (`uvx` needs `--from`
because the package and the command have different names). From TestPyPI the dependencies live on PyPI, so
add `--extra-index-url https://pypi.org/simple/` (`pipx install --index-url https://test.pypi.org/simple/
--pip-args="--extra-index-url https://pypi.org/simple/" rastro-cli`). The git hook calls `rastro` by name, so the
tool's bin directory must be on the `PATH` git sees (`pipx ensurepath` / `uv tool update-shell`);
`rastro init` warns when it is not.
