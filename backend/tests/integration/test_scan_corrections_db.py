"""Scan correction flags (#631) on real Postgres.

Through the real endpoints and services: ``/scan`` hands back the scan
record's id and each book's candidate index; an "Add it yourself" save that
names them writes one ``scan_corrections`` row with the record's feature,
tier and model and counts it into ``scan_corrections_daily`` in the same
transaction; a second save of the same candidate adds nothing; another
user's record, an expired one or an index out of range record nothing and
answer the same; the rows cascade with the scan record while the daily
totals stay; the ledger's daily run deletes old raw and daily rows only,
idempotently; and migration 0017 down and up.

Needs DATABASE_URL pointing at a Postgres database. These tests commit, so
each uses its own users (deleted afterwards, which cascades their records
and corrections) and its own feature names (daily rows deleted afterwards).
"""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ai import ledger, scan_corrections, scan_records
from app.api import user_books as user_books_api
from app.core.config import settings
from app.core.database import Base
from app.models.scan_correction import ScanCorrection, ScanCorrectionDaily
from app.models.scan_record import ScanRecord
from app.models.user import User
from app.schemas.book import EnrichedBook
from app.schemas.user_book import UnverifiedBookCreate, UserBookRead
from app.services.book_identifier import BookCandidate
from app.services.enrichment import EnrichmentService

# The /scan harness of the scan-record tests (#585): a fake provider.
from tests.integration.test_scan_records_db import _Provider, _scan

pytestmark = pytest.mark.integration

ISBN_13 = "9780306406157"
READING = [
    BookCandidate(title="Pond Notes", author="A. Local", confidence=0.7),
    BookCandidate(
        title="Second Book", author="B. Writer", confidence=0.6, isbn_13=ISBN_13
    ),
]


@pytest.fixture
async def env():
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    users: list[uuid.UUID] = []
    features: list[str] = []

    async def make_user() -> User:
        uid = uuid.uuid4()
        async with factory() as db:
            db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
            await db.commit()
        users.append(uid)
        return User(id=uid, email=f"{uid}@example.com")

    async def make_record(user, *, age=timedelta(0), candidates=READING) -> ScanRecord:
        feature = f"test.{uuid.uuid4().hex[:12]}"
        features.append(feature)
        record = ScanRecord(
            id=uuid.uuid4(),
            user_id=user.id,
            fingerprint=uuid.uuid4().hex * 2,
            feature=feature,
            tier="premium",
            model="vision-model-x",
            candidates=scan_records.serialise_candidates(candidates),
            ai_done=True,
            created_at=datetime.now(UTC) - age,
        )
        async with factory() as db:
            db.add(record)
            await db.commit()
        return record

    yield SimpleNamespace(
        factory=factory,
        make_user=make_user,
        make_record=make_record,
        features=features,
    )

    async with factory() as db:
        await db.execute(
            delete(ScanCorrectionDaily).where(ScanCorrectionDaily.feature.in_(features))
        )
        await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()
    await engine.dispose()


async def _save(env, user, ref=None, **fields):
    body = {"title": "Pond Notes", "author": "A. Local", **fields}
    if ref is not None:
        body["scan_reference"] = ref
    async with env.factory() as db:
        return await user_books_api.add_unverified_to_wishlist.__wrapped__(
            request=SimpleNamespace(),
            req=UnverifiedBookCreate(**body),
            current_user=user,
            db=db,
        )


@pytest.fixture
def no_catalog_match():
    """The real enrichment loop with each lookup replaced by "no provider
    found it" (no Open Library / Google Books call): the book is the
    reading, with no IDs and no ISBN."""

    async def one(_self, candidate):
        return EnrichedBook(
            title=candidate.title, author=candidate.author, confidence=0.5
        )

    with (
        patch.object(EnrichmentService, "_enrich_one", one),
        patch.object(settings, "turnstile_required", False),
    ):
        yield


def _ref(record, index=0):
    return {"scan_record_id": str(record.id), "candidate_index": index}


async def _corrections(env, record_id=None):
    async with env.factory() as db:
        query = select(ScanCorrection)
        if record_id is not None:
            query = query.where(ScanCorrection.scan_record_id == record_id)
        return list((await db.execute(query)).scalars())


async def _daily(env, feature):
    async with env.factory() as db:
        return list(
            (
                await db.execute(
                    select(ScanCorrectionDaily).where(
                        ScanCorrectionDaily.feature == feature
                    )
                )
            ).scalars()
        )


def _shape(read: UserBookRead) -> dict:
    data = read.model_dump(mode="json")
    for key in ("id", "created_at", "updated_at", "wishlisted_at"):
        data.pop(key)
    data["book"] = {
        k: v
        for k, v in data["book"].items()
        if k not in {"id", "created_at", "updated_at"}
    }
    return data


# ── /scan hands back the reference ───────────────────────────────────────────


