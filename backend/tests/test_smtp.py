from __future__ import annotations

import smtplib
from unittest.mock import Mock

import pytest

from app.providers.base import ProviderMessage, ProviderProfile
from app.providers.smtp import SMTPProvider, SMTPProviderError


def test_tls_modes_use_expected_transport(monkeypatch) -> None:
    connection = Mock()
    ssl = Mock(return_value=connection)
    plain = Mock(return_value=connection)
    monkeypatch.setattr(smtplib, "SMTP_SSL", ssl)
    monkeypatch.setattr(smtplib, "SMTP", plain)

    SMTPProvider(ProviderProfile("from@example.com"), "localhost", 465, "SSL", validate=False).connect()
    ssl.assert_called_once_with("localhost", 465, timeout=15)
    plain.assert_not_called()

    SMTPProvider(ProviderProfile("from@example.com"), "localhost", 587, "STARTTLS", validate=False).connect()
    connection.starttls.assert_called_once()

    connection.reset_mock()
    SMTPProvider(ProviderProfile("from@example.com"), "localhost", 587, "TLS", validate=False).connect()
    connection.starttls.assert_called_once()


def test_invalid_tls_configuration_is_rejected() -> None:
    with pytest.raises(SMTPProviderError, match="TLS mode"):
        SMTPProvider(ProviderProfile("from@example.com"), "localhost", 25, "PLAINTEXT").connect()


def test_local_smtp_mock_auth_and_send(monkeypatch) -> None:
    connection = Mock()
    monkeypatch.setattr(smtplib, "SMTP", Mock(return_value=connection))
    provider = SMTPProvider(ProviderProfile("from@example.com"), "127.0.0.1", 2525, "STARTTLS", "user", "secret", allow_private_hosts=True)
    provider.connect()
    connection.login.assert_called_once_with("user", "secret")
    result = provider.send(ProviderMessage("to@example.com", "Test", "<p>Hello</p>", "Hello"))
    assert result.provider == "SMTP"
    connection.send_message.assert_called_once()
    provider.disconnect()
