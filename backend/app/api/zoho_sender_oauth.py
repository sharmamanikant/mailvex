"""System B Zoho Mail sender OAuth + test-send API (Phase 10D).

* ``POST /senders/zoho/authorize`` — returns a Zoho consent URL bound
  to a sender connection via single-use OAuth state (AuthN via Clerk token).
* ``GET /senders/zoho/oauth/callback`` — the browser round-trip; identity
  is validated by the state, never by the query string.
* ``POST /senders/zoho/connections/{id}/validate`` — re-validate the
  connection against the Zoho Mail API (transitions to REAUTH_REQUIRED on
  auth failure).
* ``POST /senders/zoho/connections/{id}/disconnect`` — mark disconnected.
* ``POST /senders/zoho/connections/{id}/reconnect`` — start a fresh
  consent flow bound to the existing connection.
* ``POST /senders/zoho/connections/{id}/test-send`` — one explicit test
  message through the configured Zoho sender.
* ``GET /senders/zoho/connections/{id}`` — safe connection detail.

The callback route intentionally carries NO application auth: it is a browser
navigation from Zoho and is protected by the state store, its short TTL, and
the per-IP rate limit for ``/api/v1/senders/zoho/``.
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
    SenderTestResponse,
    ZohoAuthorizeRequest,
    ZohoAuthorizeResponse,
    ZohoConnectionDetailsResponse,
    ZohoTestSendRequest,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.integrations import (
    IntegrationNotFoundError,
    IntegrationService,
    IntegrationValidationError,
)
from app.services.zoho_sender_oauth import (
    ZohoOAuthError,
    ZohoSenderOAuthService,
)

logger = logging.getLogger("crcrm.zoho_oauth")

router = APIRouter(prefix="/senders", tags=["senders"])

CALLBACK_PAGE = "/integrations/zoho"


def _frontend_url() -> str:
    origin = (settings.allowed_origins[0] if settings.allowed_origins else "http://localhost:5173").rstrip("/")
    return origin


def _redirect(success: bool) -> RedirectResponse:
    target = _frontend_url() + CALLBACK_PAGE
    target += "?connected=1" if success else "?error=zoho_oauth_error"
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


@router.post("/zoho/authorize", response_model=ZohoAuthorizeResponse)
def authorize_zoho(
    payload: ZohoAuthorizeRequest,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> ZohoAuthorizeResponse:
    try:
        connection = IntegrationService(session, principal.tenant_id).get_connection(payload.connection_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    service = ZohoSenderOAuthService(session)
    try:
        url = service.authorization_url(connection, principal.user_id)
    except (ZohoOAuthError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return ZohoAuthorizeResponse(authorization_url=url)


@router.get("/zoho/oauth/callback")
def zoho_oauth_callback(
    code: str = Query(default="", min_length=1),
    state: str = Query(default="", min_length=1),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    if not code or not state:
        return _redirect(success=False)
    service = ZohoSenderOAuthService(session)
    try:
        service.complete(code=code, state=state)
    except (ZohoOAuthError, ValueError) as exc:
        logger.warning("zoho sender oauth callback rejected: %s", exc)
        return _redirect(success=False)
    except Exception:
        logger.exception("zoho sender oauth callback failed")
        return _redirect(success=False)
    return _redirect(success=True)


@router.get("/zoho/connections/{connection_id}", response_model=ZohoConnectionDetailsResponse)
def zoho_connection_details(
    connection_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.read")),
    session: Session = Depends(get_db),
) -> ZohoConnectionDetailsResponse:
    service = IntegrationService(session, principal.tenant_id)
    try:
        return ZohoConnectionDetailsResponse.model_validate(service.get_connection_details(connection_id))
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None


@router.post("/zoho/connections/{connection_id}/validate", response_model=ZohoConnectionDetailsResponse)
def zoho_validate(
    connection_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> ZohoConnectionDetailsResponse:
    service = IntegrationService(session, principal.tenant_id)
    try:
        service.validate_connection(connection_id, actor_id=principal.user_id)
        return ZohoConnectionDetailsResponse.model_validate(service.get_connection_details(connection_id))
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None


@router.post("/zoho/connections/{connection_id}/disconnect", response_model=ZohoConnectionDetailsResponse)
def zoho_disconnect(
    connection_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.disconnect")),
    session: Session = Depends(get_db),
) -> ZohoConnectionDetailsResponse:
    service = IntegrationService(session, principal.tenant_id)
    try:
        service.disconnect_connection(connection_id, actor_id=principal.user_id)
        return ZohoConnectionDetailsResponse.model_validate(service.get_connection_details(connection_id))
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None


@router.post("/zoho/connections/{connection_id}/reconnect", response_model=ZohoAuthorizeResponse)
def zoho_reconnect(
    connection_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> ZohoAuthorizeResponse:
    service = IntegrationService(session, principal.tenant_id)
    try:
        connection = service.get_connection(connection_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    if connection.provider.upper() != "ZOHO":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a Zoho connection") from None
    oauth = ZohoSenderOAuthService(session)
    try:
        url = oauth.authorization_url(connection, principal.user_id)
    except (ZohoOAuthError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return ZohoAuthorizeResponse(authorization_url=url)


@router.post("/zoho/connections/{connection_id}/test-send", response_model=SenderTestResponse)
def zoho_test_send(
    connection_id: UUID,
    payload: ZohoTestSendRequest,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> SenderTestResponse:
    service = IntegrationService(session, principal.tenant_id)
    try:
        connection = service.get_connection(connection_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    if connection.provider.upper() != "ZOHO":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a Zoho connection") from None
    if connection.status != "CONNECTED":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Sender must be connected before test-send") from None
    account = service.get_sender_account_by_connection(connection_id)
    try:
        result = service.send_test_email(account.id, str(payload.recipient), actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    except EmailProviderError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(status_code=_status_for(exc), detail=_safe_message(exc), headers=headers) from None
    except IntegrationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return SenderTestResponse(
        sender_id=account.id,
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
    if exc.code in (ProviderErrorCode.INVALID_RECIPIENT, ProviderErrorCode.MESSAGE_REJECTED):
        return status.HTTP_400_BAD_REQUEST
    return status.HTTP_502_BAD_GATEWAY


def _safe_message(exc: EmailProviderError) -> str:
    # Normalized provider errors already carry safe, user-facing messages.
    return exc.message