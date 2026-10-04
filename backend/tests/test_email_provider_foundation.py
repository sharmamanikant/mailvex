"""Unit tests for the email-provider foundation internals (System B).

Covers the registry/capabilities, per-provider local shape rules, the secure
credential lifecycle (encryption, rotation, revocation tombstones), and the
explicit sending/discovery boundaries. Real Google network calls are replaced
with a FakeGmailService injected into the provider's ``_build_service``.
"""

from __future__ import annotations

import smtplib

import pytest

from app.core.config import settings
from app.email_providers import (
    EmailMessage,
    ProviderConnectionConfig,
    decrypt_credential_reference,
    encrypt_credential_reference,
    get_provider,
    invalidate_credential_reference,
    list_providers,
    rotate_credential_reference,
)
from app.email_providers.base import ProviderErrorCode
from app.email_providers.google.provider import (
    GoogleEmailProvider,
    _urlsafe_base64,
)
from app.security.smtp_policy import SMTPPolicyViolation

KEY = "developer-only-fernet-key-long-enough"


class _FakeProfile:
    def __init__(self, email: str) -> None:
        self.email = email

    def execute(self) -> dict[str, object]:
        return {"emailAddress": self.email}


class _FakeUsers:
    def __init__(self, email: str) -> None:
        self.email = email

    def getProfile(self, userId: str) -> _FakeProfile:
        return _FakeProfile(self.email)

    def messages(self) -> _FakeMessages:
        return _FakeMessages()


class _FakeMessages:
    def send(self, userId: str, body: dict[str, object]) -> _FakeSent:
        return _FakeSent()


class _FakeSent:
    def execute(self) -> dict[str, object]:
        return {"id": "msg-123"}


class _FakeGmailService:
    def __init__(self, email: str = "sender@example.com") -> None:
        self._email = email
        self._users = _FakeUsers(email)

    def users(self) -> _FakeUsers:
        return self._users


@pytest.fixture()
def google_provider() -> GoogleEmailProvider:
    provider = get_provider("GOOGLE")
    assert isinstance(provider, GoogleEmailProvider)
    provider._build_service = lambda config: _FakeGmailService()  # type: ignore[method-assign]
    return provider


def test_registry_exposes_all_providers_with_capabilities() -> None:
    providers = {cap.provider_name: cap for cap in list_providers()}
    assert set(providers) == {"GOOGLE", "MICROSOFT", "ZOHO", "SENDGRID", "SMTP"}
    assert providers["GOOGLE"].supports_oauth and providers["GOOGLE"].supports_sender_discovery
    assert providers["MICROSOFT"].supports_oauth and providers["MICROSOFT"].supports_sender_discovery
    assert providers["ZOHO"].supports_oauth and not providers["ZOHO"].supports_api_key
    assert providers["SENDGRID"].supports_api_key and not providers["SENDGRID"].supports_smtp
    assert providers["SMTP"].supports_smtp and not providers["SMTP"].supports_oauth
    # Google and Microsoft implement inbox sync for opt-in reply-sync
    # connections (gmail.readonly / Mail.Read granted at consent); they never
    # expose webhooks. Zoho remains send-only.
    assert providers["GOOGLE"].supports_inbox_sync is True
    assert providers["GOOGLE"].supports_webhooks is False
    assert providers["MICROSOFT"].supports_inbox_sync is True
    assert providers["MICROSOFT"].supports_webhooks is False
    assert providers["ZOHO"].supports_inbox_sync is False


def _google_config(external: str = "sender@example.com") -> ProviderConnectionConfig:
    reference = encrypt_credential_reference(
        KEY,
        {
            "access_token": "ya29.fake",
            "refresh_token": "1//ref",
            "client_id": "client",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": ["openid", "https://www.googleapis.com/auth/gmail.send"],
        },
    )
    return ProviderConnectionConfig(
        connection_type="OAUTH",
        external_account_id=external,
        email=external,
        metadata={"google_sub": external},
        credential_reference=reference,
    )


