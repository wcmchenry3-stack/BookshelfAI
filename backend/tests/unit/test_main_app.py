"""App-level behaviour in app/main.py: body-size guard, lifespan task
handling and the dev-only test login's success path."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

# app.main is reloaded by test_environment_hardening, so it is imported inside
# each test: the ``app`` and the ``app.main`` patch targets must be the same
# module generation.


def _main():
    import app.main

    return app.main


class TestRequestSizeLimit:
    def test_oversized_content_length_is_rejected_before_routing(self):
        too_big = _main().RequestSizeLimitMiddleware.MAX_BODY_SIZE + 1
        resp = TestClient(_main().app).post(
            "/auth/logout", content=b"{}", headers={"content-length": str(too_big)}
        )
        assert resp.status_code == 413
        assert resp.json() == {"detail": "Request body too large"}

    def test_body_just_under_the_limit_is_not_rejected_as_too_large(self):
        at_limit = _main().RequestSizeLimitMiddleware.MAX_BODY_SIZE
        resp = TestClient(_main().app).post(
            "/auth/logout", content=b"{}", headers={"content-length": str(at_limit)}
        )
        assert resp.status_code != 413


class TestLifespan:
    def test_startup_seeds_settings_and_shutdown_stops_the_cleanup_task(self):
        state = {"cancelled": False}

        async def fake_cleanup():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        seed = AsyncMock()
        with (
            patch("app.main.seed_ai_settings_at_startup", seed),
            patch("app.main.cleanup_expired_tokens", fake_cleanup),
            TestClient(_main().app),
        ):
            seed.assert_awaited_once()
        assert state["cancelled"] is True


@pytest.fixture(scope="module")
def rsa_pems():
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
def _jwt_keys(request, rsa_pems):
    if request.cls is not TestTestLoginSuccess:
        yield
        return
    with (
        patch("app.auth.jwt.settings.jwt_private_key", rsa_pems[0]),
        patch("app.auth.jwt.settings.jwt_public_key", rsa_pems[1]),
    ):
        yield


class TestTestLoginSuccess:
    @staticmethod
    def _patch_db(existing_user):
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing_user
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        db.add = MagicMock()

        async def refresh(obj):
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

        db.refresh = AsyncMock(side_effect=refresh)

        async def fake_get_db():
            yield db

        return db, patch("app.core.database.get_db", fake_get_db)

    def _login(self, existing_user=None, allowed=()):
        db, db_patch = self._patch_db(existing_user)
        with (
            db_patch,
            patch("app.main.settings.test_auth_secret", "s3cret"),
            patch("app.main.settings.allowed_emails", ",".join(allowed)),
        ):
            resp = TestClient(_main().app).post(
                "/auth/test-login",
                json={"secret": "s3cret", "email": "e2e@example.com"},
            )
        return resp, db

    def test_new_user_is_created_and_gets_working_tokens(self):
        resp, db = self._login()
        assert resp.status_code == 200
        body = resp.json()
        assert body["expires_in"] > 0
        # Access token decodes and names the created user.
        from app.auth.jwt import decode_token

        claims = decode_token(body["access_token"])
        assert claims["type"] == "access"
        added = [c.args[0] for c in db.add.call_args_list]
        kinds = {type(o).__name__ for o in added}
        assert kinds == {"User", "RefreshToken"}
        user = next(o for o in added if type(o).__name__ == "User")
        assert user.email == "e2e@example.com"
        assert user.google_id == "test_e2e@example.com"
        assert claims["sub"] == str(user.id)
        assert decode_token(body["refresh_token"])["type"] == "refresh"

    def test_allow_listed_email_is_accepted(self):
        resp, _ = self._login(allowed=["other@example.com", "e2e@example.com"])
        assert resp.status_code == 200

    def test_existing_user_is_reused(self):
        existing = MagicMock(id=uuid.uuid4(), email="e2e@example.com")
        resp, db = self._login(existing_user=existing)
        assert resp.status_code == 200
        added = [type(c.args[0]).__name__ for c in db.add.call_args_list]
        assert added == ["RefreshToken"]  # no second user row

    def test_refresh_token_is_stored_with_its_jti(self):
        resp, db = self._login()
        stored = next(
            c.args[0]
            for c in db.add.call_args_list
            if type(c.args[0]).__name__ == "RefreshToken"
        )
        from app.auth.jwt import decode_token

        assert stored.jti == decode_token(resp.json()["refresh_token"])["jti"]
        assert stored.expires_at is not None


class TestDebugSentryRoute:
    def test_the_debug_route_raises_so_sentry_can_capture_it(self):
        client = TestClient(_main().app, raise_server_exceptions=True)
        with pytest.raises(ZeroDivisionError):
            client.get("/debug/sentry-test")
