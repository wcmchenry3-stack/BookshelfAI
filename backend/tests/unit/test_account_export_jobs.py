"""The export worker and purge (#694), database-free.

A scripted session stands in for Postgres (its ``get`` / ``scalar`` /
``execute`` answers are set per test) and Supabase / Resend are
``httpx.MockTransport``s. This covers the state changes and the branches:
claiming (and failing an exhausted row), a delivery, each kind of failure
and its error code, the final failure deleting the object, the worker loop
and its wake-up, the kill switch, the purge's batches and the storage
sweep. The same flows against real Postgres are in
``tests/integration/test_account_export_db.py``.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.sql.dml import Update

from app.core.config import settings
from app.models.account_export import AccountExport
from app.models.user import User
from app.services import _http_retry, account_export
from app.services import account_export_jobs as jobs
from app.services.email import EmailError
from app.services.object_storage import ObjectStorage, StorageError

NOW = datetime(2026, 11, 23, 8, 0, tzinfo=UTC)
KEY = f"k-{uuid.uuid5(uuid.NAMESPACE_URL, 'jobs-unit')}"
_REAL_UTCNOW = jobs.utcnow


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(_http_retry, "BACKOFF_S", 0)
    monkeypatch.setattr(jobs, "utcnow", lambda: NOW)


class Session:
    """Scripted answers; records commits, rollbacks and executed statements.

    A fenced ``UPDATE`` of ``account_exports`` applies its values to the
    ``AccountExport`` object in ``get`` while ``owned`` is true (the attempt
    still holds the row) and reports one row; otherwise none. ``scalar`` on
    the export entity returns that object while owned, on its id the id."""

    def __init__(self, *, get=None, scalars=(), results=(), owned=True):
        self.objects = get or {}
        self.scalars = list(scalars)
        self.results = list(results)
        self.owned = owned
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.added = []

    async def get(self, model, _pk, **_kw):
        return self.objects.get(model)

    async def scalar(self, stmt):
        if self.scalars:
            return self.scalars.pop(0)
        export = self.objects.get(AccountExport)
        if export is None:
            return None
        if stmt.column_descriptions[0]["expr"] is AccountExport:
            return export if self.owned else None
        return export.id

    async def execute(self, stmt):
        self.executed.append(stmt)
        if isinstance(stmt, Update):
            export = self.objects.get(AccountExport)
            if export is None or not self.owned:
                return MagicMock(rowcount=0)
            for column, bind in stmt._values.items():
                setattr(export, column.key, bind.value)
            return MagicMock(rowcount=1)
        return self.results.pop(0) if self.results else MagicMock(rowcount=0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def sessions_of(session):
    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    return _Ctx


class Storage:
    """A MockTransport Supabase recording what was done."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.uploaded, self.deleted, self.listing = [], [], []

    def handler(self, request):
        path = request.url.path
        op = (
            "sign"
            if "/object/sign/" in path
            else (
                "list"
                if "/object/list/" in path
                else "delete" if request.method == "DELETE" else "upload"
            )
        )
        if op in self.fail:
            return httpx.Response(500)
        if op == "upload":
            self.uploaded.append(request.content)
            return httpx.Response(200, json={})
        if op == "sign":
            return httpx.Response(200, json={"signedURL": "/object/sign/b/x?token=t"})
        if op == "delete":
            self.deleted.extend(json.loads(request.content)["prefixes"])
            return httpx.Response(200, json=[])
        page, self.listing = self.listing[:1], self.listing[1:]
        return httpx.Response(200, json=page[0] if page else [])

    def client(self):
        return ObjectStorage(
            "https://x.supabase.co",
            KEY,
            "b",
            transport=httpx.MockTransport(self.handler),
        )


def resend(status=200, body=None):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler), sent


def _export(**kw) -> AccountExport:
    values = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "status": "running",
        "locale": "en",
        "attempts": 1,
        "claimed_at": NOW,
        "created_at": NOW,
        "object_path": None,
        "delivery_url": None,
        "expires_at": None,
        "error_code": None,
    }
    values.update(kw)
    return AccountExport(**values)


def _user(uid) -> User:
    return User(id=uid, email="reader@example.com", google_id="g")


@pytest.fixture
def built(monkeypatch):
    monkeypatch.setattr(
        account_export, "build", AsyncMock(return_value={"schema_version": 1})
    )


# ── Requests ─────────────────────────────────────────────────────────────────


