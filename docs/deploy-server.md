# Deploying Rastro on a Linux server behind an existing Caddy

This is an alternative to the plain `docker compose up` of [INSTALLATION.md](../INSTALLATION.md#23-start-the-stack),
for a server that already runs a [Caddy](https://caddyserver.com/) (ports 80/443, automatic TLS) in Docker for other
sites. Rastro joins it through the external Docker network `proxy` and does not publish anything on the host. It adds
daily backups, resource limits and a one-command update.

```
Internet -> your Caddy (80/443) -> rastro.example.com -> rastro-api:8000 -> rastro-db (pgvector)
                                     /webhooks/github, /health, /v1/*      rastro-worker -^
```

Only `rastro-api` joins `proxy`. Postgres and the worker stay on the project's private network. The Caddy block
exposes only what GitHub and the CLI need; `/docs` and `/openapi.json` answer 404.

Files: `docker-compose.prod.yml`, `deploy/install.sh`, `deploy/deploy.sh`, `deploy/backup.sh`,
`deploy/env.prod.example`, `deploy/Caddyfile.rastro.snippet`.

If your reverse proxy is not a Caddy in Docker, use the compose file of INSTALLATION.md and put your proxy in front
of port 8000 instead.

## Capacity

Rastro adds Postgres (limit 512 MB), API (384 MB) and worker (512 MB). Before installing, run `free -h`,
`df -h /` and `docker stats --no-stream`. `install.sh` warns when less than 1.5 GB is available, because building
the images needs more than running them (add swap, or build elsewhere and push the images, if the build fails).
Container logs are capped at 3 x 10 MB each.

## 1. DNS

Create an `A` record for the domain you will use (the examples say `rastro.example.com`) pointing at the server IP.

## 2. Production GitHub App

Create a new App, separate from any development one:

- Webhook URL `https://<your domain>/webhooks/github`; webhook secret: take it from the server's `.env` after the
  first `install.sh` run (`GITHUB_WEBHOOK_SECRET`), or generate one and put the same value in `.env`.
- Same permissions and events as in [INSTALLATION.md](../INSTALLATION.md#22-create-the-github-app), including the
  **Issue comment** event used to review proposed rules from the pull request.
- Generate a private key, note the App ID, install the App on the repositories.

## 3. Install

```bash
git clone https://github.com/jnascimentogcc/rastro.git /opt/rastro && cd /opt/rastro && git checkout develop
bash deploy/install.sh        # first run creates .env, generates secrets, lists what is missing
```

Fill in `.env` (`RASTRO_DOMAIN`, `GITHUB_APP_ID`, `OPENAI_API_KEY`, `LLM_MODEL`, `EMBEDDING_MODEL`, optionally
`LLM_MODEL_IMPACT`), copy the App key to `secrets/github-app.private-key.pem`, and run `bash deploy/install.sh`
again. It is idempotent and never replaces an existing secret.

## 4. Caddy

Edit `deploy/Caddyfile.rastro.snippet` (replace the domain) and append it to the Caddyfile your Caddy uses, then reload
without downtime and check. The container name and the Caddyfile path are yours:

```bash
docker exec <your-caddy-container> caddy validate --config /etc/caddy/Caddyfile
docker exec <your-caddy-container> caddy reload --config /etc/caddy/Caddyfile
curl -i https://<your domain>/health
```

A mistake here affects every site behind that Caddy: validate before reloading. Do not recreate the container.

## 5. Connect a repository

```bash
docker compose -f docker-compose.prod.yml exec api \
  python -m rastro_api.admin issue-token --github-id <repo id> --full-name owner/repo
```

On the developer machine, `rastro init --api-url https://<your domain>` and the token. The first push after
installing the App triggers indexing (`index_repository`).

## Operations

- **Update:** `./deploy/deploy.sh` (backup, `git pull --ff-only`, rebuild, migrations, restart).
- **Backups:** daily 03:30 by cron to `/opt/rastro/backups` (14 days). They are local: copy them off the server too.
  Restore: `gunzip -c file.sql.gz | docker exec -i -e PGPASSWORD=... rastro-db psql -U rastro rastro`.
- **Keep outside the server:** `EXCERPTS_ENCRYPTION_KEYS`. Without it stored session excerpts cannot be read. Rotate
  by prepending a new key and running `python -m rastro_db.crypto reencrypt`.
- **Cost:** `LLM_MONTHLY_TOKEN_BUDGET` (default 2,000,000 per repository per month) plus a limit in the OpenAI
  dashboard. After an empty balance or budget,
  `docker compose -f docker-compose.prod.yml exec worker python -m rastro_worker.requeue`.
- **Monitoring:** point an external uptime monitor at `https://<your domain>/health`; check failed jobs with
  `SELECT status, count(*) FROM procrastinate_jobs GROUP BY 1` in `rastro-db`.
- **Crashed or killed worker:** workers write a heartbeat every 10 s; a periodic task (`recover_stalled_jobs`, every
  minute) puts back the `doing` jobs of a worker silent for 120 s, so a crash, OOM kill or deploy restart frees its
  repository by itself about two to three minutes later (look for `was running on a worker that stopped sending
  heartbeats` in the worker log). A job that kills its worker five times is marked `failed` (`giving up` in the log)
  so it cannot block the repository. Last resort if a job is stuck in `doing` and nothing recovers it:
  `UPDATE procrastinate_jobs SET status='failed' WHERE status='doing'` in `rastro-db` (use `'todo'` to run it
  again). Knobs: `WORKER_HEARTBEAT_INTERVAL_SECONDS`, `WORKER_STALLED_TIMEOUT_SECONDS`, `JOB_MAX_ATTEMPTS`.
- **Logs:** `docker compose -f docker-compose.prod.yml logs -f worker`.

## Privacy

Diffs and session excerpts are sent to OpenAI (no EU data residency in this mode). Excerpts are encrypted at rest and
secrets are redacted before upload, but decide per repository whether that is acceptable.
