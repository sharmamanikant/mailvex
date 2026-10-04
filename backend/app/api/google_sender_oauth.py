"""System B Google sender OAuth + test-send API (Phase 10B).

* ``POST /senders/google/authorize`` — returns a Google consent URL bound to a
  sender connection via single-use OAuth state (AuthN via Clerk token).
* ``GET /senders/google/oauth/callback`` — the browser round-trip; identity is
  validated by the state, never by the query string.
* ``POST /senders/{sender_id}/test`` — sends one explicit test message through
  the configured provider (spec item 18).

The callback route intentionally carries NO application auth: it is a browser
navigation from Google and is protected by the state store, its short TTL, and
the per-IP rate limit for ``/api/v1/senders/google/``.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.email_providers.base import EmailProviderError
from app.schemas.integrations import (
    GoogleAuthorizeRequest,
    GoogleAuthorizeResponse,
    SenderTestRequest,
    SenderTestResponse,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.google_sender_oauth import GoogleOAuthError, GoogleSenderOAuthService
from app.services.integrations import (
    IntegrationNotFoundError,
    IntegrationService,
    IntegrationValidationError,
)

logger = logging.getLogger("crcrm.google_oauth")

router = APIRouter(prefix="/senders", tags=["senders"])

CALLBACK_PAGE = "/integrations/google"


def _frontend_url() -> str:
    origin = (settings.allowed_origins[0] if settings.allowed_origins else "http://localhost:5173").rstrip("/")
    return origin


def _redirect(success: bool) -> RedirectResponse:
    target = _frontend_url() + CALLBACK_PAGE
    target += "?connected=1" if success else "?error=google_oauth_error"
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


@router.post("/google/authorize", response_model=GoogleAuthorizeResponse)
def authorize_google(
    payload: GoogleAuthorizeRequest,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> GoogleAuthorizeResponse:
    integration = IntegrationService(session, principal.tenant_id)
    try:
        connection = integration.get_connection(payload.connection_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    if connection.provider.upper() != "GOOGLE":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a Google connection") from None
    if payload.inbox_access and not (connection.connection_metadata or {}).get("inbox_access"):
        connection.connection_metadata = {
            **(connection.connection_metadata or {}),
            "inbox_access": True,
        }
        session.commit()
    service = GoogleSenderOAuthService(session)
    try:
        url = service.authorization_url(connection, principal.user_id)
    except (GoogleOAuthError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return GoogleAuthorizeResponse(authorization_url=url)


@router.get("/google/oauth/callback")
def google_oauth_callback(
    code: str = Query(default="", min_length=1),
    state: str = Query(default="", min_length=1),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    if not code or not state:
        return _redirect(success=False)
    service = GoogleSenderOAuthService(session)
    try:
        service.complete(code=code, state=state)
    except (GoogleOAuthError, ValueError) as exc:
        logger.warning("google sender oauth callback rejected: %s", exc)
        return _redirect(success=False)
    except Exception:
        logger.exception("google sender oauth callback failed")
        return _redirect(success=False)
    return _redirect(success=True)


@router.post("/{sender_id}/test", response_model=SenderTestResponse)
def send_test(
    sender_id: UUID,
    payload: SenderTestRequest,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> SenderTestResponse:
    service = IntegrationService(session, principal.tenant_id)
    try:
        account = service.get_sender_account(sender_id)
        connection = service.get_connection(account.connection_id)
        result = service.send_test_email(sender_id, str(payload.recipient), actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    except EmailProviderError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(status_code=_status_for(exc), detail=exc.message, headers=headers) from None
    except IntegrationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return SenderTestResponse(
        sender_id=sender_id,
        connection_id=connection.id,
        provider=connection.provider,
        from_email=connection.email or account.email,
        recipient=payload.recipient,
        message_id=result.message_id,
        sent_at=result.sent_at,
    )


def _status_for(exc: EmailProviderError) -> int:
    from app.email_providers.base import ProviderErrorCode

    if exc.code in (ProviderErrorCode.AUTH_REQUIRED, ProviderErrorCode.AUTH_FAILED, ProviderErrorCode.TOKEN_EXPIRED):
        return status.HTTP_401_UNAUTHORIZED
    if exc.code == ProviderErrorCode.RATE_LIMITED:
        return status.HTTP_429_TOO_MANY_REQUESTS
    if exc.code == ProviderErrorCode.INVALID_RECIPIENT or exc.code == ProviderErrorCode.MESSAGE_REJECTED:
        return status.HTTP_400_BAD_REQUEST
    return status.HTTP_502_BAD_GATEWAY