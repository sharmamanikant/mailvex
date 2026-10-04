"""Structured, correlation-first logging.

Every emitted record is one line of JSON with a ``ts``, ``level``, ``logger``,
and ``message`` plus the ambient request context (``request_id``,
``tenant_id``, ``user_id``). Secrets are never logged: any key that looks like
a credential is replaced with ``[REDACTED]``.
"""

from __future__ import annotations

import contextvars
import json
import logging
from typing import Any
from uuid import uuid4

_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
_TENANT_ID: contextvars.ContextVar[str] = contextvars.ContextVar("tenant_id", default="")
_USER_ID: contextvars.ContextVar[str] = contextvars.ContextVar("user_id", default="")

_SENSITIVE_KEYS = {
    "password", "password_hash", "access_token", "refresh_token", "token",
    "secret", "client_secret", "api_key", "authorization", "smtp_password",
    "authorization_code", "signature",
}

# Extra log-record attributes we copy into the structured payload verbatim.
_EXTRA_FIELDS = (
    "method", "path", "route", "status", "duration_ms", "event",
    "metric", "value", "rule", "severity", "job_id", "campaign_id",
    "provider", "failure_code", "attempt_count", "tenant_id",
)

# Fields copied from structured logger input dicts (fill_log helper).
_LOG_FIELDS = (
    "event",
    "tenant_id",
    "user_id",
    "job_id",
    "campaign_id",
    "sender_id",
    "health_check_id",
    "check_type",
    "domain",
    "provider",
    "failure_code",
    "attempt_count",
)


def current_request_id() -> str:
    return _REQUEST_ID.get()


def current_context() -> dict[str, str]:
    return {
        "request_id": _REQUEST_ID.get(),
        "tenant_id": _TENANT_ID.get(),
        "user_id": _USER_ID.get(),
    }


def redact(value: Any, key: str | None = None, seen: set[int] | None = None) -> Any:
    """Deep-copy ``value`` replacing any sensitive field with ``[REDACTED]``."""
    if seen is None:
        seen = set()
    if id(value) in seen:
        return None
    seen.add(id(value))
    if key is not None and any(part in str(key).lower() for part in _SENSITIVE_KEYS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): redact(item_value, str(item_key), seen) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, None, seen) for item in value]
    return value


class RequestContext:
    """Scoped request context so services/deps log the same correlation id."""

    def __init__(self, request_id: str | None = None, tenant_id: object | None = None, user_id: object | None = None) -> None:
        self._request_id = request_id or str(uuid4())
        self._tenant_id = str(tenant_id) if tenant_id is not None else ""
        self._user_id = str(user_id) if user_id is not None else ""
        self._tokens: list[contextvars.Token[Any]] = []

    def __enter__(self) -> RequestContext:
        self._tokens = [
            _REQUEST_ID.set(self._request_id),
            _TENANT_ID.set(self._tenant_id),
            _USER_ID.set(self._user_id),
        ]
        return self

    def __exit__(self, *_exc: object) -> None:
        for token in self._tokens:
            if token.var is _REQUEST_ID:
                _REQUEST_ID.reset(token)
            elif token.var is _TENANT_ID:
                _TENANT_ID.reset(token)
            elif token.var is _USER_ID:
                _USER_ID.reset(token)


class StructuredFormatter(logging.Formatter):
    """Single-line JSON formatter with request context and redaction."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = current_context()
        if context["request_id"]:
            payload["request_id"] = context["request_id"]
        if context["tenant_id"]:
            payload["tenant_id"] = context["tenant_id"]
        if context["user_id"]:
            payload["user_id"] = context["user_id"]
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value not in (None, ""):
                payload[field] = value
        for field in _LOG_FIELDS:
            value = getattr(record, field, None)
            if value not in (None, ""):
                payload[field] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: int = logging.INFO) -> None:
    """Replace root logging with the structured handler (idempotent-ish)."""
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)


def log_event(event: str, **fields: Any) -> None:
    """Emit a structured event line carrying safe contextual fields.

    The event name becomes the log ``message`` so it is grep-able; the
    context (tenant/user/job/campaign/provider/... keys) rides along as JSON
    fields. Values are redacted before they can reach the record.
    """
    safe = redact(fields)
    logging.getLogger("crcrm.ops").info(
        event,
        extra={key: value for key, value in safe.items() if key in _LOG_FIELDS},
    )