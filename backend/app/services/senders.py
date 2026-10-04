from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from google.oauth2.credentials import Credentials
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.billing import EVENT_SENDER_CONNECTED, UsageService
from app.core.config import settings
from app.models import EmailAccount, ProviderCredential, SenderProfile
from app.providers import (
    GmailProvider,
    MicrosoftGraphProvider,
    MockEmailProvider,
    ProviderMessage,
    ProviderProfile,
    SenderUnavailableError,
    SMTPProvider,
)
from app.providers.gmail import GMAIL_SCOPES
from app.providers.smtp import SMTPProviderError
from app.schemas.senders import SenderCreate, SenderUpdate
from app.security.credential_store import CredentialStore
from app.services.audit import AuditService

REFRESH_SKEW = timedelta(minutes=5)
REFRESH_LOCK_TIMEOUT = timedelta(minutes=2)


class SenderNotFoundError(LookupError):
    pass


class SenderConflictError(ValueError):
    pass


class SenderConnectionError(RuntimeError):
    """Safe, user-facing connection error — never leaks provider internals."""


def _encrypt_token(value: str | None, store: CredentialStore) -> str | None:
    if value is None:
        return None
    return store.encrypt({"value": value})


def _decrypt_token(blob: str | None, store: CredentialStore) -> str | None:
    if not blob:
        return None
    payload = store.decrypt(blob)
    value = payload.get("value") or payload.get("token")
    return str(value) if value is not None else None


def _decrypt_refresh(blob: str | None, store: CredentialStore) -> str | None:
    if not blob:
        return None
    payload = store.decrypt(blob)
    value = payload.get("refresh_token") or payload.get("value")
    return str(value) if value is not None else None


