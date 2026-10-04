"""Configurable compliance layer (guardrail 18).

Jurisdiction-specific legal requirements are configuration, not sending-engine
logic. ``ComplianceProfileService`` exposes the effective profile for a tenant,
seeding a STANDARD default on first access. The numeric safety thresholds here
are operational guardrails (guardrail 12); they are configurable and are never
presented as universally safe legal limits.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ComplianceProfile
from app.services.audit import AuditService

DEFAULT_JURISDICTION = "UNSPECIFIED"

# Configurable safety thresholds (guardrails 12, 22). Values are rates over a
# rolling window of at least ``min_sample_size`` sends. They are operational
# defaults that a qualified compliance review can change per tenant.
DEFAULT_SAFETY_THRESHOLDS: dict[str, Any] = {
    "hard_bounce_rate": 0.05,       # >=5% of window sends hard bounce
    "complaint_rate": 0.001,        # >=0.1% of window sends filed complaints
    "provider_error_rate": 0.10,    # >=10% of window sends failed with provider errors
    "window_days": 7,               # rolling evaluation window
    "min_sample_size": 30,          # ignore rates until at least this many sends
}

DEFAULT_RETENTION_POLICY: dict[str, Any] = {
    "message_events_days": 365,
    "delivery_events_days": 365,
    "audit_logs_days": 730,
    "contacts_policy": "retain",  # retain | delete_on_request
}


class ComplianceProfileError(ValueError):
    pass


class ComplianceProfileService:
    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    def get(self) -> ComplianceProfile:
        profile = self.session.scalar(
            select(ComplianceProfile).where(
                ComplianceProfile.tenant_id == self.tenant_id
            )
        )
        if profile is not None:
            return profile
        profile = ComplianceProfile(
            tenant_id=self.tenant_id,
            compliance_profile="STANDARD",
            jurisdiction=DEFAULT_JURISDICTION,
            retention_policy=dict(DEFAULT_RETENTION_POLICY),
            safety_thresholds=dict(DEFAULT_SAFETY_THRESHOLDS),
        )
        self.session.add(profile)
        self.session.flush()
        return profile

    def update(
        self,
        *,
        compliance_profile: str | None = None,
        jurisdiction: str | None = None,
        require_unsubscribe: bool | None = None,
        require_sender_identity: bool | None = None,
        require_policy_acceptance: bool | None = None,
        require_consent_metadata: bool | None = None,
        require_list_unsubscribe_header: bool | None = None,
        retention_policy: dict[str, Any] | None = None,
        safety_thresholds: dict[str, Any] | None = None,
        actor_id: UUID | None = None,
    ) -> ComplianceProfile:
        profile = self.get()
        actor = actor_id or self.actor_id
        previous: dict[str, Any] = {
            "compliance_profile": profile.compliance_profile,
            "jurisdiction": profile.jurisdiction,
            "require_unsubscribe": profile.require_unsubscribe,
            "require_sender_identity": profile.require_sender_identity,
            "require_policy_acceptance": profile.require_policy_acceptance,
            "require_consent_metadata": profile.require_consent_metadata,
            "require_list_unsubscribe_header": profile.require_list_unsubscribe_header,
        }
        if compliance_profile is not None:
            profile.compliance_profile = compliance_profile[:50]
        if jurisdiction is not None:
            profile.jurisdiction = jurisdiction[:100]
        if require_unsubscribe is not None:
            profile.require_unsubscribe = require_unsubscribe
        if require_sender_identity is not None:
            profile.require_sender_identity = require_sender_identity
        if require_policy_acceptance is not None:
            profile.require_policy_acceptance = require_policy_acceptance
        if require_consent_metadata is not None:
            profile.require_consent_metadata = require_consent_metadata
        if require_list_unsubscribe_header is not None:
            profile.require_list_unsubscribe_header = require_list_unsubscribe_header
        if retention_policy is not None:
            merged = dict(DEFAULT_RETENTION_POLICY)
            merged.update(retention_policy)
            profile.retention_policy = merged
        if safety_thresholds is not None:
            merged = dict(DEFAULT_SAFETY_THRESHOLDS)
            merged.update(safety_thresholds)
            profile.safety_thresholds = merged

        AuditService(self.session, self.tenant_id, actor).record(
            "COMPLIANCE_PROFILE_UPDATED",
            "compliance_profile",
            profile.id,
            {
                "changes": {
                    key: {"from": previous.get(key), "to": getattr(profile, key)}
                    for key in previous
                    if previous.get(key) != getattr(profile, key)
                }
            },
        )
        self.session.flush()
        self.session.commit()
        return profile

    @staticmethod
    def thresholds(profile: ComplianceProfile) -> dict[str, Any]:
        merged = dict(DEFAULT_SAFETY_THRESHOLDS)
        merged.update(profile.safety_thresholds or {})
        return merged

    @classmethod
    def unsafe_threshold(cls, name: str, value: Any) -> bool:
        """Guard-rail sanity check used by the settings schema.

        Returns True when a threshold value is outside a sane configurable
        range (negative rate, window too small, sample too naive). These bounds
        are sanity limits, not legal advice.
        """
        if isinstance(value, bool):
            return True
        if name in {"hard_bounce_rate", "complaint_rate", "provider_error_rate"}:
            return not (0.0 < float(value) < 1.0)
        if name == "window_days":
            return int(value) < 1
        if name == "min_sample_size":
            return int(value) < 1
        return False