async def test_scan_response_carries_the_record_id_and_candidate_indexes(
    env, no_catalog_match
):
    user = await env.make_user()
    provider = _Provider()
    with provider.patch():
        first = await _scan(env.factory, user.id)
        repeat = await _scan(env.factory, user.id)

    async with env.factory() as db:
        (stored,) = (
            await db.execute(select(ScanRecord).where(ScanRecord.user_id == user.id))
        ).scalars()
    assert first.scan_record_id == stored.id
    assert [b.candidate_index for b in first.books] == [0]
    # A repeat served from the record names the same record.
    assert repeat.repeat is True
    assert repeat.scan_record_id == stored.id
    assert [b.candidate_index for b in repeat.books] == [0]


async def test_scan_reference_round_trip(env, no_catalog_match):
    """The reference /scan returned records a correction against the
    record it names, with that record's feature, tier and model."""
    user = await env.make_user()
    with _Provider().patch():
        scanned = await _scan(env.factory, user.id)
    book = scanned.books[0]
    ref = {
        "scan_record_id": str(scanned.scan_record_id),
        "candidate_index": book.candidate_index,
    }
    await _save(env, user, ref, title=book.title, author="Someone Else")

    (row,) = await _corrections(env, scanned.scan_record_id)
    async with env.factory() as db:
        record = await db.get(ScanRecord, scanned.scan_record_id)
    env.features.append(record.feature)
    assert (row.feature, row.tier, row.model) == (
        record.feature,
        record.tier,
        record.model,
    )
    assert (row.title_edited, row.author_edited, row.isbn_edited) == (
        False,
        True,
        False,
    )


# ── Saves with a reference ───────────────────────────────────────────────────


async def test_save_with_a_reference_writes_one_row_and_counts_it(env):
    user = await env.make_user()
    record = await env.make_record(user)

    saved = await _save(env, user, _ref(record), title="Pond Notes, Vol. 2")

    assert saved.unverified is True and saved.book.title == "Pond Notes, Vol. 2"
    (row,) = await _corrections(env, record.id)
    assert row.candidate_index == 0
    assert (row.feature, row.tier, row.model) == (
        record.feature,
        "premium",
        "vision-model-x",
    )
    assert (row.title_edited, row.author_edited, row.isbn_edited) == (
        True,
        False,
        False,
    )
    (daily,) = await _daily(env, record.feature)
    assert daily.day == datetime.now(UTC).date()
    assert daily.model == "vision-model-x"
    assert (
        daily.prefilled_saves,
        daily.title_edited,
        daily.author_edited,
        daily.isbn_edited,
        daily.any_edited,
    ) == (1, 1, 0, 0, 1)


async def test_second_save_for_the_same_candidate_adds_nothing(env):
    user = await env.make_user()
    record = await env.make_record(user)

    await _save(env, user, _ref(record))
    # A second entry (another title, so not a duplicate) naming the same
    # candidate: saved, but not counted again.
    second = await _save(env, user, _ref(record), title="Other", isbn=ISBN_13)

    assert second.book.title == "Other"
    (row,) = await _corrections(env, record.id)
    assert row.title_edited is False and row.isbn_edited is False
    (daily,) = await _daily(env, record.feature)
    assert (daily.prefilled_saves, daily.any_edited) == (1, 0)


async def test_rollup_aggregates_per_day_feature_and_model(env):
    user, other_user = await env.make_user(), await env.make_user()
    record = await env.make_record(user)
    other = await env.make_record(other_user)

    await _save(env, user, _ref(record, 0))  # untouched
    await _save(
        env, user, _ref(record, 1), title="Second Book", author="B. Writer", isbn=None
    )  # the reading's ISBN was never shown, so no ISBN is not an edit
    await _save(env, other_user, _ref(other, 1), title="Changed", author="Changed")
    await _save(
        env,
        other_user,
        _ref(other, 0),
        title="pond notes!",
        author="A LOCAL",
        isbn="978-0-306-40615-7",
    )

    (first,) = await _daily(env, record.feature)
    assert (
        first.prefilled_saves,
        first.title_edited,
        first.author_edited,
        first.isbn_edited,
        first.any_edited,
    ) == (2, 0, 0, 0, 0)
    (second,) = await _daily(env, other.feature)
    assert (
        second.prefilled_saves,
        second.title_edited,
        second.author_edited,
        second.isbn_edited,
        second.any_edited,
    ) == (2, 1, 1, 1, 2)
    assert "user_id" not in {c["name"] for c in await _columns(env)}


async def _columns(env):
    async with env.factory() as db:
        conn = await db.connection()
        return await conn.run_sync(
            lambda c: inspect(c).get_columns("scan_corrections_daily")
        )


# ── References that name nothing usable ──────────────────────────────────────


