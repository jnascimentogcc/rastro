#!/usr/bin/env bash
# Update Rastro in production: backup, git pull, new images, migrations (the `migrate` service
# runs before api/worker restart) and a restart. Run on the server, from the project folder.
set -euo pipefail
cd "$(dirname "$0")/.."

./deploy/backup.sh
git pull --ff-only
docker compose -f docker-compose.prod.yml up --build -d
docker image prune -f
docker compose -f docker-compose.prod.yml ps
