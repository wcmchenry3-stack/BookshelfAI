"""Consent to send scan photos to the AI provider (#1023).

Database-free: ``get_current_user`` is overridden with a ``User`` object and
``get_db`` with a mock session, as in ``test_user_preferences.py``. The real
token path and two users are in
``tests/security/test_ai_consent_authority.py``; real Postgres (the migration,
the row lock) in ``tests/integration/test_users_ai_consent_db.py``; the
``/scan`` refusal against the real quota code in ``tests/limits/``.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api import scan as scan_api
from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models.user import User
from app.services import ai_consent

PATH = "/users/me/ai-consent"
NOW = datetime.now(UTC)
EARLIER = NOW - timedelta(days=3)
LATER = NOW + timedelta(days=3)


def _user(given: datetime | None = None, withdrawn: datetime | None = None) -> User:
    return User(
        id=uuid.uuid4(),
        email="consent@example.com",
        google_id="g-consent",
        token_version=0,
        ai_consent_at=given,
        ai_consent_withdrawn_at=withdrawn,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def db():
    session = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    yield session
    app.dependency_overrides.clear()


@pytest.fixture
def signed_in(db):
    def use(user: User) -> User:
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    return use


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def enforced():
    with patch.object(settings, "ai_consent_required", True):
        yield


# ---------------------------------------------------------------------------
# The state
# ---------------------------------------------------------------------------


class TestConsentStands:
    @pytest.mark.parametrize(
        ("given", "withdrawn", "stands"),
        [
            (None, None, False),
            (NOW, None, True),
            (NOW, EARLIER, True),  # withdrawn, then given again
            (NOW, LATER, False),  # given, then withdrawn
            (NOW, NOW, False),  # a tie is not consent
            (None, NOW, False),
        ],
    )
    def test_given_and_not_withdrawn_since(self, given, withdrawn, stands):
        assert _user(given, withdrawn).ai_consent is stands

    def test_grant_sets_the_server_time_and_keeps_the_withdrawal(self):
        user = _user(EARLIER - timedelta(days=1), EARLIER)
        before = datetime.now(UTC)
        assert ai_consent.grant(user) is True
        assert before <= user.ai_consent_at <= datetime.now(UTC)
        assert user.ai_consent_withdrawn_at == EARLIER
        assert user.ai_consent is True

    def test_grant_when_consent_stands_keeps_the_first_time(self):
        user = _user(EARLIER)
        assert ai_consent.grant(user) is False
        assert user.ai_consent_at == EARLIER

    def test_withdraw_sets_the_server_time_and_keeps_the_grant(self):
        user = _user(EARLIER)
        assert ai_consent.withdraw(user) is True
        assert user.ai_consent_at == EARLIER
        assert user.ai_consent_withdrawn_at > EARLIER
        assert user.ai_consent is False

    @pytest.mark.parametrize("user", [_user(), _user(EARLIER, NOW)])
    def test_withdraw_without_consent_changes_nothing(self, user):
        before = (user.ai_consent_at, user.ai_consent_withdrawn_at)
        assert ai_consent.withdraw(user) is False
        assert (user.ai_consent_at, user.ai_consent_withdrawn_at) == before

    def test_a_clock_stepped_back_still_makes_the_latest_win(self):
        """Server time going backwards must not leave a fresh grant (or
        withdrawal) older than the previous event."""
        user = _user(LATER - timedelta(days=1), LATER)
        assert ai_consent.grant(user) is True
        assert user.ai_consent is True
        assert ai_consent.withdraw(user) is True
        assert user.ai_consent is False


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------


class TestGetMe:
    def test_a_user_without_consent(self, client, signed_in):
        signed_in(_user())
        body = client.get("/auth/me").json()
        assert body["ai_consent"] is False
        assert body["ai_consent_at"] is None
        assert body["ai_consent_withdrawn_at"] is None

    def test_a_user_with_consent(self, client, signed_in):
        signed_in(_user(EARLIER))
        body = client.get("/auth/me").json()
        assert body["ai_consent"] is True
        assert datetime.fromisoformat(body["ai_consent_at"]) == EARLIER
        assert body["ai_consent_withdrawn_at"] is None

    def test_a_withdrawn_consent_shows_both_times(self, client, signed_in):
        signed_in(_user(EARLIER, NOW))
        body = client.get("/auth/me").json()
        assert body["ai_consent"] is False
        assert datetime.fromisoformat(body["ai_consent_at"]) == EARLIER
        assert datetime.fromisoformat(body["ai_consent_withdrawn_at"]) == NOW


# ---------------------------------------------------------------------------
# POST / DELETE /users/me/ai-consent
# ---------------------------------------------------------------------------


class TestGrant:
    def test_records_consent(self, client, signed_in, db):
        user = signed_in(_user())
        resp = client.post(PATH)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ai_consent"] is True
        assert body["ai_consent_withdrawn_at"] is None
        assert user.ai_consent is True
        db.commit.assert_awaited_once()

    def test_reads_the_consent_under_the_row_lock(self, client, signed_in, db):
        user = signed_in(_user())
        client.post(PATH)
        db.refresh.assert_awaited_once_with(
            user,
            attribute_names=["ai_consent_at", "ai_consent_withdrawn_at"],
            with_for_update={"key_share": True},
        )

    def test_is_idempotent(self, client, signed_in):
        user = signed_in(_user(EARLIER))
        resp = client.post(PATH)
        assert resp.status_code == 200
        assert datetime.fromisoformat(resp.json()["ai_consent_at"]) == EARLIER
        assert user.ai_consent_at == EARLIER

    def test_ignores_a_client_timestamp(self, client, signed_in):
        """The time is the server's: a body (a client clock) changes nothing."""
        user = signed_in(_user())
        long_ago = "2000-01-01T00:00:00+00:00"
        resp = client.post(PATH, json={"ai_consent_at": long_ago, "ai_consent": False})
        assert resp.status_code == 200
        assert user.ai_consent is True
        assert user.ai_consent_at > NOW - timedelta(minutes=1)

    def test_after_a_withdrawal_gives_consent_again(self, client, signed_in):
        user = signed_in(_user(EARLIER - timedelta(days=1), EARLIER))
        assert client.post(PATH).json()["ai_consent"] is True
        assert user.ai_consent_withdrawn_at == EARLIER

    def test_logs_the_user_id_only(self, client, signed_in, caplog):
        user = signed_in(_user())
        with caplog.at_level("INFO", logger="app.api.users"):
            client.post(PATH)
        assert f"AI consent given (user_id={user.id})" in caplog.text
        assert user.email not in caplog.text

    def test_unauthenticated_is_401(self, client, db):
        assert client.post(PATH).status_code == 401

    def test_rate_limited_with_rate_limit_writes(self, client, signed_in):
        signed_in(_user())
        allowed = int(settings.rate_limit_writes.split("/")[0])
        for _ in range(allowed):
            assert client.post(PATH).status_code == 200
        assert client.post(PATH).status_code == 429


