"""Refresh-token families and reuse detection (#690).

- Sign-in starts a family (``family_id`` = the first row's id); rotation
  inherits it and marks the old row rotated (``used_at``, ``replaced_by_id``).
- A rotated token presented again after the grace window is reuse: every
  refresh row of the user is revoked (the whole family included),
  ``users.token_version`` is bumped so every access token dies, the answer is
  401 ``refresh_reused``, and one Sentry warning carries the user id and
  family id only (never a token, jti or email).
- Within the grace window, a replay of a token whose successor is unused is a
  client retry: a fresh pair in the same family displaces the successor. A
  displaced token is always reuse, and each rotated token gets one retry, so
  two holders cannot take turns inside the window.
- A concurrent refresh with the same token whose twin's successor is still
  unused is handled like a retry, so a thief who wins the race is caught when
  either holder refreshes again.
- The route keeps the auth rate limit.

Database-free: a small stateful fake session stands in for Postgres. The same
flows run against real Postgres in tests/integration/test_refresh_families_db.py.
"""

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
from tests.unit._sentry_capture import captured_app_events

pytestmark = pytest.mark.security

CLAIMS = {
    "sub": "google-sub-690",
    "email": "reuse.victim@example.com",
    "email_verified": True,
    "name": "Reuse Test",
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
        patch.object(settings, "refresh_reuse_grace_seconds", 30),
    ):
        yield


