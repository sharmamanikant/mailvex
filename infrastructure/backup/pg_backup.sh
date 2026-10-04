#!/usr/bin/env sh
# Phase 22 - PostgreSQL logical backup with rotation + integrity verification.
#
# Produces a plain pg_dump per run, gzip-compresses it, verifies the archive
# can be restored into an isolated scratch database (psql -f), and prunes old
# backups beyond the configured retention policy.
#
# Designed to run either inside the `backup` Docker sidecar (default env vars)
# or from a host cron by supplying the same variables. Uses `pg_dump` exactly
# once and never touches live data.
set -eu

: "${PGHOST:=postgres}"
: "${PGPORT:=5432}"
: "${PGUSER:=app}"
: "${PGDATABASE:=crcrm}"
: "${PGPASSWORD:=}"
: "${BACKUP_DIR:=/backups}"
: "${DB_RETENTION_DAYS:=14}"
: "${DB_BACKUP_PREFIX:=crcrm-db}"

BACKUP_DIR="$(echo "$BACKUP_DIR" | sed 's:/*$::')"
mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%d%H%M%S)"
DUMP="$BACKUP_DIR/$DB_BACKUP_PREFIX-$STAMP.sql.gz"
VERIFY_DB="crcrm_verify_$STAMP"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"; }

cleanup() {
    if [ -n "${VERIFY_DB:-}" ]; then
        PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres -qc "DROP DATABASE IF EXISTS $VERIFY_DB" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT INT TERM

log "Beginning PostgreSQL backup of $PGDATABASE on $PGHOST:$PGPORT"
PGPASSWORD="$PGPASSWORD" pg_dump \
    -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
    --format=plain --no-owner --no-privileges \
    | gzip -9 > "$DUMP"
test -s "$DUMP" || { log "ERROR: dump is empty; refusing to keep file"; rm -f "$DUMP"; exit 1; }

log "Verifying backup integrity (isolated restore: $VERIFY_DB)"
PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 -qc "CREATE DATABASE $VERIFY_DB"
zcat "$DUMP" | PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$VERIFY_DB" -v ON_ERROR_STOP=1 -q >/dev/null 2>&1
log "Integrity check passed: archive restored without errors"

log "Pruning backups older than $DB_RETENTION_DAYS days"
find "$BACKUP_DIR" -maxdepth 1 -type f -name "$DB_BACKUP_PREFIX-*.sql.gz" \
    -mtime +"$DB_RETENTION_DAYS" -delete

log "Backup complete: $DUMP"
ls -lh "$DUMP"
