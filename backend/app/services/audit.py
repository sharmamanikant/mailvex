from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.logging import current_request_id
from app.models import AuditLog

_SENSITIVE_KEYS = {
    "password", "password_hash", "access_token", "refresh_token", "token",
    "secret", "client_secret", "api_key", "authorization", "smtp_password",
}

# Phase 8 #5: canonical outbound-send audit actions. The send engine emits
# exactly these; ``_safe_metadata`` enforces that no credentials / provider
# tokens / API keys can ever reach the audit log (none are stored on the
# ``OutboundMessage`` model at all, so nothing here can leak provider data).
EMAIL_SEND_QUEUED = "EMAIL_SEND_QUEUED"
EMAIL_SEND_PROCESSING = "EMAIL_SEND_PROCESSING"
EMAIL_SEND_RETRYING = "EMAIL_SEND_RETRYING"
EMAIL_SEND_SENT = "EMAIL_SEND_SENT"
EMAIL_SEND_FAILED = "EMAIL_SEND_FAILED"
EMAIL_SEND_CANCELLED = "EMAIL_SEND_CANCELLED"


def _safe_metadata(value: Any, key: str | None = None) -> Any:
    """Ensure audit records cannot become an alternate credential store."""
    if key and any(part in key.lower() for part in _SENSITIVE_KEYS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _safe_metadata(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_safe_metadata(item) for item in value]
    return value


class AuditService:
    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None, request_id: str | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.request_id = request_id

    def record(self, action: str, resource_type: str, resource_id: UUID | None = None, metadata: dict[str, Any] | None = None) -> AuditLog:
        event = AuditLog(
            tenant_id=self.tenant_id,
            actor_id=self.actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=self.request_id or current_request_id() or None,
            audit_metadata=_safe_metadata(metadata or {}),
        )
        self.session.add(event)
        return event