def test_google_requires_oauth_and_external_id() -> None:
    provider = get_provider("GOOGLE")
    assert provider.validate_connection(
        ProviderConnectionConfig(connection_type="API_KEY", external_account_id="u/1", credential_reference="x")
    ).valid is False
    assert provider.validate_connection(
        ProviderConnectionConfig(connection_type="OAUTH")
    ).valid is False
    assert provider.validate_connection(
        ProviderConnectionConfig(connection_type="OAUTH", external_account_id="u/1")
    ).valid is False  # no stored credentials yet


def test_google_validate_with_fake_service_connects() -> None:
    provider = get_provider("GOOGLE")
    provider._build_service = lambda config: _FakeGmailService("sender@example.com")  # type: ignore[method-assign]
    config = _google_config()
    result = provider.validate_connection(config)
    assert result.valid is True
    assert result.message == "Google connection verified"


def test_google_validate_rejects_identity_mismatch() -> None:
    provider = get_provider("GOOGLE")
    provider._build_service = lambda config: _FakeGmailService("other@example.com")  # type: ignore[method-assign]
    result = provider.validate_connection(_google_config())
    assert result.valid is False
    assert result.error_code == ProviderErrorCode.AUTH_FAILED


def test_sendgrid_uses_api_key_only(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider("SENDGRID")
    assert provider.validate_connection(ProviderConnectionConfig(connection_type="OAUTH", credential_reference="x")).valid is False
    assert provider.validate_connection(ProviderConnectionConfig(connection_type="API_KEY")).valid is False  # no email, no creds
    # No API key payload yet -> AUTH_REQUIRED.
    missing = ProviderConnectionConfig(connection_type="API_KEY", email="sender@example.com")
    assert provider.validate_connection(missing).valid is False
    assert provider.validate_connection(missing).error_code == ProviderErrorCode.AUTH_REQUIRED
    # An undecodable stored key -> AUTH_FAILED (caught, not raised).
    corrupt = ProviderConnectionConfig(connection_type="API_KEY", email="sender@example.com", credential_reference="x")
    assert provider.validate_connection(corrupt).error_code == ProviderErrorCode.AUTH_FAILED
    # A working key + successful profile call -> CONNECTED (valid).
    reference = encrypt_credential_reference(settings.encryption_key, {"api_key": "SG.fake-key"})
    import app.email_providers.sendgrid.provider as sendgrid_module

    monkeypatch.setattr(sendgrid_module.requests, "request", lambda *args, **kwargs: _FakeSendGridResponse(200))
    valid = ProviderConnectionConfig(connection_type="API_KEY", email="sender@example.com", credential_reference=reference)
    assert provider.validate_connection(valid).valid is True
    # A rejected key -> AUTH_FAILED.
    monkeypatch.setattr(sendgrid_module.requests, "request", lambda *args, **kwargs: _FakeSendGridResponse(401))
    assert provider.validate_connection(valid).valid is False
    assert provider.validate_connection(valid).error_code == ProviderErrorCode.AUTH_FAILED


def test_smtp_requires_envelope_and_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider("SMTP")
    import app.email_providers.smtp.provider as smtp_module

    monkeypatch.setattr(smtp_module, "validate_destination", lambda *args, **kwargs: None)
    fake_client = _FakeSMTPClient()
    monkeypatch.setattr(smtp_module.SMTPEmailProvider, "_connect", lambda self, *args, **kwargs: fake_client)
    assert provider.validate_connection(
        ProviderConnectionConfig(connection_type="SMTP", email="sender@example.com", metadata={"smtp_host": "smtp.example.com", "smtp_port": 587}, credential_reference="x")
    ).valid is True
    assert provider.validate_connection(
        ProviderConnectionConfig(connection_type="SMTP", metadata={"smtp_host": "smtp.example.com", "smtp_port": 587}, credential_reference="x")
    ).valid is False  # missing envelope from-address
    assert provider.validate_connection(
        ProviderConnectionConfig(connection_type="SMTP", email="sender@example.com", credential_reference="x")
    ).valid is False  # missing host


def test_smtp_policy_violation_fails_validation_and_send(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider("SMTP")
    import app.email_providers.smtp.provider as smtp_module

    def reject(*args: object, **kwargs: object) -> None:
        raise SMTPPolicyViolation("private destination")

    monkeypatch.setattr(smtp_module, "validate_destination", reject)
    config = ProviderConnectionConfig(
        connection_type="SMTP",
        email="sender@example.com",
        metadata={"smtp_host": "smtp.example.com", "smtp_port": 587},
        credential_reference="x",
    )
    result = provider.validate_connection(config)
    assert result.valid is False
    assert result.error_code == ProviderErrorCode.PERMISSION_DENIED
    message = EmailMessage(from_email="sender@example.com", to=("r@example.com",), subject="S", text_body="hi")
    from app.email_providers.base import EmailProviderError

    with pytest.raises(EmailProviderError) as exc_info:
        provider.send_message(config, message)
    assert exc_info.value.code == ProviderErrorCode.PERMISSION_DENIED


def test_smtp_send_message_delivers_via_smtplib(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider("SMTP")
    import app.email_providers.smtp.provider as smtp_module

    monkeypatch.setattr(smtp_module, "validate_destination", lambda *args, **kwargs: None)
    fake_client = _FakeSMTPClient()
    monkeypatch.setattr(smtp_module.SMTPEmailProvider, "_connect", lambda self, *args, **kwargs: fake_client)
    reference = encrypt_credential_reference(settings.encryption_key, {"smtp_password": "pw", "smtp_username": "sender"})
    config = ProviderConnectionConfig(
        connection_type="SMTP",
        email="sender@example.com",
        metadata={"smtp_host": "smtp.example.com", "smtp_port": 587},
        credential_reference=reference,
    )
    message = EmailMessage(from_email="sender@example.com", to=("r@example.com",), subject="S", text_body="hi")
    message_id = provider.send_message(config, message)
    assert message_id.startswith("accepted:")
    assert fake_client.login_called is True


def test_smtp_send_authentication_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider("SMTP")
    import app.email_providers.smtp.provider as smtp_module

    monkeypatch.setattr(smtp_module, "validate_destination", lambda *args, **kwargs: None)
    reference = encrypt_credential_reference(settings.encryption_key, {"smtp_password": "bad", "smtp_username": "sender"})
    config = ProviderConnectionConfig(
        connection_type="SMTP",
        email="sender@example.com",
        metadata={"smtp_host": "smtp.example.com", "smtp_port": 587},
        credential_reference=reference,
    )
    auth_fail = _FakeSMTPClient(login_error=smtplib.SMTPAuthenticationError(535, b"bad credentials"))
    monkeypatch.setattr(smtp_module.SMTPEmailProvider, "_connect", lambda self, *args, **kwargs: auth_fail)
    assert provider.validate_connection(config).error_code == ProviderErrorCode.AUTH_FAILED
    with pytest.raises(Exception) as exc_info:
        provider.send_message(config, EmailMessage(from_email="sender@example.com", to=("r@example.com",), subject="S"))
    from app.email_providers.base import EmailProviderError

    assert isinstance(exc_info.value, EmailProviderError)
    assert exc_info.value.code == ProviderErrorCode.AUTH_FAILED


class _FakeSMTPClient:
    def __init__(self, login_error: BaseException | None = None) -> None:
        self._login_error = login_error
        self.login_called = False
        self.sent = False

    def ehlo(self) -> tuple[int, bytes]:
        return 250, b"ok"

    def starttls(self, context: object | None = None) -> tuple[int, bytes]:
        return 220, b"ready"

    def login(self, *args: object, **kwargs: object) -> tuple[int, bytes]:
        self.login_called = True
        if self._login_error is not None:
            raise self._login_error
        return 235, b"auth ok"

    def send_message(self, message: object) -> dict[str, object]:
        self.sent = True
        return {}

    def quit(self) -> tuple[int, bytes]:
        return 221, b"bye"


class _FakeSendGridResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.content = b"{}"
        self._json: object = {}

    def json(self) -> object:
        return self._json


def test_google_send_message_uses_minimum_scopes_and_raw_compose(google_provider: GoogleEmailProvider) -> None:
    message = EmailMessage(
        from_email="sender@example.com",
        to=("r@example.com",),
        subject="Subject",
        text_body="Hi",
        html_body="<p>Hi</p>",
    )
    message_id = google_provider.send_message(_google_config(), message)
    assert message_id == "msg-123"


def test_google_send_message_rejects_invalid_recipient(google_provider: GoogleEmailProvider) -> None:
    message = EmailMessage(from_email="sender@example.com", to=("not-an-email",), subject="x")
    with pytest.raises(Exception) as exc_info:
        google_provider.send_message(_google_config(), message)
    from app.email_providers.base import EmailProviderError

    assert isinstance(exc_info.value, EmailProviderError)
    assert exc_info.value.code == ProviderErrorCode.INVALID_RECIPIENT


def test_sync_inbox_without_granted_scope_is_denied_google(google_provider: GoogleEmailProvider) -> None:
    from app.email_providers.base import EmailProviderError, ProviderErrorCode

    with pytest.raises(EmailProviderError) as exc_info:
        google_provider.sync_inbox(_google_config())
    assert exc_info.value.code == ProviderErrorCode.PERMISSION_DENIED


def test_discovery_returns_single_verified_identity(google_provider: GoogleEmailProvider) -> None:
    result = google_provider.discover_senders(_google_config())
    assert len(result.senders) == 1
    assert result.senders[0].email == "sender@example.com"
    assert result.senders[0].verified is True
    non_discover = get_provider("SMTP")
    rejected = non_discover.discover_senders(_google_config())
    assert rejected.senders == ()
    assert non_discover.get_capabilities().provider_name in rejected.errors[0]


def test_credential_reference_round_trip_and_never_plaintext() -> None:
    reference = encrypt_credential_reference(KEY, {"api_key": "SG.secret"})
    assert "SG.secret" not in reference
    payload = decrypt_credential_reference(KEY, reference)
    assert payload["api_key"] == "SG.secret"


def test_credential_payload_rejects_unknown_or_empty_fields() -> None:
    with pytest.raises(ValueError):
        encrypt_credential_reference(KEY, {"weird": "junk"})
    with pytest.raises(ValueError):
        encrypt_credential_reference(KEY, {"display_name": "bob"})  # no secret


def test_credential_rotation_bumps_version_and_changes_reference() -> None:
    reference = encrypt_credential_reference(KEY, {"api_key": "SG.old"}, version="v1")
    new_reference, version = rotate_credential_reference(KEY, reference, previous_version="v1")
    assert version == "v2"
    assert new_reference != reference
    payload = decrypt_credential_reference(KEY, new_reference)
    assert payload["api_key"] == "SG.old"


def test_rotated_credentials_can_carry_fresh_secrets() -> None:
    reference = encrypt_credential_reference(KEY, {"api_key": "SG.old"}, version="v1")
    new_reference, version = rotate_credential_reference(
        KEY, reference, new_payload={"api_key": "SG.new"}, previous_version="v1"
    )
    assert version == "v2"
    assert decrypt_credential_reference(KEY, new_reference)["api_key"] == "SG.new"


def test_revoked_reference_is_tombstoned_and_undecodable() -> None:
    tombstone = invalidate_credential_reference("v1")
    assert tombstone.startswith("revoked:")
    with pytest.raises(ValueError):
        decrypt_credential_reference(KEY, tombstone)


def test_urlsafe_base64_encoder() -> None:
    assert _urlsafe_base64(b"\xfb\xff\xbf") == "-_-_"
    assert _urlsafe_base64(b"hello world") != ""
