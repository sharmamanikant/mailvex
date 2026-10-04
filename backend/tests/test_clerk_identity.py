"""Clerk identity (System A): token verification + user mapping (Phase 10A).

Tests mint real RS256 Clerk-shaped JWTs with a local RSA key served through a
fake JWKS, so the verifier's full crypto path (header, kid, signature, issuer,
audience, expiry) is exercised end-to-end without any network access. API
tests swap ``get_verifier`` for the fake so requests hit the real mapping,
provisioning, RBAC and tenancy code.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import get_db
from app.identity import dependencies as identity_dependencies
from app.identity.clerk import ClerkJWTVerifier, ClerkTokenError
from app.main import app
from app.models import AuditLog, Base, Role, Tenant, User, UserRole
from app.security import permissions as permissions_module
from app.security.passwords import hash_password
from app.security.tokens import TokenService

PASSWORD = "correct horse battery staple"
ISSUER = "https://accounts.crcrm.test"
KID = "crcrm-key-1"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    public = private_key.public_key()
    numbers = public.public_numbers()
    n_bytes = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    e_bytes = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {"kty": "RSA", "kid": kid, "n": _b64(n_bytes), "e": _b64(e_bytes), "alg": "RS256", "use": "sig"}


def _mint_token(
    private_key: rsa.RSAPrivateKey,
    kid: str = KID,
    *,
    sub: str = "user_test_001",
    email: str | None = "clerk@example.com",
    issuer: str = ISSUER,
    audience: str | None = None,
    expiry_offset: int = 3600,
    extra_claims: dict[str, object] | None = None,
    include_sub: bool = True,
) -> str:
    now = int(datetime.now(UTC).timestamp())
    claims: dict[str, object] = {
        "sub": sub,
        "sid": f"sess_{sub}",
        "iss": issuer,
        "iat": now,
        "nbf": now - 10,
        "exp": now + expiry_offset,
    }
    if email is not None:
        claims["email"] = email
    claims["first_name"] = "Clara"
    claims["last_name"] = "Owens"
    if audience is not None:
        claims["aud"] = audience
    if extra_claims:
        claims.update(extra_claims)
    if not include_sub:
        del claims["sub"]
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


class _FakeClerkJWTVerifier(ClerkJWTVerifier):
    def __init__(
        self,
        private_key: rsa.RSAPrivateKey,
        kid: str = KID,
        issuer: str = ISSUER,
        audience: str | None = None,
    ) -> None:
        super().__init__(issuer=issuer, jwks_url=f"{issuer}/.well-known/jwks.json", audience=audience)
        self._private_key = private_key
        self._kid = kid

    def _fetch_jwks(self) -> list[dict[str, str]]:
        return [_jwk(self._private_key, self._kid)]


def _verifier(private_key: rsa.RSAPrivateKey, **kwargs: object) -> _FakeClerkJWTVerifier:
    return _FakeClerkJWTVerifier(private_key, **kwargs)


# --------------------------------------------------------------------- #
# Verifier unit tests (pure crypto; no DB, no network)
# --------------------------------------------------------------------- #
def test_verifier_accepts_valid_token() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier(private_key)
    identity = verifier.verify(_mint_token(private_key))
    assert identity.external_identity_id == "user_test_001"
    assert identity.email == "clerk@example.com"
    assert identity.identity_provider == "CLERK"
    assert identity.display_name == "Clara Owens"
    assert identity.mapped_email == "clerk@example.com"


def test_verifier_rejects_expired_token() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier(private_key)
    with pytest.raises(ClerkTokenError):
        verifier.verify(_mint_token(private_key, expiry_offset=-300))


def test_verifier_rejects_wrong_issuer() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier(private_key)
    with pytest.raises(ClerkTokenError):
        verifier.verify(_mint_token(private_key, issuer="https://evil.example.com"))


def test_verifier_rejects_tampered_signature() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier(private_key)
    token = _mint_token(private_key)
    header, payload, _signature = token.split(".")
    forged = f"{header}.{payload}.invalid-signature"
    with pytest.raises(ClerkTokenError):
        verifier.verify(forged)


def test_verifier_rejects_missing_subject() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier(private_key)
    with pytest.raises(ClerkTokenError):
        verifier.verify(_mint_token(private_key, include_sub=False))


def test_verifier_rejects_missing_kid() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier(private_key)
    body = {"sub": "user_test_001", "exp": int(datetime.now(UTC).timestamp()) + 60}
    no_kid = jwt.encode(body, private_key, algorithm="RS256", headers={})
    with pytest.raises(ClerkTokenError, match="key id"):
        verifier.verify(no_kid)


def test_verifier_rejects_unknown_kid_even_after_refresh() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier(private_key, kid="current-key")
    with pytest.raises(ClerkTokenError, match="Signing key"):
        verifier.verify(_mint_token(private_key, kid="rotated-key"))


def test_verifier_honors_configured_audience() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier(private_key, audience="crcrm")
    assert verifier.verify(_mint_token(private_key, audience="crcrm")).external_identity_id == "user_test_001"
    with pytest.raises(ClerkTokenError):
        verifier.verify(_mint_token(private_key, audience="somewhere-else"))


def test_verifier_maps_absent_email_to_placeholder() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier(private_key)
    identity = verifier.verify(_mint_token(private_key, email=None))
    assert identity.email is None
    assert identity.mapped_email == "clerk-user_test_001@identity.local"


# --------------------------------------------------------------------- #
# API fixture: server-side verification + provisioning on a real DB
# --------------------------------------------------------------------- #
@pytest.fixture()
def clerk_api(tmp_path, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _FakeClerkJWTVerifier(private_key)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'clerk.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(permissions_module, "get_verifier", lambda: verifier)
    monkeypatch.setattr(identity_dependencies, "get_verifier", lambda: verifier)
    client = TestClient(app)
    yield client, session_factory, private_key
    app.dependency_overrides.clear()
    engine.dispose()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_legacy_user(session_factory: sessionmaker, email: str = "clerk@example.com") -> tuple[UUID, UUID]:
    with session_factory() as session:
        tenant = Tenant(name="Legacy Co", slug=f"lc-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        role = Role(tenant_id=tenant.id, name="Admin", is_system=True, description="Workspace admin")
        session.add(role)
        session.flush()
        user = User(
            tenant_id=tenant.id,
            email=email,
            password_hash=hash_password(PASSWORD),
            display_name="Legacy User",
            status="ACTIVE",
        )
        session.add(user)
        session.flush()
        session.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id))
        session.commit()
        return user.id, tenant.id


def _count_users(session_factory: sessionmaker) -> int:
    with session_factory() as session:
        return len(list(session.scalars(select(User)).all()))


def _user(session_factory: sessionmaker, user_id: UUID) -> User:
    with session_factory() as session:
        created = session.get(User, user_id)
        assert created is not None
        return created


# --------------------------------------------------------------------- #
# Provisions & maps
# --------------------------------------------------------------------- #
def test_me_requires_authentication(clerk_api) -> None:
    client, _factory, _key = clerk_api
    assert client.get("/api/v1/auth/me").status_code == 401


def test_clerk_token_provisions_user_owner_role(clerk_api) -> None:
    client, factory, private_key = clerk_api
    response = client.get("/api/v1/auth/me", headers=_headers(_mint_token(private_key)))
    assert response.status_code == 200
    body = response.json()
    assert set(body["roles"]) == {"OWNER"}
    assert "ADMIN" not in body["roles"]
    assert body["email"] == "clerk@example.com"

    created = _user(factory, UUID(body["id"]))
    assert created.external_identity_id == "user_test_001"
    assert created.identity_provider == "CLERK"
    assert created.password_hash is None
    assert created.last_login_at is not None


def test_clerk_provisioning_is_idempotent(clerk_api) -> None:
    client, factory, private_key = clerk_api
    token = _mint_token(private_key)
    first = client.get("/api/v1/auth/me", headers=_headers(token)).json()
    second = client.get("/api/v1/auth/me", headers=_headers(token)).json()
    assert first["id"] == second["id"]
    assert first["tenant_id"] == second["tenant_id"]
    assert _count_users(factory) == 1


def test_clerk_identity_links_existing_legacy_account(clerk_api) -> None:
    client, factory, private_key = clerk_api
    user_id, tenant_id = _seed_legacy_user(factory)
    token = _mint_token(private_key, email="clerk@example.com")
    response = client.get("/api/v1/auth/me", headers=_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(user_id)
    assert body["tenant_id"] == str(tenant_id)
    assert body["roles"] == ["Admin"]

    linked = _user(factory, user_id)
    assert linked.external_identity_id == "user_test_001"
    assert _count_users(factory) == 1  # no duplicate/provisioned user


def test_clerk_provisioning_records_link_audit_without_secrets(clerk_api) -> None:
    client, factory, private_key = clerk_api
    client.get("/api/v1/auth/me", headers=_headers(_mint_token(private_key)))
    with factory() as session:
        rows = list(session.scalars(select(AuditLog).where(AuditLog.action == "CLERK_IDENTITY_LINKED")).all())
    assert len(rows) == 1
    serialized = str(rows[0].audit_metadata)
    assert "user_test_001" in serialized
    for forbidden in ("access_token", "token_claims", "refresh_token", "pii"):
        assert forbidden not in serialized


# --------------------------------------------------------------------- #
# Rejection matrix at the API boundary
# --------------------------------------------------------------------- #
def test_expired_clerk_token_rejected_by_api(clerk_api) -> None:
    client, _factory, private_key = clerk_api
    response = client.get("/api/v1/auth/me", headers=_headers(_mint_token(private_key, expiry_offset=-300)))
    assert response.status_code == 401


def test_wrong_issuer_clerk_token_rejected_by_api(clerk_api) -> None:
    client, _factory, private_key = clerk_api
    response = client.get("/api/v1/auth/me", headers=_headers(_mint_token(private_key, issuer="https://evil.example.com")))
    assert response.status_code == 401


def test_tampered_clerk_token_rejected_by_api(clerk_api) -> None:
    client, factory, private_key = clerk_api
    forged = _mint_token(private_key).rsplit(".", 1)[0] + ".broken"
    response = client.get("/api/v1/auth/me", headers=_headers(forged))
    assert response.status_code == 401
    assert _count_users(factory) == 0  # never provision from an unverified token


# --------------------------------------------------------------------- #
# Tenancy isolation for Clerk-authenticated principals
# --------------------------------------------------------------------- #
def test_clerk_users_get_distinct_tenants(clerk_api) -> None:
    client, factory, private_key = clerk_api
    identity_a = client.get("/api/v1/auth/me", headers=_headers(_mint_token(private_key, sub="user_a", email="a@example.com"))).json()
    identity_b = client.get("/api/v1/auth/me", headers=_headers(_mint_token(private_key, sub="user_b", email="b@example.com"))).json()
    assert identity_a["tenant_id"] != identity_b["tenant_id"]
    assert _count_users(factory) == 2

    connection = client.post(
        "/api/v1/sender-connections",
        json={"provider": "GOOGLE", "connection_type": "OAUTH", "external_account_id": "user/123"},
        headers=_headers(_mint_token(private_key, sub="user_a", email="a@example.com")),
    )
    assert connection.status_code == 201
    connection_id = connection.json()["id"]

    other = client.get(
        f"/api/v1/sender-connections/{connection_id}",
        headers=_headers(_mint_token(private_key, sub="user_b", email="b@example.com")),
    )
    assert other.status_code == 404
    visible = client.get(
        "/api/v1/sender-connections",
        headers=_headers(_mint_token(private_key, sub="user_b", email="b@example.com")),
    ).json()
    assert visible == []


# --------------------------------------------------------------------- #
# Controlled legacy migration (Part 5)
# --------------------------------------------------------------------- #
def test_legacy_token_falls_back_while_legacy_enabled(clerk_api) -> None:
    client, factory, _key = clerk_api
    # But this test works on a legacy-seeded user, not a Clerk-provisioned one.
    user_id, tenant_id = _seed_legacy_user(factory, email="clerk@example.com")
    token, _ = TokenService(settings).create_access_token(user_id, tenant_id, ["Admin"])
    response = client.get("/api/v1/auth/me", headers=_headers(token))
    assert response.status_code == 200
    assert response.json()["email"] == "clerk@example.com"


def test_legacy_disabled_blocks_legacy_tokens_and_login(clerk_api) -> None:
    client, factory, private_key = clerk_api
    user_id, tenant_id = _seed_legacy_user(factory, email="clerk@example.com")
    token, _ = TokenService(settings).create_access_token(user_id, tenant_id, ["Admin"])

    object.__setattr__(settings, "legacy_auth_enabled", False)
    try:
        assert client.get("/api/v1/auth/me", headers=_headers(token)).status_code == 401
        assert client.post("/api/v1/auth/login", json={"email": "clerk@example.com", "password": PASSWORD}).status_code == 403
        assert client.post("/api/v1/auth/signup", json={"email": "new@example.com", "password": PASSWORD, "display_name": "New"}).status_code == 403
        assert client.post("/api/v1/auth/password-reset/request", json={"email": "clerk@example.com"}).status_code == 403
    finally:
        object.__setattr__(settings, "legacy_auth_enabled", True)

    # Clerk tokens keep working regardless of the legacy flag.
    assert client.get("/api/v1/auth/me", headers=_headers(_mint_token(private_key))).status_code == 200


def test_cross_tenant_api_isolation_with_clerk(clerk_api) -> None:
    client, _factory, private_key = clerk_api
    client.get("/api/v1/auth/me", headers=_headers(_mint_token(private_key, sub="user_a", email="a@example.com")))
    client.get("/api/v1/auth/me", headers=_headers(_mint_token(private_key, sub="user_b", email="b@example.com")))
    connection = client.post(
        "/api/v1/sender-connections",
        json={"provider": "SMTP", "connection_type": "SMTP", "email": "sender@example.com"},
        headers=_headers(_mint_token(private_key, sub="user_a", email="a@example.com")),
    )
    assert connection.status_code == 201
    assert (
        client.get(
            f"/api/v1/sender-connections/{connection.json()['id']}",
            headers=_headers(_mint_token(private_key, sub="user_b", email="b@example.com")),
        ).status_code
        == 404
    )