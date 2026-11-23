"""POST / DELETE /users/me/ai-consent change only the caller's own row (#1023).

Database-free (the CI security job has no Postgres): real access tokens go
through the real ``get_current_user``, and a small fake session holds two
users. The user is taken from the token and the time from the server: a body
naming another user, or carrying a timestamp, changes nothing else. The same
flow runs on real Postgres in ``tests/integration/test_users_ai_consent_db.py``.

Run on its own with:
    pytest -m security tests/security/test_ai_consent_authority.py
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token
from app.core.database import get_db
from app.main import app
from app.models.user import User
from tests.helpers import bearer, install_jwt_keys

pytestmark = pytest.mark.security

PATH = "/users/me/ai-consent"
NOW = datetime.now(UTC)
EARLIER = NOW - timedelta(days=2)


class _TwoUserDb:
    """Answers ``get(User, id)`` for its users and records commits."""

    def __init__(self, *users: User) -> None:
        self.users = {u.id: u for u in users}
        self.commits = 0

    async def get(self, model, pk, **_options):
        return self.users.get(pk) if model is User else None

    async def refresh(self, *_args, **_kwargs):
        pass

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def close(self):
        pass


def _user(tag: str, given: datetime | None = None) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{tag}@example.com",
        google_id=f"g-{tag}",
        token_version=0,
        ai_consent_at=given,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def env(_keys):
    alice, bob = _user("alice"), _user("bob", EARLIER)
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


def test_grant_changes_only_the_callers_row(env):
    client, alice, bob, tokens, fake = env
    resp = client.post(
        PATH, json={"user_id": str(bob.id)}, headers=bearer(tokens[alice.id])
    )
    assert resp.status_code == 200, resp.text
    assert alice.ai_consent is True
    assert bob.ai_consent_at == EARLIER
    assert fake.commits == 1


def test_withdraw_changes_only_the_callers_row(env):
    """Alice (no consent) withdrawing, even naming Bob, leaves Bob's consent."""
    client, alice, bob, tokens, _fake = env
    resp = client.delete(f"{PATH}?user_id={bob.id}", headers=bearer(tokens[alice.id]))
    assert resp.status_code == 200
    assert bob.ai_consent is True
    assert bob.ai_consent_withdrawn_at is None
    assert alice.ai_consent_withdrawn_at is None


def test_a_client_timestamp_is_never_stored(env):
    client, alice, _bob, tokens, _fake = env
    resp = client.post(
        PATH,
        json={"ai_consent_at": "1999-01-01T00:00:00+00:00"},
        headers=bearer(tokens[alice.id]),
    )
    assert resp.status_code == 200
    assert alice.ai_consent_at > NOW - timedelta(minutes=1)


@pytest.mark.parametrize("method", ["post", "delete"])
def test_a_token_for_an_unknown_user_writes_nothing(env, method):
    client, alice, bob, _tokens, fake = env
    ghost = create_access_token(str(uuid.uuid4()), "ghost@example.com", 0)
    resp = getattr(client, method)(PATH, headers=bearer(ghost))
    assert resp.status_code == 401
    assert alice.ai_consent_at is None
    assert bob.ai_consent is True
    assert fake.commits == 0


@pytest.mark.parametrize("method", ["post", "delete"])
def test_a_revoked_token_writes_nothing(env, method):
    client, _alice, bob, tokens, fake = env
    bob.token_version = 1  # logout-all happened after the token was issued
    resp = getattr(client, method)(PATH, headers=bearer(tokens[bob.id]))
    assert resp.status_code == 401
    assert bob.ai_consent is True
    assert fake.commits == 0
