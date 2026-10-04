"""Provider-connections API (Phase 1 provider foundation).

Organization-level (workspace) provider connections, tenant-scoped and
server-controlled OAuth:

* ``POST /provider-connections/google/connect`` - start the Workspace
  consent flow. Returns only the authorization URL; client secrets and tokens
  never leave the server.
* ``POST /provider-connections/microsoft/connect`` - start the Microsoft 365
  tenant consent flow (Phase 6). Same contract as the Google endpoint.
* ``GET /provider-connections/google/callback`` and
  ``GET /provider-connections/microsoft/callback`` - the browser round-trip
  from the provider. Identity/tenant are recovered from the single-use OAuth
  state, never from the query string. Unauthenticated by design (browser
  navigation), protected by the state store, its TTL, and the rate limit for
  ``/api/v1/provider-connections/``.
* ``GET /provider-connections`` - list the authenticated tenant's
  connections (never credentials).
* ``DELETE /provider-connections/{id}`` - disconnect (tenant-verified),
  tombstone credentials, audit.
* ``POST /provider-connections/{id}/revoke`` - mark REVOKED after a
  provider confirms the credentials are dead.
"""

from __future__ import annotations

import logging
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.email_providers.connection_base import (
    ProviderConnectionError,
)
from app.models import ProviderConnection
from app.schemas.provider_connections import (
    GoogleConnectResponse,
    Provider,
    ProviderConnectionResponse,
    ProviderConnectionStatus,
    ProviderConnectionType,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.oauth_state_store import (
    OAuthStateError,
    OAuthStateStore,
    build_state_store,
)
from app.services.provider_connections import (
    MICROSOFT_PROVIDER,
    ProviderConnectionNotFound,
    ProviderConnectionService,
)

logger = logging.getLogger("crcrm.provider_connections")

router = APIRouter(prefix="/provider-connections", tags=["provider-connections"])

CALLBACK_PAGE = "/settings/email-providers"

_SUCCESS_STATUS = "connected"
_ERROR_STATUS = "error"


def get_oauth_state_store() -> OAuthStateStore:
    """Injectable dependency so tests can substitute an in-memory store."""
    return build_state_store()


def _frontend_url() -> str:
    origin = (settings.allowed_origins[0] if settings.allowed_origins else "http://localhost:5173").rstrip("/")
    return origin


def _callback_redirect(*, status_code: str, error: str | None = None) -> RedirectResponse:
    target = _frontend_url() + CALLBACK_PAGE + f"?status={status_code}"
    if error:
        target += f"&error={error}"
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


def _request_context(request: Request) -> dict[str, Any]:
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else None)
    return {"ip_address": ip, "user_agent": request.headers.get("user-agent")}


def _service(
    session: Session,
    tenant_id: UUID,
    store: OAuthStateStore,
) -> ProviderConnectionService:
    return ProviderConnectionService(session, tenant_id, state_store=store)


def _respond(connection: ProviderConnection) -> ProviderConnectionResponse:
    return ProviderConnectionResponse(
        id=connection.id,
        provider=cast(Provider, connection.provider),
        connection_type=cast(ProviderConnectionType, connection.connection_type),
        provider_account_id=connection.provider_account_id,
        workspace_domain=connection.workspace_domain,
        display_name=connection.display_name,
        status=cast(ProviderConnectionStatus, connection.status),
        scopes=connection.scopes or [],
        credential_configured=connection.credential_reference is not None
        and not connection.credential_reference.startswith("revoked:"),
        credential_expires_at=connection.credential_expires_at,
        connected_by=connection.connected_by,
        last_sync_at=connection.last_sync_at,
        last_sync_status=connection.last_sync_status,
        last_sync_error=connection.last_sync_error,
        last_sync_started_at=connection.last_sync_started_at,
        last_sync_completed_at=connection.last_sync_completed_at,
        provider_metadata=_provider_metadata(connection),
        last_sync_stats=dict(connection.last_sync_stats or {}),
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


def _mask_tenant_id(value: Any) -> str | None:
    """Mask a Microsoft tenant id: ``6f31cbbb...c2e9`` shape, never full."""
    if not value or not isinstance(value, str):
        return None
    tenant = value.strip()
    if len(tenant) <= 8:
        return None
    return f"{tenant[:6]}...{tenant[-4:]}"


def _provider_metadata(connection: ProviderConnection) -> dict[str, Any]:
    """Non-secret provider metadata for API responses. Consent-admin identity
    and the full tenant id are deliberately not exposed."""
    raw = dict(connection.connection_metadata or {})
    return {
        "organizationName": raw.get("organizationName"),
        "defaultDomain": raw.get("defaultDomain") or connection.workspace_domain,
        "microsoftTenantId": _mask_tenant_id(raw.get("microsoftTenantId")),
    }


def _http_error(exc: ProviderConnectionError) -> HTTPException:
    safe_map: dict[str, int] = {
        "NOT_CONFIGURED": status.HTTP_503_SERVICE_UNAVAILABLE,
        "AUTH_REQUIRED": status.HTTP_409_CONFLICT,
    }
    return HTTPException(
        status_code=safe_map.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.message,
    )


@router.post("/google/connect", response_model=GoogleConnectResponse)
def start_google_connection(
    request: Request,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
    store: OAuthStateStore = Depends(get_oauth_state_store),
) -> GoogleConnectResponse:
    return _start_connection(provider="google", request=request, principal=principal, session=session, store=store)


@router.post("/microsoft/connect", response_model=GoogleConnectResponse)
def start_microsoft_connection(
    request: Request,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
    store: OAuthStateStore = Depends(get_oauth_state_store),
) -> GoogleConnectResponse:
    return _start_connection(provider="microsoft", request=request, principal=principal, session=session, store=store)


def _start_connection(
    *,
    provider: str,
    request: Request,
    principal: TenantPrincipal,
    session: Session,
    store: OAuthStateStore,
) -> GoogleConnectResponse:
    service = _service(session, principal.tenant_id, store)
    if provider == "microsoft":
        label = "Microsoft 365"
        start_fn = service.start_microsoft
    else:
        label = "Google Workspace"
        start_fn = service.start_google
    try:
        url = start_fn(principal.user_id, _request_context(request))
    except ProviderConnectionError as exc:
        raise _http_error(exc) from None
    except Exception:
        logger.exception("provider connection start failed tenant=%s provider=%s", principal.tenant_id, provider)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"The {label} connection could not be started.",
        ) from None
    return GoogleConnectResponse(authorization_url=url)


