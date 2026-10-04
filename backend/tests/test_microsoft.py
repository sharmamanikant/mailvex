from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.providers.base import ProviderMessage, ProviderProfile
from app.providers.microsoft import MicrosoftGraphError, MicrosoftGraphProvider


def response(payload: dict, status: int = 200, headers: dict[str, str] | None = None) -> Mock:
    result = Mock()
    result.status_code = status
    result.headers = headers or {}
    result.ok = status < 400
    result.content = b"{}" if payload else b""
    result.json.return_value = payload
    return result


def test_graph_profile_send_and_draft() -> None:
    provider = MicrosoftGraphProvider(ProviderProfile("old@example.com"), "access-token")
    provider.session = Mock()
    provider.session.request.side_effect = [response({"mail": "owner@example.com", "displayName": "Owner", "mailboxSettings": {"timeZone": "Pacific Standard Time"}}), response({}), response({"id": "draft-1"})]
    provider.connect()
    profile = provider.get_profile()
    assert profile.email == "owner@example.com"
    result = provider.send(ProviderMessage("recipient@example.com", "Hello", "<p>Hi</p>"))
    assert result.provider == "MICROSOFT"
    assert provider.create_draft(ProviderMessage("recipient@example.com", "Draft", "<p>Body</p>")) == "draft-1"


def test_graph_throttling_includes_deferred_retry() -> None:
    provider = MicrosoftGraphProvider(ProviderProfile("owner@example.com"), "access-token")
    provider.session = Mock()
    provider.session.request.return_value = response({}, 429, {"Retry-After": "45"})
    provider.connect()
    with pytest.raises(MicrosoftGraphError, match="defer retry") as error:
        provider.get_profile()
    assert error.value.retry_after == 45


def test_graph_requires_server_token() -> None:
    provider = MicrosoftGraphProvider(ProviderProfile("owner@example.com"))
    with pytest.raises(MicrosoftGraphError, match="credentials"):
        provider.connect()
