from __future__ import annotations

from typing import cast
from unittest.mock import Mock, patch

import pytest
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

from app.providers.base import ProviderMessage, ProviderProfile
from app.providers.gmail import GmailProvider


class FakeCredentials:
    valid = True
    expired = False
    refresh_token = "refresh-token"

    def refresh(self, request) -> None:
        self.expired = False


def test_gmail_profile_and_send_are_api_backed() -> None:
    service = Mock()
    service.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": "owner@example.com"}
    service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "gmail-message-1"}
    provider = GmailProvider(ProviderProfile("owner@example.com"), cast(Credentials, FakeCredentials()))
    with patch("app.providers.gmail.build", return_value=service):
        provider.connect()
        assert provider.authenticate()
        assert provider.get_profile().email == "owner@example.com"
        result = provider.send(ProviderMessage("recipient@example.com", "Hello", "<p>Hi</p>", "Hi"))
    assert result.provider_message_id == "gmail-message-1"
    service.users.return_value.messages.return_value.send.assert_called_once()
    raw = service.users.return_value.messages.return_value.send.call_args.kwargs["body"]["raw"]
    assert raw


def test_gmail_refreshes_expired_credentials() -> None:
    credentials = FakeCredentials()
    credentials.expired = True
    with patch("app.providers.gmail.build", return_value=Mock()) as build:
        GmailProvider(ProviderProfile("owner@example.com"), cast(Credentials, credentials)).connect()
    assert credentials.refresh_token
    build.assert_called_once()


def test_gmail_throttling_is_safe_and_deferred() -> None:
    service = Mock()
    error = HttpError(Mock(status=429), b"quota exceeded")
    service.users.return_value.messages.return_value.send.return_value.execute.side_effect = error
    provider = GmailProvider(ProviderProfile("owner@example.com"), cast(Credentials, FakeCredentials()))
    with patch("app.providers.gmail.build", return_value=service):
        provider.connect()
        with pytest.raises(RuntimeError, match="defer retry"):
            provider.send(ProviderMessage("recipient@example.com", "Hello", "<p>Hi</p>"))