async def test_request_queues_a_pending_row():
    db = Session()
    user = SimpleNamespace(id=uuid.uuid4())
    export = await jobs.request_export(db, user, "ko")
    assert (export.status, export.locale, export.created_at) == ("pending", "ko", NOW)
    assert db.added == [export]
    assert len(db.executed) == 1  # the user row lock


async def test_request_within_the_window_is_limited():
    db = Session(scalars=[NOW - timedelta(hours=23)])
    with pytest.raises(jobs.ExportLimited) as exc_info:
        await jobs.request_export(db, SimpleNamespace(id=uuid.uuid4()), "en")
    assert exc_info.value.retry_after == 3600
    db = Session(scalars=[NOW - timedelta(hours=24) + timedelta(milliseconds=1)])
    with pytest.raises(jobs.ExportLimited) as exc_info:
        await jobs.request_export(db, SimpleNamespace(id=uuid.uuid4()), "en")
    assert exc_info.value.retry_after == 1


def test_the_clock_is_utc():
    assert _REAL_UTCNOW().tzinfo is UTC


def test_object_path_has_no_user_id():
    export_id = uuid.uuid4()
    assert jobs.object_path(export_id) == f"exports/{export_id}.json"


# ── Claiming ─────────────────────────────────────────────────────────────────


async def test_claim_takes_the_oldest_open_row():
    row = _export(status="pending", attempts=0, claimed_at=None)
    db = Session(scalars=[row])
    assert await jobs.claim_next(db) == jobs.Claim(row.id, NOW)
    assert (row.status, row.attempts, row.claimed_at) == ("running", 1, NOW)
    assert db.commits == 1


async def test_claim_keeps_an_emailing_row_emailing():
    row = _export(status="emailing", attempts=1, claimed_at=NOW - timedelta(hours=1))
    assert await jobs.claim_next(Session(scalars=[row])) == jobs.Claim(row.id, NOW)
    assert (row.status, row.attempts) == ("emailing", 2)


async def test_claim_fails_an_exhausted_row_and_moves_on(caplog):
    tired = _export(attempts=jobs.MAX_ATTEMPTS, error_code="email_500")
    db = Session(scalars=[tired, None])
    with caplog.at_level("WARNING"):
        assert await jobs.claim_next(db) is None
    assert (tired.status, tired.error_code, tired.claimed_at) == (
        "failed",
        "email_500",
        None,
    )
    assert db.commits == 2
    assert "gave up" in caplog.text
    fresh = _export(attempts=jobs.MAX_ATTEMPTS)
    await jobs.claim_next(Session(scalars=[fresh, None]))
    assert fresh.error_code == "attempts_exhausted"


# ── Delivery and failures ────────────────────────────────────────────────────


def _claim(export) -> jobs.Claim:
    return jobs.Claim(export.id, export.claimed_at)


def _db(export, **kw) -> Session:
    return Session(get={AccountExport: export, User: _user(export.user_id)}, **kw)


async def test_process_uploads_signs_records_emails_and_marks_ready(built):
    export = _export()
    db = _db(export)
    storage = Storage()
    transport, sent = resend()
    assert await jobs.process(
        _claim(export),
        storage.client(),
        sessions=sessions_of(db),
        email_transport=transport,
    )
    assert storage.uploaded == [b'{"schema_version":1}']
    assert export.status == "ready" and export.expires_at == NOW + jobs.LINK_TTL
    assert export.object_path == jobs.object_path(export.id)
    assert export.size_bytes == len(storage.uploaded[0])
    assert export.delivery_url.startswith("https://x.supabase.co/storage/v1/")
    assert (export.completed_at, export.claimed_at) == (NOW, None)
    assert sent[0]["to"] == ["reader@example.com"]
    assert export.delivery_url in sent[0]["text"]
    assert "download=bookshelf-export-2026-11-23.json" in sent[0]["text"]


async def test_an_emailing_row_resends_the_recorded_link_without_rebuilding(
    monkeypatch,
):
    build = AsyncMock()
    monkeypatch.setattr(account_export, "build", build)
    url = "https://x.supabase.co/storage/v1/object/sign/b/x?token=recorded"
    export = _export(
        status="emailing", delivery_url=url, expires_at=NOW + timedelta(days=6)
    )
    storage = Storage()
    transport, sent = resend()
    assert await jobs.process(
        _claim(export),
        storage.client(),
        sessions=sessions_of(_db(export)),
        email_transport=transport,
    )
    build.assert_not_awaited()
    assert storage.uploaded == []
    assert url in sent[0]["text"]
    assert export.status == "ready"