async def test_unusable_references_record_nothing_and_answer_the_same(env):
    alice, bob = await env.make_user(), await env.make_user()
    bobs = await env.make_record(bob)
    expired = await env.make_record(alice, age=timedelta(days=31))
    own = await env.make_record(alice)

    plain = await _save(env, alice, title="Plain")
    answers = [
        await _save(env, alice, _ref(bobs), title="Foreign"),
        await _save(env, alice, _ref(expired), title="Expired"),
        await _save(env, alice, _ref(own, 2), title="Out of range"),
        await _save(
            env,
            alice,
            {"scan_record_id": str(uuid.uuid4()), "candidate_index": 0},
            title="Unknown",
        ),
    ]

    for answer in answers:
        expected = {**_shape(plain), "book": {**_shape(plain)["book"]}}
        expected["book"]["title"] = answer.book.title
        assert _shape(answer) == expected
    for record in (bobs, expired, own):
        assert await _corrections(env, record.id) == []
        assert await _daily(env, record.feature) == []


async def test_a_failed_record_never_fails_the_save(env, monkeypatch):
    user = await env.make_user()
    record = await env.make_record(user)

    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(scan_corrections.scan_records, "lookup_by_id", boom)
    saved = await _save(env, user, _ref(record))

    assert saved.unverified is True
    assert await _corrections(env, record.id) == []


# ── Lifetime ─────────────────────────────────────────────────────────────────


async def test_rows_go_with_the_scan_record_and_the_totals_stay(env):
    user = await env.make_user()
    record = await env.make_record(user)
    await _save(env, user, _ref(record), author="Changed")

    async with env.factory() as db:
        await scan_records.purge_old(db, retention_days=0)

    assert await _corrections(env, record.id) == []
    (daily,) = await _daily(env, record.feature)
    assert (daily.prefilled_saves, daily.author_edited) == (1, 1)


async def test_daily_run_purges_old_rows_only_and_is_idempotent(env):
    user = await env.make_user()
    record = await env.make_record(user)
    await _save(env, user, _ref(record, 0), title="Changed")
    await _save(env, user, _ref(record, 1), title="Second Book", author="B. Writer")
    old_feature = f"test.{uuid.uuid4().hex[:12]}"
    env.features.append(old_feature)
    now = datetime.now(UTC)
    async with env.factory() as db:
        # One raw row older than 30 days, one daily row older than 400.
        await db.execute(
            ScanCorrection.__table__.update()
            .where(
                ScanCorrection.scan_record_id == record.id,
                ScanCorrection.candidate_index == 0,
            )
            .values(created_at=now - timedelta(days=31))
        )
        db.add(
            ScanCorrectionDaily(
                day=now.date() - timedelta(days=401),
                feature=old_feature,
                model="m",
                prefilled_saves=3,
                title_edited=1,
                author_edited=0,
                isbn_edited=0,
                any_edited=1,
            )
        )
        await db.commit()
    before = await _daily(env, record.feature)

    for _ in range(2):
        async with env.factory() as db:
            await ledger.rollup_and_purge(db, now)
        (young,) = await _corrections(env, record.id)
        assert young.candidate_index == 1
        assert await _daily(env, old_feature) == []
        # The totals are untouched: each row was counted when written.
        after = await _daily(env, record.feature)
        assert [(d.day, d.prefilled_saves, d.title_edited) for d in after] == [
            (d.day, d.prefilled_saves, d.title_edited) for d in before
        ]
        assert [(d.prefilled_saves, d.title_edited) for d in after] == [(2, 1)]


# ── Migration 0017 ───────────────────────────────────────────────────────────

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0017_scan_corrections.py"
)
NEW_TABLES = ("scan_corrections", "scan_corrections_daily")


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0017", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0017", "0016")
    context = MigrationContext.configure(connection)
    before = set(inspect(connection).get_table_names())

    def diffs() -> list:
        return [
            d
            for d in compare_metadata(context, Base.metadata)
            if any(t in repr(d) for t in NEW_TABLES)
        ]

    with Operations.context(context):
        migration.downgrade()
        after_down = set(inspect(connection).get_table_names())
        # Only the new tables go.
        assert before - after_down == set(NEW_TABLES)
        migration.upgrade()
        assert set(inspect(connection).get_table_names()) == before
        assert diffs() == []
        insp = inspect(connection)
        (fk,) = insp.get_foreign_keys("scan_corrections")
        assert fk["referred_table"] == "scan_records"
        assert fk["options"].get("ondelete") == "CASCADE"
        uniques = {
            u["name"]: u["column_names"]
            for u in insp.get_unique_constraints("scan_corrections")
        }
        assert uniques["uq_scan_corrections_record_candidate"] == [
            "scan_record_id",
            "candidate_index",
        ]
        migration.downgrade()
        migration.upgrade()
        assert diffs() == []


async def test_migration_0017_downgrade_and_upgrade():
    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.run_sync(_migration_cycle)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()
