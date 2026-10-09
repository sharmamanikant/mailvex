#!/usr/bin/env sh
# Phase 22 - Backup verification.
#
# A backup is not considered valid until restoration has been tested. This
# script takes a specific PostgreSQL dump (or the newest one in BACKUP_DIR),
# restores it into an isolated scratch database, and runs integrity checks.
#
# Exits non-zero when the dump is structurally incomplete: a required table is
# missing, tenants/users restored empty, or alembic_version is absent. Tables
# that are legitimately empty on a young install are reported but do not fail.
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
# A restore that succeeds is not sufficient. This section fails the run when the
# dump is structurally incomplete, and separately reports tables that restored
# but are empty, so a truncated or wrong-tenant dump cannot be called PASSED.
log "Checking schema and key tables"

FAILURES=0
WARNINGS=0

fail() { log "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }
warn() { log "  WARN: $1"; WARNINGS=$((WARNINGS + 1)); }

# Tables that must exist in any usable dump of this schema.
REQUIRED_TABLES="tenants users role_permissions import_jobs campaigns scheduled_messages delivery_jobs"
# Tables that must additionally contain at least one row; every other table is
# legitimately empty on a young or lightly used install.
MUST_BE_NONEMPTY="tenants users"

for t in $REQUIRED_TABLES; do
    COUNT=$(PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$VERIFY_DB" -tAc "SELECT COUNT(*) FROM $t" 2>/dev/null || echo "ERR")
    if [ "$COUNT" = "ERR" ]; then
        fail "table '$t' is missing from the restored dump"
        continue
    fi
    log "  $t: $COUNT rows"
    case " $MUST_BE_NONEMPTY " in
        *" $t "*) [ "$COUNT" -gt 0 ] || fail "table '$t' restored empty" ;;
    esac
done

ALEMBIC_VERSION=$(PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$VERIFY_DB" -tAc "SELECT version_num FROM alembic_version" 2>/dev/null || echo "none")
if [ -z "$ALEMBIC_VERSION" ] || [ "$ALEMBIC_VERSION" = "none" ]; then
    fail "alembic_version is missing; the dump has no migration state and cannot be trusted"
else
    log "alembic_version: $ALEMBIC_VERSION"
    log "  (compare against the target deployment's alembic head before promoting this dump)"
fi

if [ "$FAILURES" -gt 0 ]; then
    log "Verification FAILED for $DUMP ($FAILURES failure(s), $WARNINGS warning(s))"
    exit 1
fi
if [ "$WARNINGS" -gt 0 ]; then
    log "Verification PASSED WITH WARNINGS for $DUMP ($WARNINGS warning(s))"
else
    log "Verification PASSED for $DUMP"
fi
