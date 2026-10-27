"""Sign-in identity is the Google subject, never the email (#688).

- An attacker whose verified Google account carries a victim's email address
  (an address that moved to another Google account) gets 409, never the
  victim's account, and the victim's row is untouched.
- The dev-only /auth/test-login route is absent in production and staging,
  and those environments refuse to boot with TEST_AUTH_SECRET set.
"""

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.limiter import limiter
from app.main import app
from app.models.user import User
from tests.unit.test_environment_hardening import reload_app  # noqa: F401

pytestmark = pytest.mark.security

VICTIM_EMAIL = "victim@example.com"
CLAIMS = {
    "sub": "attacker-sub-1",
    "email": VICTIM_EMAIL,
    "email_verified": True,
    "name": "Attacker",
    "picture": "https://evil.example/a.png",
}


def _victim() -> User:
    return User(
        id=uuid.uuid4(),
        email=VICTIM_EMAIL,
        google_id="victim-sub-9",
        display_name="Victim",
        avatar_url="https://ok.example/v.png",
    )


def _fake_session(*lookups, commit_error=None):
    results = []
    for value in lookups:
        r = MagicMock()
        r.scalar_one_or_none.return_value = value
        results.append(r)
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=results)
    if commit_error is not None:
        db.commit = AsyncMock(side_effect=commit_error)
    return db


def _unique_violation() -> IntegrityError:
    return IntegrityError("INSERT", {}, Exception("duplicate key"))


def _post_google(db, claims=CLAIMS):
    from app.core.database import get_db

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    try:
        with (
            patch("app.api.auth.verify_google_id_token", return_value=claims),
            patch("app.api.auth.settings") as st,
            patch("app.api.auth.create_access_token", return_value="a") as access,
            patch("app.api.auth.create_refresh_token", return_value=("r", "j")),
        ):
            st.allowed_emails_list = []
            st.jwt_expiry_hours = 24
            st.refresh_token_expiry_days = 7
            resp = TestClient(app).post("/auth/google", json={"id_token": "t"})
        return resp, access
    finally:
        app.dependency_overrides.clear()


