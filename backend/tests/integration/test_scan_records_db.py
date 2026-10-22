"""Per-user scan records on real Postgres (#585): a repeat of a finished
record makes no provider call and no enrichment call and takes nothing
(allowances, lease, global count, cost), writing one zero-cost cache_hit
ledger row; a missing enrichment step is redone alone; refusals, truncated
replies and empty lists are not saved; the key is (user, fingerprint) only,
so a provider/model switch still serves the old record; records past the
retention are never served and are purged; the per-user cap evicts oldest
first; saves are idempotent; rows go with the user; and the 0011 migration
cycle.

Needs DATABASE_URL pointing at a Postgres database.
"""

import importlib.util
import io
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import delete, func, inspect, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai import breaker, entitlements, quota, scan_records
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.providers.base import ProviderResult
from app.api import scan as scan_api
from app.core.config import settings
from app.core.database import Base
from app.models.ai_enhanced_zero_result import AIEnhancedZeroResult
from app.models.ai_quota import AIInflight
from app.models.ai_usage import AIUsage
from app.models.book import Book
from app.models.scan_record import ScanRecord
from app.models.user import User
from app.models.user_book import UserBook
from app.schemas.book import EnrichedBook
from app.services.book_identifier import BookCandidate

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0011_scan_records.py"
)
DUNE = BookCandidate(
    title="Dune", author="Frank Herbert", confidence=0.9, isbn_13="9780441013593"
)


def _jpeg(color=(10, 120, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


@pytest.fixture
async def sessions():
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    created: list[uuid.UUID] = []

    async def make_user() -> uuid.UUID:
        uid = uuid.uuid4()
        async with factory() as db:
            db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
            await db.commit()
        created.append(uid)
        return uid

    yield factory, make_user

    async with factory() as db:
        await db.execute(delete(AIUsage).where(AIUsage.user_id.in_(created)))
        await db.execute(delete(User).where(User.id.in_(created)))
        await db.commit()
    await engine.dispose()


class _Provider:
    def __init__(self, books=True, outcome="ok"):
        self.books = books
        self.outcome = outcome
        self.calls = 0

    async def identify(self, _image):
        self.calls += 1
        return ProviderResult([DUNE] if self.books else [], outcome=self.outcome)

    def patch(self, cls="OpenAIVisionProvider"):
        mock = MagicMock()
        mock.return_value.identify = self.identify
        return patch(f"app.ai.gateway.{cls}", mock)


class _Downstream:
    def __init__(self):
        self.complete = True
        self.enrich_calls = 0
        # Unique per test: books.open_library_work_id is unique.
        self.work_id = f"OL{uuid.uuid4().hex[:12]}W"


@pytest.fixture
def downstream():
    """Enrichment is mocked (no Open Library / Google Books call); the
    library check is the real DeduplicationService on the session."""
    state = _Downstream()

    with (
        patch("app.api.scan.EnrichmentService") as enrich_cls,
        patch.object(settings, "turnstile_required", False),
    ):

        async def enrich(candidates, limit):
            state.enrich_calls += 1
            enrich_cls.return_value.last_complete = state.complete
            return [
                EnrichedBook(
                    title=c.title,
                    author=c.author,
                    confidence=c.confidence,
                    open_library_work_id=state.work_id,
                )
                for c in candidates
            ]

        enrich_cls.return_value.enrich = AsyncMock(side_effect=enrich)
        yield state


@pytest.fixture
def premium():
    with patch.object(entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)):
        yield


def _upload(image=_JPEG) -> StarletteUploadFile:
    return StarletteUploadFile(
        file=io.BytesIO(image),
        filename="scan.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )


async def _scan(factory, user_id, enhanced=False, image=_JPEG):
    async with factory() as db:
        user = await db.get(User, user_id)
        return await scan_api.scan.__wrapped__(
            request=MagicMock(),
            file=_upload(image),
            current_user=user,
            db=db,
            cf_turnstile_response=None,
            enhanced=enhanced,
        )


async def _usage(factory, user_id):
    async with factory() as db:
        user = await db.get(User, user_id)
        return await quota.usage_for(
            db,
            user=user,
            feature=quota.SCAN_QUOTA_FEATURE,
            ai=await ai_settings.get_ai_settings(db),
        )


async def _state(factory, user_id):
    """Everything a hit must leave unchanged."""
    today = quota.utc_today()
    async with factory() as db:
        lease = await db.get(AIInflight, user_id)
        global_units = await quota.global_units_today(db, today)
        spend = await breaker.totals(db, today)
        zero = (
            await db.execute(
                select(func.count())
                .select_from(AIEnhancedZeroResult)
                .where(AIEnhancedZeroResult.user_id == user_id)
            )
        ).scalar_one()
    return {
        "usage": await _usage(factory, user_id),
        "lease": lease,
        "global": global_units,
        "spend": spend,
        "zero_results": zero,
    }


async def _ledger(factory, user_id) -> list[AIUsage]:
    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(AIUsage)
                    .where(AIUsage.user_id == user_id)
                    .order_by(AIUsage.id)
                )
            ).scalars()
        )


