from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt

from app.core.config import Settings


class InvalidTokenError(ValueError):
    pass


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create_access_token(self, user_id: UUID, tenant_id: UUID, roles: list[str]) -> tuple[str, datetime]:
        expires_at = datetime.now(UTC) + timedelta(minutes=self.settings.access_token_minutes)
        payload = {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "roles": roles,
            "type": "access",
            "jti": str(uuid4()),
            "iat": datetime.now(UTC),
            "exp": expires_at,
        }
        return jwt.encode(payload, self.settings.jwt_secret, algorithm="HS256"), expires_at

    def create_refresh_token(self) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(48)
        expires_at = datetime.now(UTC) + timedelta(days=self.settings.refresh_token_days)
        return token, expires_at

    def decode_access_token(self, token: str) -> dict[str, object]:
        try:
            payload = jwt.decode(token, self.settings.jwt_secret, algorithms=["HS256"], options={"require": ["sub", "tenant_id", "type", "exp"]})
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("Invalid or expired access token") from exc
        if payload.get("type") != "access":
            raise InvalidTokenError("Invalid token type")
        return payload