@router.get("/google/callback")
def google_oauth_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str | None = Query(default=None),
    session: Session = Depends(get_db),
    store: OAuthStateStore = Depends(get_oauth_state_store),
) -> Response:
    if error == "access_denied" or not code or not state:
        return _callback_redirect(status_code=_ERROR_STATUS, error="access_denied" if error == "access_denied" else "state_invalid")
    try:
        record = store.consume(state)
    except OAuthStateError as exc:
        error_code = "state_expired" if "expired" in str(exc) else "state_invalid"
        logger.warning("provider connection callback rejected reason=%s", error_code)
        return _callback_redirect(status_code=_ERROR_STATUS, error=error_code)
    service = _service(session, record.tenant_id, store)
    try:
        service.complete_with_state(record, code, state, _request_context(request))
    except ProviderConnectionError as exc:
        code = _error_token(exc.code)
        logger.warning("provider connection callback failed reason=%s", code)
        return _callback_redirect(status_code=_ERROR_STATUS, error=code)
    except Exception:
        logger.exception("provider connection callback failed")
        return _callback_redirect(status_code=_ERROR_STATUS, error="oauth_failed")
    return _callback_redirect(status_code=_SUCCESS_STATUS)


@router.get("/microsoft/callback")
def microsoft_oauth_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str | None = Query(default=None),
    session: Session = Depends(get_db),
    store: OAuthStateStore = Depends(get_oauth_state_store),
) -> Response:
    if error:
        return _callback_redirect(status_code=_ERROR_STATUS, error=_microsoft_error_token(error))
    if not code or not state:
        return _callback_redirect(status_code=_ERROR_STATUS, error="state_invalid")
    try:
        record = store.consume(state)
    except OAuthStateError as exc:
        error_code = "state_expired" if "expired" in str(exc) else "state_invalid"
        logger.warning("microsoft provider connection callback rejected reason=%s", error_code)
        return _callback_redirect(status_code=_ERROR_STATUS, error=error_code)
    service = _service(session, record.tenant_id, store)
    try:
        service.complete_with_state(record, code, state, _request_context(request), provider=MICROSOFT_PROVIDER)
    except ProviderConnectionError as exc:
        token = _error_token(exc.code)
        logger.warning("microsoft provider connection callback failed reason=%s", token)
        return _callback_redirect(status_code=_ERROR_STATUS, error=token)
    except Exception:
        logger.exception("microsoft provider connection callback failed")
        return _callback_redirect(status_code=_ERROR_STATUS, error="oauth_failed")
    return _callback_redirect(status_code=_SUCCESS_STATUS)


def _error_token(code: str | None) -> str:
    return {
        "STATE_INVALID": "state_invalid",
        "STATE_EXPIRED": "state_expired",
        "DUPLICATE": "duplicate",
        "INSUFFICIENT_SCOPE": "insufficient_scope",
        "OAUTH_EXCHANGE_FAILED": "oauth_failed",
        "IDENTITY_FAILED": "verify_failed",
        "AUTH_REQUIRED": "verify_failed",
        "NOT_CONFIGURED": "not_configured",
        "ADMIN_CONSENT_REQUIRED": "admin_consent_required",
        "SCOPE_CANCELLED": "access_denied",
    }.get(code or "", "oauth_failed")


