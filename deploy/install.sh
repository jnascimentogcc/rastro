#!/usr/bin/env bash
# First install (and safe re-run) of Rastro on the shared server. Idempotent.
#
#   1. creates the external `proxy` network if missing, and .env from deploy/env.prod.example;
#   2. generates the secrets that are empty (never replaces existing ones);
#   3. checks that the GitHub App values and the private key are present;
#   4. builds and starts the stack, waits until healthy and tests the public URL;
#   5. schedules the daily backup.
#
#   git clone https://github.com/myllmi/rastro.git /opt/rastro && cd /opt/rastro && git checkout develop
#   bash deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.prod.yml)
ENV_FILE=".env"

step() { printf '\n==> %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*"; }
fail() { printf '    ERROR: %s\n' "$*" >&2; exit 1; }

value() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }
set_value() { # no sed: values may contain / + =
  local tmp; tmp="$(mktemp)"
  grep -vE "^$1=" "$ENV_FILE" > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
}
generate_if_empty() { # generate_if_empty KEY "command printing the value"
  if [ -z "$(value "$1")" ]; then
    set_value "$1" "$(eval "$2")"
    NEW+=("$1")
  fi
}

NEW=()

step "Checks"
command -v docker >/dev/null || fail "Docker is not installed."
docker compose version >/dev/null 2>&1 || fail "the docker compose plugin is missing."
command -v openssl >/dev/null || fail "openssl is missing (apt install openssl)."
[ -f docker-compose.prod.yml ] || fail "run this script from the project folder."
RAM_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
AVAILABLE_MB="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
echo "    RAM: ${RAM_MB} MB total, ${AVAILABLE_MB} MB available (Rastro needs about 1 GB while running; the build needs more)"
[ "$AVAILABLE_MB" -ge 1500 ] || warn "less than 1.5 GB available: the image build may fail. Add swap or build elsewhere."

step "Network, folders and permissions"
docker network inspect proxy >/dev/null 2>&1 || { docker network create proxy >/dev/null; echo "    network proxy created"; }
mkdir -p backups secrets
chmod 700 backups secrets

step "Settings (.env)"
[ -f "$ENV_FILE" ] || { cp deploy/env.prod.example "$ENV_FILE"; echo "    .env created from deploy/env.prod.example"; }
chmod 600 "$ENV_FILE"
generate_if_empty POSTGRES_PASSWORD "openssl rand -hex 24"
generate_if_empty EXCERPTS_ENCRYPTION_KEYS "openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'"   # Fernet: 32 bytes, url-safe base64
generate_if_empty GITHUB_WEBHOOK_SECRET "openssl rand -hex 32"

DOMAIN="$(value RASTRO_DOMAIN)"; DOMAIN="${DOMAIN:-rastro.karpaai.com}"
MISSING=()
for key in GITHUB_APP_ID OPENAI_API_KEY LLM_MODEL EMBEDDING_MODEL; do
  [ -n "$(value "$key")" ] || MISSING+=("$key")
done
[ -f secrets/github-app.private-key.pem ] || MISSING+=("secrets/github-app.private-key.pem")
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "    Missing: ${MISSING[*]}"
  echo "    Fill them in (.env, and copy the App private key to secrets/, chmod 600) and run this script again."
  if [ "${#NEW[@]}" -gt 0 ]; then
    echo "    Secrets generated now: ${NEW[*]}"
    echo "    Use GITHUB_WEBHOOK_SECRET as the webhook secret of the production GitHub App:  grep '^GITHUB_WEBHOOK_SECRET=' $ENV_FILE"
    echo "    KEEP A COPY of EXCERPTS_ENCRYPTION_KEYS outside the server."
  fi
  exit 0
fi
chmod 600 secrets/github-app.private-key.pem
# The containers run as uid 10001 and must read the key.
chown 10001 secrets/github-app.private-key.pem 2>/dev/null || warn "could not chown the key to uid 10001; run as root or make it readable."

step "Build and start (the first time takes a few minutes)"
"${COMPOSE[@]}" up --build -d

step "Waiting for healthy"
ok=0
for _ in $(seq 1 60); do
  state="$("${COMPOSE[@]}" ps --format '{{.Service}} {{.Health}}' 2>/dev/null | grep -E '^(api|worker|postgres) ' || true)"
  if [ "$(printf '%s\n' "$state" | grep -c healthy)" -ge 3 ] && ! printf '%s' "$state" | grep -qE 'unhealthy|starting'; then ok=1; break; fi
  sleep 5
done
"${COMPOSE[@]}" ps
[ "$ok" = 1 ] || { "${COMPOSE[@]}" logs --tail 40 migrate api worker; fail "the stack did not become healthy (logs above)."; }

step "Public URL"
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "https://$DOMAIN/health" 2>&1 || true)"
if [ "$code" = 200 ]; then echo "    https://$DOMAIN/health -> 200"
else warn "https://$DOMAIN/health -> ${code:-no answer}. Check DNS and the Caddy block (docker logs azmt-caddy --tail 30)."; fi

step "Daily backup (03:30)"
chmod +x deploy/*.sh 2>/dev/null || true
LINE="30 3 * * * $(pwd)/deploy/backup.sh >> /var/log/rastro-backup.log 2>&1"
if crontab -l 2>/dev/null | grep -qF "$(pwd)/deploy/backup.sh"; then echo "    already scheduled"
else (crontab -l 2>/dev/null; echo "$LINE") | crontab - && echo "    scheduled"; fi

step "Done"
if [ "${#NEW[@]}" -gt 0 ]; then
  echo "    Secrets generated now: ${NEW[*]}"
  echo "    KEEP A COPY of EXCERPTS_ENCRYPTION_KEYS outside the server:  grep '^EXCERPTS_ENCRYPTION_KEYS=' $ENV_FILE"
fi
echo "    Next: issue a repository token (docs/deploy-hetzner.md)."
