"""Shared test helpers (#816).

Plain functions and classes, importable from any test module
(``from tests.helpers import ...``). Pytest fixtures that wrap them live in the
``conftest.py`` of the directory that needs them.

- ``rsa_pem_pair`` / ``install_jwt_keys``: a throwaway RS256 key pair and the
  settings patch that makes the app sign and verify with it.
- ``FakeAuthDb`` / ``serve_fake_db``: a small stateful fake session for the
  database-free auth flows (sign-in, refresh, logout-all, ``/auth/me``).
- ``sign_in`` / ``bearer``: the two request helpers every auth flow test uses.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import settings
from app.main import app
from app.models.refresh_token import RefreshToken
from app.models.user import User


def rsa_pem_pair() -> tuple[str, str]:
    """A fresh 2048-bit RSA key pair as ``(private_pem, public_pem)``."""
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


@contextmanager
def install_jwt_keys(keys: tuple[str, str]) -> Iterator[None]:
    """Make ``settings`` sign and verify JWTs with ``keys`` (private, public)."""
    with (
        patch.object(settings, "jwt_private_key", keys[0]),
        patch.object(settings, "jwt_public_key", keys[1]),
    ):
        yield


class FakeAuthDb:
    """One user and its refresh rows, enough for sign-in, refresh, ``/auth/me``
    and logout-all: the selects by jti, ``get`` by primary key, and the two
    UPDATEs ``revoke_all_sessions`` issues. Counts every ``execute`` and ``get``
    so a test can assert none was added."""

    def __init__(self) -> None:
        self.user: User | None = None
        self.tokens: list[RefreshToken] = []
        self.gets = 0
        self.executes = 0

    def by_jti(self, jti: str) -> RefreshToken:
        return next(t for t in self.tokens if t.jti == jti)

    async def execute(self, stmt):
        self.executes += 1
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        if getattr(stmt, "is_update", False):
            params = stmt.compile().params
            if stmt.table.name == "users":
                assert params["id_1"] == self.user.id
                self.user.token_version += 1
                result.scalar_one.return_value = self.user.token_version
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

    async def get(self, model, pk, **_options):
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


def serve_fake_db(fake) -> Iterator:
    """Override ``get_db`` with ``fake`` for one test; yields ``fake``.

    Use from a fixture: ``yield from serve_fake_db(FakeAuthDb())``.
    """
    from app.core.database import get_db

    async def _db():
        yield fake

    app.dependency_overrides[get_db] = _db
    yield fake
    app.dependency_overrides.clear()


def sign_in(client, claims: dict) -> dict:
    """POST /auth/google with Google verification stubbed to return ``claims``."""
    with patch("app.api.auth.verify_google_id_token", AsyncMock(return_value=claims)):
        resp = client.post("/auth/google", json={"id_token": "t"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