class TestEmailReuseTakeover:
    def test_victim_email_with_another_subject_is_409(self, caplog):
        victim = _victim()
        db = _fake_session(None, victim)
        with caplog.at_level(logging.INFO):
            resp, access = _post_google(db)
        assert resp.status_code == 409
        assert resp.json() == {"detail": "account_conflict"}
        access.assert_not_called()
        db.add.assert_not_called()
        db.commit.assert_not_awaited()
        assert (
            victim.email,
            victim.google_id,
            victim.display_name,
            victim.avatar_url,
        ) == (VICTIM_EMAIL, "victim-sub-9", "Victim", "https://ok.example/v.png")
        assert VICTIM_EMAIL not in caplog.text
        assert str(victim.id) in caplog.text

    def test_lookup_is_by_subject_first(self):
        """The first query filters on google_id, not email."""
        db = _fake_session(None, _victim())
        _post_google(db)
        where = str(db.execute.await_args_list[0].args[0].whereclause)
        assert where == "users.google_id = :google_id_1"

    def test_email_change_at_google_keeps_the_same_user(self):
        user = User(
            id=uuid.uuid4(), email="old@example.com", google_id="attacker-sub-1"
        )
        db = _fake_session(user, None)
        resp, access = _post_google(db)
        assert resp.status_code == 200
        assert user.email == VICTIM_EMAIL
        assert access.call_args.args[0] == str(user.id)
        assert not any(isinstance(c.args[0], User) for c in db.add.call_args_list)

    def test_missing_subject_is_401(self):
        db = _fake_session()
        resp, _ = _post_google(db, {**CLAIMS, "sub": ""})
        assert resp.status_code == 401
        db.execute.assert_not_awaited()

    def test_double_tap_with_same_new_subject_retries_and_signs_in(self):
        """The first commit loses the insert race to an identical sign-in;
        the retry finds that row by subject and signs in."""
        winner = User(id=uuid.uuid4(), email=VICTIM_EMAIL, google_id="attacker-sub-1")
        db = _fake_session(
            None, None, winner, commit_error=[_unique_violation(), None, None]
        )
        resp, access = _post_google(db)
        assert resp.status_code == 200
        db.rollback.assert_awaited_once()
        assert access.call_args.args[0] == str(winner.id)

    def test_email_taken_concurrently_by_other_subject_is_409(self):
        """The retry finds the email now held by a different subject."""
        db = _fake_session(
            None, None, None, _victim(), commit_error=[_unique_violation()]
        )
        resp, access = _post_google(db)
        assert resp.status_code == 409
        assert resp.json() == {"detail": "account_conflict"}
        db.rollback.assert_awaited_once()
        access.assert_not_called()

    def test_second_unique_violation_is_409(self):
        db = _fake_session(
            None, None, None, None, commit_error=[_unique_violation()] * 2
        )
        resp, access = _post_google(db)
        assert resp.status_code == 409
        assert db.rollback.await_count == 2
        access.assert_not_called()

    def test_case_variant_of_victim_email_is_409(self):
        """``VICTIM@Example.com`` is the victim's address too (lower(email))."""
        victim = _victim()
        db = _fake_session(None, victim)
        resp, access = _post_google(db, {**CLAIMS, "email": "VICTIM@Example.com"})
        assert resp.status_code == 409
        access.assert_not_called()
        where = str(db.execute.await_args_list[1].args[0].whereclause)
        assert where == "lower(users.email) = :lower_1"
        assert victim.email == VICTIM_EMAIL

    def test_allowlist_ignores_case(self):
        from app.core.database import get_db

        async def _get_db():
            yield _fake_session(None, None)

        app.dependency_overrides[get_db] = _get_db
        try:
            for allowed, expected in (
                (["Victim@EXAMPLE.com"], 200),
                (["someone@example.com"], 403),
            ):
                with (
                    patch(
                        "app.api.auth.verify_google_id_token",
                        return_value={**CLAIMS, "email": "victim@Example.COM"},
                    ),
                    patch("app.api.auth.settings") as st,
                    patch("app.api.auth.create_access_token", return_value="a"),
                    patch("app.api.auth.create_refresh_token", return_value=("r", "j")),
                ):
                    st.allowed_emails_list = allowed
                    st.jwt_expiry_hours = 24
                    st.refresh_token_expiry_days = 7
                    resp = TestClient(app).post("/auth/google", json={"id_token": "t"})
                assert resp.status_code == expected
        finally:
            app.dependency_overrides.clear()

    def test_google_route_keeps_its_rate_limit(self):
        assert limiter._route_limits.get("app.api.auth.google_auth")


class TestTestLoginGate:
    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_hardened_boot_fails_with_test_auth_secret(self, env):
        with pytest.raises(ValidationError) as exc:
            Settings(
                database_url="postgresql+asyncpg://u:p@localhost/db",
                environment=env,
                test_auth_secret="do-not-echo-me",
            )
        assert "TEST_AUTH_SECRET must be unset" in str(exc.value)
        assert "do-not-echo-me" not in str(exc.value)

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_hardened_boot_fails_with_whitespace_test_auth_secret(self, env):
        with pytest.raises(ValidationError):
            Settings(
                database_url="postgresql+asyncpg://u:p@localhost/db",
                environment=env,
                test_auth_secret="  ",
            )

    @pytest.mark.parametrize("env", ["development", "test"])
    def test_local_environments_accept_test_auth_secret(self, env):
        s = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            environment=env,
            test_auth_secret="s",
        )
        assert s.test_auth_secret == "s"

    def test_hardened_app_import_fails_with_test_auth_secret(
        self, reload_app, monkeypatch  # noqa: F811
    ):
        monkeypatch.setenv("TEST_AUTH_SECRET", "s")
        with pytest.raises(ValidationError):
            reload_app(
                ENVIRONMENT="production",
                DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
                CORS_ORIGINS="[]",
                TRUSTED_HOSTS='["trusted.example.com"]',
            )

    async def test_route_absent_under_production_settings(
        self, reload_app  # noqa: F811
    ):
        main_module = reload_app(
            ENVIRONMENT="production",
            DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            CORS_ORIGINS="[]",
            TRUSTED_HOSTS='["trusted.example.com"]',
        )
        assert main_module.settings.is_hardened is True
        paths = {getattr(r, "path", None) for r in main_module.app.routes}
        assert "/auth/test-login" not in paths
        async with AsyncClient(
            transport=ASGITransport(app=main_module.app),
            base_url="http://trusted.example.com",
        ) as client:
            resp = await client.post(
                "/auth/test-login", json={"secret": "s", "email": "a@b.c"}
            )
            assert resp.status_code == 404


