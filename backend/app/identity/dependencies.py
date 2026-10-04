"""FastAPI dependency exposing the verified external identity (Clerk-only)."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.identity.clerk import ClerkTokenError, get_verifier
from app.identity.schemas import VerifiedIdentity

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_identity(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> VerifiedIdentity:
    """Resolve the bearer token to a Clerk-verified identity.

    This is the strict Clerk-only path: a valid Clerk session token in, a
    ``VerifiedIdentity`` out. Endpoints that need an application user/tenant
    should use ``get_current_principal`` (which layers legacy fallback).
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    verifier = get_verifier()
    if verifier is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Clerk authentication is not configured",
        )
    try:
        return verifier.verify(credentials.credentials)
    except ClerkTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token") from exc