"""Configurable Terms of Service and Acceptable Use Policy acceptance.

The platform provides a lawful, permission-based email marketing tool; it does
not give legal advice and never represents itself as doing so. Policies are
versioned documents (seeded as ``compliance_policies`` rows) that tenants/users
accept explicitly. Acceptance is recorded once per user per policy type; sending
never re-prompts on every email. When a materially updated version is published,
``accepted_at``/``policy_version`` on the user's single record refresh on
re-acceptance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CompliancePolicy, PolicyAcceptance
from app.services.audit import AuditService

POLICY_TYPES = {"terms_of_service", "acceptable_use"}

TERMS_OF_SERVICE_VERSION = "1"
ACCEPTABLE_USE_VERSION = "1"


TERMS_OF_SERVICE = {
    "policy_type": "terms_of_service",
    "policy_version": TERMS_OF_SERVICE_VERSION,
    "title": "Terms of Service & Acceptable Use",
    "summary": (
        "By using the platform to send email you confirm you have the legal "
        "basis and permission required to contact recipients and will comply "
        "with the Acceptable Use Policy, the email providers' policies, and "
        "applicable law."
    ),
    "body": "\n".join(
        [
            "# CR+CRM Terms of Service",
            "",
            "This platform is an email marketing tool for sending lawful, permission-based email.",
            "",
            "## Your responsibilities",
            "- You are responsible for having a lawful basis or appropriate permission to contact each recipient.",
            "- You are responsible for complying with applicable email marketing and privacy laws for every jurisdiction you mail to.",
            "- You are responsible for complying with the sending domain owner's and mailbox provider's policies.",
            "- You must comply with Google, Microsoft, Zoho, SendGrid, and other provider acceptable-use policies.",
            "- You must maintain accurate sender identity information and use your own authorized sending identities.",
            "- You must honor every unsubscribe/opt-out request promptly.",
            "- You must maintain suppression lists and never send to suppressed or opted-out recipients.",
            "- You must ensure campaign content is lawful, truthful, and not misleading.",
            "",
            "## Not legal advice",
            "CR+CRM is not a law firm and does not provide legal advice. You must obtain your own legal review "
            "where required. Features such as the configurable compliance profile and safety thresholds are "
            "operational controls; they are not a guarantee of compliance with any specific law.",
            "",
            "## Policy acceptance",
            "This policy is versioned. A materially updated version may require re-acceptance before campaign "
            "sending continues. Acceptance is tracked per user per policy type; it is not requested for every email.",
        ]
    ),
    "published_at": None,
}

ACCEPTABLE_USE = {
    "policy_type": "acceptable_use",
    "policy_version": ACCEPTABLE_USE_VERSION,
    "title": "Acceptable Use Policy",
    "summary": (
        "This platform may only be used for lawful, permission-based email "
        "marketing. A list of prohibited activities is defined below."
    ),
    "body": "\n".join(
        [
            "# CR+CRM Acceptable Use Policy",
            "",
            "You may use the platform only for lawful, permission-based email marketing with an "
            "appropriate legal basis. The following are prohibited:",
            "",
            "1. Unsolicited bulk email where prohibited by applicable law (spam).",
            "2. Purchased or scraped email lists where unlawful or without consent.",
            "3. Harvesting email addresses, credential theft, phishing, or impersonation.",
            "4. Malware distribution, fraudulent campaigns, or deceptive sender identities.",
            "5. Content designed to facilitate abuse, or circumventing unsubscribe/opt-out requests.",
            "6. Bypassing provider restrictions, rate limits, or spam/security detection.",
            "7. Rotating infrastructure, domains, or accounts specifically to evade provider enforcement.",
            "8. Manipulating headers to conceal the true sender, or falsifying authentication results.",
            "9. Repeatedly contacting recipients after a valid opt-out.",
            "10. Using warmup to artificially manipulate or evade provider reputation systems.",
            "",
            "The platform is an email marketing platform, not a lead-generation or scraping platform. "
            "Any activity whose primary purpose is to evade spam filters, provider enforcement, domain "
            "reputation systems, rate limits, abuse detection, or legal requirements is prohibited.",
            "",
            "Violations may result in campaigns being blocked, senders being paused, or the account "
            "being suspended. These provisions are enforceable alongside the Terms of Service.",
        ]
    ),
    "published_at": None,
}

DEFAULT_POLICIES: dict[str, dict[str, Any]] = {
    "terms_of_service": TERMS_OF_SERVICE,
    "acceptable_use": ACCEPTABLE_USE,
}


class PolicyError(ValueError):
    pass


def _seed(session: Session) -> None:
    """Ensure the current platform policy documents exist (idempotent)."""
    for definition in DEFAULT_POLICIES.values():
        existing = session.scalar(
            select(CompliancePolicy).where(
                CompliancePolicy.policy_type == definition["policy_type"],
                CompliancePolicy.policy_version == definition["policy_version"],
            )
        )
        if existing is None:
            session.add(
                CompliancePolicy(
                    policy_type=definition["policy_type"],
                    policy_version=definition["policy_version"],
                    title=definition["title"],
                    summary=definition["summary"],
                    body=definition["body"],
                    published_at=datetime.now(UTC),
                )
            )
    session.commit()


class PolicyService:
    """Read/accept the platform's policy documents for a tenant user."""

    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        _seed(session)

    # ------------------------------------------------------------------ #
    # Document access
    # ------------------------------------------------------------------ #
    def get_document(self, policy_type: str) -> CompliancePolicy:
        if policy_type not in POLICY_TYPES:
            raise PolicyError("Unknown policy type")
        return self.current_document(policy_type)

    def current_document(self, policy_type: str) -> CompliancePolicy:
        document = self.session.scalar(
            select(CompliancePolicy)
            .where(CompliancePolicy.policy_type == policy_type)
            .order_by(CompliancePolicy.published_at.desc())
            .limit(1)
        )
        if document is None:
            raise PolicyError("Policy document is not available")
        return document

    def current_version(self, policy_type: str) -> str:
        return self.current_document(policy_type).policy_version

    def all_documents(self, policy_type: str) -> list[CompliancePolicy]:
        return list(
            self.session.scalars(
                select(CompliancePolicy)
                .where(CompliancePolicy.policy_type == policy_type)
                .order_by(CompliancePolicy.published_at.desc())
            ).all()
        )

    # ------------------------------------------------------------------ #
    # Acceptance
    # ------------------------------------------------------------------ #
    def latest_acceptance(self, policy_type: str, user_id: UUID | None = None) -> PolicyAcceptance | None:
        user_id = user_id or self.actor_id
        if user_id is None:
            return None
        return self.session.scalar(
            select(PolicyAcceptance).where(
                PolicyAcceptance.tenant_id == self.tenant_id,
                PolicyAcceptance.user_id == user_id,
                PolicyAcceptance.policy_type == policy_type,
            )
        )

    def is_accepted(self, policy_type: str, user_id: UUID | None = None) -> bool:
        latest = self.latest_acceptance(policy_type, user_id)
        if latest is None:
            return False
        try:
            return latest.policy_version == self.current_version(policy_type)
        except PolicyError:
            return True

    def is_tenant_accepted(self, policy_type: str) -> bool:
        """True when any user in the tenant has accepted the current version.

        Used by the send-time gate so a transiently unknown acceptance state
        fails closed without forcing per-email prompts.
        """
        try:
            version = self.current_version(policy_type)
        except PolicyError:
            return False
        return (
            self.session.scalar(
                select(PolicyAcceptance.id).where(
                    PolicyAcceptance.tenant_id == self.tenant_id,
                    PolicyAcceptance.policy_type == policy_type,
                    PolicyAcceptance.policy_version == version,
                )
            )
            is not None
        )

    def unaccepted_required(self, user_id: UUID | None = None) -> list[str]:
        return [policy_type for policy_type in sorted(POLICY_TYPES) if not self.is_accepted(policy_type, user_id)]

    def accept(self, policy_type: str, policy_version: str, ip_address: str | None = None) -> PolicyAcceptance:
        if self.actor_id is None:
            raise PolicyError("Authenticated user is required to accept a policy")
        if policy_type not in POLICY_TYPES:
            raise PolicyError("Unknown policy type")
        document = self.session.scalar(
            select(CompliancePolicy).where(
                CompliancePolicy.policy_type == policy_type,
                CompliancePolicy.policy_version == policy_version,
            )
        )
        if document is None:
            raise PolicyError("The requested policy version does not exist")

        existing = self.latest_acceptance(policy_type, self.actor_id)
        now = datetime.now(UTC)
        if existing is not None:
            prior_version = existing.policy_version
            existing.policy_version = policy_version
            existing.accepted_at = now
            existing.ip_address = (ip_address or "")[:64] or None
            existing.updated_at = now
            acceptance = existing
        else:
            acceptance = PolicyAcceptance(
                tenant_id=self.tenant_id,
                user_id=self.actor_id,
                policy_type=policy_type,
                policy_version=policy_version,
                accepted_at=now,
                ip_address=(ip_address or "")[:64] or None,
            )
            self.session.add(acceptance)
            prior_version = None

        AuditService(self.session, self.tenant_id, self.actor_id).record(
            "POLICY_ACCEPTED"
            if prior_version is None
            else "POLICY_RE_ACCEPTED",
            "policy",
            None,
            {
                "policy_type": policy_type,
                "policy_version": policy_version,
                "prior_version": prior_version,
            },
        )
        self.session.flush()
        self.session.commit()
        return acceptance

    def status(self, user_id: UUID | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for policy_type in sorted(POLICY_TYPES):
            document = self.current_document(policy_type)
            latest = self.latest_acceptance(policy_type, user_id)
            result.append(
                {
                    "policy_type": policy_type,
                    "title": document.title,
                    "summary": document.summary,
                    "policy_version": document.policy_version,
                    "accepted": latest is not None and latest.policy_version == document.policy_version,
                    "accepted_version": latest.policy_version if latest is not None else None,
                    "accepted_at": latest.accepted_at if latest is not None else None,
                }
            )
        return result