def _microsoft_error_token(error: str) -> str:
    """Map the Microsoft identity-platform ``error`` query parameter to the
    same tokens the frontend understands for Google."""
    lowered = error.lower()
    if lowered in {"access_denied", "user_cancelled"}:
        return "access_denied"
    if "admin_consent" in lowered or "consent" in lowered:
        return "admin_consent_required"
    if "invalid_scope" in lowered or "invalid_grant" in lowered or "interaction_required" in lowered:
        return "insufficient_scope"
    if "missing" in lowered:
        return "state_invalid"
    return "oauth_failed"


@router.get("", response_model=list[ProviderConnectionResponse])
def list_connections(
    principal: TenantPrincipal = Depends(require_permission("integrations.read")),
    session: Session = Depends(get_db),
    store: OAuthStateStore = Depends(get_oauth_state_store),
) -> list[ProviderConnectionResponse]:
    service = _service(session, principal.tenant_id, store)
    return [_respond(connection) for connection in service.list_connections()]


@router.get("/{connection_id}", response_model=ProviderConnectionResponse)
def get_connection(
    connection_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.read")),
    session: Session = Depends(get_db),
    store: OAuthStateStore = Depends(get_oauth_state_store),
) -> ProviderConnectionResponse:
    service = _service(session, principal.tenant_id, store)
    try:
        connection = service.get(connection_id)
    except ProviderConnectionNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider connection not found") from None
    return _respond(connection)


@router.delete("/{connection_id}", response_model=ProviderConnectionResponse)
def disconnect_connection(
    connection_id: UUID,
    request: Request,
    principal: TenantPrincipal = Depends(require_permission("integrations.disconnect")),
    session: Session = Depends(get_db),
    store: OAuthStateStore = Depends(get_oauth_state_store),
) -> ProviderConnectionResponse:
    service = _service(session, principal.tenant_id, store)
    try:
        connection = service.disconnect(connection_id, principal.user_id, _request_context(request))
    except ProviderConnectionNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider connection not found") from None
    except ProviderConnectionError as exc:
        raise _http_error(exc) from None
    return _respond(connection)


@router.post("/{connection_id}/revoke", response_model=ProviderConnectionResponse)
def revoke_connection(
    connection_id: UUID,
    request: Request,
    principal: TenantPrincipal = Depends(require_permission("integrations.disconnect")),
    session: Session = Depends(get_db),
    store: OAuthStateStore = Depends(get_oauth_state_store),
) -> ProviderConnectionResponse:
    service = _service(session, principal.tenant_id, store)
    try:
        connection = service.revoke(connection_id, principal.user_id, _request_context(request))
    except ProviderConnectionNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider connection not found") from None
    return _respond(connection)


@router.post("/{connection_id}/refresh", response_model=ProviderConnectionResponse)
def refresh_connection(
    connection_id: UUID,
    request: Request,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
    store: OAuthStateStore = Depends(get_oauth_state_store),
) -> ProviderConnectionResponse:
    """Manually refresh the credential for a connected provider.

    Useful during development and as a fallback if the background sweep
    has not yet picked up an expiring token. Returns the updated connection.
    """
    service = _service(session, principal.tenant_id, store)
    try:
        connection = service.refresh_credentials(
            connection_id, request_context=_request_context(request),
        )
    except ProviderConnectionNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider connection not found") from None
    except ProviderConnectionError as exc:
        raise _http_error(exc) from None
    return _respond(connection)


@router.api_route(
    "/microsoft/callback/",
    methods=["GET"],
    include_in_schema=False,
)
def microsoft_oauth_callback_slash_redirect(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    """Accept the trailing-slash form of the Microsoft callback URL so a
    redirected consent return still lands on the canonical route."""
    target = _frontend_url() + CALLBACK_PAGE
    if error:
        target += f"?status=error&error={_microsoft_error_token(error)}"
    elif code and state:
        target += f"?code={code}&state={state}&redirect=1"
    else:
        target += "?status=error&error=state_invalid"
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


@router.api_route(
    "/google/callback/",
    methods=["GET"],
    include_in_schema=False,
)
def google_oauth_callback_slash_redirect(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    """Accept the trailing-slash form of the callback URL so a redirected
    Google consent return still lands on the canonical route."""
    target = _frontend_url() + CALLBACK_PAGE
    if error:
        target += f"?status=error&error={'access_denied' if error == 'access_denied' else 'oauth_failed'}"
    elif code and state:
        target += f"?code={code}&state={state}&redirect=1"
    else:
        target += "?status=error&error=state_invalid"
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)