async def _rows(factory, user_id) -> list[ScanRecord]:
    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(ScanRecord)
                    .where(ScanRecord.user_id == user_id)
                    .order_by(ScanRecord.created_at)
                )
            ).scalars()
        )


# ---------------------------------------------------------------------------
# Hit and miss through /scan
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Lookup branches through /scan
# ---------------------------------------------------------------------------


class TestFinishedRepeat:
    async def test_no_provider_or_enrichment_call(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch():
            first = await _scan(factory, user_id)
            second = await _scan(factory, user_id)
        assert provider.calls == 1
        assert downstream.enrich_calls == 1
        assert first.repeat is False and second.repeat is True
        assert second.books == first.books
        (row,) = await _rows(factory, user_id)
        assert row.ai_done and row.enrich_done
        assert (row.feature, row.tier) == ("scan.standard", "free")
        assert row.candidates == scan_records.serialise_candidates([DUNE])
        assert row.enrichment[0]["title"] == "Dune"

    async def test_takes_nothing_and_writes_one_zero_cost_cache_hit_row(
        self, sessions, downstream
    ):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch():
            await _scan(factory, user_id)
            before = await _state(factory, user_id)
            ledger_before = await _ledger(factory, user_id)
            await _scan(factory, user_id)
            after = await _state(factory, user_id)
        assert after == before
        assert after["usage"].used_today == 1 and after["usage"].used_month == 1
        assert after["lease"] is None
        ledger_after = await _ledger(factory, user_id)
        assert len(ledger_after) == len(ledger_before) + 1
        hit = ledger_after[-1]
        assert hit.status == "cache_hit"
        assert hit.cost_micros == 0
        assert (hit.input_tokens, hit.output_tokens) == (0, 0)
        assert hit.model == ledger_before[-1].model

    async def test_served_at_the_daily_limit(self, sessions, downstream):
        """A repeat uses no allowance, so it is served even when none is left;
        a new photo is refused as before."""
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        ai_settings.reset_cache({"quota.free.daily": 1})
        with provider.patch():
            await _scan(factory, user_id)
            resp = await _scan(factory, user_id)
            with pytest.raises(HTTPException) as exc:
                await _scan(factory, user_id, image=_jpeg((1, 2, 3)))
        assert resp.repeat is True
        assert exc.value.detail == "daily_scan_limit"
        assert provider.calls == 1

    async def test_repeats_do_not_count_against_the_global_cap(
        self, sessions, downstream
    ):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch():
            await _scan(factory, user_id)
            async with factory() as db:
                before = await quota.global_units_today(db, quota.utc_today())
            for _ in range(3):
                await _scan(factory, user_id)
            async with factory() as db:
                after = await quota.global_units_today(db, quota.utc_today())
        assert after == before

    async def test_in_library_is_checked_live(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch():
            first = await _scan(factory, user_id)
            book_id = uuid.uuid4()
            async with factory() as db:
                db.add(
                    Book(
                        id=book_id,
                        title="Dune",
                        author="Frank Herbert",
                        open_library_work_id=downstream.work_id,
                    )
                )
                await db.flush()
                db.add(UserBook(user_id=user_id, book_id=book_id, status="wishlisted"))
                await db.commit()
            try:
                second = await _scan(factory, user_id)
            finally:
                async with factory() as db:
                    await db.execute(
                        delete(UserBook).where(UserBook.book_id == book_id)
                    )
                    await db.execute(delete(Book).where(Book.id == book_id))
                    await db.commit()
        assert first.books[0].in_library is False
        assert second.repeat is True and second.books[0].in_library is True
        (row,) = await _rows(factory, user_id)
        assert "already_in_library" not in row.enrichment[0]

    async def test_enhanced_repeat_writes_no_zero_result_row(
        self, sessions, downstream, premium
    ):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        ai_settings.reset_cache({"quota.premium.enhanced_monthly": 20})
        with provider.patch():
            first = await _scan(factory, user_id, enhanced=True)
            before = await _state(factory, user_id)
            second = await _scan(factory, user_id, enhanced=True)
            after = await _state(factory, user_id)
        assert provider.calls == 1
        assert after == before
        assert second.repeat is True and second.enhanced is True
        assert second.enhanced_scans_remaining == first.enhanced_scans_remaining

    async def test_enhanced_repeat_refused_once_not_premium(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch():
            with patch.object(
                entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
            ):
                await _scan(factory, user_id, enhanced=True)
            before = [r.call_id for r in await _ledger(factory, user_id)]
            with pytest.raises(HTTPException) as exc:
                await _scan(factory, user_id, enhanced=True)
        assert (exc.value.status_code, exc.value.detail) == (
            403,
            "enhanced_requires_premium",
        )
        assert provider.calls == 1
        assert [r.call_id for r in await _ledger(factory, user_id)] == before


class TestEnrichmentMissing:
    async def test_only_enrichment_is_redone_with_no_charge(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        downstream.complete = False
        with provider.patch():
            await _scan(factory, user_id)
            (row,) = await _rows(factory, user_id)
            assert row.ai_done and not row.enrich_done and row.enrichment is None
            before = await _state(factory, user_id)
            downstream.complete = True
            resp = await _scan(factory, user_id)
            after = await _state(factory, user_id)
            await _scan(factory, user_id)
        assert provider.calls == 1
        assert downstream.enrich_calls == 2  # redone once, then served
        assert after == before
        assert resp.repeat is True
        (row,) = await _rows(factory, user_id)
        assert row.enrich_done and row.enrichment[0]["title"] == "Dune"
        statuses = [r.status for r in await _ledger(factory, user_id)]
        assert statuses == ["ok", "cache_hit", "cache_hit"]


class TestKeyIsUserAndFingerprintOnly:
    async def test_provider_and_model_switch_still_serve_the_record(
        self, sessions, downstream
    ):
        factory, make_user = sessions
        user_id = await make_user()
        openai, claude = _Provider(), _Provider()
        with openai.patch(), claude.patch("AnthropicVisionProvider"):
            await _scan(factory, user_id)
            ai_settings.reset_cache(
                {
                    "scan.standard.provider": "anthropic",
                    "scan.standard.model": "claude-haiku-5-5",
                }
            )
            resp = await _scan(factory, user_id)
        assert (openai.calls, claude.calls) == (1, 0)
        assert resp.repeat is True
        (row,) = await _rows(factory, user_id)
        assert row.model != "claude-haiku-5-5"  # the model that produced it

    async def test_another_users_record_is_never_served(self, sessions, downstream):
        factory, make_user = sessions
        alice, bob = await make_user(), await make_user()
        provider = _Provider()
        with provider.patch():
            await _scan(factory, alice)
            resp = await _scan(factory, bob)
        assert resp.repeat is False
        assert provider.calls == 2
        assert len(await _rows(factory, alice)) == 1
        assert len(await _rows(factory, bob)) == 1

    async def test_enhanced_scan_replaces_a_standard_record(
        self, sessions, downstream, premium
    ):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch():
            await _scan(factory, user_id)
            resp = await _scan(factory, user_id, enhanced=True)
            again = await _scan(factory, user_id)
        assert resp.repeat is False and again.repeat is True
        assert provider.calls == 2
        (row,) = await _rows(factory, user_id)
        assert (row.feature, row.tier) == ("scan.enhanced", "premium")
        assert row.enrich_done


class TestNotSaved:
    @pytest.mark.parametrize("outcome", ["refused", "truncated"])
    async def test_refusal_and_truncation(self, sessions, downstream, outcome):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider(outcome=outcome)
        with provider.patch():
            await _scan(factory, user_id)
            await _scan(factory, user_id)
        assert provider.calls == 2
        assert await _rows(factory, user_id) == []

    async def test_malformed(self, sessions, downstream):
        """A malformed reply (#572) is a 503 and is never saved; the
        re-upload is a normal, charged scan."""
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider(books=False, outcome="malformed")
        with provider.patch(), pytest.raises(HTTPException) as exc_info:
            await _scan(factory, user_id)
        assert (exc_info.value.status_code, exc_info.value.detail) == (
            503,
            "scan_unavailable",
        )
        assert await _rows(factory, user_id) == []
        assert (await _usage(factory, user_id)).used_today == 0
        good = _Provider()
        with good.patch():
            resp = await _scan(factory, user_id)
        assert good.calls == 1 and resp.repeat is False
        assert (await _usage(factory, user_id)).used_today == 1


class TestNoMatchSaved:
    async def test_empty_list_is_saved_and_repeated_free(self, sessions, downstream):
        """A genuine no-match (an empty ok list, #572) is saved as a finished
        record; the re-upload is a free repeat (owner decision on #573)."""
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider(books=False)
        with provider.patch():
            first = await _scan(factory, user_id)
            used = (await _usage(factory, user_id)).used_today
            resp = await _scan(factory, user_id)
        assert first.books == [] and first.repeat is False
        assert provider.calls == 1
        assert resp.repeat is True and resp.books == []
        assert (await _usage(factory, user_id)).used_today == used == 1
        (row,) = await _rows(factory, user_id)
        assert row.ai_done and row.enrich_done
        assert row.candidates == [] and row.enrichment == []

    async def test_disabled_calls_the_provider_every_time(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        ai_settings.reset_cache({"scan_records.enabled": False})
        with provider.patch():
            await _scan(factory, user_id)
            await _scan(factory, user_id)
        assert provider.calls == 2
        assert await _rows(factory, user_id) == []


class TestFailureOnRealSession:
    """A scan-record failure that aborts the request's transaction must not
    fail the scan: the session is rolled back (and the user reloaded) and the
    normal path runs."""

    @staticmethod
    async def _abort(db, **_kw):
        await db.execute(select(literal(1) / literal(0)))

    async def test_lookup_failure_falls_through(self, sessions, downstream):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch(), patch.object(scan_records, "lookup", self._abort):
            resp = await _scan(factory, user_id)
        assert [b.title for b in resp.books] == ["Dune"]
        assert provider.calls == 1
        assert (await _usage(factory, user_id)).used_today == 1

    @pytest.mark.parametrize("step", ["save_ai_step", "save_enrichment_step"])
    async def test_save_failure_still_returns(self, sessions, downstream, step):
        factory, make_user = sessions
        user_id = await make_user()
        provider = _Provider()
        with provider.patch(), patch.object(scan_records, step, self._abort):
            resp = await _scan(factory, user_id)
        assert [b.title for b in resp.books] == ["Dune"]
        assert (await _usage(factory, user_id)).used_today == 1
        rows = await _rows(factory, user_id)
        assert all(not r.enrich_done for r in rows)


# ---------------------------------------------------------------------------
# Module on real Postgres: retention, purge, cap, idempotence, cascade
# ---------------------------------------------------------------------------


def _fp(n: int) -> str:
    return f"{n:064x}"


async def _save(
    factory,
    user_id,
    n=0,
    *,
    now=None,
    max_per_user=500,
    model="m",
    feature="scan.standard",
    retention=30,
):
    async with factory() as db:
        return await scan_records.save_ai_step(
            db,
            user_id=user_id,
            fingerprint=_fp(n),
            feature=feature,
            tier="free",
            model=model,
            candidates=[DUNE],
            max_per_user=max_per_user,
            retention_days=retention,
            now=now,
        )


async def _lookup(factory, user_id, n=0, *, retention=30, now=None):
    async with factory() as db:
        return await scan_records.lookup(
            db, user_id=user_id, fingerprint=_fp(n), retention_days=retention, now=now
        )


BOOK = EnrichedBook(title="Dune", author="Frank Herbert", confidence=0.9)


class TestModule:
    async def test_record_past_retention_is_not_served_and_is_purged(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        now = scan_records.utc_now()
        await _save(factory, user_id, 1, now=now - timedelta(days=30, seconds=1))
        await _save(factory, user_id, 2, now=now - timedelta(days=29))
        assert await _lookup(factory, user_id, 1, now=now) is None
        assert (await _lookup(factory, user_id, 2, now=now)).candidates == [DUNE]
        # A shorter retention applies to reads at once.
        assert await _lookup(factory, user_id, 2, now=now, retention=7) is None
        async with factory() as db:
            purged = await scan_records.purge_old(db, 30, now=now)
        assert purged >= 1
        assert [r.fingerprint for r in await _rows(factory, user_id)] == [_fp(2)]

    async def test_purge_uses_the_retention_setting(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        now = scan_records.utc_now()
        await _save(factory, user_id, 1, now=now - timedelta(days=8))
        await _save(factory, user_id, 2, now=now - timedelta(days=6))
        ai_settings.reset_cache({"scan_records.retention_days": 7})
        async with factory() as db:
            await scan_records.purge_old_scan_records(db, now=now)
        assert [r.fingerprint for r in await _rows(factory, user_id)] == [_fp(2)]

    async def test_purge_is_bounded_per_batch(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        now = scan_records.utc_now()
        for n in range(5):
            await _save(factory, user_id, n, now=now - timedelta(days=31))
        with (
            patch.object(scan_records, "PURGE_BATCH_SIZE", 2),
            patch.object(scan_records, "PURGE_MAX_BATCHES", 2),
        ):
            async with factory() as db:
                assert await scan_records.purge_old(db, 30, now=now) == 4
        assert len(await _rows(factory, user_id)) == 1

    async def test_cap_evicts_oldest_first(self, sessions):
        factory, make_user = sessions
        user_id, other = await make_user(), await make_user()
        now = scan_records.utc_now()
        for n in range(5):
            await _save(
                factory, user_id, n, now=now + timedelta(seconds=n), max_per_user=3
            )
        # The other user's rows are NEWER than every row of this user, so an
        # eviction that ignored the user would delete this user's rows.
        for n in range(4):
            await _save(
                factory,
                other,
                n,
                now=now + timedelta(minutes=5, seconds=n),
                max_per_user=3,
            )
        await _save(
            factory, user_id, 9, now=now + timedelta(seconds=10), max_per_user=3
        )
        remaining = [r.fingerprint for r in await _rows(factory, user_id)]
        assert sorted(remaining) == sorted([_fp(3), _fp(4), _fp(9)])
        # The other user keeps exactly its own cap, untouched by this user's saves.
        assert sorted(r.fingerprint for r in await _rows(factory, other)) == sorted(
            [_fp(1), _fp(2), _fp(3)]
        )

    async def test_lookup_filters_on_the_user(self, sessions):
        factory, make_user = sessions
        owner, other = await make_user(), await make_user()
        await _save(factory, owner, 3)
        assert (await _lookup(factory, owner, 3)).candidates == [DUNE]
        assert await _lookup(factory, other, 3) is None

    async def test_ai_step_retried_after_a_lost_ack_is_one_row(self, sessions):
        """A save whose commit landed but whose acknowledgement was lost, then
        retried, leaves one row with the same content."""
        factory, make_user = sessions
        user_id = await make_user()
        now = scan_records.utc_now()
        await _save(factory, user_id, 7, now=now)
        await _save(factory, user_id, 7, now=now)
        (row,) = await _rows(factory, user_id)
        assert row.ai_done and row.created_at == now

    async def test_enrichment_step_retried_is_idempotent(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        saved_at = await _save(factory, user_id, 8)
        for _ in range(2):
            async with factory() as db:
                assert await scan_records.save_enrichment_step(
                    db,
                    user_id=user_id,
                    fingerprint=_fp(8),
                    ai_created_at=saved_at,
                    books=[BOOK],
                )
        (row,) = await _rows(factory, user_id)
        assert row.enrich_done and len(row.enrichment) == 1
        record = await _lookup(factory, user_id, 8)
        assert [b.title for b in record.books] == ["Dune"]

    async def test_enrichment_never_lands_on_a_newer_answer(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        now = scan_records.utc_now()
        old = await _save(factory, user_id, 9, now=now)
        await _save(factory, user_id, 9, now=now + timedelta(seconds=1), model="new")
        async with factory() as db:
            assert not await scan_records.save_enrichment_step(
                db, user_id=user_id, fingerprint=_fp(9), ai_created_at=old, books=[BOOK]
            )
        (row,) = await _rows(factory, user_id)
        assert row.model == "new" and not row.enrich_done

    async def test_new_answer_clears_the_enrichment_step(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        saved_at = await _save(factory, user_id, 10)
        async with factory() as db:
            await scan_records.save_enrichment_step(
                db,
                user_id=user_id,
                fingerprint=_fp(10),
                ai_created_at=saved_at,
                books=[BOOK],
            )
        await _save(factory, user_id, 10, model="new")
        (row,) = await _rows(factory, user_id)
        assert not row.enrich_done and row.enrichment is None

    async def test_one_row_per_user_and_fingerprint(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            for _ in range(2):
                db.add(
                    ScanRecord(
                        user_id=user_id,
                        fingerprint=_fp(11),
                        feature="scan.standard",
                        tier="free",
                        model="m",
                        candidates=[],
                    )
                )
            with pytest.raises(IntegrityError):
                await db.commit()

    async def test_enrich_done_needs_the_ai_step_and_a_result(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            db.add(
                ScanRecord(
                    user_id=user_id,
                    fingerprint=_fp(12),
                    feature="scan.standard",
                    tier="free",
                    model="m",
                    candidates=[],
                    enrich_done=True,
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()

    async def test_ai_step_stores_sql_null_enrichment(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        await _save(factory, user_id, 13)
        async with factory() as db:
            is_null = await db.scalar(
                select(ScanRecord.enrichment.is_(None)).where(
                    ScanRecord.user_id == user_id
                )
            )
        assert is_null is True

    async def test_enrich_done_with_null_enrichment_violates_the_check(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        async with factory() as db:
            db.add(
                ScanRecord(
                    user_id=user_id,
                    fingerprint=_fp(14),
                    feature="scan.standard",
                    tier="free",
                    model="m",
                    candidates=[],
                    ai_done=True,
                    enrich_done=True,
                    enrichment=None,
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()

    async def test_standard_never_replaces_enhanced(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        now = scan_records.utc_now()
        enhanced_at = await _save(
            factory, user_id, 15, now=now, feature="scan.enhanced", model="big"
        )
        standard_at = await _save(
            factory, user_id, 15, now=now + timedelta(seconds=1), model="small"
        )
        (row,) = await _rows(factory, user_id)
        assert row.feature == "scan.enhanced" and row.model == "big"
        assert row.created_at == enhanced_at
        # The standard scan's enrichment save does not land on the enhanced row.
        async with factory() as db:
            assert not await scan_records.save_enrichment_step(
                db,
                user_id=user_id,
                fingerprint=_fp(15),
                ai_created_at=standard_at,
                books=[BOOK],
            )
        (row,) = await _rows(factory, user_id)
        assert not row.enrich_done and row.enrichment is None

    async def test_standard_replaces_an_expired_enhanced_record(self, sessions):
        """An enhanced record past retention is not served (lookup), so the
        standard scan that ran instead must replace it; otherwise every
        repeat is charged until the purge runs (Codex, PR #571)."""
        factory, make_user = sessions
        user_id = await make_user()
        now = scan_records.utc_now()
        await _save(
            factory,
            user_id,
            17,
            now=now - timedelta(days=8),
            feature="scan.enhanced",
            model="big",
        )
        standard_at = await _save(factory, user_id, 17, now=now, retention=7)
        (row,) = await _rows(factory, user_id)
        assert row.feature == "scan.standard" and row.created_at == standard_at
        async with factory() as db:
            assert await scan_records.save_enrichment_step(
                db,
                user_id=user_id,
                fingerprint=_fp(17),
                ai_created_at=standard_at,
                books=[BOOK],
            )
        assert await _lookup(factory, user_id, 17, retention=7, now=now) is not None

    async def test_enhanced_replaces_standard_and_same_replaces_same(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        now = scan_records.utc_now()
        await _save(factory, user_id, 16, now=now, model="small")
        await _save(
            factory,
            user_id,
            16,
            now=now + timedelta(seconds=1),
            feature="scan.enhanced",
            model="big",
        )
        (row,) = await _rows(factory, user_id)
        assert row.feature == "scan.enhanced" and row.model == "big"
        await _save(
            factory,
            user_id,
            16,
            now=now + timedelta(seconds=2),
            feature="scan.enhanced",
            model="big2",
        )
        (row,) = await _rows(factory, user_id)
        assert row.model == "big2"
        await _save(factory, user_id, 17, now=now, model="a")
        await _save(factory, user_id, 17, now=now + timedelta(seconds=1), model="b")
        models = [
            r.model for r in await _rows(factory, user_id) if r.fingerprint == _fp(17)
        ]
        assert models == ["b"]

    async def test_deleting_the_user_removes_the_records(self, sessions):
        factory, make_user = sessions
        user_id = await make_user()
        await _save(factory, user_id, 1)
        await _save(factory, user_id, 2)
        async with factory() as db:
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
        assert await _rows(factory, user_id) == []


# ---------------------------------------------------------------------------
# Migration 0011
# ---------------------------------------------------------------------------


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0011", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0011", "0010")
    context = MigrationContext.configure(connection)
    table = "scan_records"

    def present() -> bool:
        return inspect(connection).has_table(table)

    def diffs() -> list:
        return [d for d in compare_metadata(context, Base.metadata) if table in repr(d)]

    with Operations.context(context):
        if present():
            migration.downgrade()
        assert not present()
        migration.upgrade()
        assert present()
        assert diffs() == []
        insp = inspect(connection)
        indexes = {i["name"]: i["column_names"] for i in insp.get_indexes(table)}
        assert indexes["ix_scan_records_user_created"] == ["user_id", "created_at"]
        assert indexes["ix_scan_records_created_at"] == ["created_at"]
        uniques = {
            u["name"]: u["column_names"] for u in insp.get_unique_constraints(table)
        }
        assert uniques["uq_scan_records_user_fingerprint"] == [
            "user_id",
            "fingerprint",
        ]
        (fk,) = insp.get_foreign_keys(table)
        assert fk["referred_table"] == "users"
        assert fk["options"].get("ondelete") == "CASCADE"
        columns = {c["name"] for c in insp.get_columns(table)}
        assert columns == {
            "id",
            "user_id",
            "fingerprint",
            "feature",
            "tier",
            "model",
            "candidates",
            "enrichment",
            "ai_done",
            "enrich_done",
            "created_at",
            "updated_at",
        }
        migration.downgrade()
        assert not present()
        migration.upgrade()
        assert diffs() == []


async def test_migration_upgrade_check_downgrade_cycle():
    """Runs 0011 in a transaction that is rolled back (Postgres DDL is
    transactional), so the shared test database is left as it was."""
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
