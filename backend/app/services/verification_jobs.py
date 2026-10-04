"""Bulk verification jobs: creation, chunked processing, and progress counters.

Scale contract
--------------
Contacts are never loaded wholesale. A run pages through the tenant with a
**keyset cursor** (``created_at, id``) in fixed-size chunks, so memory is
O(chunk_size) whether the tenant holds 400 contacts or 1,000,000. Counters are
denormalised onto ``verification_jobs`` so the CRM UI polls one small row
instead of counting a million contacts per request.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import settings
from app.models import Contact, ContactListMember, VerificationJob
from app.services.verification import (
    VERIFICATION_DUPLICATE,
    VERIFICATION_INVALID,
    VERIFICATION_LIKELY_VALID,
    VERIFICATION_NEEDS_REVIEW,
    VERIFICATION_RISKY,
    VERIFICATION_UNKNOWN,
    VERIFICATION_VERIFIED,
    ContactVerdict,
)

logger = logging.getLogger(__name__)

JOB_QUEUED = "QUEUED"
JOB_RUNNING = "RUNNING"
JOB_COMPLETED = "COMPLETED"
JOB_FAILED = "FAILED"
JOB_CANCELLED = "CANCELLED"

SCOPE_SELECTION = "SELECTION"
SCOPE_TENANT = "TENANT"

MAX_EXPLICIT_IDS = 5000

_COUNT_FIELDS: dict[str, str] = {
    VERIFICATION_VERIFIED: "valid_count",
    VERIFICATION_LIKELY_VALID: "valid_count",
    VERIFICATION_NEEDS_REVIEW: "needs_review_count",
    VERIFICATION_RISKY: "risky_count",
    VERIFICATION_INVALID: "invalid_count",
    VERIFICATION_DUPLICATE: "duplicate_count",
    VERIFICATION_UNKNOWN: "unknown_count",
}


class VerificationJobError(RuntimeError):
    pass


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    # Round-tripping through SQLite yields a naive datetime, and a naive bound
    # against a TIMESTAMPTZ column is interpreted in the server's local zone,
    # which would silently shift the window. Assume UTC, which is how every
    # timestamp in this codebase is written.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


class VerificationJobService:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def create(
        self,
        actor_id: UUID | None,
        contact_ids: list[UUID] | None = None,
        probe_smtp: bool = False,
        filters: dict[str, Any] | None = None,
    ) -> VerificationJob:
        if contact_ids is not None and len(contact_ids) > MAX_EXPLICIT_IDS:
            raise VerificationJobError(
                f"At most {MAX_EXPLICIT_IDS} contacts can be queued in one explicit selection"
            )
        payload: dict[str, Any] = {"probe_smtp": probe_smtp}
        if contact_ids is not None:
            payload["contact_ids"] = [str(item) for item in contact_ids]
            payload["processed_ids"] = 0
            scope = SCOPE_SELECTION
            total = self._count_selection(contact_ids)
        else:
            payload["filters"] = filters or {}
            scope = SCOPE_TENANT
            total = self._count_conditions(self._conditions(filters or {}))
        job = VerificationJob(
            tenant_id=self._tenant_id,
            created_by_id=actor_id,
            status=JOB_QUEUED,
            scope=scope,
            total_count=total,
            request_payload=payload,
        )
        self._session.add(job)
        self._session.commit()
        return job

    def claim(self, job_id: UUID) -> VerificationJob | None:
        job = self._session.get(VerificationJob, job_id)
        if job is None or job.tenant_id != self._tenant_id:
            return None
        if job.status in (JOB_COMPLETED, JOB_FAILED, JOB_CANCELLED):
            return None
        if job.status == JOB_QUEUED:
            job.status = JOB_RUNNING
            job.started_at = datetime.now(UTC)
            self._session.commit()
        return job

    def next_batch(self, job: VerificationJob) -> list[Contact]:
        payload = dict(job.request_payload or {})
        explicit = payload.get("contact_ids")
        limit = settings.verification_chunk_size
        if explicit is not None:
            done = int(payload.get("processed_ids") or 0)
            window = explicit[done : done + limit]
            payload["processed_ids"] = done + len(window)
            job.request_payload = payload
            if not window:
                return []
            return list(
                self._session.scalars(
                    select(Contact).where(
                        Contact.tenant_id == self._tenant_id,
                        Contact.id.in_([UUID(item) for item in window]),
                    )
                ).all()
            )

        cursor = payload.get("cursor")
        statement = select(Contact).where(
            Contact.tenant_id == self._tenant_id, *self._conditions(payload.get("filters") or {})
        )
        if cursor:
            statement = statement.where(Contact.id > UUID(str(cursor)))
        # Keyset on the primary key: ``id`` is a total order, so every row is
        # visited exactly once across chunks and a resumed run can never skip or
        # repeat one. A (created_at, id) composite would additionally depend on
        # timestamp precision matching between the server default and the bound
        # value, which differs per dialect.
        statement = statement.order_by(Contact.id).limit(limit)
        batch = list(self._session.scalars(statement).all())
        if batch:
            payload["cursor"] = str(batch[-1].id)
            job.request_payload = payload
        return batch

    def has_more(self, job: VerificationJob) -> bool:
        payload = dict(job.request_payload or {})
        explicit = payload.get("contact_ids")
        if explicit is not None:
            return int(payload.get("processed_ids") or 0) < len(explicit)
        return bool(payload.get("cursor"))

    def record(self, job: VerificationJob, verdicts: list[ContactVerdict], failed: int = 0) -> None:
        deltas: dict[str, int] = {field: 0 for field in _COUNT_FIELDS.values()}
        for verdict in verdicts:
            field = _COUNT_FIELDS.get(verdict.verification_status)
            if field is not None:
                deltas[field] += 1
        for field, delta in deltas.items():
            setattr(job, field, (getattr(job, field) or 0) + delta)
        job.processed_count = (job.processed_count or 0) + len(verdicts) + failed
        job.failed_count = (job.failed_count or 0) + failed
        self._session.commit()

    def is_cancelled(self, job: VerificationJob) -> bool:
        self._session.refresh(job)
        return job.status == JOB_CANCELLED

    def finish(self, job: VerificationJob, error: str | None = None) -> None:
        job.status = JOB_FAILED if error else JOB_COMPLETED
        job.error_message = error
        job.finished_at = datetime.now(UTC)
        self._session.commit()

    def cancel(self, job_id: UUID) -> VerificationJob | None:
        job = self._session.get(VerificationJob, job_id)
        if job is None or job.tenant_id != self._tenant_id:
            return None
        job.status = JOB_CANCELLED
        job.finished_at = datetime.now(UTC)
        self._session.commit()
        return job

    def _conditions(self, filters: dict[str, Any]) -> list[ColumnElement[bool]]:
        clauses: list[ColumnElement[bool]] = []
        source = filters.get("source")
        if source:
            clauses.append(Contact.source == source)
        status = filters.get("status")
        if status:
            clauses.append(Contact.status == status)
        list_id = filters.get("list_id")
        if list_id:
            clauses.append(
                Contact.id.in_(
                    select(ContactListMember.contact_id).where(
                        ContactListMember.tenant_id == self._tenant_id,
                        ContactListMember.contact_list_id == UUID(str(list_id)),
                    )
                )
            )
        created_after = filters.get("created_after")
        if created_after:
            clauses.append(Contact.created_at >= _parse_timestamp(created_after))
        return clauses

    def _count_conditions(self, clauses: list[ColumnElement[bool]]) -> int:
        statement = select(func.count(Contact.id)).where(
            Contact.tenant_id == self._tenant_id, *clauses
        )
        return int(self._session.scalar(statement) or 0)

    def _count_selection(self, contact_ids: list[UUID]) -> int:
        if not contact_ids:
            return 0
        statement = select(func.count(Contact.id)).where(
            Contact.tenant_id == self._tenant_id, Contact.id.in_(contact_ids)
        )
        return int(self._session.scalar(statement) or 0)
