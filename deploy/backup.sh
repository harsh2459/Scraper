#!/usr/bin/env bash
# Automated daily Postgres backup — runs as the tmtracker user via
# tmtracker-backup.timer/.service. Same pg_dump invocation already
# documented as a manual step in HOW-TO-UPDATE.md's BACKUP section, just
# scheduled and with retention pruning instead of "remember to run this
# weekly-ish".
set -euo pipefail

APP_DIR="/opt/tmtracker"
BACKUP_DIR="$APP_DIR/backups"
RETENTION_DAYS=14

# Loaded the same way the app itself reads DB config — PG_* vars in the
# scraper's own .env (both services point at the same database, so either
# .env would do; the scraper's is the canonical one).
if [ -f "$APP_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$APP_DIR/.env"
  set +a
fi

PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_DBNAME="${PG_DBNAME:-datascraper}"
PG_USER="${PG_USER:-tmtracker}"

mkdir -p "$BACKUP_DIR"

STAMP=$(date +%F_%H%M%S)
OUT_FILE="$BACKUP_DIR/datascraper-$STAMP.dump"

# -Fc (custom format) rather than plain SQL — smaller, and restorable with
# pg_restore including selective table/schema restore, unlike a flat .sql.
PGPASSWORD="${PG_PASSWORD:-}" pg_dump \
  -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DBNAME" \
  --no-owner --no-privileges -Fc \
  -f "$OUT_FILE"

echo "backup.sh: wrote $OUT_FILE ($(du -h "$OUT_FILE" | cut -f1))"

# Prune anything older than RETENTION_DAYS — keeps disk usage bounded
# without needing a separate cleanup job.
find "$BACKUP_DIR" -name 'datascraper-*.dump' -mtime "+$RETENTION_DAYS" -print -delete

echo "backup.sh: done (retention: ${RETENTION_DAYS}d)"
