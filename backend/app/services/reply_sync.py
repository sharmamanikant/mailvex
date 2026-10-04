"""System B reply sync (Phase 10Q).

When a recipient replies to a campaign message, the reply's ``In-Reply-To``
header references the provider message id we stored on send. For System B
senders with ``reply_sync_enabled`` and a provider that supports inbox sync,
``sync_sender`` pulls inbound and links each reply to the original sent
``Message`` by ``provider_message_id``, appending a ``REPLY`` event so the
commit can surface on the campaign/timeline without inventing new tables.

Webhook-driven ingestion will layer on top once a provider implements
``handle_webhook``; this pull model is the System B counterpart of the legacy
``/inbox/sync`` path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.email_providers import get_provider
from app.email_providers.base import EmailProviderError, ProviderConnectionConfig
from app.models import Message, MessageEvent, SenderAccount, SenderConnection


class ReplySyncUnavailable(RuntimeError):
    """The sender/provider cannot perform reply sync right now."""


class ReplySyncService:
    """Links inbound replies to their originating System B campaign send."""

    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def sync_sender(self, sender_account_id: UUID) -> int:
        """Pull inbound for one sender; returns the number of linked replies."""
        account = self.session.scalar(
            select(SenderAccount).where(
                SenderAccount.id == sender_account_id,
                SenderAccount.tenant_id == self.tenant_id,
            )
        )
        if account is None:
            raise LookupError("Sender account not found")
        if not account.reply_sync_enabled:
            return 0
        if account.status != "ACTIVE":
            return 0
        connection = self.session.scalar(
            select(SenderConnection).where(
                SenderConnection.id == account.connection_id,
                SenderConnection.tenant_id == self.tenant_id,
            )
        )
        if connection is None or connection.status in {"FAILED", "DISABLED", "DISCONNECTED", "REAUTH_REQUIRED"}:
            raise ReplySyncUnavailable("Sender connection is not usable")
        provider = get_provider(connection.provider)
        if not provider.get_capabilities().supports_inbox_sync:
            return 0
        try:
            inbound = provider.sync_inbox(_provider_config(connection), limit=100)
        except EmailProviderError as exc:
            raise ReplySyncUnavailable(exc.message) from exc
        matched = 0
        for raw in inbound:
            if self._link(raw):
                matched += 1
        self.session.commit()
        return matched

    def _link(self, raw: dict[str, object]) -> bool:
        tokens = _reference_tokens(raw.get("in_reply_to")) | _reference_tokens(raw.get("references"))
        if not tokens:
            return False
        original = self.session.scalar(
            select(Message)
            .where(
                Message.tenant_id == self.tenant_id,
                (Message.provider_message_id.in_(sorted(tokens))) | (Message.message_id.in_(sorted(tokens))),
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        if original is None:
            return False
        original.events.append(
            MessageEvent(
                tenant_id=self.tenant_id,
                event_type="REPLY",
                provider_event_id=str(raw.get("message_id") or raw.get("id") or ""),
                provider_payload={
                    "reply_id": raw.get("message_id") or raw.get("id"),
                    "in_reply_to": raw.get("in_reply_to"),
                    "references": raw.get("references"),
                    "subject": raw.get("subject"),
                    "from": raw.get("from"),
                    "received_at": raw.get("received_at") or datetime.now(UTC).isoformat(),
                },
                occurred_at=datetime.now(UTC),
            )
        )
        return True


def _reference_tokens(value: object) -> set[str]:
    """Split an ``In-Reply-To``/``References`` value into bare message-id tokens
    (angle brackets stripped, surrounding commas/whitespace removed)."""
    if not isinstance(value, str) or not value:
        return set()
    tokens: set[str] = set()
    for part in value.replace(",", " ").split():
        token = part.strip()
        if token.startswith("<") and token.endswith(">"):
            token = token[1:-1]
        if token:
            tokens.add(token)
    return tokens


def _provider_config(connection: SenderConnection) -> ProviderConnectionConfig:
    return ProviderConnectionConfig(
        connection_type=connection.connection_type,
        external_account_id=connection.external_account_id,
        email=connection.email,
        metadata=dict(connection.connection_metadata or {}),
        credential_reference=connection.credential_reference,
        credential_version=connection.credential_version,
        credential_expires_at=connection.credential_expires_at,
    )