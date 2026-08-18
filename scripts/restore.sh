#!/usr/bin/env bash
#
# Restore from a backup. Deliberately requires an explicit target database name
# and refuses to overwrite one that already has tables, so a restore cannot
# silently destroy live data.
#
# Usage: scripts/restore.sh <archive.dump> <target-database>

set -euo pipefail

archive="${1:?usage: restore.sh <archive.dump> <target-database>}"
target="${2:?usage: restore.sh <archive.dump> <target-database>}"
DB_USER="${BEROAPP_DB_USER:-beroapp}"
DB_HOST="${BEROAPP_DB_HOST:-127.0.0.1}"
DB_PORT="${BEROAPP_DB_PORT:-5432}"

[ -f "$archive" ] || { echo "archive not found: $archive" >&2; exit 1; }

existing=$(psql --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" --dbname=postgres \
    -tAc "SELECT 1 FROM pg_database WHERE datname='$target'" || true)

if [ "$existing" = "1" ]; then
    tables=$(psql --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" --dbname="$target" \
        -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
    if [ "$tables" -gt 0 ]; then
        echo "refusing to restore over '$target', which already has $tables tables." >&2
        echo "drop it explicitly first if that is what you intend." >&2
        exit 1
    fi
else
    createdb --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" "$target"
fi

pg_restore --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" \
           --dbname="$target" --no-owner --no-privileges "$archive"

echo "restored $archive into $target"
echo "next: psql -d $target -f ops/grants.sql"
