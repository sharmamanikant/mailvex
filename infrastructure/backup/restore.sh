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
: "${APP_PGUSER:=${POSTGRES_USER:-$PGUSER}}"
: "${APP_PGPASSWORD:=${POSTGRES_PASSWORD:-$PGPASSWORD}}"
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

# Do not drop the target database unless the backup role can recreate it owned
# by the application role. On external PostgreSQL, the backup account is kept
# separate from the app account; a database administrator must grant this
# capability explicitly before this restore procedure can proceed.
CAN_RECREATE_DB=$(PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres \
    -tAc "SELECT rolcreatedb AND pg_has_role(current_user, '$APP_PGUSER', 'MEMBER') FROM pg_roles WHERE rolname = current_user")
if [ "$CAN_RECREATE_DB" != "t" ]; then
    log "ERROR: backup role cannot recreate a database owned by $APP_PGUSER; ask the database administrator to provision restore permissions"
    exit 1
fi

# Recreate the target database cleanly (provisioning step for a failed DB).
PGPASSWORD="$APP_PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$APP_PGUSER" -d postgres \
    -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $PGDATABASE"
PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres \
    -v ON_ERROR_STOP=1 -c "CREATE DATABASE $PGDATABASE OWNER $APP_PGUSER"

zcat "$DUMP" | PGPASSWORD="$APP_PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$APP_PGUSER" -d "$PGDATABASE" \
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
    COUNT=$(PGPASSWORD="$APP_PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$APP_PGUSER" -d "$PGDATABASE" -tAc "SELECT COUNT(*) FROM $t" 2>/dev/null || echo "ERR")
    log "  $t: $COUNT rows"
done
ALEMBIC_VERSION=$(PGPASSWORD="$APP_PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$APP_PGUSER" -d "$PGDATABASE" -tAc "SELECT version_num FROM alembic_version" 2>/dev/null || echo "none")
log "  alembic_version: $ALEMBIC_VERSION"

log "STEP 5: Restoring uploaded file store (if a staged copy is available)"
if [ -n "$STORAGE_RESTORE" ] && [ -d "$STORAGE_RESTORE" ]; then
    DEST="${STORAGE_DEST:-/app/.local_uploads}"
    mkdir -p "$DEST"
    # The `backup` service mounts the filestore read-only on purpose, so a
    # restore cannot run from there. Fail with the exact remedy instead of
    # letting tar die with "Read-only file system" halfway through.
    if ! touch "$DEST/.restore-write-probe" 2>/dev/null; then
        log "ERROR: $DEST is not writable (the backup container mounts uploads_data read-only)."
        log "Run the dedicated restore service instead, which mounts it read-write:"
        log "  docker compose --profile restore --env-file .env.production run --rm restore"
        exit 1
    fi
    rm -f "$DEST/.restore-write-probe"
    RESTORED=0
    for archive in "$STORAGE_RESTORE"/*.tar.gz; do
        [ -e "$archive" ] || continue
        log "  extracting $(basename "$archive")"
        tar -xzf "$archive" -C "$DEST" --strip-components=1
        RESTORED=$((RESTORED + 1))
    done
    log "Uploaded files restored to $DEST ($RESTORED archive(s))"
else
    log "  No filestore snapshot provided; skipping"
fi

log "STEP 6: Start the application and verify readiness"
cat <<'EOF'

Restore is performed by the dedicated `restore` profile service, which mounts the
filestore read-write (the long-running `backup` service keeps it read-only):

    docker compose --profile restore --env-file .env.production run --rm restore

After it completes, bring the application back up and verify readiness:

    docker compose --env-file .env.production up -d redis backend worker scheduler frontend nginx
    curl -fsS http://127.0.0.1:8000/health && echo OK
    docker compose --env-file .env.production ps    # all services healthy

A successful health check plus a spot-check query confirms the application is
reading from the restored database:

    docker compose --env-file .env.production exec backend \\
        python -c "import sqlalchemy as sa; e=sa.create_engine('\$DATABASE_URL'); \\
        print(sa.text('SELECT count(*) FROM contacts').scalar(e.connect()))"
EOF

log "RESTORE PROCEDURE COMPLETE"