class TestWithdraw:
    def test_withdraws_consent(self, client, signed_in, db):
        user = signed_in(_user(EARLIER))
        resp = client.delete(PATH)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ai_consent"] is False
        assert datetime.fromisoformat(body["ai_consent_at"]) == EARLIER
        assert body["ai_consent_withdrawn_at"] is not None
        assert user.ai_consent is False
        db.commit.assert_awaited_once()

    def test_reads_the_consent_under_the_row_lock(self, client, signed_in, db):
        user = signed_in(_user(EARLIER))
        client.delete(PATH)
        db.refresh.assert_awaited_once_with(
            user,
            attribute_names=["ai_consent_at", "ai_consent_withdrawn_at"],
            with_for_update={"key_share": True},
        )

    def test_is_idempotent(self, client, signed_in):
        user = signed_in(_user(EARLIER, NOW))
        resp = client.delete(PATH)
        assert resp.status_code == 200
        assert resp.json()["ai_consent"] is False
        assert user.ai_consent_withdrawn_at == NOW

    def test_without_ever_consenting_changes_nothing(self, client, signed_in):
        user = signed_in(_user())
        resp = client.delete(PATH)
        assert resp.status_code == 200
        assert resp.json() == {
            "ai_consent": False,
            "ai_consent_at": None,
            "ai_consent_withdrawn_at": None,
        }
        assert user.ai_consent_withdrawn_at is None

    def test_logs_the_user_id_only(self, client, signed_in, caplog):
        user = signed_in(_user(EARLIER))
        with caplog.at_level("INFO", logger="app.api.users"):
            client.delete(PATH)
        assert f"AI consent withdrawn (user_id={user.id})" in caplog.text
        assert user.email not in caplog.text

    def test_unauthenticated_is_401(self, client, db):
        assert client.delete(PATH).status_code == 401

    def test_rate_limited_with_rate_limit_writes(self, client, signed_in):
        signed_in(_user(EARLIER))
        allowed = int(settings.rate_limit_writes.split("/")[0])
        for _ in range(allowed):
            assert client.delete(PATH).status_code == 200
        assert client.delete(PATH).status_code == 429


