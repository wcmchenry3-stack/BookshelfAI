"""Access-token revocation via users.token_version (#689).

- Every access token carries ``tv``; ``get_current_user`` compares it with the
  user row it already loads. A missing, malformed or stale ``tv`` is 401
  ``token_revoked``; a ``tv`` edited without re-signing fails the signature.
- ``POST /auth/logout-all`` bumps token_version and revokes every refresh
  token, so neither the old access token nor any old refresh token works.
- A refresh token whose ``tv`` is stale (one minted by a refresh that raced the
  bump) cannot mint tokens either.

Database-free: a small stateful fake session stands in for Postgres. The same
flows run against real Postgres in tests/integration/test_token_version_db.py.
"""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.limiter import limiter
from app.main import app
from app.models.refresh_token import RefreshToken
from app.models.user import User

pytestmark = pytest.mark.security

CLAIMS = {
    "sub": "google-sub-689",
    "email": "revoke@example.com",
    "email_verified": True,
    "name": "Revoke Test",
    "picture": None,
}


@pytest.fixture(scope="module")
def _keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    return private, public


@pytest.fixture(autouse=True)
def _jwt_settings(_keys):
    with (
        patch.object(settings, "jwt_private_key", _keys[0]),
        patch.object(settings, "jwt_public_key", _keys[1]),
    ):
        yield


class FakeDb:
    """Stateful enough for sign-in, refresh, /auth/me and logout-all: one
    user, its refresh-token rows, and the two UPDATEs revoke_all_sessions
    issues. Counts every statement so a test can assert none was added."""

    def __init__(self) -> None:
        self.user: User | None = None
        self.tokens: list[RefreshToken] = []
        self.gets = 0
        self.executes = 0

    async def execute(self, stmt):
        self.executes += 1
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        if getattr(stmt, "is_update", False):
            params = stmt.compile().params
            if stmt.table.name == "users":
                assert params["id_1"] == self.user.id
                self.user.token_version += 1
            elif stmt.table.name == "refresh_tokens":
                for t in self.tokens:
                    if t.user_id == params["user_id_1"] and t.revoked_at is None:
                        t.revoked_at = params["revoked_at"]
            return result
        entity = stmt.column_descriptions[0]["entity"]
        if entity is User:
            result.scalar_one_or_none.return_value = self.user
        elif entity is RefreshToken:
            jtis = {str(v) for v in stmt.compile().params.values()}
            result.scalar_one_or_none.return_value = next(
                (t for t in self.tokens if t.jti in jtis), None
            )
        return result

    async def get(self, model, pk):
        self.gets += 1
        if model is User and self.user is not None and self.user.id == pk:
            return self.user
        return None

    def add(self, obj):
        if isinstance(obj, User):
            obj.id = obj.id or uuid.uuid4()
            self.user = obj
        elif isinstance(obj, RefreshToken):
            self.tokens.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def refresh(self, obj, attrs=None):
        if isinstance(obj, User):
            obj.created_at = obj.updated_at = datetime.now(UTC)

    def begin_nested(self):
        db = self

        class _Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.fixture
