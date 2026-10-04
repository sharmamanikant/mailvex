"""Integrations API — email-provider foundation (System B).

Endpoints manage provider connections, encrypted credentials, sender
discovery and sender accounts. Credentials are accepted once and never
returned; responses expose only a ``credential_configured`` flag.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.integrations import (
    CredentialStatusResponse,
    CredentialUpload,
    DiscoveryResponse,
    IntegrationCreate,
    ProviderCapabilityResponse,
    ReplySyncResponse,
    SenderAccountCreate,
    SenderAccountResponse,
    SenderAccountUpdate,
    SenderConnectionResponse,
    SenderImportRequest,
    SenderImportResponse,
    WarmupSettingsResponse,
    WarmupSettingsUpdate,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.integrations import (
    IntegrationConflictError,
    IntegrationNotFoundError,
    IntegrationService,
    IntegrationValidationError,
    serialize_connection,
)
from app.services.reply_sync import ReplySyncUnavailable

router = APIRouter(prefix="/integrations", tags=["integrations"])


def service(session: Session, principal: TenantPrincipal) -> IntegrationService:
    return IntegrationService(session, principal.tenant_id)


def _connection_response(data: dict[str, Any]) -> SenderConnectionResponse:
    return SenderConnectionResponse(**data)


@router.get("/providers", response_model=list[ProviderCapabilityResponse])
def list_providers(principal: TenantPrincipal = Depends(require_permission("integrations.read")), session: Session = Depends(get_db)) -> list[ProviderCapabilityResponse]:
    return [ProviderCapabilityResponse(**cap.__dict__) for cap in service(session, principal).list_providers()]


@router.post("", response_model=SenderConnectionResponse, status_code=status.HTTP_201_CREATED)
def create_connection(payload: IntegrationCreate, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> SenderConnectionResponse:
    try:
        connection = service(session, principal).create_connection(payload, created_by=principal.user_id)
    except IntegrationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except IntegrationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return _connection_response(serialize_connection(connection))


@router.get("", response_model=list[SenderConnectionResponse])
def list_connections(principal: TenantPrincipal = Depends(require_permission("integrations.read")), session: Session = Depends(get_db)) -> list[SenderConnectionResponse]:
    return [_connection_response(serialize_connection(connection)) for connection in service(session, principal).list_connections()]


@router.get("/senders", response_model=list[SenderAccountResponse])
def list_all_senders(principal: TenantPrincipal = Depends(require_permission("integrations.read")), session: Session = Depends(get_db)) -> list[SenderAccountResponse]:
    accounts = service(session, principal).list_all_senders()
    return [SenderAccountResponse.model_validate(account) for account in accounts]


@router.get("/{connection_id}", response_model=SenderConnectionResponse)
def get_connection(connection_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.read")), session: Session = Depends(get_db)) -> SenderConnectionResponse:
    try:
        connection = service(session, principal).get_connection(connection_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    return _connection_response(serialize_connection(connection))


@router.post("/{connection_id}/credentials", response_model=CredentialStatusResponse)
def store_credentials(connection_id: UUID, payload: CredentialUpload, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> CredentialStatusResponse:
    try:
        connection = service(session, principal).store_credentials(connection_id, payload, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return CredentialStatusResponse(
        connection_id=connection.id,
        configured=connection.credential_reference is not None,
        credential_version=connection.credential_version,
        expires_at=connection.credential_expires_at,
    )


@router.post("/{connection_id}/validate", response_model=SenderConnectionResponse)
def validate_connection(connection_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> SenderConnectionResponse:
    try:
        connection = service(session, principal).validate_connection(connection_id, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    return _connection_response(serialize_connection(connection))


@router.post("/{connection_id}/disconnect", response_model=SenderConnectionResponse)
def disconnect_connection(connection_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.disconnect")), session: Session = Depends(get_db)) -> SenderConnectionResponse:
    try:
        connection = service(session, principal).disconnect_connection(connection_id, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    return _connection_response(serialize_connection(connection))


@router.post("/{connection_id}/refresh-credentials", response_model=CredentialStatusResponse)
def refresh_credentials(connection_id: UUID, payload: CredentialUpload | None = None, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> CredentialStatusResponse:
    try:
        connection = service(session, principal).refresh_credentials(connection_id, payload=payload, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    except IntegrationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return CredentialStatusResponse(
        connection_id=connection.id,
        configured=True,
        credential_version=connection.credential_version,
        expires_at=connection.credential_expires_at,
    )


@router.post("/{connection_id}/discover", response_model=DiscoveryResponse)
def discover_senders(connection_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.discover")), session: Session = Depends(get_db)) -> DiscoveryResponse:
    try:
        return service(session, principal).discover_senders(connection_id, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None


@router.get("/{connection_id}/senders", response_model=list[SenderAccountResponse])
def list_senders(connection_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.read")), session: Session = Depends(get_db)) -> list[SenderAccountResponse]:
    try:
        accounts = service(session, principal).list_senders(connection_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    return [SenderAccountResponse.model_validate(account) for account in accounts]


@router.post("/{connection_id}/senders", response_model=SenderAccountResponse, status_code=status.HTTP_201_CREATED)
def register_sender(connection_id: UUID, payload: SenderAccountCreate, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> SenderAccountResponse:
    try:
        account = service(session, principal).register_sender(connection_id, payload, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    except IntegrationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return SenderAccountResponse.model_validate(account)


@router.patch("/senders/{sender_id}", response_model=SenderAccountResponse)
def update_sender_status(sender_id: UUID, payload: SenderAccountUpdate, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> SenderAccountResponse:
    try:
        account = service(session, principal).update_sender_status(sender_id, payload, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    except IntegrationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return SenderAccountResponse.model_validate(account)


@router.post("/senders/import", response_model=SenderImportResponse)
def import_senders(payload: SenderImportRequest, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> SenderImportResponse:
    try:
        accounts, errors = service(session, principal).import_senders(
            payload.connection_id,
            [SenderAccountCreate(**item.model_dump()) for item in payload.senders],
            actor_id=principal.user_id,
        )
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    return SenderImportResponse(
        imported=len(accounts),
        skipped=len(errors),
        accounts=[SenderAccountResponse.model_validate(account) for account in accounts],
        errors=errors,
    )


@router.post("/senders/{sender_id}/disable", response_model=SenderAccountResponse)
def disable_sender(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> SenderAccountResponse:
    try:
        account = service(session, principal).set_sender_status(sender_id, "DISABLED", actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    except IntegrationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return SenderAccountResponse.model_validate(account)


@router.post("/senders/{sender_id}/enable", response_model=SenderAccountResponse)
def enable_sender(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> SenderAccountResponse:
    try:
        account = service(session, principal).set_sender_status(sender_id, "ACTIVE", actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    except IntegrationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return SenderAccountResponse.model_validate(account)


@router.post("/senders/{sender_id}/enable-campaign", response_model=SenderAccountResponse)
def enable_campaign(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> SenderAccountResponse:
    try:
        account = service(session, principal).enable_campaign(sender_id, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    return SenderAccountResponse.model_validate(account)


@router.post("/senders/{sender_id}/disable-campaign", response_model=SenderAccountResponse)
def disable_campaign(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> SenderAccountResponse:
    try:
        account = service(session, principal).disable_campaign(sender_id, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    return SenderAccountResponse.model_validate(account)


@router.post("/senders/{sender_id}/enable-warmup", response_model=SenderAccountResponse)
def enable_warmup(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> SenderAccountResponse:
    try:
        account = service(session, principal).enable_warmup(sender_id, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    return SenderAccountResponse.model_validate(account)


@router.post("/senders/{sender_id}/disable-warmup", response_model=SenderAccountResponse)
def disable_warmup(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> SenderAccountResponse:
    try:
        account = service(session, principal).disable_warmup(sender_id, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    return SenderAccountResponse.model_validate(account)


@router.get("/senders/{sender_id}/warmup", response_model=WarmupSettingsResponse)
def get_warmup_settings(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.read")), session: Session = Depends(get_db)) -> WarmupSettingsResponse:
    try:
        settings_row = service(session, principal).get_warmup_settings(sender_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    return WarmupSettingsResponse.model_validate(settings_row)


@router.put("/senders/{sender_id}/warmup", response_model=WarmupSettingsResponse)
def update_warmup_settings(sender_id: UUID, payload: WarmupSettingsUpdate, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> WarmupSettingsResponse:
    try:
        settings_row = service(session, principal).update_warmup_settings(sender_id, payload, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    except IntegrationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return WarmupSettingsResponse.model_validate(settings_row)


@router.post("/senders/{sender_id}/sync-replies", response_model=ReplySyncResponse)
def sync_sender_replies(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.read")), session: Session = Depends(get_db)) -> ReplySyncResponse:
    try:
        count = service(session, principal).sync_replies(sender_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    except ReplySyncUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return ReplySyncResponse(sender_id=sender_id, linked_replies=count)


@router.post("/senders/{sender_id}/enable-reply-sync", response_model=SenderAccountResponse)
def enable_sender_reply_sync(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> SenderAccountResponse:
    try:
        account = service(session, principal).enable_reply_sync(sender_id, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    except IntegrationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return SenderAccountResponse.model_validate(account)


@router.post("/senders/{sender_id}/disable-reply-sync", response_model=SenderAccountResponse)
def disable_sender_reply_sync(sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.connect")), session: Session = Depends(get_db)) -> SenderAccountResponse:
    try:
        account = service(session, principal).disable_reply_sync(sender_id, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender account not found") from None
    return SenderAccountResponse.model_validate(account)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(connection_id: UUID, principal: TenantPrincipal = Depends(require_permission("integrations.disconnect")), session: Session = Depends(get_db)) -> Response:
    try:
        service(session, principal).delete_connection(connection_id, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)