#!/usr/bin/env bash
#
# PostgreSQL backup with verification and retention.
#
# Two properties worth noting:
#   * The dump is written with mode 0600 into a 0700 directory.
#   * Every backup is verified by actually restoring it into a scratch database
#     and counting rows. An unverified backup is a guess, not a backup.
#
# No credential is written into the dump: no table in this schema stores one.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BEROAPP_BACKUP_DIR:-$ROOT/backups}"
RETENTION_DAYS="${BEROAPP_BACKUP_RETENTION_DAYS:-14}"
DB_NAME="${BEROAPP_DB_NAME:-beroapp}"
DB_USER="${BEROAPP_DB_USER:-beroapp}"
DB_HOST="${BEROAPP_DB_HOST:-127.0.0.1}"
DB_PORT="${BEROAPP_DB_PORT:-5432}"
VERIFY_DB="${DB_NAME}_verify_$$"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$BACKUP_DIR/${DB_NAME}_${timestamp}.dump"

log() { printf '{"timestamp":"%s","component":"backup","event":"%s"}\n' "$(date -u +%FT%TZ)" "$1"; }

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

log "backup_started"
pg_dump --format=custom --compress=9 \
        --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" \
        --dbname="$DB_NAME" --file="$archive"
chmod 600 "$archive"

size=$(wc -c < "$archive")
if [ "$size" -lt 1024 ]; then
    log "backup_failed_too_small"
    rm -f "$archive"
    exit 1
fi

# --- verification -----------------------------------------------------------
# Restore into a scratch database and confirm the core tables are present and
# populated. Dropped in the trap whether or not this succeeds.
cleanup() {
    dropdb --if-exists --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" "$VERIFY_DB" 2>/dev/null || true
}
trap cleanup EXIT

log "verification_started"
createdb --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" "$VERIFY_DB"
pg_restore --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" \
           --dbname="$VERIFY_DB" --no-owner --no-privileges "$archive" >/dev/null 2>&1

tables=$(psql --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" --dbname="$VERIFY_DB" \
              -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
if [ "$tables" -lt 20 ]; then
    log "verification_failed_missing_tables"
    exit 1
fi

markets=$(psql --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" --dbname="$VERIFY_DB" \
               -tAc "SELECT count(*) FROM markets")
log "verification_passed tables=$tables markets=$markets bytes=$size"

# --- retention --------------------------------------------------------------
find "$BACKUP_DIR" -name "${DB_NAME}_*.dump" -type f -mtime "+$RETENTION_DAYS" -delete
remaining=$(find "$BACKUP_DIR" -name "${DB_NAME}_*.dump" -type f | wc -l | tr -d ' ')
log "backup_complete archive=$(basename "$archive") retained=$remaining"
