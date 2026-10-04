#!/usr/bin/env sh
# Phase 22 - Orchestrated backup: PostgreSQL + uploaded file store + config.
#
# What is backed up                       How
# --------------------                    ------------------------------
# PostgreSQL (contacts, campaigns,        logical pg_dump via pg_backup.sh
#   templates, audit records, ...)
# Uploaded import files                   rsync of the app storage root
#                                          (filestore) into BACKUP_DIR
# Application configuration               copy of a supplied env file / config
#                                          directory into BACKUP_DIR
#
# Redis is intentionally NOT fully backed up: it holds ephemeral queues,
# metrics, and broker state that is rebuilt on demand. Only its durable
# AOF/point-in-time files are captured (small, cheap) for debugging; they are
# not part of the restore contract.
#
# Retention:
#   * DB dumps   - DB_RETENTION_DAYS (default 14)
#   * filestore  - FILES_RETENTION_DAYS (default 30)
#   * config     - CONFIG_RETENTION_DAYS (default 30)
#
# A backup is only considered valid once restore has been tested; see
# verify_backup.sh and the documented restore procedure in
# docs/operations/backup-disaster-recovery.md.
set -eu

: "${BACKUP_DIR:=/backups}"
: "${PG_BACKUP_SCRIPT:=/scripts/pg_backup.sh}"
: "${STORAGE_ROOT:=/app/.local_uploads}"
: "${CONFIG_FILE:=}"
: "${CONFIG_DIR:=$CONFIG_FILE}"
: "${DB_RETENTION_DAYS:=14}"
: "${FILES_RETENTION_DAYS:=30}"
: "${CONFIG_RETENTION_DAYS:=30}"

BACKUP_DIR="$(echo "$BACKUP_DIR" | sed 's:/*$::')"
mkdir -p "$BACKUP_DIR"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"; }

log "Backing up PostgreSQL"
sh "$PG_BACKUP_SCRIPT"

STAMP="$(date -u +%Y%m%d%H%M%S)"

# --- Uploaded file store ------------------------------------------------ #
TS_DIR="$BACKUP_DIR/filestore/$STAMP"
if [ -d "$STORAGE_ROOT" ] && [ -n "$(find "$STORAGE_ROOT" -mindepth 1 -print -quit 2>/dev/null)" ]; then
    mkdir -p "$TS_DIR"
    tar -czf "$TS_DIR/uploads.tar.gz" -C "$(dirname "$STORAGE_ROOT")" "$(basename "$STORAGE_ROOT")"
    log "Uploaded files staged to $TS_DIR/uploads.tar.gz"
else
    log "No uploaded files present; skipping filestore snapshot"
fi
find "$BACKUP_DIR/filestore" -maxdepth 2 -type d -mtime +"$FILES_RETENTION_DAYS" -exec rm -rf {} + 2>/dev/null || true

# --- Application configuration ------------------------------------------ #
if [ -n "$CONFIG_FILE" ] && [ -f "$CONFIG_FILE" ]; then
    CONFIG_DIR="$BACKUP_DIR/config/$STAMP"
    mkdir -p "$CONFIG_DIR"
    cp "$CONFIG_FILE" "$CONFIG_DIR/"
    log "Configuration captured from $CONFIG_FILE"
fi
find "$BACKUP_DIR/config" -maxdepth 2 -type d -mtime +"$CONFIG_RETENTION_DAYS" -exec rm -rf {} + 2>/dev/null || true

# --- Redis (ephemeral; best-effort point-in-time only) ------------------ #
if command -v redis-cli >/dev/null 2>&1; then
    redis-cli -h "${REDIS_HOST:-redis}" SAVE >/dev/null 2>&1 || log "WARN: could not trigger Redis SAVE (ephemeral data is not part of restore contract)"
fi

log "Backup run complete: $BACKUP_DIR"
