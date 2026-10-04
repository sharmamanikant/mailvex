"""Microsoft Graph HTTP client for the org-level Microsoft 365 flow (Phase 6).

A small, purpose-built HTTP boundary for the provider-connection + mailbox
discovery + sender-health work in Phase 6. It deliberately does NOT own tokens:
the caller supplies a fresh access token (the workspace connection adapter and
the discovery provider decrypt the stored credential payload and refresh inside
the provider boundary, mirroring the Google Directory provider).

Behaviour:

* Bounded retries with exponential backoff for 429 / 5xx / transport failures
  (respecting ``Retry-After``). 400 / 401 / 403 / 404 are never retried.
* Pagination helper that follows ``@odata.nextLink`` and refuses links that
  point outside the configured Graph base (prevents an attacker-influenced
  response from redirecting requests to an arbitrary host).
* Every failure is normalized onto :class:`MicrosoftGraphError` with a stable,
  non-secret ``code`` (module-level constants). Callers translate those codes
  into ``ProviderConnectionError`` / audit reasons.
* Tokens are never logged; request metadata is limited to path + status.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

import requests

from app.core.config import settings

logger = logging.getLogger("crcrm.microsoft.graph")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# $select for directory user enumeration (kept minimal).
GRAPH_USERS_SELECT = (
    "id,userPrincipalName,mail,displayName,givenName,surname,"
    "department,jobTitle,accountEnabled,userType"
)

# Normalized Microsoft Graph error codes (stable, non-secret).
AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
ADMIN_CONSENT_REQUIRED = "ADMIN_CONSENT_REQUIRED"
TENANT_NOT_FOUND = "TENANT_NOT_FOUND"
RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
THROTTLED = "THROTTLED"
RATE_LIMITED = "RATE_LIMITED"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
INVALID_REQUEST = "INVALID_REQUEST"
TOKEN_EXPIRED = "TOKEN_EXPIRED"
TOKEN_REVOKED = "TOKEN_REVOKED"
UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"

# Codes that mean the stored refresh token is permanently dead.
PERMANENT_TOKEN_CODES = frozenset({TOKEN_EXPIRED, TOKEN_REVOKED, AUTHENTICATION_FAILED})

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 10.0


class MicrosoftGraphError(Exception):
    """A failed Microsoft Graph request with a stable, non-secret code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after


def _retry_after(response: requests.Response | None) -> int | None:
    if response is None:
        return None
    try:
        value = response.headers.get("Retry-After") or response.headers.get("retry-after")
        if value is None:
            return None
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def _backoff(attempt: int, retry_after: int | None) -> float:
    if retry_after is not None:
        return float(retry_after)
    delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    return float(min(delay, _MAX_BACKOFF_SECONDS))


def map_graph_error(response: requests.Response) -> MicrosoftGraphError:
    """Translate an HTTP response into a normalized :class:`MicrosoftGraphError`."""
    status = int(getattr(response, "status_code", 0))
    retry_after_value = _retry_after(response)
    body: dict[str, Any] = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            body = parsed
    except ValueError:
        pass
    error = body.get("error") or {}
    code = str(error.get("code") or "").lower()
    message = str(error.get("message") or body.get("error_description") or body.get("message") or "").lower()

    if status == 401:
        if "expired" in message or code in ("invalidauthenticationtoken", "tokentypeisnotallowed"):
            return MicrosoftGraphError(
                TOKEN_EXPIRED, "Microsoft access token expired", status=status, retry_after=retry_after_value
            )
        if "revoked" in message:
            return MicrosoftGraphError(
                TOKEN_REVOKED, "Microsoft authorization was revoked", status=status, retry_after=retry_after_value
            )
        return MicrosoftGraphError(
            AUTHENTICATION_FAILED, "Microsoft rejected the credentials", status=status, retry_after=retry_after_value
        )
    if status == 403:
        if "consent" in code or "consent" in message or "admin_consent_required" in code:
            return MicrosoftGraphError(
                ADMIN_CONSENT_REQUIRED,
                "Organization admin consent is required for Microsoft Graph access",
                status=status,
                retry_after=retry_after_value,
            )
        return MicrosoftGraphError(
            AUTHORIZATION_FAILED, "Microsoft denied access for this account", status=status, retry_after=retry_after_value
        )
    if status == 404:
        if "tenant" in code or "tenant" in message:
            return MicrosoftGraphError(
                TENANT_NOT_FOUND, "The Microsoft tenant could not be found", status=status, retry_after=retry_after_value
            )
        return MicrosoftGraphError(
            RESOURCE_NOT_FOUND, "The requested Microsoft resource was not found", status=status, retry_after=retry_after_value
        )
    if status == 429:
        return MicrosoftGraphError(
            THROTTLED, "Microsoft is throttling requests", status=status, retry_after=retry_after_value
        )
    if status == 400:
        return MicrosoftGraphError(
            INVALID_REQUEST, "Microsoft rejected the Graph request", status=status, retry_after=retry_after_value
        )
    if 500 <= status < 600:
        return MicrosoftGraphError(
            PROVIDER_UNAVAILABLE, "Microsoft Graph is unavailable", status=status, retry_after=retry_after_value
        )
    return MicrosoftGraphError(
        UNKNOWN_PROVIDER_ERROR, "Microsoft Graph request failed", status=status, retry_after=retry_after_value
    )


