# Restore Test Results (Phase 22)

This log records real restore tests performed on the backup pipeline. A backup
is not considered valid until restoration has been tested end-to-end in an
isolated environment.

---

## Test 1 — 2026-09-01: Full database + filestore restore (isolated Docker)

### Environment

| Item        | Value                                                     |
| ----------- | --------------------------------------------------------- |
| Orchestrator| Docker Engine 29.7.2 (isolated bridge network `phase22net`)|
| PostgreSQL  | `postgres:16-alpine`                                      |
| Backup tool | `infrastructure/backup/*.sh` run inside a `postgres:16-alpine` client |
| Data        | Representative production-like schema + seed data         |
| Isolation   | Dedicated source + client containers, then destroyed after test |

### Procedure executed

1. Seeded a source database with 8 tables (tenants, users, contacts, templates,
   campaigns, import_jobs, audit_logs, alembic_version) and realistic rows.
2. Ran `pg_backup.sh` → produced `crcrm-db-20260901050416.sql.gz` (1.7 KB) and
   the **inline isolated-restore integrity check passed**.
3. Ran `verify_backup.sh` on the dump → **restore succeeded**, key tables counted,
   `alembic_version=20260902_27`, **PASS**.
4. Simulated disaster: `DROP DATABASE crcrm`.
5. Ran `restore.sh` → recreated the database, loaded the dump.
6. Validated data recovered (row counts + full contact rows + alembic version).

### Database restore validation

Pre-disaster vs post-restore:

| Table           | Pre-test | Post-restore |
| --------------- | -------- | ------------ |
| contacts        | 4        | 4            |
| templates       | 1        | 1            |
| users           | 1        | 1            |
| campaigns       | 1        | 1            |
| import_jobs     | 1        | 1            |
| audit_logs      | 1        | 1            |
| alembic_version | 20260902_27 | 20260902_27 |

Restored contact emails (ordered): `alice@example.com`, `bob@example.com`,
`carol@example.com`, `dave@example.com` — all present and correct.

**Result: PASS**

### Filestore backup & restore validation

1. Created uploaded import files (`leads.csv`, `leads.csv.report.json`) under a
   fake storage root.
2. Ran `backup.sh` → PostgreSQL dump verified, uploaded files staged to
   `filestore/<ts>/uploads.tar.gz`, config captured to `config/<ts>/`.
3. Deleted the uploaded files (simulated storage loss).
4. Ran `restore.sh` with `STORAGE_RESTORE` set → **uploads restored** to
   `/app/.local_uploads`; file contents verified byte-identical.

**Result: PASS**

### Metrics observed

| Metric                       | Value                                                       |
| ---------------------------- | ----------------------------------------------------------- |
| Backup run (DB + verify)     | ~1 s (small dataset; real times scale with DB size)         |
| Full restore (DB + filestore)| ~1 s (small dataset; real times scale with DB size)         |
| RTO achieved (this test)     | well under the 2 h target                                   |

---

## Notes / known constraints

- The simplified test schema omitted a few app tables (`role_permissions`,
  `scheduled_messages`, `delivery_jobs`), so `verify_backup.sh` reported
  `ERR` (table not found) for those rows. This is a **test-fixture artifact**,
  not a pipeline defect; the restore itself succeeded and key tables validated.
  The `alembic upgrade head` step is expected to run inside the app container
  (not the bare DB client used here) and was reported as skipped accordingly.
- Restore of the uploaded file store was validated with `--strip-components=1`
  landing paths back under `/app/.local_uploads`.

---

## Sign-off

- [x] Backup pipeline produces a verified dump (inline integrity check).
- [x] Standalone `verify_backup.sh` passes.
- [x] Database restore recovers all seeded rows and schema.
- [x] Filestore backup + restore round-trip verified.
- [x] Config capture verified.
- [x] Redis intentionally excluded from restore contract (ephemeral) — see
      `docs/operations/backup-disaster-recovery.md`.
