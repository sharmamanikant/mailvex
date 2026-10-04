"""Microsoft 365 mailbox discovery provider (Phase 6).

Enumerates a Microsoft 365 tenant's mailboxes through Microsoft Graph's
``GET /users`` endpoint so Phase 2-style mailbox sync works against a
CONNECTED Microsoft 365 org connection.

Only send-capable tenant members (``userType == Member``) are persisted;
Guests and any record without a usable email are counted as ``skipped`` and
never become Mailbox rows. ``accountEnabled: false`` maps to ``SUSPENDED``.

Credentials are handled exactly like the Google Directory provider: the
stored payload is decrypted inside the provider boundary, refreshed (rotated)
when near expiry via the connection adapter, and the raw token is used only to
construct a short-lived :class:`MicrosoftGraphClient`. Tokens never leave this
module.

Raises ``ProviderConnectionError``:
  - ``AUTH_REQUIRED``     — permanent token revocation / expired access token.
  - otherwise the normalized Graph code (``RATE_LIMITED``, ``THROTTLED``,
    ``ADMIN_CONSENT_REQUIRED``, ``PROVIDER_UNAVAILABLE``, ...) so the sync
    audit trail can distinguish the failure classes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import settings
from app.email_providers.connection_base import (
    AUTH_REQUIRED,
    ProviderConnectionError,
    get_connection_provider,
)
from app.email_providers.credentials import (
    decrypt_credential_reference,
    payload_expires_at,
)
from app.email_providers.mailbox_discovery import (
    DiscoveredMailbox,
    DiscoveryPage,
    DiscoveryResult,
    MailboxDiscoveryProvider,
)
from app.email_providers.microsoft.graph_client import (
    GRAPH_USERS_SELECT,
    PERMANENT_TOKEN_CODES,
    MicrosoftGraphClient,
    MicrosoftGraphError,
)

_MS_MAX_TOP = 200  # keep each payload light; Graph allows up to 999


def _to_connection_error(exc: MicrosoftGraphError) -> ProviderConnectionError:
    if exc.code in PERMANENT_TOKEN_CODES:
        return ProviderConnectionError(
            AUTH_REQUIRED, "Microsoft authorization expired; reconnect required"
        )
    return ProviderConnectionError(exc.code, exc.message)


class MicrosoftMailboxDiscoveryProvider(MailboxDiscoveryProvider):
    """Microsoft Graph directory enumeration for a Microsoft 365 tenant."""

    provider_name = "MICROSOFT"

    def discover(
        self,
        *,
        credential_reference: str,
        credential_version: str,
        workspace_domain: str | None = None,
    ) -> DiscoveryResult:
        payload = decrypt_credential_reference(settings.encryption_key, credential_reference)

        expires_at = payload_expires_at(payload)
        rotated_reference: str | None = None
        rotated_version: str | None = None
        rotated_expires: Any = None

        if expires_at is not None and expires_at <= datetime.now(UTC) + timedelta(minutes=1):
            try:
                conn_provider = get_connection_provider("MICROSOFT")
                rot = conn_provider.refresh_credentials(
                    credential_reference=credential_reference,
                    credential_version=credential_version,
                )
                payload = decrypt_credential_reference(settings.encryption_key, rot.credential_reference)
                rotated_reference = rot.credential_reference
                rotated_version = rot.credential_version
                rotated_expires = rot.expires_at
            except ProviderConnectionError:
                raise
            except Exception as exc:
                raise ProviderConnectionError(
                    "REFRESH_FAILED", f"Temporary failure refreshing Microsoft credentials: {exc}"
                ) from exc

        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise ProviderConnectionError(
                AUTH_REQUIRED, "Microsoft credentials are expired; reconnect required"
            )

        result = DiscoveryResult()
        params: dict[str, Any] = {
            "$top": str(_MS_MAX_TOP),
            "$select": GRAPH_USERS_SELECT,
        }
        try:
            client = MicrosoftGraphClient(access_token)
            for page in client.get_paged("/users", params=params):
                mailboxes: list[DiscoveredMailbox] = []
                skipped = 0
                for user in page:
                    if not isinstance(user, dict):
                        skipped += 1
                        continue
                    user_type = _eligible_user_type(user)
                    if user_type != "USER":
                        skipped += 1
                        continue
                    email = str(
                        user.get("mail") or user.get("userPrincipalName") or ""
                    ).strip().lower()
                    if not email:
                        skipped += 1
                        continue
                    account_id = str(user.get("id") or "").strip() or email
                    mailboxes.append(
                        DiscoveredMailbox(
                            provider_mailbox_id=account_id,
                            email=email,
                            display_name=_clean(user.get("displayName")) or None,
                            first_name=_clean(user.get("givenName")) or None,
                            last_name=_clean(user.get("surname")) or None,
                            department=_clean(user.get("department")) or None,
                            job_title=_clean(user.get("jobTitle")) or None,
                            user_type="USER",
                            is_suspended=user.get("accountEnabled") is False,
                            primary_email=email,
                        )
                    )
                result.pages.append(DiscoveryPage(mailboxes=mailboxes, skipped=skipped))
        except MicrosoftGraphError as exc:
            raise _to_connection_error(exc) from None

        result.rotated_credential_reference = rotated_reference
        result.rotated_credential_version = rotated_version
        result.rotated_expires_at = rotated_expires
        return result


def _eligible_user_type(user: dict[str, Any]) -> str:
    """Map Microsoft ``userType`` onto the discovery ``USER``/``OTHER`` split.

    Conservative: only ``Member`` (a real tenant user) is sender-eligible;
    ``Guest`` and any unknown value are not promoted to a Mailbox.
    """
    user_type = str(user.get("userType") or "").strip().lower()
    if user_type == "member":
        return "USER"
    return "OTHER"


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _register_microsoft_discovery_provider() -> None:
    from app.email_providers.mailbox_discovery import _DISCOVERY_PROVIDERS

    _DISCOVERY_PROVIDERS["MICROSOFT"] = MicrosoftMailboxDiscoveryProvider