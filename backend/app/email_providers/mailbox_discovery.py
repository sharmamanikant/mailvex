"""Provider abstraction for workspace mailbox discovery (Phase 2).

Defines the ABC :class:`MailboxDiscoveryProvider` and the concrete
``GoogleMailboxDiscoveryProvider`` that calls the Google Workspace
Directory API to enumerate eligible users.

Only send-capable Workspace users (``user_type == USER``) are yielded
by the discovery pipeline. Non-user directory objects are counted but
not persisted — the caller logs them.

Google API docs referenced:
  https://developers.google.com/admin-sdk/directory/reference/rest/v1/users/list
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from app.core.config import settings
from app.email_providers.connection_base import (
    AUTH_REQUIRED,
    REFRESH_FAILED,
    ProviderConnectionError,
    get_connection_provider,
)
from app.email_providers.credentials import (
    decrypt_credential_reference,
    payload_expires_at,
)

logger = logging.getLogger("crcrm.mailboxes.discovery")


# ------------------------------------------------------------------ #
# Data model for a single discovered mailbox entry
# ------------------------------------------------------------------ #
@dataclass
class DiscoveredMailbox:
    """A single workspace user as returned by the directory provider."""

    provider_mailbox_id: str
    email: str
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    department: str | None = None
    job_title: str | None = None
    user_type: str = "USER"           # USER | ALIAS | GROUP | OTHER
    is_suspended: bool = False
    is_deleted: bool = False
    primary_email: str = ""           # populated by provider during normalization


@dataclass
class DiscoveryPage:
    """A single page of discovered mailboxes."""

    mailboxes: list[DiscoveredMailbox]
    skipped: int = 0                  # non-USER objects encountered on this page


@dataclass
class DiscoveryResult:
    """Final result of a discovery run."""

    pages: list[DiscoveryPage] = field(default_factory=list)
    # Updated credential info (rotated internally if token was expired).
    rotated_credential_reference: str | None = None
    rotated_credential_version: str | None = None
    rotated_expires_at: object | None = None  # datetime | None

    @property
    def all_mailboxes(self) -> list[DiscoveredMailbox]:
        result: list[DiscoveredMailbox] = []
        for page in self.pages:
            result.extend(page.mailboxes)
        return result

    @property
    def total_discovered(self) -> int:
        return sum(len(page.mailboxes) for page in self.pages)

    @property
    def total_skipped(self) -> int:
        return sum(page.skipped for page in self.pages)


# ------------------------------------------------------------------ #
# ABC
# ------------------------------------------------------------------ #
class MailboxDiscoveryProvider(ABC):
    """Abstraction for provider-specific mailbox enumeration.

    Subclasses are registered in ``_DISCOVERY_PROVIDERS`` and fetched
    via :func:`get_mailbox_discovery_provider`.
    """

    provider_name: ClassVar[str] = ""

    @abstractmethod
    def discover(
        self,
        *,
        credential_reference: str,
        credential_version: str,
        workspace_domain: str | None = None,
    ) -> DiscoveryResult:
        """Enumerate all eligible workspace users.

        Must handle pagination internally, token refresh (credential
        rotation), and yield only users that are *sender-eligible*
        (user_type ``USER``). Non-user objects should be counted and
        reported via ``DiscoveryPage.skipped`` but not persisted.

        Raises ``ProviderConnectionError`` on:
          - ``AUTH_REQUIRED`` — permanent token revocation / invalid_grant
          - ``REFRESH_FAILED`` — transient network / 5xx error
        """
        ...


# ------------------------------------------------------------------ #
# Google Workspace Directory implementation
# ------------------------------------------------------------------ #
_GOOGLE_ELIGIBLE_USER_TYPES = frozenset({"USER"})
_GOOGLE_MAX_RESULTS = 200            # Directory API max is 500; 200 keeps payload light
_GOOGLE_DIRECTORY_FIELDS = (
    "users(id,primaryEmail,name(givenName,familyName,fullName),"
    "department,jobTitle,suspended,type,kind),nextPageToken"
)
_GOOGLE_MAX_RETRIES = 3
_GOOGLE_RETRYABLE_STATES = frozenset({429, 500, 502, 503, 504})


class GoogleMailboxDiscoveryProvider(MailboxDiscoveryProvider):
    """Google Workspace Directory API implementation."""

    provider_name = "GOOGLE"

    def discover(
        self,
        *,
        credential_reference: str,
        credential_version: str,
        workspace_domain: str | None = None,
    ) -> DiscoveryResult:
        from datetime import UTC, datetime, timedelta

        from google.auth.exceptions import RefreshError
        from google.oauth2.credentials import Credentials as GoogleCredentials

        # Decrypt stored credentials and refresh access token if needed.
        payload = decrypt_credential_reference(settings.encryption_key, credential_reference)
        expires_at = payload_expires_at(payload)
        rotated_reference: str | None = None
        rotated_version: str | None = None
        rotated_expires: object | None = None

        if expires_at and expires_at <= datetime.now(UTC) + timedelta(minutes=1):
            try:
                conn_provider = get_connection_provider("GOOGLE")
                rot = conn_provider.refresh_credentials(
                    credential_reference=credential_reference,
                    credential_version=credential_version,
                )
                payload = dict(decrypt_credential_reference(settings.encryption_key, rot.credential_reference))
                rotated_reference = rot.credential_reference
                rotated_version = rot.credential_version
                rotated_expires = rot.expires_at
            except ProviderConnectionError:
                raise
            except Exception as exc:
                raise ProviderConnectionError(
                    REFRESH_FAILED, f"Temporary failure refreshing credentials: {exc}"
                ) from exc

        raw_scopes = payload.get("scopes", [])
        scope_list = [str(scope) for scope in raw_scopes] if isinstance(raw_scopes, (list, tuple)) else []
        creds = GoogleCredentials(  # type: ignore[no-untyped-call]
            token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            token_uri=str(payload.get("token_uri") or "https://oauth2.googleapis.com/token"),
            client_id=str(payload.get("client_id") or settings.google_client_id),
            client_secret=str(settings.google_client_secret),
            scopes=sorted(scope_list),
        )

        # Build the Directory service.
        from googleapiclient.discovery import build

        try:
            directory = build("admin", "directory_v1", credentials=creds, cache_discovery=False)
        except RefreshError as exc:
            raise ProviderConnectionError(
                AUTH_REQUIRED, "Google authorization expired; reconnect required"
            ) from exc
        except Exception as exc:
            raise ProviderConnectionError(
                REFRESH_FAILED, f"Temporary failure building Google service: {exc}"
            ) from exc

        result = DiscoveryResult()
        page_token: str | None = None
        attempt: int = 0

        while True:
            list_kwargs: dict[str, object] = {
                "maxResults": _GOOGLE_MAX_RESULTS,
                "fields": _GOOGLE_DIRECTORY_FIELDS,
                "customer": "my_customer",
                "projection": "full",
                "orderBy": "email",
            }
            if workspace_domain:
                list_kwargs.pop("customer", None)
                list_kwargs["domain"] = workspace_domain
            if page_token:
                list_kwargs["pageToken"] = page_token

            attempt = 0
            try:
                response = directory.users().list(**list_kwargs).execute()
            except RefreshError as exc:
                raise ProviderConnectionError(
                    AUTH_REQUIRED, "Google authorization expired; reconnect required"
                ) from exc
            except Exception as exc:
                exc_str = str(exc).lower()
                # Rate limit / server errors — retry with backoff
                is_retryable = any(code in exc_str for code in ("429", "503", "500", "502", "504", "rate limit", "internalerror", "backenderror"))
                if is_retryable and attempt < _GOOGLE_MAX_RETRIES:
                    attempt += 1
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise ProviderConnectionError(
                    REFRESH_FAILED, f"Google Directory API error: {exc}"
                ) from exc

            attempt = 0
            raw_users = response.get("users", [])
            mailboxes: list[DiscoveredMailbox] = []
            skipped = 0

            for user in raw_users:
                user_type = (user.get("type") or "USER").upper()
                if user_type not in _GOOGLE_ELIGIBLE_USER_TYPES:
                    skipped += 1
                    continue
                name_obj = user.get("name", {})
                email = (user.get("primaryEmail") or "").strip().lower()
                if not email:
                    skipped += 1
                    continue
                mailboxes.append(DiscoveredMailbox(
                    provider_mailbox_id=str(user.get("id", "")),
                    email=email,
                    display_name=name_obj.get("fullName"),
                    first_name=name_obj.get("givenName"),
                    last_name=name_obj.get("familyName"),
                    department=user.get("department"),
                    job_title=user.get("jobTitle"),
                    user_type=user_type,
                    is_suspended=bool(user.get("suspended")),
                    primary_email=email,
                ))

            result.pages.append(DiscoveryPage(mailboxes=mailboxes, skipped=skipped))

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        result.rotated_credential_reference = rotated_reference
        result.rotated_credential_version = rotated_version
        result.rotated_expires_at = rotated_expires

        return result


# ------------------------------------------------------------------ #
# Registry
# ------------------------------------------------------------------ #
_DISCOVERY_PROVIDERS: dict[str, type[MailboxDiscoveryProvider]] = {
    "GOOGLE": GoogleMailboxDiscoveryProvider,
}


def get_mailbox_discovery_provider(name: str) -> MailboxDiscoveryProvider:
    cls = _DISCOVERY_PROVIDERS.get(name)
    if cls is None:
        raise ProviderConnectionError(AUTH_REQUIRED, f"Mailbox discovery not supported for provider '{name}'")
    return cls()
