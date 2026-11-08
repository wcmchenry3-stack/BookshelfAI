"""PATCH /users/me/preferences changes only the caller's own row (#913).

Database-free (the CI security job has no Postgres): real access tokens go
through the real ``get_current_user``, and a small fake session holds two
users. The user is taken from the token; nothing in the body (``user_id``,
``id``) can point the write at someone else, and a body that tries is 422.
The same flow runs on real Postgres in
``tests/integration/test_users_default_tab_db.py``.

Run on its own with:
    pytest -m security tests/security/test_user_preferences_authority.py
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.helpers import bearer, install_jwt_keys

pytestmark = pytest.mark.security

PATH = "/users/me/preferences"
NOW = datetime.now(UTC)


class _TwoUserDb:
    """Answers ``get(User, id)`` for its users and records commits."""

    def __init__(self, *users: User) -> None:
        self.users = {u.id: u for u in users}
        self.commits = 0

    async def get(self, model, pk, **_options):
        return self.users.get(pk) if model is User else None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def close(self):
        pass


def _user(tag: str, default_tab: str | None = None) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{tag}@example.com",
        google_id=f"g-{tag}",
        token_version=0,
        default_tab=default_tab,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def env(_keys):
    alice, bob = _user("alice"), _user("bob", "my-books")
    fake = _TwoUserDb(alice, bob)

    async def _db():
        yield fake

    app.dependency_overrides[get_db] = _db
    with install_jwt_keys(_keys):
        tokens = {
            u.id: create_access_token(str(u.id), u.email, u.token_version)
            for u in (alice, bob)
        }
        try:
            yield TestClient(app), alice, bob, tokens, fake
        finally:
            app.dependency_overrides.clear()


def test_changes_only_the_callers_row(env):
    client, alice, bob, tokens, fake = env
    resp = client.patch(
        PATH, json={"default_tab": "wishlist"}, headers=bearer(tokens[alice.id])
    )
    assert resp.status_code == 200, resp.text
    assert alice.default_tab == "wishlist"
    assert bob.default_tab == "my-books"
    assert fake.commits == 1

    me = client.get("/auth/me", headers=bearer(tokens[bob.id]))
    assert me.json()["default_tab"] == "my-books"


@pytest.mark.parametrize("field", ["user_id", "id", "email"])
def test_a_body_naming_another_user_is_refused(env, field):
    client, alice, bob, tokens, fake = env
    target = str(bob.id) if field != "email" else bob.email
    resp = client.patch(
        PATH,
        json={"default_tab": "scan", field: target},
        headers=bearer(tokens[alice.id]),
    )
    assert resp.status_code == 422
    assert alice.default_tab is None
    assert bob.default_tab == "my-books"
    assert fake.commits == 0


def test_a_token_for_an_unknown_user_writes_nothing(env, _keys):
    client, alice, bob, _tokens, fake = env
    ghost = create_access_token(str(uuid.uuid4()), "ghost@example.com", 0)
    resp = client.patch(PATH, json={"default_tab": "scan"}, headers=bearer(ghost))
    assert resp.status_code == 401
    assert alice.default_tab is None
    assert bob.default_tab == "my-books"
    assert fake.commits == 0


def test_a_revoked_token_writes_nothing(env):
    client, alice, _bob, tokens, fake = env
    alice.token_version = 1  # logout-all happened after the token was issued
    resp = client.patch(
        PATH, json={"default_tab": "wishlist"}, headers=bearer(tokens[alice.id])
    )
    assert resp.status_code == 401
    assert alice.default_tab is None
    assert fake.commits == 0
