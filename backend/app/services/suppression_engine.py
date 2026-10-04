from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Contact, SuppressionEntry
from app.services.audit import AuditService
from app.utils.emails import normalize_email

# Provider-derived types may never be silently removed by operators.
PROTECTED_TYPES = frozenset({"COMPLAINT", "HARD_BOUNCE", "UNSUBSCRIBED"})

SUPPRESSION_TYPES = frozenset(
    {"UNSUBSCRIBED", "HARD_BOUNCE", "COMPLAINT", "MANUAL", "ADMIN_BLOCKED"}
)


class SuppressionEngineError(ValueError):
    pass


class SuppressionEngine:
    """Tenant-wide suppression safety control (Phase 14).

    This is the single authoritative 'is this recipient suppressed?' check that
    every send gate consults. Provider events and operator actions both write
    here. Provider-derived entries (COMPLAINT, HARD_BOUNCE, UNSUBSCRIBED) are
    protected and cannot be removed through the public API.
    """

    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    @staticmethod
    def _norm(email: str) -> str:
        return normalize_email(email)

    def get(self, suppression_id: UUID) -> SuppressionEntry | None:
        return self.session.scalar(
            select(SuppressionEntry).where(
                SuppressionEntry.id == suppression_id,
                SuppressionEntry.tenant_id == self.tenant_id,
            )
        )

    def is_suppressed(self, email: str) -> bool:
        """Global gate: True if the recipient is actively suppressed."""
        normalized = self._norm(email)
        return (
            self.session.scalar(
                select(SuppressionEntry.id).where(
                    SuppressionEntry.tenant_id == self.tenant_id,
                    SuppressionEntry.email_normalized == normalized,
                    SuppressionEntry.active.is_(True),
                )
            )
            is not None
        )

    def list_entries(
        self,
        search: str | None = None,
        entry_type: str | None = None,
        include_inactive: bool = False,
    ) -> list[SuppressionEntry]:
        statement = select(SuppressionEntry).where(
            SuppressionEntry.tenant_id == self.tenant_id
        )
        if not include_inactive:
            statement = statement.where(SuppressionEntry.active.is_(True))
        if search and search.strip():
            statement = statement.where(
                SuppressionEntry.email_normalized.ilike(f"%{search.strip().lower()}%")
                | SuppressionEntry.reason.ilike(f"%{search.strip()}%")
            )
        if entry_type:
            statement = statement.where(SuppressionEntry.type == entry_type)
        return [
            *self.session.scalars(
                statement.order_by(SuppressionEntry.created_at.desc())
            ).all()
        ]

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def suppress(
        self,
        email: str,
        entry_type: str,
        source: str,
        reason: str | None = None,
        provider: str | None = None,
        campaign_id: UUID | None = None,
        contact_id: UUID | None = None,
        *,
        audit: bool = True,
    ) -> SuppressionEntry:
        if entry_type not in SUPPRESSION_TYPES:
            raise SuppressionEngineError("Invalid suppression type")
        normalized = self._norm(email)
        existing = self.session.scalar(
            select(SuppressionEntry).where(
                SuppressionEntry.tenant_id == self.tenant_id,
                SuppressionEntry.email_normalized == normalized,
            )
        )
        if existing is not None:
            existing.active = True
            existing.type = entry_type
            existing.source = source
            existing.reason = reason or existing.reason
            existing.provider = provider or existing.provider
            existing.campaign_id = campaign_id or existing.campaign_id
            existing.updated_at = datetime.now(UTC)
            entry = existing
        else:
            entry = SuppressionEntry(
                tenant_id=self.tenant_id,
                email_normalized=normalized,
                type=entry_type,
                source=source,
                reason=reason,
                provider=provider,
                campaign_id=campaign_id,
                contact_id=contact_id,
                active=True,
            )
            self.session.add(entry)
        if audit:
            AuditService(self.session, self.tenant_id, self.actor_id).record(
                entry_type if entry_type in {"UNSUBSCRIBED", "HARD_BOUNCE", "COMPLAINT"}
                else "SUPPRESSION_CREATED",
                "suppression",
                entry.id,
                {
                    "email": normalized,
                    "type": entry_type,
                    "source": source,
                    "provider": provider,
                },
            )
        self.session.flush()
        return entry

    def remove(
        self,
        suppression_id: UUID,
        *,
        force: bool = False,
    ) -> None:
        entry = self.get(suppression_id)
        if entry is None:
            raise SuppressionEngineError("Suppression record not found")
        if not force and entry.type in PROTECTED_TYPES:
            raise SuppressionEngineError(
                "Provider-derived suppressions cannot be removed"
            )
        email = entry.email_normalized
        entry.active = False
        entry.updated_at = datetime.now(UTC)
        # Reflect removal on any linked contact suppression marker.
        contact = None
        if entry.contact_id is not None:
            contact = self.session.get(Contact, entry.contact_id)
        elif contact is None:
            contact = self.session.scalar(
                select(Contact).where(
                    Contact.tenant_id == self.tenant_id,
                    Contact.email == email,
                )
            )
        if contact is not None and not self.is_suppressed(email):
            contact.suppression_status = "CLEAR"
        AuditService(self.session, self.tenant_id, self.actor_id).record(
            "SUPPRESSION_REMOVED",
            "suppression",
            entry.id,
            {"email": email, "type": entry.type, "force": force},
        )
        self.session.flush()

    def list_types(self) -> list[str]:
        return sorted(SUPPRESSION_TYPES)