class SenderService:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def _account(self, sender_id: UUID) -> EmailAccount:
        account = self.session.scalar(
            select(EmailAccount)
            .options(selectinload(EmailAccount.profile), selectinload(EmailAccount.credential))
            .where(EmailAccount.id == sender_id, EmailAccount.tenant_id == self.tenant_id)
        )
        if account is None:
            raise SenderNotFoundError("Sender not found")
        return account

    def list(self) -> list[EmailAccount]:
        return list(self.session.scalars(
            select(EmailAccount)
            .options(selectinload(EmailAccount.profile))
            .where(EmailAccount.tenant_id == self.tenant_id)
            .order_by(EmailAccount.email)
        ).all())

    def get(self, sender_id: UUID) -> EmailAccount:
        return self._account(sender_id)

    def update(self, sender_id: UUID, payload: SenderUpdate) -> EmailAccount:
        account = self._account(sender_id)
        values = payload.model_dump(exclude_unset=True, exclude={"smtp_password", "company", "designation", "phone", "signature"})
        if "email" in values:
            values["email"] = str(values["email"]).lower()
        if values.get("reply_to"):
            values["reply_to"] = str(values["reply_to"]).lower()
        for key, value in values.items():
            setattr(account, key, value)
        if account.profile is None:
            account.profile = SenderProfile(tenant_id=self.tenant_id)
        profile_values = payload.model_dump(exclude_unset=True, include={"company", "designation", "phone", "signature"})
        for key, value in profile_values.items():
            setattr(account.profile, key, value)
        if payload.smtp_password:
            if account.credential is None:
                account.credential = ProviderCredential(tenant_id=self.tenant_id, encrypted_credential_ref="inline-encrypted", encryption_key_version="v1")
            account.credential.encrypted_credential_payload = CredentialStore(settings.encryption_key).encrypt({"password": payload.smtp_password})
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise SenderConflictError("A sender with this email already exists") from exc
        if account.provider == "SMTP" and (payload.smtp_host is not None or payload.smtp_port is not None or payload.smtp_tls_mode is not None or payload.smtp_username is not None or payload.smtp_password):
            _, authenticated = self.test_connection(account.id)
            if not authenticated:
                account.status = "REAUTH_REQUIRED"
                self.session.commit()
        return self._account(account.id)

    def create(self, payload: SenderCreate, created_by: UUID | None = None) -> tuple[EmailAccount, bool]:
        account = EmailAccount(
            tenant_id=self.tenant_id,
            provider=payload.provider,
            email=str(payload.email).lower(),
            display_name=payload.display_name,
            reply_to=str(payload.reply_to).lower() if payload.reply_to else None,
            timezone=payload.timezone,
            smtp_host=payload.smtp_host,
            smtp_port=payload.smtp_port,
            smtp_tls_mode=payload.smtp_tls_mode,
            smtp_username=str(payload.smtp_username).lower() if payload.smtp_username else None,
            status="CONNECTED",
            connection_status="CONNECTED",
            health_score=100,
            created_by=created_by,
        )
        profile = SenderProfile(
            tenant_id=self.tenant_id,
            company=payload.company,
            designation=payload.designation,
            phone=payload.phone,
            signature=payload.signature,
        )
        account.profile = profile
        if payload.encrypted_credential_ref:
            account.credential = ProviderCredential(
                tenant_id=self.tenant_id,
                encrypted_credential_ref=payload.encrypted_credential_ref,
                encryption_key_version=payload.encryption_key_version or "unknown",
                encrypted_credential_payload=CredentialStore(settings.encryption_key).encrypt({"password": payload.smtp_password}) if payload.smtp_password else None,
            )
        elif payload.smtp_password:
            account.credential = ProviderCredential(
                tenant_id=self.tenant_id,
                encrypted_credential_ref="inline-encrypted",
                encryption_key_version="v1",
                encrypted_credential_payload=CredentialStore(settings.encryption_key).encrypt({"password": payload.smtp_password}),
            )
        self.session.add(account)
        UsageService(self.session, self.tenant_id, created_by).meter(
            EVENT_SENDER_CONNECTED,
            resource_type="email_account",
            metadata={"email": str(payload.email).lower()},
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise SenderConflictError("A sender with this email already exists") from exc
        adapter = self.provider(account)
        try:
            adapter.connect()
            authenticated = adapter.authenticate()
        except SMTPProviderError:
            account.status = "REAUTH_REQUIRED"
            account.health_score = Decimal("50")
            self.session.commit()
            authenticated = False
        finally:
            adapter.disconnect()
        return self._account(account.id), authenticated

    def disconnect(self, sender_id: UUID) -> EmailAccount:
        account = self._account(sender_id)
        if account.status == "DISABLED":
            return account
        account.status = "DISCONNECTED"
        account.connection_status = "DISCONNECTED"
        AuditService(self.session, self.tenant_id).record(
            "SENDER_DISCONNECTED", "sender", account.id, {"email": account.email, "provider": account.provider}
        )
        self.session.commit()
        return account

    def enable(self, sender_id: UUID) -> tuple[EmailAccount, bool]:
        account = self._account(sender_id)
        if account.status == "DISCONNECTED":
            account.status = "CONNECTED"
            account.connection_status = "CONNECTED"
            AuditService(self.session, self.tenant_id).record(
                "SENDER_ENABLED", "sender", account.id, {"email": account.email, "provider": account.provider}
            )
            self.session.commit()
            return account, True
        adapter = self.provider(account)
        try:
            adapter.connect()
            authenticated = adapter.authenticate()
            account.status = "CONNECTED" if authenticated else "REAUTH_REQUIRED"
            account.connection_status = "CONNECTED" if authenticated else "REAUTH_REQUIRED"
            self.session.commit()
            return account, authenticated
        except Exception:
            account.status = "REAUTH_REQUIRED"
            account.connection_status = "REAUTH_REQUIRED"
            self.session.commit()
            return account, False

    def test_connection(self, sender_id: UUID) -> tuple[EmailAccount, bool]:
        account = self._account(sender_id)
        adapter = self.provider(account)
        verify = getattr(adapter, "verify", None)
        successful = False
        try:
            if callable(verify) and account.provider == "SMTP":
                stages = verify()
                successful = stages.get("auth", False) if stages.get("dns") and stages.get("tcp") else False
            else:
                adapter.connect()
                successful = adapter.authenticate()
        except Exception:
            successful = False
        finally:
            adapter.disconnect()
        account.status = "CONNECTED" if successful else "HEALTH_WARNING"
        AuditService(self.session, self.tenant_id).record(
            "SENDER_TEST_CONNECTION", "sender", account.id, {"email": account.email, "provider": account.provider, "successful": successful}
        )
        self.session.commit()
        return account, successful

    def send_test_email(self, sender_id: UUID, recipient: str) -> bool:
        account = self._account(sender_id)
        adapter = self.provider(account)
        adapter.connect()
        try:
            adapter.send(ProviderMessage(recipient, "CR+CRM test email", "<p>Test email from CR+CRM</p>", "Test email from CR+CRM", account.reply_to))
            self.mark_used(account)
            AuditService(self.session, self.tenant_id).record(
                "SENDER_TEST_EMAIL", "sender", account.id, {"email": account.email, "provider": account.provider, "recipient": recipient}
            )
            self.session.commit()
            return True
        except Exception:
            return False
        finally:
            adapter.disconnect()

    def delete(self, sender_id: UUID) -> None:
        account = self._account(sender_id)
        self.session.delete(account)
        self.session.commit()

    def health_check(self, sender_id: UUID) -> tuple[EmailAccount, bool]:
        account = self._account(sender_id)
        adapter = self.provider(account)
        adapter.connect()
        healthy = adapter.health_check()
        if account.status not in {"DISABLED", "DISCONNECTED", "SUSPENDED"}:
            account.status = "CONNECTED" if healthy else "HEALTH_WARNING"
            account.connection_status = "CONNECTED" if healthy else "REAUTH_REQUIRED"
            account.health_score = Decimal("100") if healthy else Decimal("50")
            self.session.commit()
        return account, healthy

    def provider(self, account: EmailAccount):
        profile = ProviderProfile(
            email=account.email,
            display_name=account.display_name,
            reply_to=account.reply_to,
            timezone=account.timezone,
        )
        if account.provider == "GOOGLE":
            credentials = self._google_credentials(account)
            if credentials is not None:
                return GmailProvider(profile, credentials)
            return MockEmailProvider("GOOGLE", profile)
        if account.provider == "MICROSOFT":
            ms_credentials = self._microsoft_credentials(account)
            if ms_credentials is not None:
                ms_access, ms_refresh, ms_client_id, ms_client_secret, ms_tenant_id = ms_credentials
                return MicrosoftGraphProvider(profile, ms_access, ms_refresh, ms_client_id, ms_client_secret, ms_tenant_id)
            return MockEmailProvider("MICROSOFT", profile)
        if account.provider == "SMTP":
            password = None
            if account.credential and account.credential.encrypted_credential_payload:
                password = str(CredentialStore(settings.encryption_key).decrypt(account.credential.encrypted_credential_payload).get("password", ""))
            return SMTPProvider(
                profile,
                account.smtp_host,
                account.smtp_port,
                account.smtp_tls_mode,
                account.smtp_username,
                password,
                allow_private_hosts=settings.smtp_allow_private_hosts,
                allow_plaintext_auth=settings.smtp_allow_plaintext_auth,
                allow_insecure_ports=settings.smtp_allow_insecure_ports,
            )
        return MockEmailProvider("FUTURE_ESP", profile)

    def _google_credentials(self, account: EmailAccount) -> Credentials | None:
        store = CredentialStore(settings.encryption_key)
        access_token = _decrypt_token(account.access_token_encrypted, store)
        refresh_token = _decrypt_refresh(account.refresh_token_encrypted, store)
        if access_token is not None or refresh_token is not None:
            return Credentials(
                token=access_token,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret,
                scopes=account.scopes or list(GMAIL_SCOPES),
            )
        if account.credential and account.credential.encrypted_credential_payload:
            payload = store.decrypt(account.credential.encrypted_credential_payload)
            return Credentials.from_authorized_user_info(payload)
        return None

    def _microsoft_credentials(self, account: EmailAccount) -> tuple[str, str, str, str, str] | None:
        store = CredentialStore(settings.encryption_key)
        access_token = _decrypt_token(account.access_token_encrypted, store)
        refresh_token = _decrypt_refresh(account.refresh_token_encrypted, store)
        if access_token is not None or refresh_token is not None:
            return (
                access_token or "",
                refresh_token or "",
                settings.microsoft_client_id,
                settings.microsoft_client_secret,
                settings.microsoft_tenant_id,
            )
        if account.credential and account.credential.encrypted_credential_payload:
            payload = store.decrypt(account.credential.encrypted_credential_payload)
            client_id = str(payload.get("client_id", settings.microsoft_client_id))
            client_secret = str(payload.get("client_secret", settings.microsoft_client_secret))
            return (
                str(payload.get("access_token", "")),
                str(payload.get("refresh_token", "")),
                client_id,
                client_secret,
                settings.microsoft_tenant_id,
            )
        return None

    def mark_used(self, account: EmailAccount) -> None:
        account.last_used_at = datetime.now(UTC)

    def refresh_tokens(self, account: EmailAccount) -> None:
        """Concurrency-safe access-token refresh, persisting encrypted tokens.

        Uses a DB row lock (SELECT ... FOR UPDATE) so concurrent refreshes for
        the same sender serialize. Also uses the `refresh_in_progress` column
        as a cross-process guard; if a refresh finished very recently the
        token is already fresh and we skip the network round-trip.
        """
        if account.provider not in {"GOOGLE", "MICROSOFT"}:
            return
        store = CredentialStore(settings.encryption_key)
        expires_at = account.token_expires_at
        now = datetime.now(UTC)
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at is not None and (expires_at - now) > REFRESH_SKEW:
            return
        if account.refresh_token_encrypted is None:
            account.connection_status = "REAUTH_REQUIRED"
            return
        if account.refresh_in_progress is not None and (now - account.refresh_in_progress) < REFRESH_LOCK_TIMEOUT:
            return
        account.refresh_in_progress = now
        self.session.commit()
        self.session.refresh(account)
        try:
            fresh = self.provider(account).refresh_credentials()
        except (SenderUnavailableError, RuntimeError):
            self._mark_reauth(account, store)
            return
        account.access_token_encrypted = _encrypt_token(fresh.access_token, store)
        if fresh.refresh_token:
            account.refresh_token_encrypted = _encrypt_token(fresh.refresh_token, store)
        account.token_expires_at = fresh.expires_at
        account.scopes = fresh.scopes or account.scopes
        account.last_used_at = now
        account.refresh_in_progress = None
        account.status = "CONNECTED" if account.status not in {"DISABLED", "DISCONNECTED", "SUSPENDED"} else account.status
        account.connection_status = "CONNECTED" if account.status not in {"DISABLED", "DISCONNECTED", "SUSPENDED"} else account.connection_status
        AuditService(self.session, self.tenant_id).record(
            "SENDER_TOKEN_REFRESHED", "sender", account.id, {"email": account.email, "provider": account.provider}
        )
        self.session.commit()

    def _mark_reauth(self, account: EmailAccount, store: CredentialStore) -> None:
        account.status = "REAUTH_REQUIRED"
        account.connection_status = "REAUTH_REQUIRED"
        account.refresh_in_progress = None
        AuditService(self.session, self.tenant_id).record(
            "SENDER_AUTH_FAILED", "sender", account.id, {"email": account.email, "provider": account.provider, "reason": "refresh_token_invalid"}
        )
        self.session.commit()
        self._notify_reauth(account)

    def _notify_reauth(self, account: EmailAccount) -> None:
        # Best-effort notification; never blocks the sending path and never
        # leaks tokens.
        try:
            from app.services.transactional_email import send_reauth_notification

            send_reauth_notification(account.email, account.display_name or account.email)
        except Exception:
            pass

    def reconnect(self, sender_id: UUID, user_id: UUID) -> str:
        """Return an authorization URL for re-authenticating an OAuth sender."""
        account = self._account(sender_id)
        if account.provider == "GOOGLE":
            from app.services.google_oauth import GoogleOAuthService

            return GoogleOAuthService(self.session, settings).authorization_url(
                user_id, self.tenant_id, sender_id=sender_id
            )
        if account.provider == "MICROSOFT":
            from app.services.microsoft_oauth import MicrosoftOAuthService

            return MicrosoftOAuthService(self.session, settings).authorization_url(
                user_id, self.tenant_id, sender_id=sender_id
            )
        if account.provider == "SMTP":
            raise SenderConnectionError("SMTP accounts are reconnected by updating their credentials")
        raise SenderConnectionError("This provider is not yet supported for reconnect")
