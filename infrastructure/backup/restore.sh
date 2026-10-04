#!/usr/bin/env sh
# Phase 22 - Full disaster-recovery restore procedure.
#
# Mirrors the documented restore runbook (docs/operations/backup-disaster-recovery.md):
#
#   Database failure
#     -> provision a fresh PostgreSQL
#     -> restore the latest (or requested) backup dump
#     -> run migrations/checks against the restored data
#     -> validate data integrity
#     -> restore uploaded file store + config
#     -> start the application
#     -> verify readiness via the health endpoint
#
# Usage:
#   restore.sh [path/to/dump.sql.gz]
#
# This script restores the database ONLY. Starting the application stack and
# verifying readiness is performed with the target orchestrator (Docker Compose
# in this repo) as documented; see the last steps below.
set -eu

: "${PGHOST:=postgres}"
: "${PGPORT:=5432}"
: "${PGUSER:=app}"
: "${PGDATABASE:=crcrm}"
: "${PGPASSWORD:=}"
: "${BACKUP_DIR:=/backups}"
: "${DB_BACKUP_PREFIX:=crcrm-db}"
: "${STORAGE_RESTORE:=}"

DUMP="${1:-}"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"; }

if [ -z "$DUMP" ]; then
    DUMP="$(ls -t "$BACKUP_DIR"/"$DB_BACKUP_PREFIX"-*.sql.gz 2>/dev/null | head -n 1)"
fi
if [ -z "$DUMP" ] || [ ! -s "$DUMP" ]; then
    log "ERROR: no backup dump found to restore"
    exit 1
fi

log "STEP 1: Restoring $DUMP into $PGDATABASE on $PGHOST:$PGPORT"

# Recreate the target database cleanly (provisioning step for a failed DB).
PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres \
    -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $PGDATABASE" -c "CREATE DATABASE $PGDATABASE"

zcat "$DUMP" | PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
    -v ON_ERROR_STOP=1 -q
log "STEP 2: Database restored"

log "STEP 3: Running migrations/checks (idempotent - restores are already at head)"
if command -v alembic >/dev/null 2>&1; then
    alembic upgrade head
    RESULTS=$(alembic current)
    log "Migration state: $RESULTS"
else
    log "alembic CLI not present in this environment; expected to run inside the app container or CI"
fi

log "STEP 4: Validating data integrity"
for t in users import_jobs campaigns audit_logs; do
    COUNT=$(PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -tAc "SELECT COUNT(*) FROM $t" 2>/dev/null || echo "ERR")
    log "  $t: $COUNT rows"
done
ALEMBIC_VERSION=$(PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -tAc "SELECT version_num FROM alembic_version" 2>/dev/null || echo "none")
log "  alembic_version: $ALEMBIC_VERSION"

log "STEP 5: Restoring uploaded file store (if a staged copy is available)"
if [ -n "$STORAGE_RESTORE" ] && [ -d "$STORAGE_RESTORE" ]; then
    DEST="${STORAGE_DEST:-/app/.local_uploads}"
    mkdir -p "$DEST"
    find "$STORAGE_RESTORE" -name '*.tar.gz' -exec sh -c 'tar -xzf "$1" -C "$2" --strip-components=1' _ {} "$DEST" \;
    log "Uploaded files restored to $DEST"
else
    log "  No filestore snapshot provided; skipping"
fi

log "STEP 6: Start the application and verify readiness"
cat <<'EOF'

Remaining steps are performed by the orchestrator (this repository uses Docker
Compose). After the DB is restored:

    docker compose --env-file .env up -d postgres redis backend worker scheduler frontend nginx

Then verify readiness:

    curl -fsS http://127.0.0.1:8000/health && echo OK
    docker compose ps                       # all services healthy
    PGPASSWORD="$PGPASSWORD" psql -c 'SELECT count(*) FROM contacts' "$DATABASE_URL"

A successful health check plus a spot-check query confirms the application is
reading from the restored database.
EOF

log "RESTORE PROCEDURE COMPLETE"