class TestTestLoginIdentity:
    def _post(self, db, body, secret="sekrit", raw=None):
        from app import main
        from app.core import database

        async def _get_db():
            yield db

        with (
            patch.object(main.settings, "test_auth_secret", secret),
            patch.object(main.settings, "allowed_emails", ""),
            patch.object(database, "get_db", _get_db),
            patch("app.auth.jwt.create_access_token", return_value="a"),
            patch("app.auth.jwt.create_refresh_token", return_value=("r", "j")),
        ):
            client = TestClient(app)
            if raw is not None:
                return client.post(
                    "/auth/test-login",
                    content=raw,
                    headers={"Content-Type": "application/json"},
                )
            return client.post("/auth/test-login", json=body)

    def test_email_of_a_real_account_is_409(self):
        victim = _victim()
        db = _fake_session(None, victim)
        resp = self._post(db, {"secret": "sekrit", "email": VICTIM_EMAIL})
        assert resp.status_code == 409
        assert resp.json() == {"detail": "account_conflict"}
        assert "access_token" not in resp.text
        db.add.assert_not_called()
        assert victim.google_id == "victim-sub-9"

    def test_creates_user_by_synthetic_subject(self):
        db = _fake_session(None, None)
        resp = self._post(db, {"secret": "sekrit", "email": "e2e@example.com"})
        assert resp.status_code == 200
        created = db.add.call_args_list[0].args[0]
        assert isinstance(created, User)
        assert created.google_id == "test_e2e@example.com"
        where = str(db.execute.await_args_list[0].args[0].whereclause)
        assert where == "users.google_id = :google_id_1"

    @pytest.mark.parametrize(
        "body",
        [
            [],
            {"secret": "sekrit", "email": 5},
            {"secret": ["sekrit"], "email": "e2e@example.com"},
        ],
    )
    def test_malformed_body_is_422(self, body):
        db = _fake_session()
        resp = self._post(db, body)
        assert resp.status_code == 422
        db.execute.assert_not_awaited()

    def test_test_login_keeps_its_rate_limit(self):
        assert limiter._route_limits.get("app.main.test_login")

    def test_whitespace_only_secret_counts_as_unset(self):
        db = _fake_session()
        resp = self._post(db, {"secret": "  ", "email": "e2e@x.com"}, secret="  ")
        assert resp.status_code == 403
        assert "not configured" in resp.json()["detail"]
        db.execute.assert_not_awaited()

    @pytest.mark.parametrize("raw", [b"not json", b"{", b"\xff\xfe"])
    def test_non_json_body_is_422(self, raw):
        db = _fake_session()
        resp = self._post(db, None, raw=raw)
        assert resp.status_code == 422
        db.execute.assert_not_awaited()

    def test_commit_unique_violation_twice_is_409(self):
        db = _fake_session(
            None, None, None, None, commit_error=[_unique_violation()] * 2
        )
        resp = self._post(db, {"secret": "sekrit", "email": "e2e@example.com"})
        assert resp.status_code == 409
        assert resp.json() == {"detail": "account_conflict"}
        assert db.rollback.await_count == 2

    def test_commit_race_with_same_subject_retries_and_signs_in(self):
        winner = User(
            id=uuid.uuid4(), email="e2e@example.com", google_id="test_e2e@example.com"
        )
        db = _fake_session(
            None, None, winner, commit_error=[_unique_violation(), None, None]
        )
        resp = self._post(db, {"secret": "sekrit", "email": "e2e@example.com"})
        assert resp.status_code == 200
        db.rollback.assert_awaited_once()