async def test_a_reused_key_means_the_email_already_went_out(built, caplog):
    export = _export(status="emailing", delivery_url="https://x/s?t=1", expires_at=NOW)
    transport, _ = resend(409, {"name": "invalid_idempotent_request"})
    with caplog.at_level("INFO"):
        assert await jobs.process(
            _claim(export),
            Storage().client(),
            sessions=sessions_of(_db(export)),
            email_transport=transport,
        )
    assert export.status == "ready"
    assert "already sent" in caplog.text


async def test_another_409_from_resend_is_a_failure(built):
    export = _export(status="emailing", delivery_url="https://x/s?t=1", expires_at=NOW)
    transport, _ = resend(409, {"name": "concurrent_idempotent_requests"})
    assert not await jobs.process(
        _claim(export),
        Storage().client(),
        sessions=sessions_of(_db(export)),
        email_transport=transport,
    )
    assert (export.status, export.error_code) == ("emailing", "email_409")


async def test_nothing_to_do_when_the_row_or_user_is_gone(built):
    storage = Storage()
    claim = jobs.Claim(uuid.uuid4(), NOW)
    assert not await jobs.process(
        claim, storage.client(), sessions=sessions_of(Session())
    )
    export = _export()
    db = Session(get={AccountExport: export})
    assert not await jobs.process(
        _claim(export), storage.client(), sessions=sessions_of(db)
    )
    assert storage.uploaded == []


async def test_a_lost_claim_before_emailing_sends_nothing_and_keeps_the_file(built):
    """Another attempt took the row over while this one was uploading: it
    writes the same path, so this one deletes nothing and emails nothing."""
    export = _export()
    db = _db(export, owned=False)
    storage = Storage()
    transport, sent = resend()
    assert not await jobs.process(
        _claim(export),
        storage.client(),
        sessions=sessions_of(db),
        email_transport=transport,
    )
    assert sent == [] and storage.deleted == []
    assert export.status == "running" and export.delivery_url is None


async def test_a_row_deleted_during_the_upload_deletes_the_object(built):
    export = _export()

    class Vanishing(Session):
        async def scalar(self, stmt):
            return None  # the row is gone when the upload finishes

    db = Vanishing(get={AccountExport: export, User: _user(export.user_id)})
    db.owned = False
    storage = Storage()
    assert not await jobs.process(
        _claim(export), storage.client(), sessions=sessions_of(db)
    )
    assert storage.deleted == [jobs.object_path(export.id)]


@pytest.mark.parametrize(
    ("fail", "email_status", "code", "status"),
    [
        ({"upload"}, 200, "storage_upload_500", "pending"),
        ({"sign"}, 200, "storage_sign_500", "pending"),
        (set(), 503, "email_503", "emailing"),
    ],
)
async def test_a_failed_attempt_goes_back_to_the_queue(
    built, fail, email_status, code, status
):
    export = _export(attempts=1)
    transport, _ = resend(email_status)
    ok = await jobs.process(
        _claim(export),
        Storage(fail).client(),
        sessions=sessions_of(_db(export)),
        email_transport=transport,
    )
    assert not ok
    assert (export.status, export.error_code, export.claimed_at) == (
        status,
        code,
        NOW,
    )


def test_error_codes():
    assert jobs._error_code(StorageError("list", None)) == "storage_list_unreachable"
    assert jobs._error_code(EmailError(None)) == "email_unreachable"
    assert jobs._error_code(ValueError()) == "unexpected_error"


async def test_the_last_attempt_fails_for_good_and_deletes_the_object(
    monkeypatch, caplog
):
    monkeypatch.setattr(
        account_export, "build", AsyncMock(side_effect=RuntimeError("bug"))
    )
    export = _export(attempts=jobs.MAX_ATTEMPTS, object_path="exports/old.json")
    storage = Storage()
    with caplog.at_level("WARNING"):
        assert not await jobs.process(
            _claim(export), storage.client(), sessions=sessions_of(_db(export))
        )
    assert (export.status, export.error_code, export.claimed_at) == (
        "failed",
        "unexpected_error",
        None,
    )
    assert storage.deleted == ["exports/old.json"]
    assert "failed for good" in caplog.text
    assert "reader@example.com" not in caplog.text


