"""EmailAccount sender center (relocated from the legacy /senders API).

Phase 3 repurposed ``/api/v1/senders`` for the new Mailbox-bound Sender
entity. The previous EmailAccount-based sender center now lives here under
``/email-senders`` unchanged, preserving its endpoints (list/create/patch/
detail/disconnect/health/test/enable/reconnect/delete) plus the legacy
Google/Microsoft connect+callback helpers used by the existing sender
integration flows.

The provider OAuth routers (``google_sender_oauth`` / ``microsoft_sender_oauth``
/ ``zoho_sender_oauth``) stay at ``/senders`` with their distinct sub-paths.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import EmailAccount
from app.schemas.senders import (
    ReconnectResponse,
    SenderConnectionResponse,
    SenderCreate,
    SenderHealthResponse,
    SenderOperationResponse,
    SenderResponse,
    SenderUpdate,
    TestEmailRequest,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.google_oauth import GoogleOAuthError, GoogleOAuthService
from app.services.health import SenderHealthService
from app.services.microsoft_oauth import MicrosoftOAuthError, MicrosoftOAuthService
from app.services.senders import (
    SenderConflictError,
    SenderConnectionError,
    SenderNotFoundError,
    SenderService,
)

router = APIRouter(prefix="/email-senders", tags=["email-senders"])


def serialize_sender(account: EmailAccount) -> SenderResponse:
    profile = account.profile
    return SenderResponse(
        id=account.id,
        tenant_id=account.tenant_id,
        email=account.email,
        display_name=account.display_name,
        reply_to=account.reply_to,
        provider=account.provider,
        status=account.status,
        connection_status=account.connection_status,
        health_score=account.health_score,
        timezone=account.timezone,
        smtp_host=account.smtp_host,
        smtp_port=account.smtp_port,
        smtp_tls_mode=account.smtp_tls_mode,
        smtp_username=account.smtp_username,
        company=profile.company if profile else None,
        designation=profile.designation if profile else None,
        phone=profile.phone if profile else None,
        signature=profile.signature if profile else None,
        last_connected_at=account.last_connected_at,
        last_used_at=account.last_used_at,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def service(session: Session, principal: TenantPrincipal) -> SenderService:
    return SenderService(session, principal.tenant_id)


@router.get("/google/connect")
def connect_google(principal: TenantPrincipal = Depends(require_permission("senders.connect"))) -> dict[str, str]:
    from app.core.config import settings

    try:
        url = GoogleOAuthService(None, settings).authorization_url(principal.user_id, principal.tenant_id)
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None
    return {"authorization_url": url}


@router.get("/google/callback")
def google_callback(code: str = Query(..., min_length=1), state: str = Query(..., min_length=1), session: Session = Depends(get_db)) -> RedirectResponse:
    from app.core.config import settings

    try:
        GoogleOAuthService(session, settings).complete(code, state)
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return RedirectResponse(url="/email-senders?connected=google", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/microsoft/connect")
def connect_microsoft(principal: TenantPrincipal = Depends(require_permission("senders.connect"))) -> dict[str, str]:
    from app.core.config import settings

    try:
        url = MicrosoftOAuthService(None, settings).authorization_url(principal.user_id, principal.tenant_id)
    except MicrosoftOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None
    return {"authorization_url": url}


@router.get("/microsoft/callback")
def microsoft_callback(code: str = Query(..., min_length=1), state: str = Query(..., min_length=1), session: Session = Depends(get_db)) -> RedirectResponse:
    from app.core.config import settings

    try:
        MicrosoftOAuthService(session, settings).complete(code, state)
    except MicrosoftOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return RedirectResponse(url="/email-senders?connected=microsoft", status_code=status.HTTP_303_SEE_OTHER)


@router.get("", response_model=list[SenderResponse])
def get_senders(principal: TenantPrincipal = Depends(require_permission("senders.read")), session: Session = Depends(get_db)) -> list[SenderResponse]:
    return [serialize_sender(item) for item in service(session, principal).list()]


@router.post("", response_model=SenderConnectionResponse, status_code=status.HTTP_201_CREATED)
def create_sender(payload: SenderCreate, principal: TenantPrincipal = Depends(require_permission("senders.connect")), session: Session = Depends(get_db)) -> SenderConnectionResponse:
    from app.services.audit import AuditService

    try:
        account, authenticated = service(session, principal).create(payload, created_by=principal.user_id)
        AuditService(session, principal.tenant_id).record(
            "SENDER_CREATED", "sender", account.id, {"email": account.email, "provider": account.provider}
        )
    except SenderConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return SenderConnectionResponse(sender=serialize_sender(account), provider_authenticated=authenticated)


@router.patch("/{sender_id}", response_model=SenderResponse)
def update_sender(sender_id: UUID, payload: SenderUpdate, principal: TenantPrincipal = Depends(require_permission("senders.connect")), session: Session = Depends(get_db)) -> SenderResponse:
    try:
        return serialize_sender(service(session, principal).update(sender_id, payload))
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    except SenderConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.get("/{sender_id}", response_model=SenderResponse)
def get_sender(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("senders.read")), session: Session = Depends(get_db)) -> SenderResponse:
    try:
        return serialize_sender(service(session, principal).get(sender_id))
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None


@router.post("/{sender_id}/disconnect", response_model=SenderResponse)
def disconnect_sender(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("senders.disconnect")), session: Session = Depends(get_db)) -> SenderResponse:
    try:
        return serialize_sender(service(session, principal).disconnect(sender_id))
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None


@router.get("/{sender_id}/health")
def get_sender_health(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("senders.read")), session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        service = SenderHealthService(session, principal.tenant_id)
        result = service.evaluate(sender_id)
        result["history"] = service.history(sender_id)
        return result
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None


@router.post("/{sender_id}/health-check", response_model=SenderHealthResponse)
def check_sender_health(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("senders.read")), session: Session = Depends(get_db)) -> SenderHealthResponse:
    try:
        account, healthy = service(session, principal).health_check(sender_id)
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    return SenderHealthResponse(sender_id=account.id, status=account.status, health_score=account.health_score, healthy=healthy)


@router.post("/{sender_id}/test-connection", response_model=SenderOperationResponse)
def test_connection(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("senders.connect")), session: Session = Depends(get_db)) -> SenderOperationResponse:
    try:
        account, successful = service(session, principal).test_connection(sender_id)
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    return SenderOperationResponse(sender_id=account.id, successful=successful, message="SMTP connection verified" if successful else "SMTP connection failed")


@router.post("/{sender_id}/test-email", response_model=SenderOperationResponse)
def send_test_email(sender_id: UUID, payload: TestEmailRequest, principal: TenantPrincipal = Depends(require_permission("senders.connect")), session: Session = Depends(get_db)) -> SenderOperationResponse:
    try:
        successful = service(session, principal).send_test_email(sender_id, str(payload.recipient))
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    except Exception:
        successful = False
    return SenderOperationResponse(sender_id=sender_id, successful=successful, message="Test email accepted" if successful else "Test email failed")


@router.post("/{sender_id}/enable", response_model=SenderConnectionResponse)
def enable_sender(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("senders.connect")), session: Session = Depends(get_db)) -> SenderConnectionResponse:
    try:
        account, authenticated = service(session, principal).enable(sender_id)
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    return SenderConnectionResponse(sender=serialize_sender(account), provider_authenticated=authenticated)


@router.post("/{sender_id}/reconnect", response_model=ReconnectResponse)
def reconnect_sender(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("senders.connect")), session: Session = Depends(get_db)) -> ReconnectResponse:
    try:
        account = service(session, principal).get(sender_id)
        authorization_url = service(session, principal).reconnect(sender_id, principal.user_id)
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    except SenderConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return ReconnectResponse(sender_id=sender_id, provider=account.provider, authorization_url=authorization_url)


@router.delete("/{sender_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sender(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("senders.disconnect")), session: Session = Depends(get_db)) -> Response:
    try:
        service(session, principal).delete(sender_id)
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)