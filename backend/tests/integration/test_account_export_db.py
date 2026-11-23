"""Account export on real Postgres (#694).

Supabase Storage and Resend are faked at the HTTP boundary
(``httpx.MockTransport``): the real clients, the real worker and the real
database run; nothing leaves the process.

- Over HTTP: ``POST /account/export`` queues a row (202) and a keyed retry
  replays it; the worker then builds the file, uploads it, signs a 7-day
  link and emails it to the account's address; the row ends ``ready``.
- Contents for a seeded user: every exported table present with the
  user's rows (trashed entries included), the second user's rows absent,
  no secret value (token ids, request hashes, device keys, fingerprints).
- The daily cap under the row lock, also for concurrent requests.
- Failures: a failed attempt waits out the lease and is retried, the third
  failure is final (no email, the object deleted); a run that died
  (expired lease) is taken over after a restart; two workers never claim
  the same row.
- Expiry: the link stops working after 7 days (the fake honours the
  signed expiry) and the purge deletes the object, then the row, in
  batches; a storage failure keeps the rows; without storage only rows
  with no object go; the sweep deletes files older than 8 days that no row
  knows (an account deleted before its export expired).
- Deleting the user deletes their export rows (``ON DELETE CASCADE``).
- On a migrated database: RLS, the app role's grants, migration 0034's
  cycle and its resume after a half-applied upgrade.

Needs DATABASE_URL pointing at a Postgres database.
"""

import asyncio
import importlib.util
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api._idempotency import HEADER, REPLAYED_HEADER
from app.auth.jwt import create_access_token
from app.core.config import settings
from app.core.database import APP_DB_ROLE, Base, get_db
from app.main import app
from app.models.account_export import AccountExport
from app.models.ai_enhanced_zero_result import AIEnhancedZeroResult
from app.models.ai_quota import AIQuota
from app.models.ai_usage import AIUsage
from app.models.book import Book
from app.models.device_key import DeviceKey
from app.models.edition import Edition
from app.models.idempotency_key import IdempotencyKey
from app.models.library_event import LibraryEvent
from app.models.refresh_token import RefreshToken
from app.models.scan_correction import ScanCorrection
from app.models.scan_record import ScanRecord
from app.models.user import User
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook
from app.services import (
    _http_retry,
    account_export,
    account_export_jobs,
    account_export_purge,
)
from app.services.object_storage import ObjectStorage
from tests.helpers import bearer, install_jwt_keys, rsa_pem_pair
from tests.integration.test_db_role import run_rolled_back

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0034_account_exports.py"
)
TABLE = "account_exports"
BASE = "https://export-test.supabase.co"
BUCKET = "account-exports"
KEY = f"svc-{uuid.uuid5(uuid.NAMESPACE_URL, 'export-db-test')}"


# ── Fake Supabase Storage and Resend ─────────────────────────────────────────


@dataclass
class FakeSupabase:
    """Objects in one bucket, signed tokens with their expiry, and failure
    injection. ``download`` answers like the real service would."""

    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    objects: dict[str, tuple[bytes, datetime]] = field(default_factory=dict)
    tokens: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    sign_requests: list[int] = field(default_factory=list)
    fail: set[str] = field(default_factory=set)  # operations answering 500

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        path = unquote(request.url.path).removeprefix("/storage/v1")
        if path.startswith("/object/sign/"):
            op = "sign"
        elif path.startswith("/object/list/"):
            op = "list"
        elif request.method == "DELETE":
            op = "delete"
        else:
            op = "upload"
        if op in self.fail:
            return httpx.Response(500)
        if op == "upload":
            name = path.removeprefix(f"/object/{BUCKET}/")
            assert request.headers["x-upsert"] == "true"
            self.objects[name] = (request.content, self.now)
            return httpx.Response(200, json={"Key": name})
        if op == "sign":
            name = path.removeprefix(f"/object/sign/{BUCKET}/")
            if name not in self.objects:
                return httpx.Response(400, json={"error": "not_found"})
            expires_in = json.loads(request.content)["expiresIn"]
            self.sign_requests.append(expires_in)
            token = f"tok-{uuid.uuid4().hex}"
            self.tokens[token] = (name, self.now + timedelta(seconds=expires_in))
            return httpx.Response(
                200, json={"signedURL": f"/object/sign/{BUCKET}/{name}?token={token}"}
            )
        if op == "delete":
            for name in json.loads(request.content)["prefixes"]:
                self.objects.pop(name, None)
            return httpx.Response(200, json=[])
        prefix = json.loads(request.content)["prefix"] + "/"
        listed = sorted(
            (
                (created, name.removeprefix(prefix))
                for name, (_, created) in self.objects.items()
                if name.startswith(prefix)
            ),
        )
        return httpx.Response(
            200,
            json=[{"name": n, "id": n, "created_at": c.isoformat()} for c, n in listed],
        )

    def download(self, url: str) -> int:
        """The status a browser would get for ``url`` at ``self.now``."""
        token = parse_qs(urlsplit(url).query)["token"][0]
        name, expires = self.tokens[token]
        if self.now >= expires:
            return 400  # Supabase: the signed token has expired
        return 200 if name in self.objects else 404

    def storage(self) -> ObjectStorage:
        return ObjectStorage(
            BASE, KEY, BUCKET, transport=httpx.MockTransport(self.handler)
        )