class FakeDb:
    """One user and its refresh rows: the selects by jti, ``get`` by primary
    key, and the two UPDATEs ``revoke_all_sessions`` issues."""

    def __init__(self) -> None:
        self.user: User | None = None
        self.tokens: list[RefreshToken] = []
        # Runs once when /auth/refresh takes the user-row lock, after its
        # first read of the refresh row: a test interleaves a twin request.
        self.on_user_lock = None

    def by_jti(self, jti: str) -> RefreshToken:
        return next(t for t in self.tokens if t.jti == jti)

    async def execute(self, stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        if getattr(stmt, "is_update", False):
            params = stmt.compile().params
            if stmt.table.name == "users":
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
        if model is User and self.user is not None and self.user.id == pk:
            hook, self.on_user_lock = self.on_user_lock, None
            if hook is not None:
                hook()
            return self.user
        if model is RefreshToken:
            return next((t for t in self.tokens if t.id == pk), None)
        return None

    def add(self, obj):
        if isinstance(obj, User):
            obj.id = obj.id or uuid.uuid4()
            obj.token_version = obj.token_version or 0
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


def _refresh(client, token: str):
    return client.post("/auth/refresh", json={"refresh_token": token})


def _jti(token: str) -> str:
    return jwt.decode(token, settings.jwt_public_key, algorithms=["RS256"])["jti"]


def _age(row: RefreshToken, seconds: int) -> None:
    """Pretend ``row`` was rotated ``seconds`` ago."""
    row.used_at -= timedelta(seconds=seconds)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestFamilies:
    def test_sign_in_starts_a_family_and_rotation_inherits_it(self, db, client):
        first = _sign_in(client)
        root = db.by_jti(_jti(first["refresh_token"]))
        assert root.family_id == root.id

        second = _refresh(client, first["refresh_token"])
        assert second.status_code == 200, second.text
        child = db.by_jti(_jti(second.json()["refresh_token"]))
        assert child.family_id == root.family_id
        assert child.id != root.id
        assert root.used_at is not None and root.revoked_at is not None
        assert root.replaced_by_id == child.id
        assert child.used_at is None and child.revoked_at is None

        # A second sign-in (another device) is another family.
        other = _sign_in(client)
        assert db.by_jti(_jti(other["refresh_token"])).family_id != root.family_id


class TestReuse:
    def test_reuse_revokes_family_and_access_tokens(self, db, client):
        stolen = _sign_in(client)
        rotated = _refresh(client, stolen["refresh_token"]).json()
        other_device = _sign_in(client)
        _age(db.by_jti(_jti(stolen["refresh_token"])), 31)

        resp = _refresh(client, stolen["refresh_token"])
        assert resp.status_code == 401
        assert resp.json() == {"detail": "refresh_reused"}

        # Every row revoked, the family's and the other device's alike, and
        # nothing new issued.
        assert len(db.tokens) == 3
        assert all(t.revoked_at is not None for t in db.tokens)
        assert db.user.token_version == 1
        # Every access token and refresh token the user held is dead.
        for tokens in (stolen, rotated, other_device):
            me = client.get("/auth/me", headers=_bearer(tokens["access_token"]))
            assert me.status_code == 401
            assert me.json()["detail"] == "token_revoked"
        assert _refresh(client, rotated["refresh_token"]).status_code == 401
        assert _refresh(client, other_device["refresh_token"]).status_code == 401

    def test_successor_already_rotated_is_reuse_even_within_window(self, db, client):
        stolen = _sign_in(client)
        second = _refresh(client, stolen["refresh_token"]).json()
        assert _refresh(client, second["refresh_token"]).status_code == 200

        resp = _refresh(client, stolen["refresh_token"])
        assert resp.status_code == 401
        assert resp.json()["detail"] == "refresh_reused"
        assert db.user.token_version == 1

    def test_zero_grace_makes_every_replay_reuse(self, db, client):
        tokens = _sign_in(client)
        assert _refresh(client, tokens["refresh_token"]).status_code == 200
        _age(db.by_jti(_jti(tokens["refresh_token"])), 1)
        with patch.object(settings, "refresh_reuse_grace_seconds", 0):
            resp = _refresh(client, tokens["refresh_token"])
        assert resp.json()["detail"] == "refresh_reused"

    def test_alert_carries_user_and_family_only(self, db, client):
        stolen = _sign_in(client)
        rotated = _refresh(client, stolen["refresh_token"]).json()
        row = db.by_jti(_jti(stolen["refresh_token"]))
        _age(row, 60)

        with captured_app_events() as events:
            resp = _refresh(client, stolen["refresh_token"])
        assert resp.json()["detail"] == "refresh_reused"

        alerts = [e for e in events if e.get("level") == "warning"]
        assert len(alerts) == 1
        (event,) = alerts
        assert event["message"] == (
            "Refresh token reuse detected; all sessions revoked"
        )
        assert event["tags"]["user_id"] == str(db.user.id)
        assert event["tags"]["family_id"] == str(row.family_id)
        assert event["fingerprint"] == ["auth-refresh-token-reuse"]
        # No request block (the body holds the token), no user block.
        assert "request" not in event and "user" not in event
        text = json.dumps(event, default=str)
        for secret in (
            stolen["refresh_token"],
            stolen["refresh_token"].split(".")[1],
            stolen["access_token"],
            rotated["refresh_token"],
            rotated["access_token"],
            *(t.jti for t in db.tokens),
            CLAIMS["email"],
            CLAIMS["sub"],
        ):
            assert secret not in text

    def test_reuse_log_names_ids_only(self, db, client, caplog):
        stolen = _sign_in(client)
        _refresh(client, stolen["refresh_token"])
        row = db.by_jti(_jti(stolen["refresh_token"]))
        _age(row, 60)
        with caplog.at_level("WARNING"):
            _refresh(client, stolen["refresh_token"])
        assert f"user_id={db.user.id}" in caplog.text
        assert f"family_id={row.family_id}" in caplog.text
        assert row.jti not in caplog.text
        assert stolen["refresh_token"] not in caplog.text
        assert CLAIMS["email"] not in caplog.text

    def test_alert_failure_still_answers_refresh_reused(self, db, client):
        tokens = _sign_in(client)
        _refresh(client, tokens["refresh_token"])
        _age(db.by_jti(_jti(tokens["refresh_token"])), 60)
        with patch(
            "app.auth.refresh_family.capture_service_warning",
            side_effect=RuntimeError("sentry down"),
        ):
            resp = _refresh(client, tokens["refresh_token"])
        assert resp.status_code == 401
        assert resp.json()["detail"] == "refresh_reused"
        assert db.user.token_version == 1

    def test_replay_after_sign_out_everywhere_is_not_a_second_alert(self, db, client):
        tokens = _sign_in(client)
        rotated = _refresh(client, tokens["refresh_token"]).json()
        _age(db.by_jti(_jti(tokens["refresh_token"])), 60)
        assert (
            client.post(
                "/auth/logout-all", headers=_bearer(rotated["access_token"])
            ).status_code
            == 204
        )
        with captured_app_events() as events:
            resp = _refresh(client, tokens["refresh_token"])
        assert resp.json()["detail"] == "token_revoked"
        assert db.user.token_version == 1
        assert not [e for e in events if e.get("level") == "warning"]


def _warnings(events) -> list:
    return [e for e in events if e.get("level") == "warning"]


def _assert_everything_revoked(db, client, *token_sets) -> None:
    assert all(t.revoked_at is not None for t in db.tokens)
    assert db.user.token_version == 1
    for tokens in token_sets:
        me = client.get("/auth/me", headers=_bearer(tokens["access_token"]))
        assert me.status_code == 401
        # (Each token's refresh row is revoked above; refreshing them all
        # here would hit the 5/minute auth limit.)


class TestGraceWindow:
    def test_retry_within_window_replaces_the_successor(self, db, client):
        tokens = _sign_in(client)
        first = _refresh(client, tokens["refresh_token"])
        assert first.status_code == 200
        lost = first.json()  # the response the client never received

        with captured_app_events() as events:
            retry = _refresh(client, tokens["refresh_token"])
        assert retry.status_code == 200, retry.text
        assert not _warnings(events)
        body = retry.json()
        assert body["refresh_token"] != lost["refresh_token"]

        root = db.by_jti(_jti(tokens["refresh_token"]))
        lost_row = db.by_jti(_jti(lost["refresh_token"]))
        new_row = db.by_jti(_jti(body["refresh_token"]))
        assert new_row.family_id == root.family_id
        # The root keeps pointing at the lost successor, which is displaced
        # (rotated into the new row): the root's one retry is spent.
        assert root.replaced_by_id == lost_row.id
        assert lost_row.revoked_at is not None and lost_row.used_at is not None
        assert lost_row.displaced_at is not None
        assert lost_row.replaced_by_id == new_row.id
        assert new_row.displaced_at is None
        assert db.user.token_version == 0
        # The retried pair works.
        assert (
            client.get("/auth/me", headers=_bearer(body["access_token"])).status_code
            == 200
        )
        assert _refresh(client, body["refresh_token"]).status_code == 200

    def test_second_retry_of_the_same_token_is_reuse(self, db, client):
        """#734 review (Codex P1 / S2): one retry per rotated token. Parallel
        or repeated replays inside the window get one retry, then reuse."""
        tokens = _sign_in(client)
        lost = _refresh(client, tokens["refresh_token"]).json()
        retry = _refresh(client, tokens["refresh_token"])
        assert retry.status_code == 200

        with captured_app_events() as events:
            again = _refresh(client, tokens["refresh_token"])
        assert again.status_code == 401
        assert again.json()["detail"] == "refresh_reused"
        assert len(_warnings(events)) == 1
        _assert_everything_revoked(db, client, lost, retry.json())

    def test_displaced_token_is_reuse_inside_the_window(self, db, client):
        """#734 review (Codex P1 / S2): a thief replays the stolen token A
        within the window and displaces the real client's B with C. When the
        real client presents B, still inside the window, it is reuse, not
        another retry: holders cannot alternate."""
        stolen = _sign_in(client)
        real = _refresh(client, stolen["refresh_token"]).json()  # B
        thief = _refresh(client, stolen["refresh_token"])  # C displaces B
        assert thief.status_code == 200
        b_row = db.by_jti(_jti(real["refresh_token"]))
        assert b_row.displaced_at is not None
        assert b_row.used_at is not None  # inside the window

        with captured_app_events() as events:
            back = _refresh(client, real["refresh_token"])
        assert back.status_code == 401
        assert back.json()["detail"] == "refresh_reused"
        (alert,) = _warnings(events)
        assert alert["tags"]["family_id"] == str(b_row.family_id)
        _assert_everything_revoked(db, client, real, thief.json())

    def test_holders_cannot_alternate(self, db, client):
        """The alternation the review reproduced: A, A, B, A, ... inside the
        window. The third refresh is already reuse; nothing is left live."""
        a = _sign_in(client)
        b = _refresh(client, a["refresh_token"]).json()
        assert _refresh(client, a["refresh_token"]).status_code == 200
        codes = [
            _refresh(client, t["refresh_token"]).json().get("detail") for t in (b, a)
        ]
        assert codes == ["refresh_reused", "token_revoked"]
        assert all(t.revoked_at is not None for t in db.tokens)


class TestRace:
    """Two requests with one token: the first takes the user lock and
    rotates; the second read the row as live and waited (Replay.RACE)."""

    @staticmethod
    def _race(db, client, token: str):
        twin: list = []
        db.on_user_lock = lambda: twin.append(_refresh(client, token))
        second = _refresh(client, token)
        (first,) = twin
        return first, second

    def test_race_gives_both_a_pair_and_displaces_the_first(self, db, client):
        """A benign double refresh no longer signs the user out: both get a
        pair, and only the last one minted stays usable."""
        tokens = _sign_in(client)
        with captured_app_events() as events:
            first, second = self._race(db, client, tokens["refresh_token"])
        assert [first.status_code, second.status_code] == [200, 200]
        assert not _warnings(events)
        assert db.user.token_version == 0

        root = db.by_jti(_jti(tokens["refresh_token"]))
        first_row = db.by_jti(_jti(first.json()["refresh_token"]))
        second_row = db.by_jti(_jti(second.json()["refresh_token"]))
        assert first_row.displaced_at is not None
        assert first_row.replaced_by_id == second_row.id
        assert root.replaced_by_id == first_row.id
        assert [t for t in db.tokens if t.revoked_at is None] == [second_row]
        assert second_row.family_id == root.family_id

    def test_race_with_successor_already_gone_is_plain_401(self, db, client):
        """The twin's successor was logged out before this request got the
        lock: nothing to displace; the plain 401, no alert."""
        tokens = _sign_in(client)
        twin: list = []

        def twin_then_logout():
            resp = _refresh(client, tokens["refresh_token"])
            twin.append(resp)
            client.post(
                "/auth/logout", json={"refresh_token": resp.json()["refresh_token"]}
            )

        db.on_user_lock = twin_then_logout
        with captured_app_events() as events:
            resp = _refresh(client, tokens["refresh_token"])
        assert twin[0].status_code == 200
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Refresh token revoked or not found"
        assert not _warnings(events)
        assert db.user.token_version == 0

    @pytest.mark.parametrize("thief_first", [True, False])
    def test_thief_who_wins_the_race_is_caught(self, db, client, thief_first):
        """#734 review S1: a thief with a stolen live token wins the user
        lock and gets T1; the real client's in-flight refresh with the same
        token used to get a plain 401, sign out, and leave the thief rotating
        undetected. Now the victim gets V (T1 displaced), and when the victim
        and the thief each refresh once more, reuse fires and everything is
        revoked, in either order."""
        stolen = _sign_in(client)
        thief, victim = self._race(db, client, stolen["refresh_token"])
        assert thief.status_code == victim.status_code == 200
        thief, victim = thief.json(), victim.json()

        with captured_app_events() as events:
            if thief_first:
                t = _refresh(client, thief["refresh_token"])
                v = _refresh(client, victim["refresh_token"])
                assert t.json()["detail"] == "refresh_reused"
                # V was revoked (not rotated) by the reuse: the plain 401.
                assert v.status_code == 401
                assert v.json()["detail"] != "refresh_reused"
            else:
                v = _refresh(client, victim["refresh_token"])
                assert v.status_code == 200
                t = _refresh(client, thief["refresh_token"])
                assert t.json()["detail"] == "refresh_reused"
                victim = v.json()
        assert len(_warnings(events)) == 1
        _assert_everything_revoked(db, client, thief, victim)


class TestDisplacedOutsideWindow:
    def test_lost_successor_turning_up_later_is_reuse(self, db, client):
        """The attacker replays the stolen token within the window and gets a
        pair; the real client's successor is now rotated, so when it next
        refreshes, the family is revoked."""
        tokens = _sign_in(client)
        client_copy = _refresh(client, tokens["refresh_token"]).json()
        attacker = _refresh(client, tokens["refresh_token"]).json()
        _age(db.by_jti(_jti(client_copy["refresh_token"])), 31)

        resp = _refresh(client, client_copy["refresh_token"])
        assert resp.json()["detail"] == "refresh_reused"
        assert _refresh(client, attacker["refresh_token"]).status_code == 401
        me = client.get("/auth/me", headers=_bearer(attacker["access_token"]))
        assert me.status_code == 401

    def test_successor_logged_out_within_window_is_plain_401(self, db, client):
        tokens = _sign_in(client)
        rotated = _refresh(client, tokens["refresh_token"]).json()
        assert (
            client.post(
                "/auth/logout", json={"refresh_token": rotated["refresh_token"]}
            ).status_code
            == 204
        )
        resp = _refresh(client, tokens["refresh_token"])
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Refresh token revoked or not found"
        assert db.user.token_version == 0


class TestRateLimit:
    def test_refresh_keeps_the_auth_limit(self):
        from limits import parse

        limits = limiter._route_limits["app.api.auth.refresh"]
        assert [lim.limit for lim in limits] == [parse(settings.rate_limit_auth)]

    def test_sixth_refresh_in_a_minute_is_429(self, db, client):
        tokens = _sign_in(client)
        codes = [
            _refresh(client, tokens["refresh_token"]).status_code for _ in range(6)
        ]
        assert 429 not in codes[:5]
        assert codes[5] == 429
