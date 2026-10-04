from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, ImportJob, Tenant
from app.services import storage_retention


@pytest.fixture()
def retention_env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()

    tenant = Tenant(name="Retention Tenant", slug=f"ret-{uuid4().hex[:8]}")
    session.add(tenant)
    session.commit()

    root = tmp_path / "storage"
    object.__setattr__(storage_retention.settings, "storage_root", str(root))

    yield {"session": session, "tenant": tenant, "root": root, "Session": Session}
    session.close()


@pytest.fixture(autouse=True)
def _reset_retention():
    object.__setattr__(storage_retention.settings, "import_file_retention_days", 30)
    yield


def _make_job(session, tenant, status, finished_at, source_path, report_path=None) -> ImportJob:
    job = ImportJob(
        tenant_id=tenant.id,
        filename="contacts.csv",
        file_type="csv",
        status=status,
        source_file_ref=str(source_path),
        error_report_ref=str(report_path) if report_path else None,
        finished_at=finished_at,
    )
    session.add(job)
    session.commit()
    return job


def _write(path: Path, data: str = "a,b,c\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return path


def test_retention_days_clamped_default():
    object.__setattr__(storage_retention.settings, "import_file_retention_days", 30)
    assert storage_retention.retention_days() == 30


def test_retention_days_clamped_below_min():
    object.__setattr__(storage_retention.settings, "import_file_retention_days", -5)
    assert storage_retention.retention_days() >= storage_retention.settings.import_file_retention_days_min


def test_retention_days_clamped_above_max():
    object.__setattr__(storage_retention.settings, "import_file_retention_days", 99999)
    assert storage_retention.retention_days() <= storage_retention.settings.import_file_retention_days_max


def test_sweep_deletes_stale_terminal_files(retention_env):
    session = retention_env["session"]
    tenant = retention_env["tenant"]
    root = retention_env["root"]

    old = datetime.now(UTC) - timedelta(days=60)
    stale_source = _write(root / "imports" / "t1" / "stale.csv")
    _make_job(session, tenant, "COMPLETED", old, stale_source)

    result = storage_retention.sweep_import_files(session, root)
    assert result["deleted_files"] == 1
    assert not stale_source.exists()


def test_sweep_keeps_active_files(retention_env):
    session = retention_env["session"]
    tenant = retention_env["tenant"]
    root = retention_env["root"]

    active_source = _write(root / "imports" / "t2" / "active.csv")
    _make_job(session, tenant, "IMPORTING", None, active_source)

    result = storage_retention.sweep_import_files(session, root)
    assert result["kept_active"] == 1
    assert active_source.exists()


def test_sweep_keeps_recent_terminal_files(retention_env):
    session = retention_env["session"]
    tenant = retention_env["tenant"]
    root = retention_env["root"]

    recent = datetime.now(UTC)
    recent_source = _write(root / "imports" / "t3" / "recent.csv")
    _make_job(session, tenant, "COMPLETED", recent, recent_source)

    result = storage_retention.sweep_import_files(session, root)
    assert result["kept_recent"] == 1
    assert recent_source.exists()


def test_sweep_unreferenced_stale_files_deleted(retention_env):
    session = retention_env["session"]
    tenant = retention_env["tenant"]
    root = retention_env["root"]

    old = datetime.now(UTC) - timedelta(days=60)
    orphan = _write(root / "imports" / "t4" / "orphan.csv")
    referenced = _write(root / "imports" / "t4" / "ref.csv")
    _make_job(session, tenant, "FAILED", old, referenced)

    result = storage_retention.sweep_import_files(session, root)
    assert result["deleted_files"] == 2
    assert not orphan.exists()
    assert not referenced.exists()


def test_sweep_deletes_stale_error_report(retention_env):
    session = retention_env["session"]
    tenant = retention_env["tenant"]
    root = retention_env["root"]

    old = datetime.now(UTC) - timedelta(days=60)
    source = _write(root / "imports" / "t5" / "src.csv")
    report = _write(root / "imports" / "t5" / "src.csv.report.json", '{"errors": []}')
    _make_job(session, tenant, "COMPLETED_WITH_ERRORS", old, source, report)

    result = storage_retention.sweep_import_files(session, root)
    assert result["deleted_files"] == 2
    assert not source.exists()
    assert not report.exists()


def test_sweep_with_empty_root(retention_env):
    session = retention_env["session"]
    result = storage_retention.sweep_import_files(session, retention_env["root"])
    assert result["deleted_files"] == 0


def test_stage_for_backup_copies_tree(retention_env, tmp_path):
    session = retention_env["session"]
    root = retention_env["root"]

    _write(root / "imports" / "t6" / "a.csv", "x,y\n")
    _write(root / "imports" / "t6" / "nested" / "b.json", "{}")

    backup_dir = tmp_path / "backups"
    result = storage_retention.stage_for_backup(session, backup_dir)
    assert result["files"] == 2
    assert result["bytes"] > 0
    staged = list(backup_dir.rglob("*"))
    assert any(p.name == "a.csv" for p in staged)
    assert any(p.name == "b.json" for p in staged)