# ---------------------------------------------------------------------------
# POST /scan
# ---------------------------------------------------------------------------


def test_the_setting_defaults_to_off():
    from app.core.config import Settings

    assert Settings.model_fields["ai_consent_required"].default is False


class TestRequireForScan:
    def test_off_lets_a_user_without_consent_through(self):
        with patch.object(settings, "ai_consent_required", False):
            ai_consent.require_for_scan(_user())

    def test_on_refuses_without_consent(self, enforced):
        with pytest.raises(HTTPException) as err:
            ai_consent.require_for_scan(_user())
        assert (err.value.status_code, err.value.detail) == (
            403,
            "ai_consent_required",
        )

    def test_on_refuses_a_withdrawn_consent(self, enforced):
        with pytest.raises(HTTPException):
            ai_consent.require_for_scan(_user(EARLIER, NOW))

    def test_on_lets_a_user_with_consent_through(self, enforced):
        ai_consent.require_for_scan(_user(EARLIER))


# Every step of scan() after the kill switches, none of which may run when
# consent is refused: the request count, the bot check, the upload, the
# saved-answer lookup, the quota reservation and the provider call.
_LATER_STEPS = (
    "_count_scan_request",
    "_check_bot_protection",
    "_read_validated_upload",
    "_normalise_image",
    "_serve_saved_answer",
    "_reserve_scan_quota",
    "_run_provider",
    "_record_and_enrich",
)


async def _scan(user):
    mocks = {name: AsyncMock(name=name) for name in _LATER_STEPS}
    mocks["_read_validated_upload"].return_value = b"raw"
    mocks["_normalise_image"].return_value = b"norm"
    mocks["_serve_saved_answer"].return_value = None
    mocks["_run_provider"].return_value = (MagicMock(), None)
    mocks["_record_and_enrich"].return_value = ([], None)
    patches = [patch.object(scan_api, n, m) for n, m in mocks.items()]
    patches += [
        patch.object(scan_api, "_load_scan_settings", AsyncMock()),
        patch.object(scan_api, "_fingerprint", MagicMock(return_value="fp")),
        patch.object(scan_api, "_enhanced_remaining", AsyncMock(return_value=None)),
        patch.object(scan_api.catalog_token, "sign_books", return_value=[]),
    ]
    for p in patches:
        p.start()
    try:
        try:
            await scan_api.scan.__wrapped__(
                request=MagicMock(),
                file=MagicMock(),
                current_user=user,
                db=AsyncMock(),
                cf_turnstile_response="tok",
                enhanced=False,
                attempt=1,
            )
            outcome = None
        except HTTPException as exc:
            outcome = (exc.status_code, exc.detail)
    finally:
        for p in reversed(patches):
            p.stop()
    return outcome, mocks


class TestScanEndpoint:
    async def test_refused_before_any_count_quota_or_provider(self, enforced):
        outcome, mocks = await _scan(_user())
        assert outcome == (403, "ai_consent_required")
        for name, mock in mocks.items():
            assert mock.await_count == 0, name

    async def test_refused_after_the_kill_switches(self, enforced):
        """A switched-off scan says so first: the consent prompt would not
        help."""
        with patch.object(
            scan_api,
            "_load_scan_settings",
            AsyncMock(side_effect=HTTPException(503, "scan_disabled")),
        ), pytest.raises(HTTPException) as err:
            await scan_api.scan.__wrapped__(
                request=MagicMock(),
                file=MagicMock(),
                current_user=_user(),
                db=AsyncMock(),
                cf_turnstile_response=None,
                enhanced=False,
                attempt=1,
            )
        assert err.value.detail == "scan_disabled"

    async def test_allowed_with_consent(self, enforced):
        outcome, mocks = await _scan(_user(EARLIER))
        assert outcome is None
        mocks["_reserve_scan_quota"].assert_awaited_once()
        mocks["_run_provider"].assert_awaited_once()

    async def test_allowed_without_consent_when_the_setting_is_off(self):
        with patch.object(settings, "ai_consent_required", False):
            outcome, mocks = await _scan(_user())
        assert outcome is None
        mocks["_run_provider"].assert_awaited_once()

    async def test_off_never_reads_the_consent(self):
        """With the setting off the check does not touch the user row (a
        user object without the consent columns still scans)."""
        with patch.object(settings, "ai_consent_required", False):
            outcome, _ = await _scan(SimpleNamespace(id=uuid.uuid4()))
        assert outcome is None
