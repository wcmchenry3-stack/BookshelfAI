"""App Attest through the endpoints that use it (#1051), DB mocked.

- ``POST /attestation/register`` with the real verifier (test PKI root):
  a valid attestation stores the COSE public key with counter 0; a broken
  one is 400 ``attestation_invalid`` and stores nothing.
- ``_bot_check._verify_attestation`` for iOS: the assertion is verified with
  the stored key and counter, and the new counter is written with a
  compare-and-set whose row count decides the outcome. The concurrent case on
  real Postgres is in ``tests/integration/test_app_attest_db.py``.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api import _bot_check
from app.api.attestation import get_apple_verifier
from app.auth.dependencies import get_current_user
from app.core.app_attest import LiveAppleAttestVerifier
from app.core.attestation import issue_nonce
from app.core.database import get_db
from app.main import app
from app.models.device_key import DeviceKey
from app.models.user import User
from tests._app_attest_pki import APP_ID, NOW, Device, Pki

_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000001051")
_SECRET = str(uuid.uuid5(uuid.NAMESPACE_URL, "app-attest-flow-test"))


@pytest.fixture(scope="module")
def pki() -> Pki:
    return Pki()


@pytest.fixture
def device(pki) -> Device:
    return Device(pki)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "attestation_nonce_secret", _SECRET)
    monkeypatch.setattr(settings, "attestation_apple_app_id", APP_ID)


# ---------------------------------------------------------------------------
# POST /attestation/register
# ---------------------------------------------------------------------------


def _register(pki, body: dict):
    captured = []

    async def _db():
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock(return_value=result)
        captured.append(db)
        yield db

    app.dependency_overrides[get_current_user] = lambda: User(
        id=_USER_ID, email="attest@example.com"
    )
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_apple_verifier] = lambda: LiveAppleAttestVerifier(
        root_ca_pem=pki.root_pem, now=lambda: NOW
    )
    try:
        resp = TestClient(app).post("/attestation/register", json=body)
    finally:
        app.dependency_overrides.clear()
    return resp, captured[0]


def test_register_stores_attested_public_key(pki, device):
    nonce = issue_nonce(_USER_ID, _SECRET)
    resp, db = _register(
        pki,
        {
            "key_id": device.key_id,
            "attestation_b64": device.attestation(nonce),
            "nonce": nonce,
        },
    )
    assert resp.status_code == 200
    row = db.add.call_args.args[0]
    assert row.key_id == device.key_id
    assert row.platform == "ios"
    assert row.public_key_cbor == device.public_key_cbor
    assert row.counter == 0
    db.commit.assert_awaited_once()


def test_register_attestation_for_other_nonce_is_400(pki, device):
    nonce = issue_nonce(_USER_ID, _SECRET)
    other = issue_nonce(_USER_ID, _SECRET)
    resp, db = _register(
        pki,
        {
            "key_id": device.key_id,
            "attestation_b64": device.attestation(other),
            "nonce": nonce,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "attestation_invalid"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# /scan: iOS assertion
# ---------------------------------------------------------------------------


def _scan_db(row, rowcount: int = 1):
    found = MagicMock()
    found.scalar_one_or_none.return_value = row
    updated = SimpleNamespace(rowcount=rowcount)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[found, updated])
    return db


def _row(device: Device, counter: int = 3) -> DeviceKey:
    return DeviceKey(
        id=uuid.uuid4(),
        user_id=_USER_ID,
        platform="ios",
        key_id=device.key_id,
        public_key_cbor=device.public_key_cbor,
        counter=counter,
    )


async def _verify(db, *, key_id, assertion, nonce) -> bool:
    return await _bot_check._verify_attestation(
        platform="ios",
        key_id=key_id,
        assertion_b64=assertion,
        integrity_token=None,
        nonce=nonce,
        user_id=_USER_ID,
        db=db,
    )


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


async def test_valid_assertion_advances_counter_with_compare_and_set(device):
    nonce = issue_nonce(_USER_ID, _SECRET)
    db = _scan_db(_row(device, counter=3))
    assert await _verify(
        db, key_id=device.key_id, assertion=device.assertion(nonce, 4), nonce=nonce
    )
    stmt = db.execute.await_args_list[1].args[0]
    sql = _compiled(stmt)
    assert sql.startswith("UPDATE device_keys SET counter=4")
    assert "device_keys.counter < 4" in sql
    db.commit.assert_awaited_once()


async def test_assertion_losing_the_counter_race_fails(device):
    nonce = issue_nonce(_USER_ID, _SECRET)
    db = _scan_db(_row(device, counter=3), rowcount=0)
    assert not await _verify(
        db, key_id=device.key_id, assertion=device.assertion(nonce, 4), nonce=nonce
    )


@pytest.mark.parametrize("counter", [3, 2])
async def test_replayed_counter_fails_without_update(device, counter):
    nonce = issue_nonce(_USER_ID, _SECRET)
    db = _scan_db(_row(device, counter=3))
    assert not await _verify(
        db,
        key_id=device.key_id,
        assertion=device.assertion(nonce, counter),
        nonce=nonce,
    )
    assert db.execute.await_count == 1
    db.commit.assert_not_awaited()


async def test_unknown_key_fails(device):
    nonce = issue_nonce(_USER_ID, _SECRET)
    db = _scan_db(None)
    assert not await _verify(
        db, key_id=device.key_id, assertion=device.assertion(nonce, 1), nonce=nonce
    )


async def test_missing_fields_or_app_id_fail_before_the_db(device, monkeypatch):
    nonce = issue_nonce(_USER_ID, _SECRET)
    db = _scan_db(_row(device))
    assertion = device.assertion(nonce, 9)
    assert not await _verify(db, key_id=None, assertion=assertion, nonce=nonce)
    assert not await _verify(db, key_id=device.key_id, assertion=None, nonce=nonce)
    monkeypatch.setattr(_bot_check.settings, "attestation_apple_app_id", "")
    assert not await _verify(db, key_id=device.key_id, assertion=assertion, nonce=nonce)
    db.execute.assert_not_awaited()
