# Backup & Disaster Recovery (Phase 22)

This document specifies the production backup strategy, retention policy, and
disaster-recovery (DR) procedures for the **CR+CRM** platform.

> **A backup is not considered valid until restoration has been tested.** The
> restore runbook below is exercised regularly; results are recorded in
> [restore-test-results.md](restore-test-results.md).

---

## 1. What is backed up

| Asset                                    | Scope                                            | Mechanism                                                        |
| ---------------------------------------- | ------------------------------------------------ | ---------------------------------------------------------------- |
| **PostgreSQL**                           | All tables: tenants, users, contacts, campaigns, templates, scheduled/delivery jobs, suppression, compliance, audit records, import job metadata | Logical `pg_dump` (plain, gzip) via `infrastructure/backup/pg_backup.sh` |
| **Uploaded import files**                | Raw contact-import source files + error reports  | Staging snapshot of the storage root (`STORAGE_ROOT`) into the backup dir |
| **Application configuration**            | `.env` / secrets-referencing config file         | Copy of `CONFIG_FILE` into the backup dir                        |
| **File storage (templates/attachments)** | Template body/config lives in PostgreSQL         | Covered by the PostgreSQL dump (stored in DB)                    |
| **Redis**                                | **Not backed up** (see below)                    | Only best-effort `SAVE` of durable point-in-time files, for debugging |

### Why Redis is not part of the restore contract

Redis holds **ephemeral** data only:

- Celery **broker** (queued tasks) — rebuilt by the scheduler from the durable
  database-backed schedule (`scheduled_messages`, `delivery_jobs`).
- Celery **result backend** — transient task results with short TTLs.
- **Metrics/ops samples** — short-TTL counters, rebuilt on demand.
- **Rate-limit / heartbeat** keys — transient.

Backing up this data would waste storage for state that offers no recovery
value. It is **deliberately excluded** from the restore procedure. After a
restore, scheduled work is re-dispatched by the scheduler from the database.

---

## 2. Backup objectives (RPO / RTO)

| Target      | Value | Rationale                                                        |
| ----------- | ----- | ---------------------------------------------------------------- |
| **RPO**     | ≤ 1 h | Database dumps are taken every hour (default 3600 s); you can lose at most one hour of changes. |
| **RTO**     | ≤ 2 h | Provision + restore + verify + bring the stack back online from the latest dump. |

These are **targets**, not guarantees. Actual RTO depends on dump size, network,
and infrastructure speed. Validate them during the periodic restore test.

---

## 3. Backup frequency

| Asset              | Frequency (default) | Env var                    |
| ------------------ | ------------------- | -------------------------- |
| PostgreSQL dump    | every 60 min        | `BACKUP_INTERVAL_SECONDS`  |
| Uploaded files     | with each run       | (same run)                 |
| Application config | with each run       | (same run)                 |

The production Docker sidecar runs the pipeline on a loop. For host cron:
`0 * * * *  sh /opt/crcrm/infrastructure/backup/backup.sh`.

---

## 4. Retention

| Asset              | Retention (days) | Env var                |
| ------------------ | ---------------- | ---------------------- |
| PostgreSQL dumps   | 14               | `DB_RETENTION_DAYS`    |
| Uploaded file store| 30               | `FILES_RETENTION_DAYS` |
| App config         | 30               | `CONFIG_RETENTION_DAYS`|

Retired by age; this keeps full-disk growth bounded while preserving enough
history for point-in-time recovery within the RPO window.

### Uploaded file retention (in-application)

Separately, the application enforces a retention policy on uploaded contact
import files so they are **never retained indefinitely**:

- Config: `IMPORT_FILE_RETENTION_DAYS` (default **30**, clamped to 1–365).
- The hourly scheduler task `crcrm.sweep_stale_import_files` deletes source
  files and error reports whose owning `ImportJob` reached a **terminal** state
  (`COMPLETED`, `COMPLETED_WITH_ERRORS`, `FAILED`, `CANCELLED`) and has aged
  past the window.
- **Active** (uploaded / validating / ready / importing) files are **never**
  deleted, and **recent** terminal files are preserved for the full window.
- Storage root is configurable via `STORAGE_ROOT` (default `.local_uploads`).

---

## 5. Backup verification

Every backup run is verified *as it is created*:

1. `pg_dump` produces a gzip archive.
2. The archive is restored into an **isolated scratch database**
   (`crcrm_verify_<ts>`), which is dropped immediately afterward.
