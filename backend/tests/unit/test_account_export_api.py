"""``POST /account/export`` (#694), database-free.

``get_current_user`` is overridden with a ``User`` and ``get_db`` with a mock
session (as in ``test_ai_consent.py``); the queueing service is patched.
Covers auth, the write rate limit, the kill switch (503 before anything is
queued), 202 with the queued row and a wake-up of the worker, the daily cap
(409 with ``retry_after`` and ``Retry-After``), body validation (locale,
unknown fields), and the ``Idempotency-Key`` replay. Real Postgres (the
row lock, the cap's window, the job) is in
``tests/integration/test_account_export_db.py``.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api import users as account_api
from app.api._idempotency import HEADER, REPLAYED_HEADER
from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models.user import User
from app.services import account_export_jobs
from tests._idempotency_fake import install_fake_store

PATH = "/account/export"
NOW = datetime(2026, 11, 23, 8, 0, tzinfo=UTC)


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        email="exporter@example.com",
        google_id="g-export",
        token_version=0,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def db():
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    yield session
    app.dependency_overrides.clear()


@pytest.fixture
def user(db):
    u = _user()
    app.dependency_overrides[get_current_user] = lambda: u
    return u


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def configured():
    with patch.object(account_export_jobs, "configured", return_value=True):
        yield


@pytest.fixture
def queued():
    calls = []

    async def fake(db, user, locale, now=None):
        calls.append((user.id, locale))
        return SimpleNamespace(id=uuid.uuid4(), status="pending", created_at=NOW)

    with (
        patch.object(account_export_jobs, "request_export", fake),
        patch.object(account_export_jobs, "wake", MagicMock()) as wake,
    ):
        yield SimpleNamespace(calls=calls, wake=wake)


def test_unauthenticated_is_401(client, db):
    assert client.post(PATH).status_code == 401


def test_unconfigured_is_503_and_queues_nothing(client, user, db, queued):
    with patch.object(account_export_jobs, "configured", return_value=False):
        resp = client.post(PATH, json={"locale": "de"})
    assert resp.status_code == 503
    assert resp.json() == {"detail": account_api.EXPORT_NOT_CONFIGURED}
    assert queued.calls == []
    db.commit.assert_not_awaited()


def test_the_kill_switch_follows_storage_and_email_settings():
    key = f"k-{uuid.uuid5(uuid.NAMESPACE_URL, 'kill-switch')}"
    full = {
        "supabase_url": "https://ref.supabase.co",
        "supabase_service_role_key": key,
        "resend_api_key": key,
    }
    for missing in (None, *full):
        values = {k: ("" if k == missing else v) for k, v in full.items()}
        with patch.multiple(settings, **values):
            assert account_export_jobs.configured() is (missing is None), missing


def test_queues_an_export_and_answers_202(client, user, db, configured, queued):
    resp = client.post(PATH, json={"locale": "fr-CA"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert set(body) == {"id", "status", "created_at"}
    assert queued.calls == [(user.id, "fr-CA")]
    db.commit.assert_awaited_once()
    queued.wake.assert_called_once()


def test_no_body_means_english(client, user, db, configured, queued):
    assert client.post(PATH).status_code == 202
    assert queued.calls == [(user.id, "en")]


@pytest.mark.parametrize(
    "body",
    [{"locale": "xx"}, {"locale": "fr"}, {"locale": 3}, {"user_id": "someone"}],
)
def test_a_bad_body_is_422(client, user, db, configured, queued, body):
    resp = client.post(PATH, json=body)
    assert resp.status_code == 422
    assert queued.calls == []


def test_the_daily_cap_is_409_with_retry_after(client, user, db, configured):
    async def limited(*_a, **_k):
        raise account_export_jobs.ExportLimited(3600)

    with patch.object(account_export_jobs, "request_export", limited):
        resp = client.post(PATH)
    assert resp.status_code == 409
    assert resp.json() == {"detail": {"code": "export_limit", "retry_after": 3600}}
    assert resp.headers["retry-after"] == "3600"
    db.commit.assert_not_awaited()


def test_logs_the_user_id_only(client, user, db, configured, queued, caplog):
    with caplog.at_level("INFO", logger="app.api.users"):
        client.post(PATH)
    assert f"user_id={user.id}" in caplog.text
    assert user.email not in caplog.text


def test_rate_limited_with_rate_limit_writes(client, user, db, configured, queued):
    allowed = int(settings.rate_limit_writes.split("/")[0])
    for _ in range(allowed):
        assert client.post(PATH).status_code == 202
    assert client.post(PATH).status_code == 429


def test_a_keyed_retry_replays_the_202(
    client, user, db, configured, queued, monkeypatch
):
    fake = install_fake_store(monkeypatch)
    headers = {HEADER: str(uuid.uuid4())}
    first = client.post(PATH, json={"locale": "en"}, headers=headers)
    again = client.post(PATH, json={"locale": "en"}, headers=headers)
    assert first.status_code == again.status_code == 202
    assert again.json() == first.json()
    assert again.headers[REPLAYED_HEADER] == "true"
    assert len(queued.calls) == 1
    assert fake.claims == 2 and fake.completes == 1


def test_a_keyed_cap_refusal_replays_too(client, user, db, configured, monkeypatch):
    install_fake_store(monkeypatch)
    calls = []

    async def limited(*_a, **_k):
        calls.append(1)
        raise account_export_jobs.ExportLimited(60)

    headers = {HEADER: str(uuid.uuid4())}
    with patch.object(account_export_jobs, "request_export", limited):
        first = client.post(PATH, headers=headers)
        again = client.post(PATH, headers=headers)
    assert first.status_code == again.status_code == 409
    assert again.headers[REPLAYED_HEADER] == "true"
    assert len(calls) == 1


def test_a_keyed_503_releases_the_key(client, user, db, queued, monkeypatch):
    fake = install_fake_store(monkeypatch)
    headers = {HEADER: str(uuid.uuid4())}
    with patch.object(account_export_jobs, "configured", return_value=False):
        assert client.post(PATH, headers=headers).status_code == 503
    assert fake.releases == 1
    with patch.object(account_export_jobs, "configured", return_value=True):
        assert client.post(PATH, headers=headers).status_code == 202
