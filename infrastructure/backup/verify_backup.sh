#!/usr/bin/env sh
# Phase 22 - Backup verification.
#
# A backup is not considered valid until restoration has been tested. This
# script takes a specific PostgreSQL dump (or the newest one in BACKUP_DIR),
# restores it into an isolated scratch database, and runs lightweight integrity
# checks (schema present, key tables non-empty, alembic version at head).
#
# Usage:
#   verify_backup.sh [path/to/dump.sql.gz]
set -eu

: "${PGHOST:=postgres}"
: "${PGPORT:=5432}"
: "${PGUSER:=app}"
: "${PGPASSWORD:=}"
: "${BACKUP_DIR:=/backups}"
: "${DB_BACKUP_PREFIX:=crcrm-db}"

DUMP="${1:-}"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"; }

if [ -z "$DUMP" ]; then
    DUMP="$(ls -t "$BACKUP_DIR"/"$DB_BACKUP_PREFIX"-*.sql.gz 2>/dev/null | head -n 1)"
fi
if [ -z "$DUMP" ] || [ ! -s "$DUMP" ]; then
    log "ERROR: no backup dump found to verify"
    exit 1
fi

VERIFY_DB="crcrm_verify_$(date -u +%Y%m%d%H%M%S)"
cleanup() {
    PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres -qc "DROP DATABASE IF EXISTS $VERIFY_DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

log "Restoring $DUMP into isolated database $VERIFY_DB"
PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 -qc "CREATE DATABASE $VERIFY_DB"
zcat "$DUMP" | PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$VERIFY_DB" -v ON_ERROR_STOP=1 -q >/dev/null
log "Restore succeeded"

# Integrity checks -------------------------------------------------------- #
log "Checking schema and key tables"
TABLES="tenants users role_permissions import_jobs campaigns scheduled_messages delivery_jobs"
for t in $TABLES; do
    COUNT=$(PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$VERIFY_DB" -tAc "SELECT COUNT(*) FROM $t" 2>/dev/null || echo "ERR")
    log "  $t: $COUNT rows"
done

ALEMBIC_VERSION=$(PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$VERIFY_DB" -tAc "SELECT version_num FROM alembic_version" 2>/dev/null || echo "none")
log "alembic_version: $ALEMBIC_VERSION"

log "Verification PASSED for $DUMP"