async def test_the_last_attempt_keeps_the_file_once_an_email_may_have_gone(built):
    export = _export(
        status="emailing",
        attempts=jobs.MAX_ATTEMPTS,
        object_path="exports/x.json",
        delivery_url="https://x/s?t=1",
        expires_at=NOW + timedelta(days=7),
    )
    transport, _ = resend(500)
    storage = Storage()
    await jobs.process(
        _claim(export),
        storage.client(),
        sessions=sessions_of(_db(export)),
        email_transport=transport,
    )
    assert export.status == "failed"
    assert storage.deleted == []


async def test_a_stale_attempt_never_records_its_failure(built):
    """Fencing: once another attempt holds (or finished) the row, a failure
    of this one changes nothing and deletes nothing."""
    export = _export(
        status="ready", attempts=jobs.MAX_ATTEMPTS, object_path="exports/x.json"
    )
    storage = Storage({"upload"})
    await jobs.process(
        _claim(export),
        storage.client(),
        sessions=sessions_of(_db(export, owned=False)),
    )
    assert (export.status, export.error_code) == ("ready", None)
    assert storage.deleted == []


async def test_a_failed_cleanup_of_the_object_is_logged(built, caplog):
    export = _export(attempts=jobs.MAX_ATTEMPTS, object_path="exports/x.json")
    with caplog.at_level("WARNING"):
        await jobs.process(
            _claim(export),
            Storage({"sign", "delete"}).client(),
            sessions=sessions_of(_db(export)),
        )
    assert export.status == "failed"
    assert "could not delete a failed export's object" in caplog.text


async def test_recording_a_failure_never_raises(built, caplog):
    class Broken(Session):
        async def scalar(self, stmt):
            raise OSError("db gone")

    export = _export()
    db = Broken(get={AccountExport: export, User: _user(export.user_id)})
    with caplog.at_level("ERROR"):
        ok = await jobs.process(
            _claim(export), Storage({"upload"}).client(), sessions=sessions_of(db)
        )
    assert not ok
    assert "could not record a failure" in caplog.text


# ── The worker ───────────────────────────────────────────────────────────────


@pytest.fixture
def configured():
    with patch.multiple(
        settings,
        supabase_url="https://x.supabase.co",
        supabase_service_role_key=KEY,
        resend_api_key=KEY,
    ):
        yield


async def test_process_pending_is_off_while_unconfigured():
    with patch.object(settings, "resend_api_key", ""):
        assert await jobs.process_pending(sessions=sessions_of(Session())) == 0


async def test_process_pending_works_through_the_queue(configured, monkeypatch):
    ids = [jobs.Claim(uuid.uuid4(), NOW), jobs.Claim(uuid.uuid4(), NOW)]
    claims = iter([*ids, None])
    monkeypatch.setattr(
        jobs, "claim_next", AsyncMock(side_effect=lambda db: next(claims))
    )
    processed = AsyncMock(return_value=True)
    monkeypatch.setattr(jobs, "process", processed)
    assert await jobs.process_pending(sessions=sessions_of(Session())) == 2
    assert [c.args[0] for c in processed.await_args_list] == ids
    assert isinstance(processed.await_args_list[0].args[1], ObjectStorage)


