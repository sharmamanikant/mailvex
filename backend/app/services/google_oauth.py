from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar, cast
from uuid import UUID

import jwt
from google_auth_oauthlib.flow import Flow
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.billing import EVENT_SENDER_CONNECTED, UsageService
from app.core.config import Settings
from app.models import EmailAccount, SenderProfile
from app.providers.base import ProviderProfile
from app.providers.gmail import GMAIL_SCOPES, GmailProvider
from app.security.credential_store import CredentialStore

STATE_TTL_MINUTES = 10


class GoogleOAuthError(ValueError):
    pass


class GoogleOAuthService:
    """Google Workspace / Gmail OAuth flow.

    Tokens are encrypted and stored in dedicated EmailAccount columns
    (access_token_encrypted / refresh_token_encrypted). The OAuth state is a
    signed JWT bound to the initiating user and tenant, single-use, and
    expires after STATE_TTL_MINUTES — providing CSRF mitigation. This service
    never stores or collects Google passwords.
    """

    _used_states: ClassVar[dict[str, datetime]] = {}

    def __init__(self, session: Session | None, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.credentials = CredentialStore(settings.encryption_key)

    def _prune_expired_states(self) -> None:
        now = datetime.now(UTC)
        expires_before = now - timedelta(days=1)
        expired = [nonce for nonce, expires_at in self._used_states.items() if expires_at <= expires_before]
        for nonce in expired:
            self._used_states.pop(nonce, None)

    def _client_config(self) -> dict[str, Any]:
        if not self.settings.google_client_id or not self.settings.google_client_secret:
            raise GoogleOAuthError("Google OAuth is not configured")
        return {"web": {"client_id": self.settings.google_client_id, "client_secret": self.settings.google_client_secret, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [self.settings.google_redirect_uri]}}

    def authorization_url(self, user_id: UUID, tenant_id: UUID, sender_id: UUID | None = None) -> str:
        self._prune_expired_states()
        nonce = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(minutes=STATE_TTL_MINUTES)
        claims: dict[str, Any] = {
            "type": "google_oauth",
            "nonce": nonce,
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "sender_id": str(sender_id) if sender_id else None,
            "exp": expires_at,
        }
        state = jwt.encode(claims, self.settings.jwt_secret, algorithm="HS256")
        self._used_states[nonce] = expires_at
        flow = Flow.from_client_config(self._client_config(), scopes=list(GMAIL_SCOPES), state=state)
        flow.redirect_uri = self.settings.google_redirect_uri
        url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
        return cast(str, url)

    def complete(self, code: str, state: str) -> EmailAccount:
        try:
            claims = jwt.decode(state, self.settings.jwt_secret, algorithms=["HS256"], options={"require": ["type", "nonce", "sub", "tenant_id", "exp"]})
        except jwt.PyJWTError as exc:
            raise GoogleOAuthError("Invalid OAuth state") from exc
        nonce = str(claims["nonce"])
        self._prune_expired_states()
        if claims.get("type") != "google_oauth" or nonce not in self._used_states:
            raise GoogleOAuthError("Invalid or already-used OAuth state")
        self._used_states.pop(nonce, None)
        tenant_id = UUID(str(claims["tenant_id"]))
        user_id = UUID(str(claims["sub"]))
        sender_id = UUID(str(claims["sender_id"])) if claims.get("sender_id") else None
        flow = Flow.from_client_config(self._client_config(), scopes=list(GMAIL_SCOPES), state=state)
        flow.redirect_uri = self.settings.google_redirect_uri
        try:
            flow.fetch_token(code=code)
            google = GmailProvider(ProviderProfile(email="pending"), flow.credentials)
            google.connect()
            profile = google.get_profile()
            provider_account_id = str(profile.email or "").lower()
        except Exception as exc:
            raise GoogleOAuthError("Google authorization could not be completed") from exc
        now = datetime.now(UTC)
        if sender_id is not None:
            account = self._find_sender(sender_id, tenant_id)
            if account is None:
                raise GoogleOAuthError("The sender no longer exists; please reconnect fresh")
            # Reconnect must target the same Google account.
            if account.email.lower() != profile.email.lower():
                raise GoogleOAuthError("The reconnected Google account does not match this sender")
            account.display_name = profile.display_name
            account.reply_to = profile.reply_to
            account.timezone = profile.timezone
            account.status = "CONNECTED"
            account.connection_status = "CONNECTED"
            account.oauth_provider_account_id = provider_account_id
            account.access_token_encrypted = self.credentials.encrypt({"token": flow.credentials.token, "client_id": flow.credentials.client_id, "client_secret": flow.credentials.client_secret, "token_uri": flow.credentials.token_uri, "scopes": list(flow.credentials.scopes or GMAIL_SCOPES)})
            account.refresh_token_encrypted = self.credentials.encrypt({"refresh_token": flow.credentials.refresh_token}) if flow.credentials.refresh_token else None
            account.token_expires_at = flow.credentials.expiry
            account.scopes = list(flow.credentials.scopes or GMAIL_SCOPES)
            account.last_connected_at = now
            account.health_score = Decimal("100")
        else:
            if self.session is None:
                raise GoogleOAuthError("Database service unavailable")
            existing = self._find_by_email(profile.email.lower(), tenant_id)
            if existing is not None:
                raise GoogleOAuthError("A sender with this Google account already exists; use reconnect to refresh it")
            account = EmailAccount(
                tenant_id=tenant_id,
                provider="GOOGLE",
                email=profile.email.lower(),
                display_name=profile.display_name,
                reply_to=profile.reply_to,
                timezone=profile.timezone,
                status="CONNECTED",
                connection_status="CONNECTED",
                oauth_provider_account_id=provider_account_id,
                access_token_encrypted=self.credentials.encrypt({"token": flow.credentials.token, "client_id": flow.credentials.client_id, "client_secret": flow.credentials.client_secret, "token_uri": flow.credentials.token_uri, "scopes": list(flow.credentials.scopes or GMAIL_SCOPES)}),
                refresh_token_encrypted=self.credentials.encrypt({"refresh_token": flow.credentials.refresh_token}) if flow.credentials.refresh_token else None,
                token_expires_at=flow.credentials.expiry,
                scopes=list(flow.credentials.scopes or GMAIL_SCOPES),
                last_connected_at=now,
                created_by=user_id,
                health_score=100,
            )
            account.profile = SenderProfile(tenant_id=tenant_id)
            self.session.add(account)
            UsageService(self.session, tenant_id, user_id).meter(
                EVENT_SENDER_CONNECTED,
                resource_type="email_account",
                metadata={"email": account.email, "provider": "GOOGLE"},
            )
        if self.session is None:
            raise GoogleOAuthError("Database service unavailable")
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise GoogleOAuthError("A sender with this Google account already exists") from exc
        return account

    def _find_sender(self, sender_id: UUID, tenant_id: UUID) -> EmailAccount | None:
        if self.session is None:
            return None
        account = self.session.get(EmailAccount, sender_id)
        if account is not None and account.tenant_id != tenant_id:
            return None
        return account

    def _find_by_email(self, email: str, tenant_id: UUID) -> EmailAccount | None:
        if self.session is None:
            return None
        return self.session.scalar(
            select(EmailAccount).where(EmailAccount.email == email, EmailAccount.tenant_id == tenant_id)
        )
