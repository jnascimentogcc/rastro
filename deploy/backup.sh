#!/usr/bin/env bash
# PostgreSQL backup (compressed pg_dump with rotation). Local only: copy the files off the server too.
#
#   ./deploy/backup.sh
# Cron (installed by deploy/install.sh): 30 3 * * * /opt/rastro/deploy/backup.sh >> /var/log/rastro-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source .env

BACKUP_DIR="${BACKUP_DIR:-$(pwd)/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$BACKUP_DIR/rastro-$TIMESTAMP.sql.gz"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# Write to a temp name so a failed dump never leaves a truncated file that looks valid.
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" rastro-db pg_dump -U rastro --clean --if-exists rastro \
  | gzip > "$TARGET.partial"
mv "$TARGET.partial" "$TARGET"

find "$BACKUP_DIR" -name 'rastro-*.sql.gz' -mtime "+$KEEP_DAYS" -delete

echo "Backup saved to $TARGET"
