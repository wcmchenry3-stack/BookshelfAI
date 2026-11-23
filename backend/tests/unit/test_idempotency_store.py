"""The ``Idempotency-Key`` store (#685, ``app/services/idempotency.py``)
against a scripted session: the claim's branches (new, replay, reused,
pending, expired, the pending lease, a row released meanwhile), settling and
the batched purge. Real SQL on real Postgres is in
``tests/integration/test_idempotency_db.py``."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.idempotency_key import MAX_RESPONSE_BODY_BYTES
from app.services import idempotency as store
from app.services.idempotency import StoredResponse

USER = uuid.UUID("00000000-0000-0000-0000-000000000685")
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
HASH = "a" * 64


def _inserted(record_id):
    return SimpleNamespace(scalar_one_or_none=lambda: record_id)


def _found(row):
    return SimpleNamespace(one_or_none=lambda: row)


def _row(state="complete", request_hash=HASH, age=timedelta(minutes=5), **response):
    return SimpleNamespace(
        id=9,
        state=state,
        request_hash=request_hash,
        response_status=response.get("status", 201),
        response_body=response.get("body", '{"id":1}'),
        response_headers=response.get("headers", {"etag": '"1"'}),
        created_at=NOW - age,
    )


def _db(*results):
    return SimpleNamespace(
        execute=AsyncMock(side_effect=list(results)),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    monkeypatch.setattr(store, "utcnow", lambda: NOW)


def _sql(db, call=0) -> str:
    return str(db.execute.await_args_list[call].args[0])


async def test_a_new_key_inserts_a_pending_row_and_commits():
    db = _db(_inserted(42))
    assert await store.claim(db, USER, "k", HASH) == 42
    sql = _sql(db)
    assert "INSERT INTO idempotency_keys" in sql
    assert "ON CONFLICT ON CONSTRAINT uq_idempotency_keys_user_key DO NOTHING" in sql
    params = db.execute.await_args.args[0].compile().params
    assert params["state"] == "pending"
    assert params["created_at"] == NOW
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


async def test_a_completed_key_with_the_same_request_is_replayed():
    db = _db(_inserted(None), _found(_row()))
    stored = await store.claim(db, USER, "k", HASH)
    assert stored == StoredResponse(201, '{"id":1}', {"etag": '"1"'})
    assert "WHERE idempotency_keys.user_id" in _sql(db, 1)
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


async def test_a_stored_response_without_headers_has_none():
    db = _db(_inserted(None), _found(_row(status=204, body=None, headers=None)))
    assert await store.claim(db, USER, "k", HASH) == StoredResponse(204, None, {})


async def test_another_request_with_the_key_is_refused():
    db = _db(_inserted(None), _found(_row(request_hash="b" * 64)))
    with pytest.raises(store.KeyReusedError):
        await store.claim(db, USER, "k", HASH)
    db.commit.assert_awaited_once()


async def test_a_pending_key_is_in_progress():
    db = _db(_inserted(None), _found(_row(state="pending", age=timedelta(seconds=5))))
    with pytest.raises(store.KeyInProgressError):
        await store.claim(db, USER, "k", HASH)


@pytest.mark.parametrize(
    ("state", "age"),
    [
        ("complete", store.KEY_TTL),  # expired: never replayed
        ("complete", store.KEY_TTL + timedelta(hours=3)),
        ("pending", store.PENDING_LEASE),  # its request most likely died
        ("committed", store.KEY_TTL),  # committed, but out of the window
    ],
)
async def test_a_stale_row_is_deleted_and_the_key_claimed_afresh(state, age):
    stale = _row(state=state, request_hash="b" * 64, age=age)
    db = _db(_inserted(None), _found(stale), MagicMock(rowcount=1), _inserted(43))
    assert await store.claim(db, USER, "k", HASH) == 43
    sql = _sql(db, 2)
    assert sql.startswith("DELETE FROM idempotency_keys WHERE idempotency_keys.id")
    # The DELETE re-checks staleness, so a row committed (or claimed afresh)
    # since the read is never deleted.
    assert "idempotency_keys.state =" in sql and "created_at <=" in sql
    params = db.execute.await_args_list[2].args[0].compile().params
    assert params == {
        "id_1": 9,
        "created_at_1": NOW - store.KEY_TTL,
        "state_1": "pending",
        "created_at_2": NOW - store.PENDING_LEASE,
    }
    db.commit.assert_awaited_once()


async def test_a_takeover_that_lost_the_race_reads_the_row_again():
    """The stale pending row was committed by its request between the read
    and the DELETE: the guarded DELETE matches nothing, the next insert
    conflicts and the re-read finds it committed."""
    stale = _row(state="pending", age=store.PENDING_LEASE)
    now_committed = _row(state="committed", age=store.PENDING_LEASE)
    db = _db(
        _inserted(None),
        _found(stale),
        MagicMock(rowcount=0),
        _inserted(None),
        _found(now_committed),
    )
    with pytest.raises(store.KeyOutcomeUnknownError):
        await store.claim(db, USER, "k", HASH)


@pytest.mark.parametrize(
    "age", [timedelta(seconds=1), store.PENDING_LEASE, timedelta(hours=23)]
)
async def test_a_committed_key_is_never_taken_over_or_rerun(age):
    db = _db(_inserted(None), _found(_row(state="committed", age=age)))
    with pytest.raises(store.KeyOutcomeUnknownError):
        await store.claim(db, USER, "k", HASH)
    assert db.execute.await_count == 2  # no DELETE
    db.commit.assert_awaited_once()


async def test_a_committed_key_on_another_request_is_still_reused():
    db = _db(_inserted(None), _found(_row(state="committed", request_hash="b" * 64)))
    with pytest.raises(store.KeyReusedError):
        await store.claim(db, USER, "k", HASH)


# ── The commit marker ────────────────────────────────────────────────────────


def _session(rowcount):
    from sqlalchemy.ext.asyncio import AsyncSession

    db = AsyncSession()
    db.sync_session.execute = MagicMock(return_value=MagicMock(rowcount=rowcount))
    return db


def test_the_marker_turns_the_claim_committed_in_the_commit():
    db = _session(rowcount=1)
    disarm = store.arm_commit_marker(db, 7)
    db.sync_session.dispatch.before_commit(db.sync_session)
    stmt = db.sync_session.execute.call_args.args[0]
    assert str(stmt).startswith("UPDATE idempotency_keys SET state=")
    params = stmt.compile().params
    assert params["state"] == "committed" and params["id_1"] == 7
    assert set(params["state_1"]) == {"pending", "committed"}
    disarm()
    disarm()  # idempotent
    db.sync_session.dispatch.before_commit(db.sync_session)
    assert db.sync_session.execute.call_count == 1


def test_a_commit_whose_claim_was_taken_over_fails():
    db = _session(rowcount=0)
    store.arm_commit_marker(db, 7)
    with pytest.raises(store.KeyLostError):
        db.sync_session.dispatch.before_commit(db.sync_session)


async def test_a_row_just_under_the_window_is_still_replayed():
    db = _db(_inserted(None), _found(_row(age=store.KEY_TTL - timedelta(seconds=1))))
    assert isinstance(await store.claim(db, USER, "k", HASH), StoredResponse)


async def test_a_row_released_meanwhile_is_claimed_on_the_next_insert():
    db = _db(_inserted(None), _found(None), _inserted(44))
    assert await store.claim(db, USER, "k", HASH) == 44


async def test_a_key_that_keeps_changing_hands_is_in_progress():
    db = _db(*[_inserted(None), _found(None)] * 3)
    with pytest.raises(store.KeyInProgressError):
        await store.claim(db, USER, "k", HASH)
    db.commit.assert_awaited_once()


async def test_complete_stores_the_response_on_the_pending_row():
    db = _db(MagicMock())
    await store.complete(db, 7, StoredResponse(200, "{}", {"etag": '"2"'}))
    stmt = db.execute.await_args.args[0]
    assert str(stmt).startswith("UPDATE idempotency_keys SET state=")
    params = stmt.compile().params
    assert params["state"] == "complete"
    assert params["response_status"] == 200
    assert params["response_headers"] == {"etag": '"2"'}
    assert set(params["state_1"]) == {"pending", "committed"}
    db.commit.assert_awaited_once()


async def test_complete_without_headers_stores_null():
    db = _db(MagicMock())
    await store.complete(db, 7, StoredResponse(204, None))
    assert db.execute.await_args.args[0].compile().params["response_headers"] is None


async def test_release_deletes_only_the_pending_row():
    db = _db(MagicMock())
    await store.release(db, 7)
    stmt = db.execute.await_args.args[0]
    assert str(stmt).startswith("DELETE FROM idempotency_keys")
    assert stmt.compile().params == {"id_1": 7, "state_1": "pending"}
    db.commit.assert_awaited_once()


def _ids(n):
    return SimpleNamespace(scalars=lambda: list(range(n)))


async def test_purge_deletes_in_batches_until_a_short_one(monkeypatch):
    monkeypatch.setattr(store, "PURGE_BATCH_SIZE", 2)
    db = _db(_ids(2), MagicMock(rowcount=2), _ids(1), MagicMock(rowcount=1))
    assert await store.purge_expired(db, NOW) == 3
    assert db.commit.await_count == 2
    select_sql = _sql(db, 0)
    assert "idempotency_keys.created_at <" in select_sql and "LIMIT" in select_sql
    cutoff = db.execute.await_args_list[1].args[0].compile().params["created_at_1"]
    assert cutoff == NOW - timedelta(hours=24)


async def test_purge_with_nothing_old_deletes_nothing():
    db = _db(_ids(0))
    assert await store.purge_expired(db) == 0
    db.commit.assert_awaited_once()


async def test_purge_stops_after_max_batches(monkeypatch):
    monkeypatch.setattr(store, "PURGE_BATCH_SIZE", 1)
    monkeypatch.setattr(store, "PURGE_MAX_BATCHES", 2)
    db = _db(_ids(1), MagicMock(rowcount=None), _ids(1), MagicMock(rowcount=1))
    assert await store.purge_expired(db, NOW) == 1
    assert db.execute.await_count == 4


def test_the_window_lease_and_batches_are_the_documented_ones():
    assert store.KEY_TTL == timedelta(hours=24)
    assert store.PENDING_LEASE == timedelta(minutes=2)
    assert store.PURGE_BATCH_SIZE == 1_000
    assert store.PURGE_MAX_BATCHES == 200
    assert MAX_RESPONSE_BODY_BYTES == 65_536
    assert store.REPLAYED_HEADERS == ("etag", "location")


def test_the_clock_is_aware_utc(monkeypatch):
    monkeypatch.undo()  # the real clock, not the frozen one
    now = store.utcnow()
    assert now.tzinfo is UTC
    assert abs(now - datetime.now(UTC)) < timedelta(seconds=5)
