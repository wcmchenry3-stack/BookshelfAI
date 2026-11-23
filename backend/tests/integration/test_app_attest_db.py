"""App Attest on real Postgres (#1051).

- ``POST /attestation/register`` with the real verifier (test PKI root)
  stores the attested key as a COSE key with counter 0.
- An assertion through ``_bot_check._verify_attestation`` passes and moves the
  stored counter; replaying it (same counter) or an older one fails.
- Two requests replaying one assertion at the same moment: both read the old
  counter and both pass the signature and counter checks (a barrier holds
  them there), but the compare-and-set ``UPDATE ... WHERE counter < new``
  lets exactly one through.

Needs DATABASE_URL pointing at a Postgres database.
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import _bot_check
from app.api.attestation import get_apple_verifier
from app.auth.jwt import create_access_token
from app.core.app_attest import LiveAppleAttestVerifier
from app.core.attestation import issue_nonce
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models.device_key import DeviceKey
from app.models.user import User
from tests._app_attest_pki import APP_ID, NOW, Device, Pki
from tests.helpers import bearer, install_jwt_keys, rsa_pem_pair

pytestmark = pytest.mark.integration

_SECRET = str(uuid.uuid5(uuid.NAMESPACE_URL, "app-attest-db-test"))


@pytest.fixture
async def env(monkeypatch):
    monkeypatch.setattr(settings, "attestation_nonce_secret", _SECRET)
    monkeypatch.setattr(settings, "attestation_apple_app_id", APP_ID)
    pki = Pki()
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _db():
        async with factory() as session:
            yield session

    uid = uuid.uuid4()
    async with factory() as db:
        db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
        await db.commit()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_apple_verifier] = lambda: LiveAppleAttestVerifier(
        root_ca_pem=pki.root_pem, now=lambda: NOW
    )
    client = AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )
    try:
        with install_jwt_keys(rsa_pem_pair()):
            auth = bearer(create_access_token(str(uid), f"{uid}@example.com", 0))
            yield factory, client, auth, uid, Device(pki)
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
        async with factory() as db:
            await db.execute(delete(DeviceKey).where(DeviceKey.user_id == uid))
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()
        await engine.dispose()


async def _register(client, auth, uid, device: Device) -> None:
    nonce = issue_nonce(uid, _SECRET)
    resp = await client.post(
        "/attestation/register",
        json={
            "key_id": device.key_id,
            "attestation_b64": device.attestation(nonce),
            "nonce": nonce,
        },
        headers=auth,
    )
    assert resp.status_code == 200, resp.text


async def _stored(factory, uid) -> DeviceKey:
    async with factory() as db:
        return (
            await db.execute(select(DeviceKey).where(DeviceKey.user_id == uid))
        ).scalar_one()


async def _scan_check(factory, uid, device: Device, nonce: str, assertion: str):
    async with factory() as db:
        return await _bot_check._verify_attestation(
            platform="ios",
            key_id=device.key_id,
            assertion_b64=assertion,
            integrity_token=None,
            nonce=nonce,
            user_id=uid,
            db=db,
        )


async def test_register_then_assert_then_replay(env):
    factory, client, auth, uid, device = env
    await _register(client, auth, uid, device)
    row = await _stored(factory, uid)
    assert row.platform == "ios"
    assert row.counter == 0
    assert row.public_key_cbor == device.public_key_cbor

    nonce = issue_nonce(uid, _SECRET)
    assertion = device.assertion(nonce, 2)
    assert await _scan_check(factory, uid, device, nonce, assertion) is True
    row = await _stored(factory, uid)
    assert row.counter == 2
    assert row.last_used_at is not None

    # The same assertion again, and an older counter with a fresh nonce.
    assert await _scan_check(factory, uid, device, nonce, assertion) is False
    older_nonce = issue_nonce(uid, _SECRET)
    older = device.assertion(older_nonce, 1)
    assert await _scan_check(factory, uid, device, older_nonce, older) is False
    assert (await _stored(factory, uid)).counter == 2


async def test_concurrent_replay_passes_only_once(env, monkeypatch):
    factory, client, auth, uid, device = env
    await _register(client, auth, uid, device)
    barrier = asyncio.Barrier(2)
    real = LiveAppleAttestVerifier()

    class _HeldVerifier:
        """The real assertion checks, then wait until both requests got here."""

        async def verify_assertion(self, **kwargs):
            result = await real.verify_assertion(**kwargs)
            await barrier.wait()
            return result

    monkeypatch.setattr(_bot_check, "apple_verifier", _HeldVerifier)
    nonce = issue_nonce(uid, _SECRET)
    assertion = device.assertion(nonce, 1)
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            *(_scan_check(factory, uid, device, nonce, assertion) for _ in range(2))
        ),
        timeout=10,
    )
    assert sorted(outcomes) == [False, True]
    assert (await _stored(factory, uid)).counter == 1
