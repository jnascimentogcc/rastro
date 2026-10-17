# Deploying Rastro on the shared Hetzner server

Rastro runs next to Azmt and Radar Publico on the same server. It reuses the Caddy that Azmt owns
(ports 80/443, automatic TLS) through the external Docker network `proxy`, exactly like Radar.

```
Internet -> azmt-caddy (80/443) -> rastro.karpaai.com -> rastro-api:8000 -> rastro-db (pgvector)
                                      /webhooks/github, /health, /v1/*     rastro-worker -^
```

Only `rastro-api` joins `proxy`. Postgres and the worker stay on the project's private network and
nothing is published on the host. The Caddy block exposes only what GitHub and the CLI need; `/docs`
and `/openapi.json` answer 404.

Files: `docker-compose.prod.yml`, `deploy/install.sh`, `deploy/deploy.sh`, `deploy/backup.sh`,
`deploy/env.prod.example`, `deploy/Caddyfile.rastro.snippet`.

## Capacity

The server size is kept as is. Rastro adds Postgres (limit 512 MB), API (384 MB) and worker
(512 MB). Before installing, run `free -h`, `df -h /` and `docker stats --no-stream`. `install.sh`
warns when less than 1.5 GB is available, because building the images needs more than running them
(add swap, or build elsewhere and push the images, if the build fails). Container logs are capped at
3 x 10 MB each.

## 1. DNS

Create an `A` record `rastro.karpaai.com` pointing at the server IP (same as `radar.karpaai.com`).

## 2. Production GitHub App

Create a new App, separate from the development one (which keeps its smee webhook):

- Webhook URL `https://rastro.karpaai.com/webhooks/github`; webhook secret: take it from the
  server's `.env` after the first `install.sh` run (`GITHUB_WEBHOOK_SECRET`), or generate one and
  put the same value in `.env`.
- Same permissions and events as the development App (see README, "GitHub App setup").
- Generate a private key, note the App ID, install the App on the repositories.

## 3. Install

```bash
git clone https://github.com/myllmi/rastro.git /opt/rastro && cd /opt/rastro && git checkout develop
bash deploy/install.sh        # first run creates .env, generates secrets, lists what is missing
```

Fill in `.env` (`GITHUB_APP_ID`, `OPENAI_API_KEY`, `LLM_MODEL`, optionally `LLM_MODEL_IMPACT`),
copy the App key to `secrets/github-app.private-key.pem`, and run `bash deploy/install.sh` again.
It is idempotent and never replaces an existing secret.

## 4. Caddy

Append `deploy/Caddyfile.rastro.snippet` to the Azmt Caddyfile on the server
(`/opt/azmt/deploy/Caddyfile`), then reload without downtime and check:

```bash
docker exec azmt-caddy caddy validate --config /etc/caddy/Caddyfile
docker exec azmt-caddy caddy reload --config /etc/caddy/Caddyfile
curl -i https://rastro.karpaai.com/health
```

A mistake here affects all three apps: validate before reloading. Do not recreate the container.

## 5. Connect a repository

```bash
docker compose -f docker-compose.prod.yml exec api \
  python -m rastro_api.admin issue-token --github-id <repo id> --full-name owner/repo
```

On the developer machine, `rastro init` with `https://rastro.karpaai.com` and the token. The first
push after installing the App triggers indexing (`index_repository`).

## Operations

- **Update:** `./deploy/deploy.sh` (backup, `git pull --ff-only`, rebuild, migrations, restart).
- **Backups:** daily 03:30 by cron to `/opt/rastro/backups` (14 days). They are local: copy them off
  the server too. Restore: `gunzip -c file.sql.gz | docker exec -i -e PGPASSWORD=... rastro-db psql -U rastro rastro`.
- **Keep outside the server:** `EXCERPTS_ENCRYPTION_KEYS`. Without it stored session excerpts cannot
  be read. Rotate by prepending a new key and running `python -m rastro_db.crypto reencrypt`.
- **Cost:** `LLM_MONTHLY_TOKEN_BUDGET` (default 2,000,000 per repository per month) plus a limit in the
  OpenAI dashboard. After an empty balance or budget, `docker compose -f docker-compose.prod.yml exec worker python -m rastro_worker.requeue`.
- **Monitoring:** point an external uptime monitor at `https://rastro.karpaai.com/health`; check
  failed jobs with `SELECT status, count(*) FROM procrastinate_jobs GROUP BY 1` in `rastro-db`.
- **Logs:** `docker compose -f docker-compose.prod.yml logs -f worker`.

## Privacy

Diffs and session excerpts are sent to OpenAI (no EU data residency in this mode). Excerpts are
encrypted at rest and secrets are redacted before upload, but decide per repository whether that is
acceptable.