async def test_the_worker_polls_and_survives_a_failed_pass(monkeypatch, caplog):
    passes = []

    async def one_pass():
        passes.append(1)
        if len(passes) == 1:
            raise OSError("db down")
        if len(passes) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(jobs, "process_pending", one_pass)
    monkeypatch.setattr(jobs, "INITIAL_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(jobs, "POLL_SECONDS", 0.01)
    with caplog.at_level("ERROR"), pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(jobs.run_worker(), 5)
    assert len(passes) == 3
    assert "worker pass failed" in caplog.text


async def test_wake_runs_a_pass_at_once(monkeypatch):
    ran = asyncio.Event()

    async def one_pass():
        ran.set()

    monkeypatch.setattr(jobs, "process_pending", one_pass)
    monkeypatch.setattr(jobs, "INITIAL_DELAY_SECONDS", 60)
    task = asyncio.create_task(jobs.run_worker())
    try:
        await asyncio.sleep(0)  # the worker starts and waits
        jobs.wake()
        await asyncio.wait_for(ran.wait(), 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_wake_without_a_worker_does_nothing(monkeypatch):
    monkeypatch.setattr(jobs, "_wake_event", None)
    jobs.wake()


def test_wake_from_another_thread_or_loop(monkeypatch):
    loop = MagicMock()
    loop.is_closed.return_value = False
    event = MagicMock()
    monkeypatch.setattr(jobs, "_wake_loop", loop)
    monkeypatch.setattr(jobs, "_wake_event", event)
    jobs.wake()  # no running loop here
    loop.call_soon_threadsafe.assert_called_once_with(event.set)


# ── Purge and sweep ──────────────────────────────────────────────────────────


def _rows(*rows):
    result = MagicMock()
    result.all.return_value = list(rows)
    return result


async def test_purge_deletes_objects_then_rows_in_batches(monkeypatch):
    monkeypatch.setattr(jobs, "PURGE_BATCH_SIZE", 2)
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = Session(
        results=[
            _rows((a, "exports/a.json"), (b, None)),
            MagicMock(rowcount=2),
            _rows((c, "exports/c.json")),
            MagicMock(rowcount=1),
        ]
    )
    storage = Storage()
    assert await jobs.purge_expired(db, storage.client()) == 3
    assert storage.deleted == ["exports/a.json", "exports/c.json"]
    assert db.commits == 2 and db.rollbacks == 2


async def test_purge_stops_on_an_empty_batch_and_needs_no_storage_without_objects():
    db = Session(results=[_rows()])
    assert await jobs.purge_expired(db, None) == 0
    db = Session(results=[_rows((uuid.uuid4(), None)), MagicMock(rowcount=1)])
    assert await jobs.purge_expired(db, None) == 1
    where = str(db.executed[0].compile(compile_kwargs={"literal_binds": False}))
    assert "object_path IS NULL" in where


async def test_sweep_deletes_old_objects_only():
    storage = Storage()
    old = (NOW - timedelta(days=9)).isoformat()
    new = (NOW - timedelta(days=1)).isoformat()
    storage.listing = [
        [
            {"name": "a.json", "id": "1", "created_at": old},
            {"name": "b.json", "id": "2", "created_at": new},
            {"name": "c.json", "id": "3"},
        ]
    ]
    assert await jobs.sweep_orphans(storage.client()) == 1
    assert storage.deleted == ["exports/a.json"]


async def test_sweep_pages_while_every_object_on_a_page_is_old(monkeypatch):
    monkeypatch.setattr(jobs, "PURGE_BATCH_SIZE", 1)
    old = (NOW - timedelta(days=9)).isoformat()
    storage = Storage()
    storage.listing = [
        [{"name": "a.json", "id": "1", "created_at": old}],
        [{"name": "b.json", "id": "2", "created_at": old}],
        [],
    ]
    assert await jobs.sweep_orphans(storage.client()) == 2


# ── The document's loaders ───────────────────────────────────────────────────


async def test_the_catalog_reads_the_books_and_editions_the_library_uses():
    book = SimpleNamespace(**{c: f"b.{c}" for c in account_export.CATALOG_BOOK_COLUMNS})
    edition = SimpleNamespace(
        **{c: f"e.{c}" for c in account_export.CATALOG_EDITION_COLUMNS}
    )
    results = []
    for obj in (book, edition):
        result = MagicMock()
        result.scalars.return_value = [obj]
        results.append(result)
    db = Session(results=results)
    entries = [
        SimpleNamespace(book_id=uuid.uuid4(), edition_id=uuid.uuid4()),
        SimpleNamespace(book_id=None, edition_id=None),
    ]
    catalog = await account_export._catalog(db, entries)
    assert catalog["books"][0]["title"] == "b.title"
    assert catalog["editions"][0]["isbn_13"] == "e.isbn_13"


async def test_the_loaders_read_only_the_users_rows():
    uid = uuid.uuid4()
    rows = MagicMock()
    rows.scalars.return_value = ["row"]
    db = Session(results=[rows, rows], get={User: "the user"})
    assert await account_export.SECTIONS["user_books"].load(db, uid) == ["row"]
    assert await account_export.SECTIONS["scan_corrections"].load(db, uid) == ["row"]
    assert await account_export.SECTIONS["users"].load(db, uid) == ["the user"]
    assert await account_export.SECTIONS["users"].load(Session(), uid) == []
    owned, corrections = (str(s) for s in db.executed)
    assert "WHERE user_books.user_id = " in owned
    assert "JOIN scan_records" in corrections
    assert "WHERE scan_records.user_id = " in corrections
