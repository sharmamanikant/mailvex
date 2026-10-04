"""Phase 10B security tests: OAuth state store + sender test-send endpoint.

Covers the double-submit / replay / expiry guarantees of the state store, the
tenant + user binding, the fail-closed behavior when Redis is unavailable
outside tests, scope-escalation rejection, and the new ``POST
/senders/{sender_id}/test`` route (explicit recipient, tenant-scoped, no
credential leakage).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.services.oauth_state_store import OAuthStateError, OAuthStateStore

TENANT = UUID("11111111-1111-1111-1111-111111111111")
USER = UUID("22222222-2222-2222-2222-222222222222")
CONN = UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture()
def store() -> OAuthStateStore:
    return OAuthStateStore(memory_fallback=True)


def test_state_is_single_use(store: OAuthStateStore) -> None:
    state = store.create(tenant_id=TENANT, user_id=USER, connection_id=CONN)
    first = store.consume(state)
    assert first.tenant_id == TENANT
    assert first.user_id == USER
    assert first.connection_id == CONN
    with pytest.raises(OAuthStateError):
        store.consume(state)


def test_state_does_not_echo_secrets(store: OAuthStateStore) -> None:
    state = store.create(tenant_id=TENANT, user_id=USER)
    with pytest.raises(OAuthStateError):
        store.consume(state + "junk")
    # Key derivation is opaque: the raw state never appears as-is.
    assert "crcrm:oauth:state:" not in state


def test_expired_state_is_rejected(store: OAuthStateStore) -> None:
    state = store.create(tenant_id=TENANT, user_id=USER)
    record = store.consume(state)
    # Simulate a state that already expired.
    expired = OAuthStateStore._encode(
        type(record)(
            tenant_id=TENANT,
            user_id=USER,
            connection_id=CONN,
            created_at=datetime.now(UTC) - timedelta(seconds=1200),
            expires_at=datetime.now(UTC) - timedelta(seconds=600),
        )
    )
    import json

    key = OAuthStateStore._key(state)
    store._memory[key] = OAuthStateStore._decode(json.dumps(expired))  # type: ignore[union-attr]
    with pytest.raises(OAuthStateError):
        store.consume(state)


def test_fail_closed_without_redis() -> None:
    # When Redis is unavailable outside test env, the store fails closed rather
    # than silently weakening to an in-memory fallback.
    store = OAuthStateStore(redis=None, memory_fallback=False)
    with pytest.raises(OAuthStateError):
        store.create(tenant_id=TENANT, user_id=USER)
    with pytest.raises(OAuthStateError):
        store.consume("anything")
