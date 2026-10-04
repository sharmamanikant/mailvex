"""Sender-connection orchestration for the email-provider foundation (System B).

The service owns connection lifecycle, credential storage (encrypted, behind
the credential store), sender discovery/import, and audit events. It is
independent of Clerk and of the existing legacy sender code, which remains
untouched during this phase.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, NamedTuple, overload
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.email_providers import (
    EmailMessage,
    EmailProviderError,
    ProviderConnectionConfig,
    decrypt_credential_reference,
    encrypt_credential_reference,
    get_provider,
    list_providers,
    rotate_credential_reference,
)
from app.email_providers.base import (
    ProviderCapabilities,
    ProviderErrorCode,
    ProviderSenderProfile,
)
from app.email_providers.credentials import payload_expires_at
from app.email_providers.google.provider import GMAIL_INBOX_SCOPE
from app.email_providers.microsoft.provider import MAIL_READ_SCOPE
from app.models import SenderAccount, SenderConnection, WarmupSettings
from app.schemas.integrations import (
    CredentialUpload,
    DiscoveredSenderResponse,
    DiscoveryResponse,
    IntegrationCreate,
    SenderAccountCreate,
    SenderAccountUpdate,
    WarmupSettingsUpdate,
)
from app.services.audit import AuditService
from app.services.sender_health import SenderHealthService


class IntegrationNotFoundError(LookupError):
    pass


class IntegrationConflictError(ValueError):
    pass


class IntegrationValidationError(ValueError):
    pass


# Providers that actually implement ``sync_inbox`` and the mailbox-read scope
# they require at consent time. ``enable_reply_sync`` refuses anything else.
_INBOX_SCOPE_BY_PROVIDER: dict[str, str] = {
    "GOOGLE": GMAIL_INBOX_SCOPE,
    "MICROSOFT": MAIL_READ_SCOPE,
}


def serialize_connection(connection: SenderConnection) -> dict[str, object]:
    return {
        "id": connection.id,
        "tenant_id": connection.tenant_id,
        "provider": connection.provider,
        "connection_type": connection.connection_type,
        "status": connection.status,
        "external_account_id": connection.external_account_id,
        "email": connection.email,
        "credential_configured": connection.credential_reference is not None,
        "credential_version": connection.credential_version,
        "credential_expires_at": connection.credential_expires_at,
        "metadata": connection.connection_metadata or {},
        "last_connected_at": connection.last_connected_at,
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
    }


_REAUTH_CODES = frozenset(
    {
        ProviderErrorCode.AUTH_REQUIRED,
        ProviderErrorCode.AUTH_FAILED,
        ProviderErrorCode.TOKEN_EXPIRED,
    }
)


def _google_action(provider: str, google_action: str, generic_action: str) -> str:
    return google_action if provider.upper() == "GOOGLE" else generic_action


def _microsoft_action(provider: str, microsoft_action: str, generic_action: str) -> str:
    return microsoft_action if provider.upper() == "MICROSOFT" else generic_action


def _provider_action(provider: str, google: str, microsoft: str, generic: str) -> str:
    upper = provider.upper()
    if upper == "GOOGLE":
        return google
    if upper == "MICROSOFT":
        return microsoft
    return generic


_WEEKDAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}


def _validate_weekdays(weekdays: list[str]) -> None:
    normalized = {str(day).upper() for day in weekdays}
    if not normalized:
        raise IntegrationValidationError("weekdays must not be empty")
    unknown = normalized - _WEEKDAYS
    if unknown:
        raise IntegrationValidationError(f"Invalid weekdays: {', '.join(sorted(unknown))}")


class TestSendResult(NamedTuple):
    message_id: str
    sent_at: datetime


class IntegrationService:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self._encryption_key = settings.encryption_key

    # ------------------------------------------------------------------ #
    # Providers
    # ------------------------------------------------------------------ #
    def list_providers(self) -> list[ProviderCapabilities]:
        return list_providers()

    def _provider_config(self, connection: SenderConnection) -> ProviderConnectionConfig:
        return ProviderConnectionConfig(
            connection_type=connection.connection_type,
            external_account_id=connection.external_account_id,
            email=connection.email,
            metadata=dict(connection.connection_metadata or {}),
            credential_reference=connection.credential_reference,
            credential_version=connection.credential_version,
            credential_expires_at=connection.credential_expires_at,
        )

    # ------------------------------------------------------------------ #
    # Connections
    # ------------------------------------------------------------------ #
    def create_connection(self, payload: IntegrationCreate, created_by: UUID | None = None) -> SenderConnection:
        try:
            provider = get_provider(payload.provider)
        except KeyError as exc:
            raise IntegrationValidationError(f"Unknown email provider: {payload.provider}") from exc
        allowed_types = provider.get_capabilities().connection_types
        if payload.connection_type not in allowed_types:
            raise IntegrationValidationError(
                f"{payload.provider} does not support {payload.connection_type} connections"
            )
        metadata = dict(payload.metadata)
        if payload.smtp_host:
            metadata["smtp_host"] = payload.smtp_host
        if payload.smtp_port is not None:
            metadata["smtp_port"] = payload.smtp_port
        if payload.smtp_username:
            metadata["smtp_username"] = payload.smtp_username
        if payload.external_account_id:
            duplicate = self.session.scalar(
                select(SenderConnection).where(
                    SenderConnection.tenant_id == self.tenant_id,
                    SenderConnection.provider == payload.provider.upper(),
                    SenderConnection.external_account_id == payload.external_account_id,
                )
            )
            if duplicate is not None:
                raise IntegrationConflictError(
                    "A connection for this account already exists; reconnect to refresh it"
                )
        connection = SenderConnection(
            tenant_id=self.tenant_id,
            provider=payload.provider.upper(),
            connection_type=payload.connection_type,
            external_account_id=payload.external_account_id,
            email=str(payload.email).lower() if payload.email else None,
            status="CONNECTING",
            connection_metadata=metadata,
            created_by=created_by,
        )
        self.session.add(connection)
        self.session.flush()
        AuditService(self.session, self.tenant_id, created_by).record(
            "SENDER_CONNECTION_CREATED",
            "sender_connection",
            connection.id,
            {
                "provider": connection.provider,
                "connection_type": connection.connection_type,
                "external_account_id": connection.external_account_id,
            },
        )
        self.session.commit()
        return connection

    def _connection(self, connection_id: UUID) -> SenderConnection:
        connection = self.session.scalar(
            select(SenderConnection).where(
                SenderConnection.id == connection_id,
                SenderConnection.tenant_id == self.tenant_id,
            )
        )
        if connection is None:
            raise IntegrationNotFoundError("Integration not found")
        return connection

    def get_connection(self, connection_id: UUID) -> SenderConnection:
        return self._connection(connection_id)

    def get_connection_details(self, connection_id: UUID) -> dict[str, object]:
        """Safe connection detail surface — never exposes credentials.

        Returns only non-sensitive fields plus (when recorded) the last
        validation/error timestamps and error code from connection metadata.
        """
        connection = self._connection(connection_id)
        metadata = dict(connection.connection_metadata or {})
        return {
            "id": connection.id,
            "provider": connection.provider,
            "connection_type": connection.connection_type,
            "status": connection.status,
            "external_account_id": connection.external_account_id,
            "email": connection.email,
            "created_at": connection.created_at,
            "updated_at": connection.updated_at,
            "last_validated_at": connection.last_connected_at,
            "last_error_code": metadata.get("last_error_code"),
            "last_error_at": metadata.get("last_error_at"),
        }

    def list_connections(self) -> list[SenderConnection]:
        return list(self.session.scalars(
            select(SenderConnection)
            .where(SenderConnection.tenant_id == self.tenant_id)
            .order_by(SenderConnection.created_at.desc())
        ).all())

    def store_credentials(self, connection_id: UUID, payload: CredentialUpload, actor_id: UUID | None = None) -> SenderConnection:
        connection = self._connection(connection_id)
        secret_payload: dict[str, object] = {
            key: value
            for key, value in {
                "api_key": payload.api_key,
                "smtp_password": payload.smtp_password,
                "client_secret": payload.client_secret,
                "access_token": payload.access_token,
                "refresh_token": payload.refresh_token,
                "client_id": payload.client_id,
                "smtp_username": payload.smtp_username,
                "tenant_id": payload.tenant_id,
                "scopes": payload.scopes,
            }.items()
            if value not in (None, [])
        }
        if payload.expires_at is not None:
            secret_payload["expires_at"] = payload.expires_at
        if connection.credential_reference is not None:
            connection.credential_reference, connection.credential_version = rotate_credential_reference(
                self._encryption_key,
                connection.credential_reference,
                new_payload=secret_payload,
                previous_version=connection.credential_version,
            )
        else:
            connection.credential_reference = encrypt_credential_reference(
                self._encryption_key, secret_payload, version="v1"
            )
            connection.credential_version = "v1"
        connection.credential_expires_at = payload.expires_at
        if payload.smtp_host:
            connection.connection_metadata = {**connection.connection_metadata, "smtp_host": payload.smtp_host}
        if payload.smtp_port is not None:
            connection.connection_metadata = {**connection.connection_metadata, "smtp_port": payload.smtp_port}
        AuditService(self.session, self.tenant_id, actor_id).record(
            "SENDER_CREDENTIAL_UPDATED",
            "sender_connection",
            connection.id,
            {"provider": connection.provider, "version": connection.credential_version},
        )
        self.session.commit()
        return connection

    def validate_connection(self, connection_id: UUID, actor_id: UUID | None = None) -> SenderConnection:
        connection = self._connection(connection_id)
        provider = get_provider(connection.provider)
        result = provider.validate_connection(self._provider_config(connection))
        audit = AuditService(self.session, self.tenant_id, actor_id)
        if result.valid:
            connection.status = "CONNECTED"
            connection.last_connected_at = datetime.now(UTC)
            connection.connection_metadata = {
                **(connection.connection_metadata or {}),
                "last_error_code": None,
                "last_error_at": None,
            }
            audit.record(
                _provider_action(
                    connection.provider,
                    "GOOGLE_CONNECTION_VALIDATED",
                    "MICROSOFT_CONNECTION_VALIDATED",
                    "SENDER_CONNECTION_VALIDATED",
                ),
                "sender_connection",
                connection.id,
                {"provider": connection.provider},
            )
        elif result.error_code in _REAUTH_CODES:
            connection.status = "REAUTH_REQUIRED"
            connection.connection_metadata = {
                **(connection.connection_metadata or {}),
                "last_error_code": result.error_code.value,
                "last_error_at": datetime.now(UTC).isoformat(),
            }
            audit.record(
                _provider_action(
                    connection.provider,
                    "GOOGLE_CONNECTION_REAUTH_REQUIRED",
                    "MICROSOFT_REAUTH_REQUIRED",
                    "SENDER_CONNECTION_FAILED",
                ),
                "sender_connection",
                connection.id,
                {"provider": connection.provider, "reason": result.message, "code": result.error_code.value},
            )
        else:
            connection.status = "FAILED"
            connection.connection_metadata = {
                **(connection.connection_metadata or {}),
                "last_error_code": (result.error_code.value if result.error_code else "UNKNOWN"),
                "last_error_at": datetime.now(UTC).isoformat(),
            }
            audit.record(
                _provider_action(
                    connection.provider,
                    "GOOGLE_CONNECTION_FAILED",
                    "MICROSOFT_OAUTH_FAILED",
                    "SENDER_CONNECTION_FAILED",
                ),
                "sender_connection",
                connection.id,
                {"provider": connection.provider, "reason": result.message},
            )
        self.session.commit()
        return connection

    def disconnect_connection(self, connection_id: UUID, actor_id: UUID | None = None) -> SenderConnection:
        connection = self._connection(connection_id)
        connection.status = "DISCONNECTED"
        AuditService(self.session, self.tenant_id, actor_id).record(
            _provider_action(
                connection.provider,
                "GOOGLE_CONNECTION_DISCONNECTED",
                "MICROSOFT_CONNECTION_DISCONNECTED",
                "SENDER_DISCONNECTED",
            ),
            "sender_connection",
            connection.id,
            {"provider": connection.provider},
        )
        self.session.commit()
        return connection

    def refresh_credentials(self, connection_id: UUID, payload: CredentialUpload | None = None, actor_id: UUID | None = None) -> SenderConnection:
        connection = self._connection(connection_id)
        if connection.credential_reference is None:
            raise IntegrationValidationError("No credentials stored; upload credentials first")
        if payload is not None:
            secret_payload: dict[str, object] = {
                key: value
                for key, value in {
                    "api_key": payload.api_key,
                    "smtp_password": payload.smtp_password,
                    "client_secret": payload.client_secret,
                    "access_token": payload.access_token,
                    "refresh_token": payload.refresh_token,
                    "client_id": payload.client_id,
                    "smtp_username": payload.smtp_username,
                    "tenant_id": payload.tenant_id,
                    "scopes": payload.scopes,
                }.items()
                if value not in (None, [])
            }
            if payload.expires_at is not None:
                secret_payload["expires_at"] = payload.expires_at
            new_reference, new_version = rotate_credential_reference(
                self._encryption_key,
                connection.credential_reference,
                new_payload=secret_payload,
                previous_version=connection.credential_version,
            )
            connection.credential_expires_at = payload.expires_at
        else:
            current = decrypt_credential_reference(self._encryption_key, connection.credential_reference)
            new_reference, new_version = rotate_credential_reference(
                self._encryption_key,
                connection.credential_reference,
                previous_version=connection.credential_version,
            )
            connection.credential_expires_at = payload_expires_at(current)
        connection.credential_reference = new_reference
        connection.credential_version = new_version
        AuditService(self.session, self.tenant_id, actor_id).record(
            "SENDER_CREDENTIAL_ROTATED",
            "sender_connection",
            connection.id,
            {"provider": connection.provider, "version": new_version},
        )
        self.session.commit()
        return connection

    def delete_connection(self, connection_id: UUID, actor_id: UUID | None = None) -> None:
        connection = self._connection(connection_id)
        AuditService(self.session, self.tenant_id, actor_id).record(
            _provider_action(
                connection.provider,
                "GOOGLE_CONNECTION_DISCONNECTED",
                "MICROSOFT_CONNECTION_DISCONNECTED",
                "SENDER_DISCONNECTED",
            ),
            "sender_connection",
            connection.id,
            {"provider": connection.provider},
        )
        self.session.delete(connection)
        self.session.commit()

    # ------------------------------------------------------------------ #
    # Discovery & sender accounts
    # ------------------------------------------------------------------ #
    def discover_senders(self, connection_id: UUID, actor_id: UUID | None = None) -> DiscoveryResponse:
        connection = self._connection(connection_id)
        provider = get_provider(connection.provider)
        capabilities = provider.get_capabilities()
        audit = AuditService(self.session, self.tenant_id, actor_id)
        audit.record(
            "SENDER_DISCOVERY_STARTED",
            "sender_connection",
            connection.id,
            {"provider": connection.provider},
        )
        result = provider.discover_senders(self._provider_config(connection))
        profiles: tuple[ProviderSenderProfile, ...] = result.senders
        audit.record(
            "SENDER_DISCOVERED",
            "sender_connection",
            connection.id,
            {"provider": connection.provider, "discovered": len(profiles)},
        )
        self.session.commit()
        senders = [
            DiscoveredSenderResponse(
                email=profile.email,
                display_name=profile.display_name,
                external_sender_id=profile.external_sender_id,
                verified=profile.verified,
            )
            for profile in profiles
        ]
        return DiscoveryResponse(
            provider=connection.provider,
            supports_discovery=capabilities.supports_sender_discovery,
            discovered=len(profiles),
            senders=senders,
            errors=list(result.errors),
        )

    def list_senders(self, connection_id: UUID) -> list[SenderAccount]:
        self._connection(connection_id)
        return list(self.session.scalars(
            select(SenderAccount)
            .where(
                SenderAccount.tenant_id == self.tenant_id,
                SenderAccount.connection_id == connection_id,
            )
            .order_by(SenderAccount.email)
        ).all())

    def list_all_senders(self) -> list[SenderAccount]:
        """List every sender account in the tenant across connections."""
        return list(self.session.scalars(
            select(SenderAccount)
            .where(SenderAccount.tenant_id == self.tenant_id)
            .order_by(SenderAccount.email)
        ).all())

    # ------------------------------------------------------------------ #
    # Phase 10Q traffic toggles (never auto-enable)
    # ------------------------------------------------------------------ #
    def enable_campaign(self, sender_id: UUID, actor_id: UUID | None = None) -> SenderAccount:
        """Opt a sender into campaign traffic (``campaign_enabled``)."""
        account = self._sender_account(sender_id)
        account.campaign_enabled = True
        AuditService(self.session, self.tenant_id, actor_id).record(
            "SENDER_CAMPAIGN_ENABLED",
            "sender_account",
            account.id,
            {"email": account.email, "provider": account.provider},
        )
        self.session.commit()
        return account

    def disable_campaign(self, sender_id: UUID, actor_id: UUID | None = None) -> SenderAccount:
        """Remove a sender from campaign rotation immediately."""
        account = self._sender_account(sender_id)
        account.campaign_enabled = False
        AuditService(self.session, self.tenant_id, actor_id).record(
            "SENDER_CAMPAIGN_DISABLED",
            "sender_account",
            account.id,
            {"email": account.email, "provider": account.provider},
        )
        self.session.commit()
        return account

    def enable_warmup(self, sender_id: UUID, actor_id: UUID | None = None) -> SenderAccount:
        """Opt a sender into warmup traffic and ensure settings exist."""
        account = self._sender_account(sender_id)
        account.warmup_enabled = True
        settings_row = self._warmup_settings(sender_id, create=True)
        settings_row.enabled = True
        if settings_row.daily_limit and account.daily_warmup_limit != settings_row.daily_limit:
            account.daily_warmup_limit = settings_row.daily_limit
        AuditService(self.session, self.tenant_id, actor_id).record(
            "SENDER_WARMUP_ENABLED",
            "sender_account",
            account.id,
            {"email": account.email, "provider": account.provider},
        )
        self.session.commit()
        from app.services.warmup import arm_sender

        arm_sender(account.id)
        return account

    def disable_warmup(self, sender_id: UUID, actor_id: UUID | None = None) -> SenderAccount:
        """Pause warmup traffic for a sender."""
        account = self._sender_account(sender_id)
        account.warmup_enabled = False
        settings_row = self._warmup_settings(sender_id, create=False)
        if settings_row is not None:
            settings_row.enabled = False
        AuditService(self.session, self.tenant_id, actor_id).record(
            "SENDER_WARMUP_DISABLED",
            "sender_account",
            account.id,
            {"email": account.email, "provider": account.provider},
        )
        self.session.commit()
        from app.services.warmup import disarm_sender

        disarm_sender(account.id)
        return account

    def get_warmup_settings(self, sender_id: UUID) -> WarmupSettings:
        """Return a sender's warmup settings, materializing defaults on first read."""
        self._sender_account(sender_id)
        return self._warmup_settings(sender_id, create=True)

    def update_warmup_settings(
        self,
        sender_id: UUID,
        payload: WarmupSettingsUpdate,
        actor_id: UUID | None = None,
    ) -> WarmupSettings:
        account = self._sender_account(sender_id)
        settings_row = self._warmup_settings(sender_id, create=True)
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("weekdays") is not None:
            _validate_weekdays(changes["weekdays"])
        minimum = changes.get("minimum_delay", settings_row.minimum_delay)
        maximum = changes.get("maximum_delay", settings_row.maximum_delay)
        if minimum > maximum:
            raise IntegrationValidationError("minimum_delay cannot exceed maximum_delay")
        if changes.get("target_provider_distribution") is not None:
            if any(share < 0 for share in changes["target_provider_distribution"].values()):
                raise IntegrationValidationError("target_provider_distribution shares cannot be negative")
        for field, value in changes.items():
            setattr(settings_row, field, value)
        if "daily_limit" in changes:
            account.daily_warmup_limit = changes["daily_limit"]
        AuditService(self.session, self.tenant_id, actor_id).record(
            "WARMUP_SETTINGS_UPDATED",
            "sender_account",
            account.id,
            {"email": account.email, "provider": account.provider, "fields": sorted(changes)},
        )
        self.session.commit()
        return settings_row

    @overload
    def _warmup_settings(self, sender_id: UUID, *, create: Literal[True]) -> WarmupSettings: ...

    @overload
    def _warmup_settings(self, sender_id: UUID, *, create: Literal[False]) -> WarmupSettings | None: ...

    def _warmup_settings(self, sender_id: UUID, *, create: bool) -> WarmupSettings | None:
        settings_row = self.session.scalar(
            select(WarmupSettings).where(
                WarmupSettings.tenant_id == self.tenant_id,
                WarmupSettings.sender_id == sender_id,
            )
        )
        if settings_row is None and create:
            settings_row = WarmupSettings(
                tenant_id=self.tenant_id,
                sender_id=sender_id,
                weekdays=["MON", "TUE", "WED", "THU", "FRI"],
            )
            self.session.add(settings_row)
            try:
                self.session.flush()
            except IntegrityError:
                self.session.rollback()
                settings_row = self.session.scalar(
                    select(WarmupSettings).where(
                        WarmupSettings.tenant_id == self.tenant_id,
                        WarmupSettings.sender_id == sender_id,
                    )
                )
        return settings_row

    def register_sender(self, connection_id: UUID, payload: SenderAccountCreate, actor_id: UUID | None = None) -> SenderAccount:
        connection = self._connection(connection_id)
        account = SenderAccount(
            tenant_id=self.tenant_id,
            connection_id=connection.id,
            provider=connection.provider,
            email=str(payload.email).lower(),
            display_name=payload.display_name,
            external_sender_id=payload.external_sender_id,
            status="ACTIVE",
            health_status="UNKNOWN",
        )
        self.session.add(account)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            raise IntegrationConflictError("A sender with this email already exists on this connection") from None
        AuditService(self.session, self.tenant_id, actor_id).record(
            _provider_action(connection.provider, "SENDER_IMPORTED", "MICROSOFT_SENDER_IMPORTED", "SENDER_IMPORTED"),
            "sender_account",
            account.id,
            {"email": account.email, "provider": connection.provider},
        )
        self.session.commit()
        return account

    def import_senders(
        self,
        connection_id: UUID,
        senders: list[SenderAccountCreate],
        actor_id: UUID | None = None,
    ) -> tuple[list[SenderAccount], list[str]]:
        """Bulk-import sender accounts; duplicates are reported, not fatal.

        Each row is imported inside a savepoint so a single duplicate cannot
        roll back the other successfully imported senders.
        """
        connection = self._connection(connection_id)
        imported: list[SenderAccount] = []
        errors: list[str] = []
        for payload in senders:
            account = SenderAccount(
                tenant_id=self.tenant_id,
                connection_id=connection.id,
                provider=connection.provider,
                email=str(payload.email).lower(),
                display_name=payload.display_name,
                external_sender_id=payload.external_sender_id,
                status="ACTIVE",
                health_status="UNKNOWN",
            )
            try:
                with self.session.begin_nested():
                    self.session.add(account)
                    self.session.flush()
            except IntegrityError:
                errors.append(f"{payload.email}: already imported")
                continue
            AuditService(self.session, self.tenant_id, actor_id).record(
                _provider_action(connection.provider, "SENDER_IMPORTED", "MICROSOFT_SENDER_IMPORTED", "SENDER_IMPORTED"),
                "sender_account",
                account.id,
                {"email": account.email, "provider": connection.provider},
            )
            imported.append(account)
        self.session.commit()
        return imported, errors

    def set_sender_status(
        self,
        sender_id: UUID,
        status: Literal["ACTIVE", "DISABLED"],
        actor_id: UUID | None = None,
    ) -> SenderAccount:
        """Enable/disable a sender account directly (spec: ``/enable``, ``/disable``)."""
        return self.update_sender_status(sender_id, SenderAccountUpdate(status=status), actor_id=actor_id)

    def _sender_account(self, sender_id: UUID) -> SenderAccount:
        account = self.session.scalar(
            select(SenderAccount).where(
                SenderAccount.id == sender_id,
                SenderAccount.tenant_id == self.tenant_id,
            )
        )
        if account is None:
            raise IntegrationNotFoundError("Sender account not found")
        return account

    def get_sender_account(self, sender_id: UUID) -> SenderAccount:
        """Public, tenant-scoped sender lookup (used by the test-send route)."""
        return self._sender_account(sender_id)

    def get_sender_account_by_connection(self, connection_id: UUID) -> SenderAccount:
        """Return the primary (first) sender account on a connection."""
        connection = self._connection(connection_id)
        accounts = list(self.session.scalars(
            select(SenderAccount).where(
                SenderAccount.tenant_id == self.tenant_id,
                SenderAccount.connection_id == connection.id,
            ).order_by(SenderAccount.created_at)
        ).all())
        if not accounts:
            raise IntegrationNotFoundError("Sender account not found")
        return accounts[0]

    def update_sender_status(self, sender_id: UUID, payload: SenderAccountUpdate, actor_id: UUID | None = None) -> SenderAccount:
        account = self._sender_account(sender_id)
        if payload.status not in ("ACTIVE", "DISABLED"):
            raise IntegrationValidationError("Only ACTIVE or DISABLED may be set manually")
        account.status = payload.status
        AuditService(self.session, self.tenant_id, actor_id).record(
            "SENDER_ENABLED" if payload.status == "ACTIVE" else "SENDER_DISABLED",
            "sender_account",
            account.id,
            {"email": account.email, "provider": account.provider},
        )
        self.session.commit()
        return account

    def sync_replies(self, sender_id: UUID) -> int:
        """Pull inbound and link replies to their originating System B sends."""
        from app.services.reply_sync import ReplySyncService

        try:
            return ReplySyncService(self.session, self.tenant_id).sync_sender(sender_id)
        except LookupError as exc:
            raise IntegrationNotFoundError("Sender account not found") from exc

    def enable_reply_sync(self, sender_id: UUID, actor_id: UUID | None = None) -> SenderAccount:
        """Opt a sender into reply-sync — requires a provider that implements
        ``sync_inbox`` AND a connection whose consent granted the mailbox-read
        scope (gmail.readonly / Mail.Read). Missing scope means the connection
        was authorized send-only and must be re-consented with inbox access."""
        account = self._sender_account(sender_id)
        connection = self._connection(account.connection_id)
        inbox_scope = _INBOX_SCOPE_BY_PROVIDER.get(connection.provider.upper() or "")
        if inbox_scope is None:
            raise IntegrationValidationError(
                f"{connection.provider} does not support reply sync"
            )
        granted = set((connection.connection_metadata or {}).get("scopes") or [])
        if inbox_scope not in granted:
            raise IntegrationValidationError(
                "Mailbox access was not granted. Re-consent this connection "
                "with inbox access enabled, then try again."
            )
        account.reply_sync_enabled = True
        AuditService(self.session, self.tenant_id, actor_id).record(
            "SENDER_REPLY_SYNC_ENABLED",
            "sender_account",
            account.id,
            {"email": account.email, "provider": connection.provider},
        )
        self.session.commit()
        return account

    def disable_reply_sync(self, sender_id: UUID, actor_id: UUID | None = None) -> SenderAccount:
        """Turn reply-sync off; the granted mailbox scope (if any) is kept so a
        later re-enable needs no new consent."""
        account = self._sender_account(sender_id)
        account.reply_sync_enabled = False
        AuditService(self.session, self.tenant_id, actor_id).record(
            "SENDER_REPLY_SYNC_DISABLED",
            "sender_account",
            account.id,
            {"email": account.email, "provider": account.provider},
        )
        self.session.commit()
        return account

    def send_test_email(self, sender_id: UUID, recipient: str, actor_id: UUID | None = None) -> TestSendResult:
        """Send a single explicit test message through the provider (spec item 18)."""
        account = self._sender_account(sender_id)
        connection = self._connection(account.connection_id)
        provider = get_provider(connection.provider)
        message = EmailMessage(
            from_email=connection.email or account.email,
            to=(recipient,),
            subject="CR+CRM test email",
            text_body="This is a test message sent from your CR+CRM integration.",
            html_body="<p>This is a test message sent from your CR+CRM integration.</p>",
        )
        sent_at = datetime.now(UTC)
        audit = AuditService(self.session, self.tenant_id, actor_id)
        is_microsoft = connection.provider.upper() == "MICROSOFT"
        requested_action = "MICROSOFT_TEST_SEND_REQUESTED" if is_microsoft else "TEST_EMAIL_SENT"
        audit.record(
            requested_action,
            "sender_account",
            account.id,
            {"provider": connection.provider, "recipient": recipient},
        )
        try:
            message_id = provider.send_message(self._provider_config(connection), message)
        except EmailProviderError as exc:
            audit.record(
                _provider_action(
                    connection.provider,
                    "TEST_EMAIL_SENT",
                    "MICROSOFT_TEST_SEND_FAILED",
                    "TEST_EMAIL_SENT",
                ),
                "sender_account",
                account.id,
                {
                    "provider": connection.provider,
                    "recipient": recipient,
                    "success": False,
                    "reason": exc.message,
                    "code": exc.code.value,
                },
            )
            self.session.commit()
            SenderHealthService(self.session, self.tenant_id).record_send_outcome(
                account.id,
                success=False,
                error_code=exc.code.value,
                actor_id=actor_id,
            )
            raise
        except Exception as exc:
            audit.record(
                _provider_action(
                    connection.provider,
                    "TEST_EMAIL_SENT",
                    "MICROSOFT_TEST_SEND_FAILED",
                    "TEST_EMAIL_SENT",
                ),
                "sender_account",
                account.id,
                {"provider": connection.provider, "recipient": recipient, "success": False},
            )
            self.session.commit()
            SenderHealthService(self.session, self.tenant_id).record_send_outcome(
                account.id,
                success=False,
                error_code="PROVIDER_UNAVAILABLE",
                actor_id=actor_id,
            )
            raise IntegrationValidationError("The provider could not send the test message") from exc
        account.last_used_at = sent_at
        audit.record(
            _provider_action(
                connection.provider,
                "TEST_EMAIL_SENT",
                "MICROSOFT_TEST_SEND_ACCEPTED",
                "TEST_EMAIL_SENT",
            ),
            "sender_account",
            account.id,
            {
                "provider": connection.provider,
                "recipient": recipient,
                "success": True,
                "message_id": message_id,
            },
        )
        self.session.commit()
        SenderHealthService(self.session, self.tenant_id).record_send_outcome(
            account.id,
            success=True,
            message_id=message_id,
            actor_id=actor_id,
        )
        return TestSendResult(message_id=message_id, sent_at=sent_at)