3. If the restore errors, the dump is rejected and the run fails loudly.

A separate `verify_backup.sh` can re-verify any archived dump on demand:

```sh
docker compose --env-file .env.production exec backup sh /scripts/verify_backup.sh /backups/crcrm-db-<stamp>.sql.gz
```

---

## 6. Restore procedure (runbook)

The recommended flow for a database failure:

```
Database failure
   ↓
Provision PostgreSQL (start a clean postgres service)
   ↓
Restore the latest backup dump into a fresh database
   ↓
Run migrations/checks (alembic current == head)
   ↓
Validate data (row counts on key tables, alembic_version)
   ↓
Restore uploaded file store from the staged snapshot
   ↓
Start the application stack
   ↓
Verify readiness (health endpoint + spot-check query)
```

### 6.1 Restore the database

```sh
# From a host with the backup dir mounted:
docker compose --env-file .env.production exec backup sh /scripts/restore.sh /backups/crcrm-db-<stamp>.sql.gz
```

`restore.sh` will re-create `PGDATABASE`, load the dump, and report migration
state plus row counts for integrity validation.

### 6.2 Bring the stack back online

```sh
docker compose --env-file .env.production up -d \
  postgres redis backend worker scheduler frontend nginx
```

### 6.3 Verify readiness

```sh
curl -fsS http://127.0.0.1:8000/health          # expect 200/OK
docker compose --env-file .env.production ps                                 # all services healthy
PGPASSWORD="$PGPASSWORD" psql -d crcrm -tAc 'SELECT count(*) FROM contacts'
```

A successful health check plus a spot-check query confirms the application is
serving from the restored database.

### 6.4 Restore uploaded files (if not already on the recovered volume)

```sh
docker compose --env-file .env.production exec backup sh /scripts/restore.sh   # DB only
# or point STORAGE_RESTORE at the filestore staging dir and run the
# full restore.sh which unpacks any *.tar.gz into STORAGE_DEST.
```

---

## 7. Responsibilities (RACI)

| Activity                          | Owner                                  |
| --------------------------------- | -------------------------------------- |
| Backup configuration & automation | Platform / DevOps engineer             |
| Running the backup pipeline       | Automated (Docker sidecar or cron)     |
| Backup verification               | Platform / DevOps engineer             |
| Periodic restore test (RPO/RTO)   | Platform / DevOps engineer (quarterly) |
| Monitoring backup/alert failures  | On-call engineer                       |
| Executing a declared DR event     | Platform / DevOps + on-call engineer   |
| Declaring a disaster              | Engineering lead / incident commander  |

---

## 8. Failure monitoring & escalation

- The backup container writes logs to `docker compose logs backup`; a failed run
  exits non-zero and surfaces in the container's restart logs.
- Recommended: alert on `backup` container health / stale dump age (e.g. via
  the ops metrics + alert pipeline added in Phase 21).

---

## 9. Off-site replication

Backups are written to the `backups_data` Docker volume, mounted as `/backups`
inside the `backup` container. A named volume is used rather than a host bind
mount because the container runs as root and a bind mount would leave
root-owned artifacts on the host that the deploy user cannot manage.

List and retrieve backups from the host with:

```bash
docker compose --env-file .env.production exec backup ls -lh /backups
docker compose --env-file .env.production cp backup:/backups/crcrm-db-<stamp>.sql.gz ./
```

**For DR against site loss, copy these artifacts off-site** (object storage /
second region) outside the scope of this repo, on the same frequency as the
dumps.

---

## 10. Files

| File                                              | Purpose                            |
| ------------------------------------------------- | ---------------------------------- |
| `infrastructure/backup/backup.sh`                 | Orchestrates DB + files + config   |
| `infrastructure/backup/pg_backup.sh`              | PostgreSQL dump + verify + rotate  |
| `infrastructure/backup/verify_backup.sh`          | On-demand backup verification      |
| `infrastructure/backup/restore.sh`                | Full database/filestore restore    |
| `infrastructure/backup/Dockerfile`                | Backup sidecar image               |
| `infrastructure/backup/backup.env.example`        | Backup configuration template      |
| `docker-compose.yml`                         | Adds the `backup` sidecar service  |
| `backend/app/services/storage_retention.py`       | In-app uploaded-file retention     |
| `backend/app/tasks/scheduler.py`                  | Registers the retention sweep task |
| `docs/operations/restore-test-results.md`         | Restore test log                   |
