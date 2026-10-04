# Phase 22 - Backup sidecar image.
#
# Runs the backup/restore/verify shell scripts against a PostgreSQL container
# and writes snapshots to a host-mounted volume. Uses the official PostgreSQL
# image so pg_dump/pg_restore/psql match the server version.
FROM postgres:16-alpine

RUN apk add --no-cache tar gzip coreutils findutils curl

WORKDIR /scripts
COPY infrastructure/backup/ /scripts/

# Entrypoint keeps the container alive (used as a Compose `command`).
CMD ["sh", "-c", "trap 'exit 0' TERM INT; while true; do sleep 3600; done"]
