"""Provider-specific details for health checks.

Google (Phase 5), Microsoft 365 (Phase 6) and SendGrid + Zoho (Phase 7)
adapters are implemented. ``ProviderHealthAdapter`` is the extension point.
None of these adapters touch credentials - they only answer provider
behavioral questions (e.g. the DKIM selector the provider signs outbound
mail with).
"""

from __future__ import annotations

from typing import Protocol

from app.models import Mailbox, ProviderConnection


class ProviderHealthAdapter(Protocol):
    def dkim_selector(
        self,
        mailbox: Mailbox | None,
        connection: ProviderConnection | None,
    ) -> str | None:
        """Return the DKIM selector the provider signs with, or None if unknown."""
        ...


class GoogleWorkspaceHealthAdapter:
    """Google Workspace signs all outbound mail with the ``google`` selector.

    This is documented, stable provider behavior rather than a guess: DKIM
    keys for Workspace are published under ``google._domainkey.<domain>``.
    """

    def dkim_selector(
        self,
        mailbox: Mailbox | None,
        connection: ProviderConnection | None,
    ) -> str | None:
        return "google"


class MicrosoftHealthAdapter:
    """Microsoft 365 signs outbound mail via *selector2 (Microsoft).

    The selector is not guaranteed to be static across tenants, so this
    adapter deliberately returns ``None`` (UNKNOWN) rather than risk a false
    PASS. DKIM should be verified through the normal O365 portal flow.
    """

    def dkim_selector(
        self,
        mailbox: Mailbox | None,
        connection: ProviderConnection | None,
    ) -> str | None:
        return None


class SendGridHealthAdapter:
    """SendGrid signs authenticated-domain outbound mail via the ``s1`` selector.

    SendGrid publishes its account-level DKIM keys under
    ``s1._domainkey.<domain>`` (with ``s2`` as the rotation fallback). This is
    documented, stable provider behavior rather than a guess: the same DNS
    records appear in the SendGrid Domain Authentication flow.
    """

    def dkim_selector(
        self,
        mailbox: Mailbox | None,
        connection: ProviderConnection | None,
    ) -> str | None:
        return "s1"


class ZohoHealthAdapter:
    """Zoho Mail signs outbound mail with the ``zoho`` selector.

    Zoho publishes its DKIM keys under ``zoho._domainkey.<domain>``, a
    documented, stable provider behavior (visible in the Zoho Mail admin
    DKIM setup flow).
    """

    def dkim_selector(
        self,
        mailbox: Mailbox | None,
        connection: ProviderConnection | None,
    ) -> str | None:
        return "zoho"


class NullHealthAdapter:
    """Adapter for providers without implemented Phase 5 behavior."""

    def dkim_selector(
        self,
        mailbox: Mailbox | None,
        connection: ProviderConnection | None,
    ) -> str | None:
        return None


def build_provider_adapter(provider: str | None) -> ProviderHealthAdapter:
    if (provider or "").upper() == "GOOGLE":
        return GoogleWorkspaceHealthAdapter()
    if (provider or "").upper() == "MICROSOFT":
        return MicrosoftHealthAdapter()
    if (provider or "").upper() == "SENDGRID":
        return SendGridHealthAdapter()
    if (provider or "").upper() == "ZOHO":
        return ZohoHealthAdapter()
    return NullHealthAdapter()