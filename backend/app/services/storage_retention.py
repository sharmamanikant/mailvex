"""Uploaded-file retention and backup staging (Phase 22, Backup + DR).

Uploaded import source files are sensitive, business-relevant data. They must
not be retained indefinitely, and only the files that the org still needs (an
active/in-flight import job) should be kept. This module owns:

* ``retention_days()``  - a bounded, validated retention window from settings.
* ``sweep_import_files`` - deletes import source files (and error reports) for
  jobs that reached a terminal state older than the retention window. Active
  (uploaded/in-flight/ready) files are never deleted, and recent terminal files
  are preserved until they age past the window.
* ``stage_for_backup`` - copies the current import storage tree into the host
  backup directory so it can be picked up by the backup pipeline without
  coupling the app to a specific object-store implementation.

The scheduler runs ``crcrm.sweep_stale_import_files`` periodically.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import ImportJob

TERMINAL_STATES = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELLED"}


def retention_days() -> int:
    """Return a validated retention window (>=minimum, <=maximum)."""
    candidate = int(settings.import_file_retention_days)
    return max(
        settings.import_file_retention_days_min,
        min(candidate, settings.import_file_retention_days_max),
    )


def _age_limit() -> datetime:
    from datetime import timedelta

    return datetime.now(UTC) - timedelta(days=retention_days())


def _is_active(job: ImportJob) -> bool:
    return job.status not in TERMINAL_STATES


def _job_finished_at(job: ImportJob) -> datetime:
    value = job.finished_at or job.created_at or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def sweep_import_files(session: Session, storage_root: Path | None = None) -> dict[str, int]:
    """Delete import source files/error reports past the retention window.

    Only files whose owning job reached a terminal state older than the
    retention window are removed. Active and recent terminal files are kept so
    an in-flight import is never broken and completed ones remain downloadable
    for the retention period.
    """
    root = (storage_root or Path(settings.storage_root)).expanduser().resolve()
    import_root = root / "imports"
    if not import_root.exists():
        return {"deleted_files": 0, "deleted_bytes": 0, "kept_active": 0, "kept_recent": 0}

    limit = _age_limit()
    removed = 0
    removed_bytes = 0
    kept_active = 0
    kept_recent = 0

    jobs = session.scalars(
        select(ImportJob).where(ImportJob.source_file_ref.is_not(None))
    ).all()
    referenced: dict[str, ImportJob] = {}
    for jb in jobs:
        if jb.source_file_ref and Path(jb.source_file_ref).exists():
            referenced[jb.source_file_ref] = jb
        if jb.error_report_ref and Path(jb.error_report_ref).exists():
            referenced[jb.error_report_ref] = jb

    for path in sorted(import_root.rglob("*")):
        if not path.is_file():
            continue
        key = str(path.resolve())
        job = referenced.get(key)
        if job is not None and _is_active(job):
            kept_active += 1
            continue
        if job is not None and _job_finished_at(job) >= limit:
            kept_recent += 1
            continue
        try:
            removed_bytes += path.stat().st_size
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue

    _prune_empty_dirs(import_root)
    return {
        "deleted_files": removed,
        "deleted_bytes": removed_bytes,
        "kept_active": kept_active,
        "kept_recent": kept_recent,
    }


def _prune_empty_dirs(root: Path) -> None:
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def stage_for_backup(session: Session, backup_dir: Path | None = None) -> dict[str, str | int]:
    """Copy the current import storage tree into a timestamped backup staging dir.

    Uploaded files are captured so they ride along with database backups and
    can be restored in a disaster without relying on the live volume's state.
    """
    root = (Path(settings.storage_root)).expanduser().resolve()
    import_root = root / "imports"
    target_root = (backup_dir or Path(settings.backup_dir)).expanduser().resolve() / "filestore"
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    target = target_root / stamp
    target.mkdir(parents=True, exist_ok=True)

    copied = 0
    copied_bytes = 0
    if import_root.exists():
        for path in import_root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(import_root)
            dest = target / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            copied += 1
            copied_bytes += path.stat().st_size
    return {"staged_dirs": str(target), "files": copied, "bytes": copied_bytes}