@dataclass
class FakeResend:
    """Resend as it treats ``Idempotency-Key``: the first request with a key
    is sent (``sent``); the same key with the same payload is answered with
    the first response and sends nothing; the same key with another payload
    is ``409 invalid_idempotent_request``. ``status`` refuses before that;
    ``timeout_after_accept`` sends (or dedupes) and then times out, as when
    the response is lost."""

    sent: list[dict] = field(default_factory=list)
    keys: dict[str, bytes] = field(default_factory=dict)
    status: int = 200
    timeout_after_accept: bool = False
    # Keep the key of a request refused with ``status`` (as Stripe does), so
    # its later reuse with another payload is a 409 although nothing was sent.
    keep_key_on_refusal: bool = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            if self.keep_key_on_refusal:
                self.keys.setdefault(
                    request.headers["idempotency-key"], request.content
                )
            return httpx.Response(self.status)
        key = request.headers["idempotency-key"]
        if key in self.keys and self.keys[key] != request.content:
            return httpx.Response(
                409, json={"statusCode": 409, "name": "invalid_idempotent_request"}
            )
        if key not in self.keys:
            self.keys[key] = request.content
            self.sent.append({"key": key, **json.loads(request.content)})
        if self.timeout_after_accept:
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(200, json={"id": f"email-{len(self.sent)}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _link(message: dict) -> str:
    return next(w for w in message["text"].split() if w.startswith(BASE))


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def env(monkeypatch):
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(_http_retry, "BACKOFF_S", 0)
    users: list[uuid.UUID] = []
    books: list[uuid.UUID] = []

    async def make_user() -> tuple[uuid.UUID, dict]:
        uid = uuid.uuid4()
        async with factory() as db:
            db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
            await db.commit()
        users.append(uid)
        return uid, bearer(create_access_token(str(uid), f"{uid}@example.com", 0))

    supabase, resend = FakeSupabase(), FakeResend()

    async def run_worker() -> int:
        return await account_export_jobs.process_pending(
            sessions=factory,
            storage=supabase.storage(),
            email_transport=resend.transport(),
        )

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    client = AsyncClient(transport=transport, base_url="http://test")
    try:
        with (
            install_jwt_keys(rsa_pem_pair()),
            patch.multiple(
                settings,
                supabase_url=BASE,
                supabase_service_role_key=KEY,
                resend_api_key=KEY,
            ),
        ):
            yield SimpleNamespace(
                factory=factory,
                client=client,
                make_user=make_user,
                books=books,
                supabase=supabase,
                resend=resend,
                run_worker=run_worker,
            )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
        async with factory() as db:
            await db.execute(
                delete(LibraryEvent).where(LibraryEvent.user_id.in_(users))
            )
            await db.execute(delete(AIUsage).where(AIUsage.user_id.in_(users)))
            await db.execute(delete(UserBook).where(UserBook.user_id.in_(users)))
            await db.execute(delete(User).where(User.id.in_(users)))
            await db.execute(delete(Edition).where(Edition.book_id.in_(books)))
            await db.execute(delete(Book).where(Book.id.in_(books)))
            await db.commit()
        await engine.dispose()


async def _exports(factory, uid) -> list[AccountExport]:
    async with factory() as db:
        rows = await db.execute(
            select(AccountExport)
            .where(AccountExport.user_id == uid)
            .order_by(AccountExport.created_at)
        )
        return list(rows.scalars())


def _at(monkeypatch, env, when: datetime) -> None:
    monkeypatch.setattr(account_export_jobs, "utcnow", lambda: when)
    env.supabase.now = when


# ── Request to delivery ──────────────────────────────────────────────────────


async def test_a_request_is_queued_then_delivered_by_email(env):
    uid, auth = await env.make_user()
    headers = {**auth, HEADER: str(uuid.uuid4())}
    first = await env.client.post(
        "/account/export", json={"locale": "de"}, headers=headers
    )
    again = await env.client.post(
        "/account/export", json={"locale": "de"}, headers=headers
    )
    assert first.status_code == again.status_code == 202, first.text
    assert again.json() == first.json() and again.headers[REPLAYED_HEADER] == "true"
    (row,) = await _exports(env.factory, uid)
    assert (row.status, row.locale, row.attempts) == ("pending", "de", 0)
    assert str(row.id) == first.json()["id"]

    assert await env.run_worker() == 1
    (row,) = await _exports(env.factory, uid)
    assert row.status == "ready" and row.error_code is None
    assert row.object_path == f"exports/{row.id}.json"
    assert row.expires_at - row.completed_at < timedelta(days=7, seconds=5)
    assert row.expires_at - row.completed_at > timedelta(days=6, hours=23)
    assert env.supabase.sign_requests == [604_800]

    data, _ = env.supabase.objects[row.object_path]
    assert row.size_bytes == len(data)
    doc = json.loads(data)
    assert next(iter(doc)) == "schema_version"
    assert doc["user_id"] == str(uid)
    assert doc["tables"]["users"][0]["email"] == f"{uid}@example.com"

    (message,) = env.resend.sent
    assert message["to"] == [f"{uid}@example.com"]
    claim = account_export_jobs.Claim(row.id, row.created_at)
    assert message["key"] == account_export_jobs.idempotency_key(claim, row.expires_at)
    assert message["key"].startswith(f"account-export-{row.id}-")
    assert message["subject"] == "Dein Bookshelf-Datenexport"
    link = _link(message)
    assert link.startswith(f"{BASE}/storage/v1/object/sign/{BUCKET}/exports/")
    assert link.replace("&", "&amp;") in message["html"]
    assert env.supabase.download(link) == 200
    assert await env.run_worker() == 0  # nothing left


async def test_a_second_request_within_a_day_is_409_then_allowed(env, monkeypatch):
    uid, auth = await env.make_user()
    assert (await env.client.post("/account/export", headers=auth)).status_code == 202
    resp = await env.client.post("/account/export", headers=auth)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "export_limit"
    assert 86_000 < detail["retry_after"] <= 86_400
    assert resp.headers["retry-after"] == str(detail["retry_after"])
    later = datetime.now(UTC) + timedelta(hours=24, seconds=5)
    monkeypatch.setattr(account_export_jobs, "utcnow", lambda: later)
    assert (await env.client.post("/account/export", headers=auth)).status_code == 202
    assert len(await _exports(env.factory, uid)) == 2


async def test_concurrent_requests_queue_one_export(env):
    uid, auth = await env.make_user()
    results = await asyncio.gather(
        *(
            env.client.post(
                "/account/export", headers={**auth, HEADER: str(uuid.uuid4())}
            )
            for _ in range(4)
        )
    )
    assert sorted(r.status_code for r in results) == [202, 409, 409, 409]
    assert len(await _exports(env.factory, uid)) == 1


async def test_unconfigured_is_503_and_queues_nothing(env):
    uid, auth = await env.make_user()
    with patch.object(settings, "supabase_url", ""):
        resp = await env.client.post("/account/export", headers=auth)
        assert await env.run_worker() == 0
    assert resp.status_code == 503
    assert resp.json() == {"detail": "export_not_configured"}
    assert await _exports(env.factory, uid) == []


async def test_no_token_is_401(env):
    resp = await env.client.post("/account/export")
    assert resp.status_code == 401


# ── Contents ─────────────────────────────────────────────────────────────────


async def _seed(factory, uid: uuid.UUID, books: list[uuid.UUID]) -> dict:
    """One row of every exported table for ``uid``; returns secret values."""
    tag = uid.hex[:8]
    secrets = {
        "jti": f"jti-{tag}",
        "family": uuid.uuid4(),
        "key_id": f"device-key-{tag}",
        "request_hash": "f" * 63 + tag[0],
        "fingerprint": tag * 8,
        "idem": f"idem-{tag}",
    }
    async with factory() as db:
        book = Book(
            title=f"Book {tag}", author="Writer", open_library_work_id=f"OL{tag}W"
        )
        db.add(book)
        await db.flush()
        books.append(book.id)
        edition = Edition(book_id=book.id, isbn_13=f"978{uid.int % 10**10:010d}")
        custom = UserCustomBook(user_id=uid, title=f"Own {tag}", author="Me")
        db.add_all([edition, custom])
        await db.flush()
        live = UserBook(
            user_id=uid,
            book_id=book.id,
            edition_id=edition.id,
            status="purchased",
            notes=f"note {tag}",
        )
        trashed = UserBook(
            user_id=uid,
            custom_book_id=custom.id,
            status="wishlisted",
            deleted_at=datetime.now(UTC),
        )
        record = ScanRecord(
            user_id=uid,
            fingerprint=secrets["fingerprint"],
            feature="scan.standard",
            tier="free",
            model="m",
            candidates=[{"title": f"Cand {tag}"}],
        )
        db.add_all([live, trashed, record])
        await db.flush()
        db.add_all(
            [
                ScanCorrection(
                    scan_record_id=record.id,
                    candidate_index=0,
                    feature="scan.standard",
                    tier="free",
                    model="m",
                    title_edited=True,
                    author_edited=False,
                    isbn_edited=False,
                ),
                AIEnhancedZeroResult(user_id=uid),
                AIQuota(
                    user_id=uid,
                    feature="scan",
                    window_kind="day",
                    window_start=datetime.now(UTC).date(),
                    units_used=2,
                ),
                AIUsage(
                    call_id=uuid.uuid4(),
                    user_id=uid,
                    feature="scan.standard",
                    model="m",
                    price_version="v1",
                    input_tokens=1,
                    output_tokens=2,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_micros=9,
                    status="ok",
                ),
                LibraryEvent(user_id=uid, user_book_id=live.id, event_type="created"),
                DeviceKey(
                    user_id=uid,
                    platform="ios",
                    key_id=secrets["key_id"],
                    public_key_cbor=b"\x01\x02cose",
                ),
                RefreshToken(
                    jti=secrets["jti"],
                    user_id=uid,
                    family_id=secrets["family"],
                    expires_at=datetime.now(UTC) + timedelta(days=30),
                ),
                IdempotencyKey(
                    user_id=uid,
                    key=secrets["idem"],
                    request_hash=secrets["request_hash"],
                    state="complete",
                ),
                AccountExport(
                    user_id=uid,
                    locale="en",
                    status="ready",
                    object_path=f"exports/old-{tag}.json",
                ),
            ]
        )
        await db.commit()
    return secrets


async def test_the_document_holds_every_table_and_only_this_user(env):
    alice, _ = await env.make_user()
    bob, _ = await env.make_user()
    alice_secrets = await _seed(env.factory, alice, env.books)
    bob_secrets = await _seed(env.factory, bob, env.books)
    async with env.factory() as db:
        doc = await account_export.build(db, alice)
    text = json.dumps(doc)

    for table, rows in doc["tables"].items():
        assert rows, f"{table} is empty for a user who has rows in it"
    assert len(doc["tables"]["user_books"]) == 2  # trashed entry included
    assert any(r["deleted_at"] for r in doc["tables"]["user_books"])
    assert len(doc["catalog"]["books"]) == 1 and len(doc["catalog"]["editions"]) == 1
    assert doc["identities"] == [{"provider": "google", "subject": f"g-{alice}"}]
    assert doc["tables"]["users"][0]["id"] == str(alice)

    assert str(bob) not in text
    assert bob.hex[:8] not in text
    for value in (*alice_secrets.values(), *bob_secrets.values()):
        assert str(value) not in text, value
    for column in (
        "jti",
        "family_id",
        "key_id",
        "public_key_cbor",
        "request_hash",
        "fingerprint",
        "token_version",
        "object_path",
        "cost_micros",
    ):
        assert f'"{column}"' not in text, column
    assert "idempotency_keys" not in doc["tables"]


# ── Failures, retries, restarts ──────────────────────────────────────────────


async def test_a_failed_attempt_retries_after_the_lease_then_gives_up(env, monkeypatch):
    uid, auth = await env.make_user()
    assert (await env.client.post("/account/export", headers=auth)).status_code == 202
    env.supabase.fail.add("sign")
    start = datetime.now(UTC)
    _at(monkeypatch, env, start)
    await env.run_worker()
    (row,) = await _exports(env.factory, uid)
    assert (row.status, row.attempts, row.error_code) == (
        "pending",
        1,
        "storage_sign_500",
    )
    assert row.object_path in env.supabase.objects  # uploaded before the failure
    assert await env.run_worker() == 0  # waits out the lease

    step = account_export_jobs.LEASE + timedelta(seconds=1)
    for n in (1, 2):
        _at(monkeypatch, env, start + step * n)
        assert await env.run_worker() == 1
    (row,) = await _exports(env.factory, uid)
    assert (row.status, row.attempts) == ("failed", 3)
    assert row.object_path not in env.supabase.objects  # deleted
    assert env.resend.sent == []


async def test_an_email_failure_is_retried_and_sends_once(env, monkeypatch):
    uid, auth = await env.make_user()
    await env.client.post("/account/export", headers=auth)
    env.resend.status = 500
    start = datetime.now(UTC)
    _at(monkeypatch, env, start)
    await env.run_worker()
    (row,) = await _exports(env.factory, uid)
    # The link was recorded before the send: the row waits as emailing.
    assert (row.status, row.error_code) == ("emailing", "email_500")
    assert row.delivery_url and row.object_path in env.supabase.objects
    env.resend.status = 200
    _at(monkeypatch, env, start + account_export_jobs.LEASE + timedelta(seconds=1))
    await env.run_worker()
    (row,) = await _exports(env.factory, uid)
    assert row.status == "ready"
    assert len(env.resend.sent) == 1


async def test_a_run_that_died_is_taken_over_after_a_restart(env):
    uid, _ = await env.make_user()
    async with env.factory() as db:
        db.add(
            AccountExport(
                user_id=uid,
                locale="ja",
                status="running",
                attempts=1,
                claimed_at=datetime.now(UTC) - account_export_jobs.LEASE * 2,
                created_at=datetime.now(UTC) - timedelta(minutes=30),
            )
        )
        await db.commit()
    assert await env.run_worker() == 1
    (row,) = await _exports(env.factory, uid)
    assert (row.status, row.attempts) == ("ready", 2)
    assert env.resend.sent[0]["subject"] == "Bookshelf データのエクスポート"


async def test_a_live_lease_is_left_alone_and_exhausted_rows_fail(env):
    uid, _ = await env.make_user()
    other, _ = await env.make_user()
    async with env.factory() as db:
        db.add_all(
            [
                AccountExport(
                    user_id=uid,
                    locale="en",
                    status="running",
                    attempts=1,
                    claimed_at=datetime.now(UTC),
                ),
                AccountExport(
                    user_id=other,
                    locale="en",
                    status="running",
                    attempts=3,
                    claimed_at=datetime.now(UTC) - timedelta(hours=1),
                ),
            ]
        )
        await db.commit()
    assert await env.run_worker() == 0
    assert (await _exports(env.factory, uid))[0].status == "running"
    (gone,) = await _exports(env.factory, other)
    assert (gone.status, gone.error_code) == ("failed", "attempts_exhausted")


async def test_two_workers_never_claim_the_same_row(env):
    for _ in range(3):
        uid, _ = await env.make_user()
        async with env.factory() as db:
            db.add(AccountExport(user_id=uid, locale="en", status="pending"))
            await db.commit()

    async def claim():
        async with env.factory() as db:
            return await account_export_jobs.claim_next(db)

    claimed = [c.id for c in await asyncio.gather(*(claim() for _ in range(6))) if c]
    assert len(claimed) == len(set(claimed)) >= 3


async def test_an_account_deleted_before_its_export_ran(env):
    uid, auth = await env.make_user()
    await env.client.post("/account/export", headers=auth)
    async with env.factory() as db:
        claim = await account_export_jobs.claim_next(db)
        await db.execute(delete(User).where(User.id == uid))
        await db.commit()
    assert not await account_export_jobs.process(
        claim, env.supabase.storage(), sessions=env.factory
    )
    assert env.supabase.objects == {} and env.resend.sent == []


# ── Expiry and purge ─────────────────────────────────────────────────────────


async def test_the_link_works_for_seven_days_then_the_file_is_gone(env, monkeypatch):
    uid, auth = await env.make_user()
    await env.client.post("/account/export", headers=auth)
    start = datetime.now(UTC)
    _at(monkeypatch, env, start)
    await env.run_worker()
    link = _link(env.resend.sent[0])
    (row,) = await _exports(env.factory, uid)

    _at(monkeypatch, env, start + timedelta(days=6, hours=23))
    assert env.supabase.download(link) == 200
    async with env.factory() as db:
        await account_export_purge.purge_expired(db, env.supabase.storage())
    assert len(await _exports(env.factory, uid)) == 1  # not expired yet

    _at(monkeypatch, env, start + timedelta(days=7, seconds=5))
    assert env.supabase.download(link) == 400  # the signed token expired
    async with env.factory() as db:
        purged = await account_export_purge.purge_expired(db, env.supabase.storage())
    assert purged >= 1
    assert await _exports(env.factory, uid) == []
    assert row.object_path not in env.supabase.objects
    env.supabase.now = start  # even with a still-valid token, the object is gone
    assert env.supabase.download(link) == 404


async def test_the_purge_runs_in_batches_and_keeps_live_rows(env, monkeypatch):
    uid, _ = await env.make_user()
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    rows = [
        ("ready", now - timedelta(days=7, hours=1), now - timedelta(hours=1), True),
        ("ready", now - timedelta(days=7, hours=2), now - timedelta(hours=2), True),
        ("failed", old, None, True),
        ("pending", old, None, False),
        ("ready", now - timedelta(days=1), now + timedelta(days=6), True),  # live
        ("failed", now - timedelta(days=1), None, False),  # recent: kept
    ]
    async with env.factory() as db:
        for i, (status, created, expires, has_object) in enumerate(rows):
            path = f"exports/batch-{uid.hex}-{i}.json" if has_object else None
            if path:
                env.supabase.objects[path] = (b"{}", created)
            db.add(
                AccountExport(
                    user_id=uid,
                    locale="en",
                    status=status,
                    created_at=created,
                    expires_at=expires,
                    object_path=path,
                )
            )
        await db.commit()
    monkeypatch.setattr(account_export_purge, "PURGE_BATCH_SIZE", 2)
    async with env.factory() as db:
        purged = await account_export_purge.purge_expired(
            db, env.supabase.storage(), now
        )
    assert purged >= 4
    left = sorted(
        (r.status, r.expires_at is not None) for r in await _exports(env.factory, uid)
    )
    assert left == [("failed", False), ("ready", True)]
    assert sorted(env.supabase.objects) == [f"exports/batch-{uid.hex}-4.json"]


async def test_a_storage_failure_keeps_the_rows(env):
    uid, _ = await env.make_user()
    async with env.factory() as db:
        db.add(
            AccountExport(
                user_id=uid,
                locale="en",
                status="ready",
                expires_at=datetime.now(UTC) - timedelta(days=1),
                object_path="exports/keep.json",
            )
        )
        await db.commit()
    env.supabase.fail.add("delete")
    async with env.factory() as db:
        with pytest.raises(Exception):  # noqa: B017 - the cleanup step logs it
            await account_export_purge.purge_expired(db, env.supabase.storage())
    assert len(await _exports(env.factory, uid)) == 1


async def test_without_storage_only_rows_with_no_object_go(env):
    uid, _ = await env.make_user()
    old = datetime.now(UTC) - timedelta(days=8)
    async with env.factory() as db:
        db.add_all(
            [
                AccountExport(
                    user_id=uid, locale="en", status="failed", created_at=old
                ),
                AccountExport(
                    user_id=uid,
                    locale="en",
                    status="failed",
                    created_at=old,
                    object_path="exports/x.json",
                ),
            ]
        )
        await db.commit()
    async with env.factory() as db:
        await account_export_purge.purge_expired(db, None)
    (left,) = await _exports(env.factory, uid)
    assert left.object_path == "exports/x.json"


async def test_the_sweep_deletes_old_files_no_row_knows(env, monkeypatch):
    now = datetime.now(UTC)
    objects = env.supabase.objects
    objects["exports/orphan-old.json"] = (b"{}", now - timedelta(days=9))
    objects["exports/recent.json"] = (b"{}", now - timedelta(days=2))
    objects["other/old.json"] = (b"{}", now - timedelta(days=30))
    monkeypatch.setattr(account_export_purge, "PURGE_BATCH_SIZE", 1)
    swept = await account_export_purge.sweep_orphans(env.supabase.storage(), now)
    assert swept == 1
    assert sorted(objects) == ["exports/recent.json", "other/old.json"]


async def test_deleting_a_user_deletes_their_export_rows(env):
    uid, auth = await env.make_user()
    await env.client.post("/account/export", headers=auth)
    assert len(await _exports(env.factory, uid)) == 1
    async with env.factory() as db:
        await db.execute(delete(User).where(User.id == uid))
        await db.commit()
    assert await _exports(env.factory, uid) == []


# ── Privileges, RLS, migration (migrated database) ───────────────────────────


def _privileges_and_cascade(connection) -> None:
    def scalar(sql, **params):
        return connection.execute(sa.text(sql), params).scalar()

    assert scalar(f"SELECT relrowsecurity FROM pg_class WHERE relname = '{TABLE}'")
    assert scalar(f"SELECT count(*) FROM pg_policies WHERE tablename = '{TABLE}'") == 0
    for role, privilege, held in (
        (APP_DB_ROLE, "SELECT", True),
        (APP_DB_ROLE, "INSERT", True),
        (APP_DB_ROLE, "UPDATE", True),
        (APP_DB_ROLE, "DELETE", True),
        (APP_DB_ROLE, "TRUNCATE", False),
        ("anon", "SELECT", False),
        ("anon", "INSERT", False),
        ("authenticated", "SELECT", False),
        ("authenticated", "INSERT", False),
    ):
        if not scalar("SELECT count(*) FROM pg_roles WHERE rolname = :r", r=role):
            continue
        assert (
            scalar(
                f"SELECT has_table_privilege(:r, 'public.{TABLE}', :p)",
                r=role,
                p=privilege,
            )
            is held
        ), (role, privilege)

    uid = uuid.uuid4()
    connection.execute(sa.text(f"SET LOCAL ROLE {APP_DB_ROLE}"))
    connection.execute(
        sa.text("INSERT INTO users (id, email, google_id) VALUES (:id, :e, :g)"),
        {"id": uid, "e": f"{uid}@example.com", "g": f"g-{uid}"},
    )
    connection.execute(
        sa.text(f"INSERT INTO {TABLE} (id, user_id, locale) VALUES (:id, :u, 'en')"),
        {"id": uuid.uuid4(), "u": uid},
    )
    assert scalar(f"SELECT status FROM {TABLE} WHERE user_id = :u", u=uid) == "pending"
    connection.execute(
        sa.text(f"UPDATE {TABLE} SET status = 'ready' WHERE user_id = :u"), {"u": uid}
    )
    connection.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": uid})
    assert scalar(f"SELECT count(*) FROM {TABLE} WHERE user_id = :u", u=uid) == 0
    connection.execute(sa.text("RESET ROLE"))


async def test_rls_grants_and_cascade_for_the_app_role():
    await run_rolled_back(_privileges_and_cascade)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0034", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _diffs(context) -> list:
    return [d for d in compare_metadata(context, Base.metadata) if TABLE in repr(d)]


def _migration_cycle(connection) -> None:
    from alembic.operations import Operations

    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0034", "0033")
    context = MigrationContext.configure(connection)

    def has_table() -> bool:
        return inspect(connection).has_table(TABLE)

    with Operations.context(context):
        migration.downgrade()
        assert not has_table()
        migration.downgrade()  # guarded: a re-run with the table gone
        migration.upgrade()
        assert has_table()
        migration.upgrade()  # guarded: a re-run with the table there
    assert _diffs(context) == []
    assert connection.execute(
        sa.text(f"SELECT relrowsecurity FROM pg_class WHERE relname = '{TABLE}'")
    ).scalar()


async def test_migration_0034_downgrade_and_upgrade():
    await run_rolled_back(_migration_cycle)


def _half_applied(connection) -> None:
    """The table is there but two of its indexes are not (an earlier attempt
    stopped part way): the re-run adds only those."""
    from alembic.operations import Operations

    migration = _load_migration()
    context = MigrationContext.configure(connection)
    connection.execute(sa.text("DROP INDEX ix_account_exports_open"))
    connection.execute(sa.text("DROP INDEX ix_account_exports_expires_at"))
    with Operations.context(context):
        migration.upgrade()
    indexes = {i["name"]: i for i in inspect(connection).get_indexes(TABLE)}
    assert {"ix_account_exports_open", "ix_account_exports_expires_at"} <= set(indexes)
    where = connection.execute(
        sa.text(
            "SELECT pg_get_expr(indpred, indrelid) FROM pg_index "
            "WHERE indexrelid = 'ix_account_exports_open'::regclass"
        )
    ).scalar()
    assert "pending" in where and "running" in where
    assert _diffs(context) == []


async def test_migration_0034_resumes_after_a_half_applied_upgrade():
    await run_rolled_back(_half_applied)


# ── Durable, idempotent delivery (review round 1) ────────────────────────────


async def _queued(env) -> tuple[uuid.UUID, dict]:
    uid, auth = await env.make_user()
    resp = await env.client.post("/account/export", headers=auth)
    assert resp.status_code == 202, resp.text
    return uid, auth


def _after_lease(monkeypatch, env, start: datetime, n: int = 1) -> None:
    _at(
        monkeypatch, env, start + (account_export_jobs.LEASE + timedelta(seconds=1)) * n
    )


async def _assert_delivered_once(env, uid) -> None:
    (row,) = await _exports(env.factory, uid)
    assert row.status == "ready", (row.status, row.error_code)
    assert len(env.resend.sent) == 1
    link = _link(env.resend.sent[0])
    assert link == row.delivery_url
    assert env.supabase.download(link) == 200


async def test_email_accepted_but_the_final_commit_fails(env, monkeypatch):
    uid, _ = await _queued(env)
    start = datetime.now(UTC)
    _at(monkeypatch, env, start)
    real = account_export_jobs._mark_ready

    async def lost_commit(*_a, **_k):
        raise OSError("connection reset during commit")

    monkeypatch.setattr(account_export_jobs, "_mark_ready", lost_commit)
    await env.run_worker()
    (row,) = await _exports(env.factory, uid)
    assert (row.status, row.error_code) == ("emailing", "unexpected_error")
    assert len(env.resend.sent) == 1

    monkeypatch.setattr(account_export_jobs, "_mark_ready", real)
    _after_lease(monkeypatch, env, start)
    assert await env.run_worker() == 1
    await _assert_delivered_once(env, uid)


async def test_email_accepted_but_every_try_times_out(env, monkeypatch):
    uid, _ = await _queued(env)
    start = datetime.now(UTC)
    _at(monkeypatch, env, start)
    env.resend.timeout_after_accept = True
    await env.run_worker()
    (row,) = await _exports(env.factory, uid)
    assert (row.status, row.error_code) == ("emailing", "email_unreachable")
    assert len(env.resend.sent) == 1

    env.resend.timeout_after_accept = False
    _after_lease(monkeypatch, env, start)
    await env.run_worker()
    await _assert_delivered_once(env, uid)


async def test_cancelled_between_the_send_and_the_commit(env, monkeypatch):
    uid, _ = await _queued(env)
    start = datetime.now(UTC)
    _at(monkeypatch, env, start)
    real = account_export_jobs._mark_ready

    async def shutdown(*_a, **_k):
        raise asyncio.CancelledError  # SIGTERM: the task is cancelled

    monkeypatch.setattr(account_export_jobs, "_mark_ready", shutdown)
    with pytest.raises(asyncio.CancelledError):
        await env.run_worker()
    (row,) = await _exports(env.factory, uid)
    assert row.status == "emailing" and row.claimed_at is not None

    monkeypatch.setattr(account_export_jobs, "_mark_ready", real)
    _after_lease(monkeypatch, env, start)
    await env.run_worker()
    await _assert_delivered_once(env, uid)


async def test_a_changed_address_after_the_send_counts_as_sent(env, monkeypatch):
    """Resend refuses the key with a different payload (409): the first
    email went out, so the row is ready and nothing more is sent."""
    uid, _ = await _queued(env)
    start = datetime.now(UTC)
    _at(monkeypatch, env, start)
    env.resend.timeout_after_accept = True
    await env.run_worker()
    env.resend.timeout_after_accept = False
    async with env.factory() as db:
        user = await db.get(User, uid)
        user.email = f"new-{uid}@example.com"
        await db.commit()
    _after_lease(monkeypatch, env, start)
    await env.run_worker()
    await _assert_delivered_once(env, uid)
    assert env.resend.sent[0]["to"] == [f"{uid}@example.com"]


async def test_failing_for_good_after_the_link_was_recorded_keeps_the_file(
    env, monkeypatch
):
    uid, _ = await _queued(env)
    start = datetime.now(UTC)
    _at(monkeypatch, env, start)
    env.resend.status = 500
    for n in range(account_export_jobs.MAX_ATTEMPTS):
        _after_lease(monkeypatch, env, start, n)
        await env.run_worker()
    (row,) = await _exports(env.factory, uid)
    assert row.status == "failed"
    assert row.object_path in env.supabase.objects  # kept until expires_at

    async with env.factory() as db:
        await account_export_purge.purge_expired(
            db, env.supabase.storage(), start + timedelta(days=7, hours=-1)
        )
    assert row.object_path in env.supabase.objects
    async with env.factory() as db:
        await account_export_purge.purge_expired(
            db, env.supabase.storage(), row.expires_at + timedelta(seconds=1)
        )
    assert row.object_path not in env.supabase.objects
    assert await _exports(env.factory, uid) == []


async def test_a_stale_attempt_cannot_touch_a_row_another_attempt_finished(
    env, monkeypatch
):
    """Fencing: attempt A's lease runs out, B takes the row over and
    delivers it; A, still running, can neither fail, reopen, finish nor
    delete anything."""
    uid, _ = await _queued(env)
    start = datetime.now(UTC)
    async with env.factory() as db:
        stale = await account_export_jobs.claim_next(db, start)
    async with env.factory() as db:
        fresh = await account_export_jobs.claim_next(
            db, start + account_export_jobs.LEASE + timedelta(seconds=1)
        )
    assert stale.id == fresh.id and stale.claimed_at != fresh.claimed_at
    assert await account_export_jobs.process(
        fresh,
        env.supabase.storage(),
        sessions=env.factory,
        email_transport=env.resend.transport(),
    )
    (done,) = await _exports(env.factory, uid)

    # A's failure is not recorded, and the file is not deleted.
    await account_export_jobs._record_failure(
        env.factory, stale, "storage_upload_500", env.supabase.storage()
    )
    # A's delivery cannot record a link or send a second email.
    assert not await account_export_jobs.process(
        stale,
        env.supabase.storage(),
        sessions=env.factory,
        email_transport=env.resend.transport(),
    )
    (row,) = await _exports(env.factory, uid)
    assert (row.status, row.error_code) == ("ready", None)
    assert row.delivery_url == done.delivery_url
    assert done.object_path in env.supabase.objects
    assert len(env.resend.sent) == 1


async def test_a_failed_export_does_not_count_toward_the_cap(env):
    uid, auth = await env.make_user()
    async with env.factory() as db:
        db.add(AccountExport(user_id=uid, locale="en", status="failed"))
        await db.commit()
    resp = await env.client.post("/account/export", headers=auth)
    assert resp.status_code == 202, resp.text
    again = await env.client.post("/account/export", headers=auth)
    assert again.status_code == 409


async def test_a_replayed_cap_refusal_keeps_retry_after(env):
    _, auth = await env.make_user()
    assert (await env.client.post("/account/export", headers=auth)).status_code == 202
    keyed = {**auth, HEADER: str(uuid.uuid4())}
    first = await env.client.post("/account/export", headers=keyed)
    replay = await env.client.post("/account/export", headers=keyed)
    assert first.status_code == replay.status_code == 409
    assert replay.headers[REPLAYED_HEADER] == "true"
    assert replay.headers["retry-after"] == first.headers["retry-after"]
    assert replay.json() == first.json()


# ── Review round 2 ───────────────────────────────────────────────────────────


async def test_a_refused_send_then_a_409_is_never_ready(env, monkeypatch):
    """Attempt 1 is refused (422) and Resend keeps the key; the address then
    changes, so attempt 2 reuses the key with another payload and gets a 409.
    Nothing was ever sent: the row must not become ready."""
    uid, _ = await _queued(env)
    start = datetime.now(UTC)
    _at(monkeypatch, env, start)
    env.resend.status, env.resend.keep_key_on_refusal = 422, True
    await env.run_worker()
    (row,) = await _exports(env.factory, uid)
    assert (row.status, row.error_code, row.email_accepted_at) == (
        "emailing",
        "email_422",
        None,
    )
    env.resend.status = 200
    async with env.factory() as db:
        user = await db.get(User, uid)
        user.email = f"changed-{uid}@example.com"
        await db.commit()
    _after_lease(monkeypatch, env, start)
    await env.run_worker()
    (row,) = await _exports(env.factory, uid)
    assert (row.status, row.error_code) == ("emailing", "email_409")
    assert env.resend.sent == []


async def test_an_expiring_link_is_re_signed_not_emailed(env, monkeypatch):
    """The export sat in emailing until its link had under an hour left
    (email was down for days): the next attempt signs a new link and emails
    that one, under a new key."""
    uid, _ = await _queued(env)
    start = datetime.now(UTC)
    _at(monkeypatch, env, start)
    env.resend.status = 503
    await env.run_worker()
    (stale,) = await _exports(env.factory, uid)
    assert stale.status == "emailing" and stale.email_accepted_at is not None
    env.resend.status = 200
    _at(monkeypatch, env, stale.expires_at - timedelta(minutes=30))
    await env.run_worker()
    (row,) = await _exports(env.factory, uid)
    assert row.status == "ready"
    assert row.delivery_url != stale.delivery_url
    assert row.expires_at > stale.expires_at
    (message,) = env.resend.sent
    assert _link(message) == row.delivery_url
    assert env.supabase.download(row.delivery_url) == 200


async def test_failed_exports_allow_three_a_day(env):
    uid, auth = await env.make_user()
    async with env.factory() as db:
        db.add_all(
            AccountExport(user_id=uid, locale="en", status="failed") for _ in range(2)
        )
        await db.commit()
    first = await env.client.post("/account/export", headers=auth)
    assert first.status_code == 202, first.text
    async with env.factory() as db:  # the third fails too
        row = await db.get(AccountExport, uuid.UUID(first.json()["id"]))
        row.status = "failed"
        await db.commit()
    limited = await env.client.post("/account/export", headers=auth)
    assert limited.status_code == 409
    assert limited.json()["detail"]["code"] == "export_limit"
    assert 86_000 < limited.json()["detail"]["retry_after"] <= 86_400


async def test_the_purge_never_deletes_a_file_someone_can_still_use(env):
    uid, _ = await env.make_user()
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    cases = {
        # Stale, but a worker claimed it a minute ago (live lease).
        "leased": {"status": "running", "claimed_at": now - timedelta(minutes=1)},
        # Stale, but re-signed: its emailed link still works.
        "relinked": {"status": "emailing", "expires_at": now + timedelta(days=6)},
        # Stale and failed, with a link that still works.
        "failed_live": {"status": "failed", "expires_at": now + timedelta(hours=2)},
        # Over: purged.
        "over": {"status": "failed", "expires_at": now - timedelta(hours=1)},
    }
    async with env.factory() as db:
        for name, values in cases.items():
            path = f"exports/{name}-{uid.hex}.json"
            env.supabase.objects[path] = (b"{}", old)
            db.add(
                AccountExport(
                    user_id=uid, locale="en", created_at=old, object_path=path, **values
                )
            )
        await db.commit()
    async with env.factory() as db:
        await account_export_purge.purge_expired(db, env.supabase.storage(), now)
    kept = sorted(r.object_path.split("-")[0] for r in await _exports(env.factory, uid))
    assert kept == ["exports/failed_live", "exports/leased", "exports/relinked"]
    assert f"exports/over-{uid.hex}.json" not in env.supabase.objects
    for name in ("leased", "relinked", "failed_live"):
        assert f"exports/{name}-{uid.hex}.json" in env.supabase.objects


async def test_the_purge_skips_a_row_a_worker_is_claiming(env):
    """The batch is locked (FOR UPDATE SKIP LOCKED): a row a worker holds
    locked while claiming it is neither picked nor deleted, nor its file."""
    uid, _ = await env.make_user()
    now = datetime.now(UTC)
    path = f"exports/claiming-{uid.hex}.json"
    env.supabase.objects[path] = (b"{}", now - timedelta(days=8))
    async with env.factory() as db:
        row = AccountExport(
            user_id=uid,
            locale="en",
            status="pending",
            created_at=now - timedelta(days=8),
            object_path=path,
        )
        db.add(row)
        await db.commit()
    async with env.factory() as worker:
        await worker.execute(
            select(AccountExport).where(AccountExport.id == row.id).with_for_update()
        )
        async with env.factory() as db:
            await account_export_purge.purge_expired(db, env.supabase.storage(), now)
        await worker.rollback()
    assert len(await _exports(env.factory, uid)) == 1
    assert path in env.supabase.objects
