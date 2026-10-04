from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID

import jwt
import requests
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.billing import EVENT_SENDER_CONNECTED, UsageService
from app.core.config import Settings
from app.models import EmailAccount, SenderProfile
from app.providers.base import ProviderProfile
from app.providers.microsoft import MicrosoftGraphProvider
from app.security.credential_store import CredentialStore

GRAPH_SCOPES = ["openid", "profile", "email", "offline_access", "User.Read", "Mail.Send"]
STATE_TTL_MINUTES = 10


class MicrosoftOAuthError(ValueError):
    pass


class MicrosoftOAuthService:
    """Microsoft 365 / Exchange Online OAuth flow.

    Tokens are encrypted and stored in dedicated EmailAccount columns
    (access_token_encrypted / refresh_token_encrypted). The OAuth state is a
    signed JWT bound to the initiating user and tenant, single-use, and expires
    after STATE_TTL_MINUTES — providing CSRF mitigation. This service never
    collects or stores Microsoft account passwords and uses the minimum
    required Graph permissions.
    """

    _used_states: ClassVar[dict[str, datetime]] = {}

    def __init__(self, session: Session | None, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.credentials = CredentialStore(settings.encryption_key)

    def _prune_expired_states(self) -> None:
        now = datetime.now(UTC)
        expired_before = now - timedelta(days=1)
        expired = [nonce for nonce, expires_at in self._used_states.items() if expires_at <= expired_before]
        for nonce in expired:
            self._used_states.pop(nonce, None)

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.settings.microsoft_tenant_id}"

    def _require_configured(self) -> None:
        if not self.settings.microsoft_client_id or not self.settings.microsoft_client_secret:
            raise MicrosoftOAuthError("Microsoft OAuth is not configured")

    def authorization_url(self, user_id: UUID, tenant_id: UUID, sender_id: UUID | None = None) -> str:
        self._require_configured()
        self._prune_expired_states()
        nonce = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(minutes=STATE_TTL_MINUTES)
        claims: dict[str, Any] = {
            "type": "microsoft_oauth",
            "nonce": nonce,
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "sender_id": str(sender_id) if sender_id else None,
            "exp": expires_at,
        }
        state = jwt.encode(claims, self.settings.jwt_secret, algorithm="HS256")
        self._used_states[nonce] = expires_at
        from urllib.parse import urlencode

        query = urlencode(
            {
                "client_id": self.settings.microsoft_client_id,
                "response_type": "code",
                "redirect_uri": self.settings.microsoft_redirect_uri,
                "response_mode": "query",
                "scope": " ".join(GRAPH_SCOPES),
                "state": state,
                "prompt": "login consent",
            }
        )
        return f"{self.authority}/oauth2/v2.0/authorize?{query}"

    def _complete_token_exchange(self, code: str) -> tuple[dict[str, Any], Any]:
        response = requests.post(
            f"{self.authority}/oauth2/v2.0/token",
            data={
                "client_id": self.settings.microsoft_client_id,
                "client_secret": self.settings.microsoft_client_secret,
                "code": code,
                "redirect_uri": self.settings.microsoft_redirect_uri,
                "grant_type": "authorization_code",
                "scope": " ".join(GRAPH_SCOPES),
            },
            timeout=15,
        )
        response.raise_for_status()
        token_data = response.json()
        access_token = str(token_data["access_token"])
        graph = MicrosoftGraphProvider(ProviderProfile("pending"), access_token)
        graph.connect()
        profile = graph.get_profile()
        graph.disconnect()
        return token_data, profile

    def complete(self, code: str, state: str) -> EmailAccount:
        try:
            claims = jwt.decode(state, self.settings.jwt_secret, algorithms=["HS256"], options={"require": ["type", "nonce", "sub", "tenant_id", "exp"]})
        except jwt.PyJWTError as exc:
            raise MicrosoftOAuthError("Invalid OAuth state") from exc
        nonce = str(claims["nonce"])
        self._prune_expired_states()
        if claims.get("type") != "microsoft_oauth" or nonce not in self._used_states:
            raise MicrosoftOAuthError("Invalid or already-used OAuth state")
        self._used_states.pop(nonce, None)
        tenant_id = UUID(str(claims["tenant_id"]))
        user_id = UUID(str(claims["sub"]))
        sender_id = UUID(str(claims["sender_id"])) if claims.get("sender_id") else None
        self._require_configured()
        try:
            token_data, profile = self._complete_token_exchange(code)
        except (requests.RequestException, KeyError, RuntimeError, ValueError) as exc:
            raise MicrosoftOAuthError("Microsoft authorization could not be completed") from exc
        provider_account_id = str(profile.email or "").lower()
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=int(token_data.get("expires_in", 3600)))
        encrypted_access = self.credentials.encrypt({"value": token_data.get("access_token")})
        encrypted_refresh = self.credentials.encrypt({"refresh_token": token_data.get("refresh_token")}) if token_data.get("refresh_token") else None
        if sender_id is not None:
            account = self._find_sender(sender_id, tenant_id)
            if account is None:
                raise MicrosoftOAuthError("The sender no longer exists; please reconnect fresh")
            if account.email.lower() != profile.email.lower():
                raise MicrosoftOAuthError("The reconnected Microsoft account does not match this sender")
            account.display_name = profile.display_name
            account.reply_to = profile.reply_to
            account.timezone = profile.timezone
            account.status = "CONNECTED"
            account.connection_status = "CONNECTED"
            account.oauth_provider_account_id = provider_account_id
            account.access_token_encrypted = encrypted_access
            account.refresh_token_encrypted = encrypted_refresh
            account.token_expires_at = expires_at
            account.scopes = list(GRAPH_SCOPES)
            account.last_connected_at = now
            account.health_score = Decimal("100")
        else:
            if self.session is None:
                raise MicrosoftOAuthError("Database service unavailable")
            existing = self._find_by_email(profile.email.lower(), tenant_id)
            if existing is not None:
                raise MicrosoftOAuthError("A sender with this Microsoft account already exists; use reconnect to refresh it")
            account = EmailAccount(
                tenant_id=tenant_id,
                provider="MICROSOFT",
                email=profile.email.lower(),
                display_name=profile.display_name,
                reply_to=profile.reply_to,
                timezone=profile.timezone,
                status="CONNECTED",
                connection_status="CONNECTED",
                oauth_provider_account_id=provider_account_id,
                access_token_encrypted=encrypted_access,
                refresh_token_encrypted=encrypted_refresh,
                token_expires_at=expires_at,
                scopes=list(GRAPH_SCOPES),
                last_connected_at=now,
                created_by=user_id,
                health_score=100,
            )
            account.profile = SenderProfile(tenant_id=tenant_id)
            self.session.add(account)
            UsageService(self.session, tenant_id, user_id).meter(
                EVENT_SENDER_CONNECTED,
                resource_type="email_account",
                metadata={"email": account.email, "provider": "MICROSOFT"},
            )
        if self.session is None:
            raise MicrosoftOAuthError("Database service unavailable")
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise MicrosoftOAuthError("A sender with this Microsoft account already exists") from exc
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