class MicrosoftGraphClient:
    """Authenticated Graph client with retries and pagination."""

    def __init__(
        self,
        access_token: str,
        *,
        session: requests.Session | None = None,
        base_url: str = GRAPH_BASE,
    ) -> None:
        if not access_token:
            raise MicrosoftGraphError(TOKEN_EXPIRED, "No Microsoft access token is available")
        self.access_token = access_token
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()

    # ------------------------------------------------------------------ #
    # Single request
    # ------------------------------------------------------------------ #
    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        return self._request("GET", url, params=params)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    timeout=30,
                )
            except requests.RequestException:
                if attempt >= _MAX_RETRIES:
                    logger.warning(
                        "microsoft_graph_error reason=transport_unavailable status=unreachable",
                    )
                    raise MicrosoftGraphError(
                        PROVIDER_UNAVAILABLE, "Microsoft Graph is unreachable"
                    ) from None
                attempt += 1
                delay = _backoff(attempt, None)
                logger.warning(
                    "microsoft_graph_request reason=retry status=transport_unavailable attempt=%s delay=%ss",
                    attempt,
                    round(delay, 1),
                )
                time.sleep(delay)
                continue

            if 200 <= response.status_code < 300:
                logger.debug("microsoft_graph_request method=%s status=%s", method, response.status_code)
                if not response.content:
                    return {}
                try:
                    data = response.json()
                except ValueError:
                    return {}
                return data if isinstance(data, dict) else {}

            retry_after_value = _retry_after(response)
            if response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                attempt += 1
                delay = _backoff(attempt, retry_after_value)
                logger.warning(
                    "microsoft_graph_throttled status=%s attempt=%s retry_after=%s",
                    response.status_code,
                    attempt,
                    retry_after_value,
                )
                time.sleep(delay)
                continue

            error = map_graph_error(response)
            logger.error(
                "microsoft_graph_error status=%s code=%s",
                response.status_code,
                error.code,
            )
            raise error from None

    # ------------------------------------------------------------------ #
    # Paginated collection
    # ------------------------------------------------------------------ #
    def get_paged(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield each ``value`` list of a paged collection (``@odata.nextLink``).

        ``params`` are applied to the FIRST request only; subsequent pages come
        from the ``@odata.nextLink`` returned by Graph. Links that point off
        the configured base URL are rejected.
        """
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        first = True
        while True:
            payload = self._request("GET", url, params=params if first else None)
            values = payload.get("value")
            if isinstance(values, list):
                yield values
            next_link = payload.get("@odata.nextLink")
            if not next_link:
                break
            next_url = str(next_link)
            if not next_url.startswith(f"{settings.microsoft_authority}") and not next_url.startswith(self._base_url):
                raise MicrosoftGraphError(
                    INVALID_REQUEST, "Microsoft Graph returned an untrusted pagination link"
                )
            url = next_url
            first = False