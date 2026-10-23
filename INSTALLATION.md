# Installing Rastro

Rastro has two parts, installed by two different people:

| Part | Who | What it is | Section |
| --- | --- | --- | --- |
| **The CLI** (`rastro`) | every developer | a command on your machine that captures your coding-agent sessions at commit time and lets you query the rules | [1](#1-for-developers-install-the-cli) |
| **The server** | one person per team (the *operator*) | an API, a worker and a database that extract the rules and comment on pull requests; it also needs a GitHub App | [2](#2-for-the-operator-install-the-server) |

If someone on your team already runs the server, you only need section 1 and two values from them: the
**server URL** and a **repository token**.

---

## 1. For developers: install the CLI

### Requirements

- Python 3.12 or newer, `git`, and [pipx](https://pipx.pypa.io/) or [uv](https://docs.astral.sh/uv/).
- Claude Code (the only coding agent supported today) and a GitHub repository.
- From the operator: the **server URL** and a **repository token** for this repository (one token per repository).

### 1.1 Install

```bash
pipx install rastro-cli          # or: uv tool install rastro-cli
```

To try the optional MCP server (section 1.3) install it next to the CLI: `pipx install rastro-mcp`, or
`uv tool install rastro-mcp`. To update later: `pipx upgrade rastro-cli` or `uv tool upgrade rastro-cli`.

Check it works, and that the tool's directory is on your `PATH` (the git hook calls `rastro` by name):

```bash
rastro --help
```

If the command is not found, run `pipx ensurepath` or `uv tool update-shell` and open a new terminal.

### 1.2 Connect a repository

Inside the repository, once:

```bash
cd /path/to/your/repo
rastro init --api-url https://your-rastro-server     # asks for the token, then installs the post-commit hook
```

- The token is stored in the system keychain, never in the repository. On a machine without a keychain
  (a server, a container) set `RASTRO_TOKEN` in the environment instead.
- `rastro init` checks the token with the server and prints `Authenticated for owner/repo`. A rejected
  token (HTTP 401/403) means it is wrong or for another repository.
- It creates `.rastro/` (git-ignored automatically) and a `post-commit` hook. An existing `post-commit` shell hook is kept: the Rastro block is added at its top.

From now on every commit uploads, in the background and without ever blocking or failing the commit, the
parts of your Claude Code conversation that produced the change (secrets are removed before upload). If the
server is down, captures wait in `.rastro/pending/`; send them later with `rastro flush`.

### 1.3 Optional: let the agent see the rules before it edits

```bash
rastro agent install            # hooks in .claude/settings.local.json (not committed) + the read-only MCP server
rastro agent install --no-mcp   # hooks only
rastro agent install --shared   # for the whole team: writes .claude/settings.json (needs `rastro-hook` on everyone's PATH)
```

`rastro-mcp` (a separate package) must be installed for the MCP part;
`rastro agent install` skips it with a message when it is missing and still installs the hooks. To allow its
tools without a prompt, add `mcp__rastro` to `permissions.allow` (it only reads). Remove everything with
`rastro agent uninstall`.

### 1.4 Check that it works

```bash
rastro rules                 # the rules recorded for this repository (empty until pull requests are merged)
rastro health                # how much of the knowledge is recorded, explained and confirmed
```

Make a change with Claude Code, commit it, and open a pull request: the server comments on the PR with
the rules the change may affect and the rules it proposes. Rules only enter the knowledge base when the PR is
merged. `.rastro/capture.log` explains any commit for which nothing was captured.

### 1.5 Uninstall

```bash
rastro agent uninstall       # if you installed it
# delete the Rastro block from .git/hooks/post-commit (the lines from `# >>> rastro >>>` to `# <<< rastro <<<`),
# or the whole file if it only held that block; then delete the .rastro/ directory
pipx uninstall rastro-cli    # or: uv tool uninstall rastro-cli
```

---

## 2. For the operator: install the server

### Requirements

- Docker with Compose, and [uv](https://docs.astral.sh/uv/) (to issue repository tokens and run maintenance commands).
- A public HTTPS URL that GitHub can reach (a domain behind a reverse proxy; for a local trial,
  [smee.io](https://smee.io)).
- A GitHub account that can create a GitHub App, and an OpenAI API key (the model is used for rule extraction,
  the impact analysis and embeddings; without it those jobs are skipped).

### 2.1 Get the code and configure

```bash
git clone https://github.com/jnascimentogcc/rastro.git && cd rastro
cp .env.example .env
mkdir -p secrets
```

Fill `.env`. The values you must set:

| Variable | What |
| --- | --- |
| `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET` | from the GitHub App (step 2.2) |
| `OPENAI_API_KEY`, `LLM_MODEL`, `EMBEDDING_MODEL` | there are no defaults on purpose; `gpt-6.1-sol` is the model measured in `README.md` ("Rule extraction") |
| `EXCERPTS_ENCRYPTION_KEYS` | encrypts the stored conversation excerpts; generate one with `uv run python -m rastro_db.crypto generate-key`. Without it they are stored in plain text, acceptable only for local trials |
| `LLM_MONTHLY_TOKEN_BUDGET` | optional cap of model tokens per repository per month |

The GitHub App's private key goes to `secrets/github-app.private-key.pem`.

### 2.2 Create the GitHub App

1. Go to <https://github.com/settings/apps/new>.
2. **Webhook URL**: `https://<your-server>/webhooks/github` (locally: a smee.io channel and
   `npx smee-client --url https://smee.io/XXXX --target http://localhost:8000/webhooks/github`).
3. **Webhook secret**: a long random string, the same you put in `.env`
   (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
4. **Repository permissions**: Contents *read-only*, Pull requests *read and write*, Metadata *read-only*
   (add Issues *read* if GitHub does not offer the *Issue comment* event without it).
5. **Subscribe to events**: Push, Pull request, Issue comment.
6. **Where can this App be installed**: only on your account (or your organisation).
7. Create it, note the **App ID**, then *Generate a private key* and save the `.pem` as above.

### 2.3 Start the stack

```bash
docker compose up -d --build --wait
curl localhost:8000/health        # {"status":"ok"}
```

This starts PostgreSQL (with pgvector), runs the migrations, and starts the API (port 8000, change it with
`API_PORT`) and the worker. After pulling new code run the same command again; migrations run first.
Put your reverse proxy in front of port 8000 and expose at least `/webhooks/github` and `/v1/*`.

For a shared Linux server behind an existing Caddy, with backups, use `deploy/install.sh` instead; see
[docs/deploy-server.md](docs/deploy-server.md).

### 2.4 Install the App on a repository

Install the App from its settings page (*Install App*) on the repositories you want. GitHub sends an
`installation` event, the server registers each repository and the worker indexes its default branch. In the
App settings, *Advanced > Recent deliveries* should show `202` answers.

### 2.5 Give each repository its token

Each repository gets its own token; the developers use it in `rastro init`. Do this **after** installing the App on the repository (2.4), and it is shown **once**. The GitHub repository id is `gh api repos/owner/repo --jq .id`:

```bash
docker compose exec api python -m rastro_api.admin issue-token --github-id <GITHUB_REPO_ID> --full-name owner/repo --default-branch main
```

Send it to the team over a private channel together with the server URL. Running the command again for the
same repository issues a new token and the old one stops working.

### 2.6 Check that it works

- `docker compose ps` shows `api`, `worker` and `postgres` running (and `migrate` exited with code 0).
- Push a commit to a repository with the App installed: the worker log shows the job
  (`docker compose logs -f worker`).
- A developer runs `rastro init`, commits something with Claude Code and opens a pull request: a Rastro comment
  appears on it.

### 2.7 Maintenance

- **Credit or budget ran out**: commits get `skipped_quota` / `skipped_budget`; after fixing it run
  `docker compose exec worker python -m rastro_worker.requeue`.
- **Rotate the encryption key**: prepend the new key to `EXCERPTS_ENCRYPTION_KEYS` (`new,old`), redeploy, run
  `python -m rastro_db.crypto reencrypt`, then drop the old key.
- **Data**: nothing is deleted when the App is uninstalled; see "App lifecycle and data retention" in
  `README.md`.

---

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `rastro: command not found` | the tool's directory is not on `PATH` (`pipx ensurepath`, `uv tool update-shell`); the git hook needs it too |
| `The server rejected the token (HTTP 401)` | wrong token, or a token reissued since; ask the operator for the current one |
| `Could not reach <url>` | wrong `--api-url`, server down, or a proxy not exposing `/v1/*` |
| `No system keychain available` | set `RASTRO_TOKEN` in the environment |
| A commit uploaded nothing | read `.rastro/capture.log`: it says why (no session file, nothing edited in the window...) and prints a recovery command |
| Captures stay in `.rastro/pending/` | the server was unreachable or the repository is not `active`; fix it and run `rastro flush` |
| No comment on the pull request | App not installed on that repository, webhook URL or secret wrong (see *Recent deliveries*), or the model account has no credit (`skipped_quota`) |
| `rastro rules` is empty | rules are only created when a pull request is merged (and the worker has a model configured) |
