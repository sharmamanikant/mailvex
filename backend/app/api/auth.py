from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.schemas.auth import (
    LoginRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    SignupRequest,
    SignupResponse,
    TokenResponse,
    UserResponse,
    UserSummaryResponse,
)
from app.security.permissions import TenantPrincipal, get_current_principal
from app.services.auth import AuthenticationError, AuthService
from app.services.transactional_email import SMTPTransactionalEmailProvider

router = APIRouter(prefix="/auth", tags=["authentication"])
REFRESH_COOKIE = "crcrm_refresh"


def _require_legacy_auth() -> None:
    """Gate legacy password-based endpoints (Part 5 controlled migration).

    While ``AUTH_LEGACY_ENABLED`` is true these stay available for existing
    clients; once the workspace is on Clerk, flip the flag and these endpoints
    refuse to mint legacy credentials without deleting any code.
    """
    if not settings.legacy_auth_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Legacy authentication is disabled; sign in with Clerk",
        )


def _user_response(principal: TenantPrincipal) -> UserResponse:
    return UserResponse(id=principal.user_id, tenant_id=principal.tenant_id, email=principal.email, display_name=principal.display_name, roles=list(principal.roles))


def _validate_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    allowed = {urlsplit(item).netloc.lower() for item in settings.allowed_origins}
    if urlsplit(origin).netloc.lower() not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin not allowed")


@router.post("/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, response: Response, request: Request, session: Session = Depends(get_db)) -> SignupResponse:
    _require_legacy_auth()
    _validate_origin(request)
    try:
        service = AuthService(session, settings)
        user, issued = service.signup(str(payload.email), payload.password, payload.display_name)
    except AuthenticationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    response.set_cookie(REFRESH_COOKIE, issued.refresh_token, httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=settings.refresh_token_days * 86400, path="/api/v1/auth")
    return SignupResponse(
        access_token=issued.access_token,
        expires_at=issued.access_expires_at,
        user=UserSummaryResponse(
            id=user.id,
            tenant_id=user.tenant_id,
            email=user.email,
            display_name=user.display_name,
            roles=list(service._roles(user)),
        ),
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, response: Response, request: Request, session: Session = Depends(get_db)) -> TokenResponse:
    _require_legacy_auth()
    _validate_origin(request)
    try:
        service = AuthService(session, settings)
        user = service.authenticate(str(payload.email), payload.password)
        issued = service.issue_tokens(user)
        user.last_login_at = datetime.now(UTC)
        service._audit(user, "LOGIN_SUCCESS", request.headers.get("x-request-id"))
        session.commit()
    except AuthenticationError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password") from None
    response.set_cookie(REFRESH_COOKIE, issued.refresh_token, httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=settings.refresh_token_days * 86400, path="/api/v1/auth")
    return TokenResponse(access_token=issued.access_token, expires_at=issued.access_expires_at)


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, response: Response, session: Session = Depends(get_db)) -> TokenResponse:
    _require_legacy_auth()
    _validate_origin(request)
    raw_token = request.cookies.get(REFRESH_COOKIE)
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        issued = AuthService(session, settings).rotate_refresh_token(raw_token, request.headers.get("x-request-id"))[1]
    except AuthenticationError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token") from None
    response.set_cookie(REFRESH_COOKIE, issued.refresh_token, httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=settings.refresh_token_days * 86400, path="/api/v1/auth")
    return TokenResponse(access_token=issued.access_token, expires_at=issued.access_expires_at)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, session: Session = Depends(get_db)) -> None:
    _validate_origin(request)
    raw_token = request.cookies.get(REFRESH_COOKIE)
    if raw_token:
        AuthService(session, settings).revoke_refresh_token(raw_token, request.headers.get("x-request-id"))
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")


@router.get("/me", response_model=UserResponse)
def me(principal: TenantPrincipal = Depends(get_current_principal)) -> UserResponse:
    return _user_response(principal)


@router.post("/password-reset/request")
def request_password_reset(payload: PasswordResetRequest, session: Session = Depends(get_db)) -> dict[str, str]:
    _require_legacy_auth()
    service = AuthService(session, settings)
    token = service.create_password_reset_token(str(payload.email))
    if token and settings.transactional_smtp_host and settings.transactional_email_from:
        try:
            provider = SMTPTransactionalEmailProvider(
                settings.transactional_smtp_host,
                settings.transactional_smtp_port,
                settings.transactional_smtp_username,
                settings.transactional_smtp_password,
                settings.transactional_email_from,
            )
            reset_link = f"{settings.password_reset_url}?token={token}"
            provider.send(
                str(payload.email),
                "Reset your CR+CRM password",
                f"Reset your password using this link: {reset_link}\nThis link expires in one hour. If you did not request this, ignore this email.",
                f'<p>Reset your password using <a href="{reset_link}">this link</a>.</p><p>This link expires in one hour. If you did not request this, ignore this email.</p>',
            )
        except Exception:
            service.remove_password_reset_token(token)
    return {"message": "If the account exists, password reset instructions will be sent."}


@router.post("/password-reset/confirm")
def confirm_password_reset(payload: PasswordResetConfirm, session: Session = Depends(get_db)) -> dict[str, str]:
    _require_legacy_auth()
    try:
        AuthService(session, settings).reset_password(payload.token, payload.new_password)
    except AuthenticationError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired reset token") from None
    return {"message": "Password reset successful"}