def db():
    from app.core.database import get_db

    fake = FakeDb()

    async def _db():
        yield fake

    app.dependency_overrides[get_db] = _db
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _sign_in(client) -> dict:
    with patch("app.api.auth.verify_google_id_token", AsyncMock(return_value=CLAIMS)):
        resp = client.post("/auth/google", json={"id_token": "t"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sign(payload: dict, private_key: str) -> str:
    return jwt.encode(payload, private_key, algorithm="RS256")


_DROP = object()  # marks a claim to leave out


def _access_payload(user: User, **overrides) -> dict:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "type": "access",
        "tv": user.token_version,
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not _DROP}


class TestAccessTokenRevocation:
    def test_tokens_carry_tv_and_15_minute_ttl(self, db, client):
        tokens = _sign_in(client)
        claims = jwt.decode(
            tokens["access_token"], settings.jwt_public_key, algorithms=["RS256"]
        )
        assert claims["tv"] == 0
        assert claims["exp"] - claims["iat"] == 15 * 60
        assert tokens["expires_in"] == 15 * 60
        refresh = jwt.decode(
            tokens["refresh_token"], settings.jwt_public_key, algorithms=["RS256"]
        )
        assert refresh["tv"] == 0

    def test_bumped_token_version_rejects_old_token_without_extra_query(
        self, db, client
    ):
        tokens = _sign_in(client)
        auth = _bearer(tokens["access_token"])

        db.gets = db.executes = 0
        assert client.get("/auth/me", headers=auth).status_code == 200
        baseline = (db.gets, db.executes)

        db.user.token_version += 1
        db.gets = db.executes = 0
        resp = client.get("/auth/me", headers=auth)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "token_revoked"
        # The check reuses the user row the dependency already loads.
        assert (db.gets, db.executes) == baseline == (1, 0)

    def test_token_without_tv_is_revoked(self, db, client, _keys):
        _sign_in(client)
        token = _sign(_access_payload(db.user, tv=_DROP), _keys[0])
        resp = client.get("/auth/me", headers=_bearer(token))
        assert resp.status_code == 401
        assert resp.json()["detail"] == "token_revoked"

    @pytest.mark.parametrize("bad", ["0", None, True, False, 0.0, [0], {"v": 0}])
    def test_malformed_tv_is_revoked(self, db, client, _keys, bad):
        _sign_in(client)
        token = _sign(_access_payload(db.user, tv=bad), _keys[0])
        resp = client.get("/auth/me", headers=_bearer(token))
        assert resp.status_code == 401
        assert resp.json()["detail"] == "token_revoked"

    def test_tv_edited_without_resigning_fails_signature(self, db, client):
        tokens = _sign_in(client)
        db.user.token_version = 3
        header, body, signature = tokens["access_token"].split(".")
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        assert claims["tv"] == 0
        claims["tv"] = 3  # what an attacker would need after a bump
        forged_body = (
            base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        )
        forged = f"{header}.{forged_body}.{signature}"
        resp = client.get("/auth/me", headers=_bearer(forged))
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid token"

    def test_tv_signed_with_another_key_fails_signature(self, db, client):
        _sign_in(client)
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pem = other.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        token = _sign(_access_payload(db.user), other_pem)
        resp = client.get("/auth/me", headers=_bearer(token))
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid token"


class TestLogoutAll:
    def test_revokes_access_and_refresh_tokens(self, db, client):
        first = _sign_in(client)  # device 1
        second = _sign_in(client)  # device 2
        assert len(db.tokens) == 2

        resp = client.post("/auth/logout-all", headers=_bearer(first["access_token"]))
        assert resp.status_code == 204
        assert resp.content == b""
        assert db.user.token_version == 1
        assert all(t.revoked_at is not None for t in db.tokens)

        for tokens in (first, second):
            me = client.get("/auth/me", headers=_bearer(tokens["access_token"]))
            assert me.status_code == 401
            assert me.json()["detail"] == "token_revoked"
            refreshed = client.post(
                "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            )
            assert refreshed.status_code == 401

        # Signing in again works and yields tokens at the new version.
        fresh = _sign_in(client)
        assert (
            client.get("/auth/me", headers=_bearer(fresh["access_token"])).status_code
            == 200
        )

    def test_refresh_token_with_stale_tv_cannot_refresh(self, db, client):
        """A refresh that raced the bump left an unrevoked row whose token
        carries the old tv: it must not mint tokens, and the row is revoked."""
        tokens = _sign_in(client)
        db.user.token_version += 1  # bump; the row stays unrevoked (the race)
        resp = client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "token_revoked"
        assert db.tokens[0].revoked_at is not None
        assert len(db.tokens) == 1  # nothing new issued

    def test_refresh_token_without_tv_cannot_refresh(self, db, client, _keys):
        _sign_in(client)
        row = db.tokens[0]
        now = datetime.now(UTC)
        legacy = _sign(
            {
                "sub": str(db.user.id),
                "iat": now,
                "exp": now + timedelta(days=1),
                "type": "refresh",
                "jti": row.jti,
            },
            _keys[0],
        )
        resp = client.post("/auth/refresh", json={"refresh_token": legacy})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "token_revoked"
        assert row.revoked_at is not None

    def test_refresh_still_works_without_a_bump(self, db, client):
        tokens = _sign_in(client)
        resp = client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert resp.status_code == 200, resp.text
        assert (
            client.get(
                "/auth/me", headers=_bearer(resp.json()["access_token"])
            ).status_code
            == 200
        )

    def test_requires_authentication(self, db, client):
        resp = client.post("/auth/logout-all")
        assert resp.status_code in (401, 403)
        resp = client.post("/auth/logout-all", headers=_bearer("not.a.jwt"))
        assert resp.status_code == 401
        assert db.user is None

    def test_refresh_token_is_not_an_access_token(self, db, client):
        tokens = _sign_in(client)
        resp = client.post("/auth/logout-all", headers=_bearer(tokens["refresh_token"]))
        assert resp.status_code == 401
        assert db.user.token_version == 0

    def test_revoked_token_cannot_call_logout_all(self, db, client):
        tokens = _sign_in(client)
        db.user.token_version = 5
        resp = client.post("/auth/logout-all", headers=_bearer(tokens["access_token"]))
        assert resp.status_code == 401
        assert db.user.token_version == 5

    def test_ignores_any_body(self, db, client):
        """No client-sent user ID or version is read: the body is ignored."""
        tokens = _sign_in(client)
        resp = client.post(
            "/auth/logout-all",
            headers=_bearer(tokens["access_token"]),
            json={"user_id": str(uuid.uuid4()), "token_version": 99},
        )
        assert resp.status_code == 204
        assert db.user.token_version == 1

    def test_has_auth_rate_limit(self):
        limits = limiter._route_limits.get("app.api.auth.logout_all")
        assert limits
        assert {str(lim.limit) for lim in limits} == {
            str(lim.limit) for lim in limiter._route_limits["app.api.auth.refresh"]
        }

    def test_rate_limit_returns_429(self, db, client):
        tokens = _sign_in(client)
        # Each call bumps the version; reset it so the token stays valid.
        codes = []
        for _ in range(6):
            codes.append(
                client.post(
                    "/auth/logout-all", headers=_bearer(tokens["access_token"])
                ).status_code
            )
            db.user.token_version = 0
        assert codes[:5] == [204] * 5
        assert codes[5] == 429
