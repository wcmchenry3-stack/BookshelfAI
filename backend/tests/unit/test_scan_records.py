"""Unit tests for per-user scan records (#585): the fingerprint and the
validated serialisation in app/ai/scan_records.py, the settings, the
enrichment completeness flag, the cleanup wiring, the response fields, and
the /scan wiring (each lookup branch, what is saved and when, what a repeat
skips, and that a scan-record failure never fails a scan). Real Postgres
behaviour is in tests/integration/test_scan_records_db.py."""

import asyncio
import hashlib
import io
import logging
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.ai import entitlements, scan_records
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.providers.base import ProviderResult, ProviderTimeoutError
from app.ai.registry import FEATURES
from app.main import app
from app.models.scan_record import ScanRecord
from app.models.user import User
from app.schemas.book import EditionPreview, EnrichedBook, ScanResponse
from app.services.book_identifier import BookCandidate, ScanUnavailableError
from app.services.enrichment import EnrichmentService
from tests.unit._quota_fake import FakeQuotaStore
from tests.unit._scan_records_fake import FakeScanRecords

USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-00000000585a"),
    email="records@example.com",
)
DUNE = BookCandidate(
    title="Dune",
    author="Frank Herbert",
    confidence=0.9,
    isbn_13="9780441013593",
    isbn_10=None,
)
DUNE_BOOK = EnrichedBook(
    open_library_work_id="OL893415W",
    google_books_id="gb_dune",
    title="Dune",
    author="Frank Herbert",
    description="Arrakis.",
    cover_url="https://books.example/dune.jpg",
    subjects=["Science fiction"],
    confidence=0.9,
    editions=[EditionPreview(isbn_13="9780441013593", format="unknown")],
)


def _jpeg(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


# ---------------------------------------------------------------------------
# Fingerprint and serialisation
# ---------------------------------------------------------------------------


def test_fingerprint_is_sha256_of_the_bytes():
    assert scan_records.fingerprint(b"abc") == hashlib.sha256(b"abc").hexdigest()
    assert len(scan_records.fingerprint(_JPEG)) == 64
    assert scan_records.fingerprint(_JPEG) != scan_records.fingerprint(_jpeg((1,)))


class TestCandidates:
    def test_only_candidate_fields(self):
        (item,) = scan_records.serialise_candidates([DUNE])
        assert set(item) == {"title", "author", "confidence", "isbn_13", "isbn_10"}
        assert scan_records.deserialise_candidates([item]) == [DUNE]

    @pytest.mark.parametrize(
        "stored",
        [
            None,
            {"title": "x"},
            ["x"],
            [{"title": "x", "author": "y", "confidence": 2.0}],
            [{"title": "x", "author": "y", "confidence": True}],
            [{"title": "x", "author": "y", "confidence": float("nan")}],
            [{"title": "x" * 501, "author": "y", "confidence": 0.5}],
            [{"title": "x", "author": None, "confidence": 0.5}],
            [{"title": "x", "author": "y", "confidence": 0.5, "raw": "text"}],
        ],
    )
    def test_invalid_rows_are_rejected(self, stored):
        with pytest.raises(scan_records.ScanRecordError):
            scan_records.deserialise_candidates(stored)

    def test_cap_is_scan_max_books(self, monkeypatch):
        # The settings object scan_records reads (another test may reload
        # app.core.config).
        monkeypatch.setattr(scan_records.settings, "scan_max_books", 2)
        with pytest.raises(scan_records.ScanRecordError):
            scan_records.serialise_candidates([DUNE] * 3)


class TestBooks:
    def test_round_trip_without_the_per_user_fields(self):
        book = DUNE_BOOK.model_copy(
            update={"already_in_library": True, "book_id": uuid.uuid4()}
        )
        (item,) = scan_records.serialise_books([book])
        assert "already_in_library" not in item
        assert "in_library" not in item
        assert "book_id" not in item
        (back,) = scan_records.deserialise_books([item])
        assert back.already_in_library is False and back.book_id is None
        assert back.model_dump(exclude={"already_in_library", "book_id"}) == (
            DUNE_BOOK.model_dump(exclude={"already_in_library", "book_id"})
        )

    @pytest.mark.parametrize(
        "stored",
        [
            None,
            "x",
            [1],
            [{"title": "x", "author": "y"}],  # no confidence
            [{"title": "x", "author": "y", "confidence": 0.5, "extra": 1}],
            [{"title": "x", "author": "y", "confidence": 0.5, "book_id": None}],
            [{"title": "x" * 501, "author": "y", "confidence": 0.5}],
        ],
    )
    def test_invalid_rows_are_rejected(self, stored):
        with pytest.raises(scan_records.ScanRecordError):
            scan_records.deserialise_books(stored)


def _row(**changes) -> ScanRecord:
    values = {
        "user_id": USER.id,
        "fingerprint": "a" * 64,
        "feature": "scan.standard",
        "tier": "free",
        "model": "gpt-4o-mini",
        "candidates": scan_records.serialise_candidates([DUNE]),
        "enrichment": scan_records.serialise_books([DUNE_BOOK]),
        "ai_done": True,
        "enrich_done": True,
        "created_at": datetime.now(UTC),
    }
    values.update(changes)
    return ScanRecord(**values)


def _db_returning(row):
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result)
    return db


class TestLookupValidation:
    async def _lookup(self, row):
        return await scan_records.lookup(
            _db_returning(row), user_id=USER.id, fingerprint="a" * 64, retention_days=30
        )

    async def test_finished_row(self):
        record = await self._lookup(_row())
        assert record.candidates == [DUNE]
        assert [b.title for b in record.books] == ["Dune"]
        assert record.enrich_done is True

    async def test_anything_but_a_row_is_no_record(self):
        assert await self._lookup(MagicMock()) is None
        assert await self._lookup(None) is None

    async def test_invalid_candidates_is_no_record(self):
        assert await self._lookup(_row(candidates=[{"title": 1}])) is None

    async def test_invalid_enrichment_is_a_missing_step(self):
        record = await self._lookup(_row(enrichment=[{"bad": 1}]))
        assert record is not None and record.books is None

    async def test_enrichment_ignored_unless_marked_done(self):
        record = await self._lookup(_row(enrich_done=False))
        assert record.books is None

    async def test_query_filters_on_user_fingerprint_ai_done_and_age(self):
        db = _db_returning(None)
        now = datetime(2026, 10, 22, tzinfo=UTC)
        await scan_records.lookup(
            db, user_id=USER.id, fingerprint="b" * 64, retention_days=7, now=now
        )
        query = db.execute.call_args[0][0]
        params = query.compile().params
        assert USER.id in params.values()
        assert "b" * 64 in params.values()
        assert now - timedelta(days=7) in params.values()
        assert "ai_done" in str(query)
        db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_defaults(self):
        ai = ai_settings.AISettings({})
        assert ai.scan_records_enabled is True
        assert ai.scan_records_retention_days == 30
        assert ai.scan_records_max_per_user == 500

    def test_no_repeat_cap_setting(self):
        """#585: repeats are free and uncapped (decision #573 point 2)."""
        assert not any(k.startswith("cache.") for k in ai_settings.DEFINITIONS)
        assert not hasattr(ai_settings.AISettings, "cache_max_hits_per_day")

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
    def test_invalid_enabled_fails_closed(self, value):
        ai_settings.reset_cache({"scan_records.enabled": value})
        rows = ai_settings._cache.rows
        assert ai_settings.AISettings(rows).scan_records_enabled is False

    @pytest.mark.parametrize(
        "key,value",
        [
            ("scan_records.retention_days", 0),
            ("scan_records.retention_days", 31),
            ("scan_records.max_per_user", 0),
            ("scan_records.max_per_user", 10_001),
        ],
    )
    def test_out_of_bounds_ignored_on_read(self, key, value):
        ai_settings.reset_cache({key: value})
        ai = ai_settings.AISettings(ai_settings._cache.rows)
        assert ai.get(key) == ai_settings.DEFINITIONS[key].default()


# ---------------------------------------------------------------------------
# Enrichment completeness (the enrich_done step)
# ---------------------------------------------------------------------------


class TestEnrichmentCompleteness:
    @pytest.fixture
    def service(self):
        svc = EnrichmentService()
        svc.ol = MagicMock()
        svc.gb = MagicMock()
        svc.ol.search_by_isbn = AsyncMock(return_value={"key": "/works/OL1W"})
        svc.ol.search = AsyncMock(return_value=None)
        svc.ol.extract_work_id = MagicMock(return_value="OL1W")
        svc.gb.search_by_isbn = AsyncMock(return_value=None)
        svc.gb.search = AsyncMock(return_value=None)
        return svc

    async def test_complete_when_every_lookup_finished(self, service):
        assert service.last_complete is None
        books = await service.enrich([DUNE], limit=15)
        assert len(books) == 1
        # A lookup that found nothing (None) still finished.
        assert service.last_complete is True

    @pytest.mark.parametrize("source", ["ol", "gb"])
    async def test_incomplete_when_a_lookup_failed(self, service, source):
        getattr(service, source).search_by_isbn = AsyncMock(
            side_effect=ConnectionError("down")
        )
        books = await service.enrich([DUNE], limit=15)
        assert len(books) == 1  # still returned, as before
        assert service.last_complete is False

    async def test_incomplete_when_a_candidate_failed(self, service):
        with patch.object(service, "_enrich_one", side_effect=RuntimeError("x")):
            assert await service.enrich([DUNE], limit=15) == []
        assert service.last_complete is False

    async def test_concurrent_calls_do_not_share_failures(self, service):
        async def search(isbn):
            await asyncio.sleep(0)
            if isbn == "bad":
                raise ConnectionError("down")

        service.gb.search_by_isbn = AsyncMock(side_effect=search)
        good = EnrichmentService()
        good.ol, good.gb = service.ol, service.gb
        bad_candidate = BookCandidate("X", "Y", 0.5, isbn_13="bad")
        await asyncio.gather(
            service.enrich([bad_candidate], limit=15), good.enrich([DUNE], limit=15)
        )
        assert service.last_complete is False
        assert good.last_complete is True


# ---------------------------------------------------------------------------
# Response fields
# ---------------------------------------------------------------------------


class TestResponseFields:
    def test_repeat_defaults_false(self):
        assert ScanResponse().model_dump()["repeat"] is False

    @pytest.mark.parametrize("flag", [True, False])
    def test_in_library_mirrors_the_library_check(self, flag):
        book = DUNE_BOOK.model_copy(update={"already_in_library": flag})
        dumped = book.model_dump(mode="json")
        assert dumped["in_library"] is flag
        assert dumped["already_in_library"] is flag  # kept: backward compatible


# ---------------------------------------------------------------------------
# Daily cleanup
# ---------------------------------------------------------------------------


def _cleanup_db():
    result = MagicMock(rowcount=0)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session), session


async def _one_cleanup_run(factory, purge):
    from app.services.token_cleanup import cleanup_expired_tokens

    with (
        patch("app.services.token_cleanup.AsyncSessionLocal", factory),
        patch("app.services.token_cleanup.purge_old_scan_usage", AsyncMock()),
        patch("app.services.token_cleanup.purge_old_quota", AsyncMock()),
        patch(
            "app.services.token_cleanup.rollup_and_purge",
            AsyncMock(return_value=(0, 0)),
        ),
        patch("app.services.token_cleanup.purge_old_spend", AsyncMock()),
        patch("app.services.token_cleanup.purge_old_zero_results", AsyncMock()),
        patch("app.services.token_cleanup.purge_old_scan_records", purge),
        patch("app.services.token_cleanup._INITIAL_DELAY_SECONDS", 0),
        patch("app.services.token_cleanup._INTERVAL_SECONDS", 9999),
        pytest.raises(asyncio.CancelledError),
    ):
        task = asyncio.create_task(cleanup_expired_tokens())
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        await task


class TestCleanup:
    async def test_run_purges_old_records(self):
        factory, _ = _cleanup_db()
        purge = AsyncMock(return_value=2)
        await _one_cleanup_run(factory, purge)
        purge.assert_awaited_once()

    async def test_purge_failure_does_not_skip_token_cleanup(self, caplog):
        factory, session = _cleanup_db()
        await _one_cleanup_run(factory, AsyncMock(side_effect=ConnectionError("x")))
        assert "scan records" in caplog.text
        assert "refresh_tokens" in str(session.execute.call_args[0][0])

    async def test_purge_uses_the_retention_setting(self):
        ai = ai_settings.AISettings({"scan_records.retention_days": 7})
        purge = AsyncMock(return_value=0)
        with (
            patch.object(ai_settings, "get_ai_settings", AsyncMock(return_value=ai)),
            patch.object(scan_records, "purge_old", purge),
        ):
            await scan_records.purge_old_scan_records(AsyncMock())
        assert purge.await_args.args[1] == 7

    async def test_purge_falls_back_to_the_upper_bound(self):
        purge = AsyncMock(return_value=0)
        with (
            patch.object(
                ai_settings,
                "get_ai_settings",
                AsyncMock(side_effect=ai_settings.AISettingsUnavailableError("x")),
            ),
            patch.object(scan_records, "purge_old", purge),
        ):
            await scan_records.purge_old_scan_records(AsyncMock())
        assert purge.await_args.args[1] == 30

    async def test_purge_deletes_in_bounded_batches(self):
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[MagicMock(rowcount=2), MagicMock(rowcount=2)]
        )
        with (
            patch.object(scan_records, "PURGE_BATCH_SIZE", 2),
            patch.object(scan_records, "PURGE_MAX_BATCHES", 2),
        ):
            assert await scan_records.purge_old(db, 30) == 4
        assert db.execute.await_count == 2


# ---------------------------------------------------------------------------
# /scan wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def store():
    s = FakeQuotaStore(default_user=USER.id)
    with s.installed():
        yield s


@pytest.fixture
def records():
    r = FakeScanRecords()
    with r.installed():
        yield r


@pytest.fixture
def client():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    async def _fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def ledger_rows():
    rows: list[dict] = []

    async def record(_db, **fields):
        rows.append(fields)

    with patch("app.api.scan.ledger.record", record):
        yield rows


class _Downstream:
    """The provider (gateway boundary), enrichment and the library check."""

    def __init__(self, cls, enrich_cls, dedup_cls):
        self.cls = cls
        self.enrich_cls = enrich_cls
        self.dedup_cls = dedup_cls
        self.complete = True
        self.in_library: set[str] = set()

    @property
    def identify(self) -> AsyncMock:
        return self.cls.return_value.identify

    @property
    def enrich(self) -> AsyncMock:
        return self.enrich_cls.return_value.enrich


@pytest.fixture
def provider():
    with (
        patch("app.ai.gateway.OpenAIVisionProvider") as cls,
        patch("app.api.scan.EnrichmentService") as enrich_cls,
        patch("app.api.scan.DeduplicationService") as dedup_cls,
    ):
        down = _Downstream(cls, enrich_cls, dedup_cls)

        async def _enrich(candidates, limit):
            enrich_cls.return_value.last_complete = down.complete
            return [DUNE_BOOK.model_copy(update={"title": c.title}) for c in candidates]

        async def _check(_db, _user_id, books):
            for b in books:
                b.already_in_library = b.title in down.in_library
            return books

        cls.return_value.identify = AsyncMock(return_value=ProviderResult([DUNE]))
        enrich_cls.return_value.enrich = AsyncMock(side_effect=_enrich)
        dedup_cls.return_value.check = AsyncMock(side_effect=_check)
        yield down


def _post(client, image=_JPEG, enhanced=False, **extra):
    data = {"enhanced": "true"} if enhanced else {}
    data.update(extra)
    return client.post(
        "/scan",
        files={"file": ("scan.jpg", io.BytesIO(image), "image/jpeg")},
        data=data,
    )


def _snapshot(store: FakeQuotaStore):
    """Everything a repeat must leave unchanged."""
    return (
        dict(store.rows),
        dict(store.leases),
        {d: list(v) for d, v in store.spend.items()},
        {u: list(v) for u, v in store.zero_results.items()},
        store.releases,
    )


@pytest.fixture
def premium(monkeypatch):
    monkeypatch.setattr(entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM))


class TestNewPhoto:
    def test_saves_the_ai_step_then_the_enrichment_step(
        self, client, store, records, provider
    ):
        resp = _post(client)
        assert resp.status_code == 200
        assert resp.json()["repeat"] is False
        assert records.calls == ["lookup", "save_ai_step", "save_enrichment_step"]
        row = records.only()
        assert row["ai_done"] and row["enrich_done"]
        assert row["feature"] == "scan.standard"
        assert row["model"] == FEATURES["scan.standard"].model
        assert row["tier"] == "free"
        assert row["max_per_user"] == 500
        assert row["candidates"] == scan_records.serialise_candidates([DUNE])
        assert [b["title"] for b in row["enrichment"]] == ["Dune"]
        # The normal path charged one unit.
        assert store.count() == 1

    def test_fingerprint_is_of_the_normalised_bytes(
        self, client, store, records, provider
    ):
        _post(client)
        ((_, fp),) = records.rows
        assert len(fp) == 64
        assert fp != scan_records.fingerprint(_JPEG)  # re-encoded first

    def test_enrichment_incomplete_saves_only_the_ai_step(
        self, client, store, records, provider, caplog
    ):
        provider.complete = False
        with caplog.at_level(logging.INFO, logger="app.ai.scan_records"):
            assert _post(client).status_code == 200
        row = records.only()
        assert row["ai_done"] and not row["enrich_done"]
        assert "save_enrichment_step" not in records.calls
        assert "event=saved ai_done=true enrich_done=false" in caplog.text

    def test_response_flags_books_in_the_library(
        self, client, store, records, provider
    ):
        provider.in_library = {"Dune"}
        (book,) = _post(client).json()["books"]
        assert book["in_library"] is True and book["already_in_library"] is True

    def test_different_photo_is_a_new_record(self, client, store, records, provider):
        _post(client)
        _post(client, image=_jpeg((200, 10, 10)))
        assert provider.identify.await_count == 2
        assert len(records.rows) == 2


class TestFinishedRepeat:
    def test_no_ai_call_no_enrichment_nothing_charged(
        self, client, store, records, provider, ledger_rows
    ):
        first = _post(client)
        before = _snapshot(store)
        second = _post(client)
        assert second.status_code == 200
        assert provider.identify.await_count == 1
        assert provider.enrich.await_count == 1  # no Google Books call
        assert _snapshot(store) == before  # no allowance, lease, spend, zero row
        body = second.json()
        assert body["repeat"] is True
        assert body["books"] == first.json()["books"]
        (row,) = ledger_rows
        assert row["status"] == "cache_hit"
        assert row["user_id"] == USER.id
        assert row["feature"] == "scan.standard"
        assert row["model"] == FEATURES["scan.standard"].model
        assert "input_tokens" not in row  # 0 tokens, cost 0

    def test_library_is_rechecked_on_a_repeat(self, client, store, records, provider):
        _post(client)
        provider.in_library = {"Dune"}
        (book,) = _post(client).json()["books"]
        assert book["in_library"] is True
        provider.in_library = set()
        (book,) = _post(client).json()["books"]
        assert book["in_library"] is False

    def test_served_with_no_allowance_left(self, client, store, records, provider):
        _post(client)
        ai_settings.reset_cache({"quota.free.daily": 0, "quota.free.monthly": 0})
        resp = _post(client)
        assert resp.status_code == 200 and resp.json()["repeat"] is True
        # A new photo is refused as before.
        assert _post(client, image=_jpeg((9, 9, 9))).status_code in (402, 429)

    def test_old_record_still_served_after_a_model_switch(
        self, client, store, records, provider
    ):
        _post(client)
        ai_settings.reset_cache({"scan.standard.model": "gpt-4o"})
        resp = _post(client)
        assert resp.json()["repeat"] is True
        assert provider.identify.await_count == 1

    def test_record_past_retention_is_not_served(
        self, client, store, records, provider
    ):
        _post(client)
        records.only()["created_at"] -= timedelta(days=30, seconds=1)
        assert _post(client).json()["repeat"] is False
        assert provider.identify.await_count == 2

    def test_lowered_retention_applies_at_once(self, client, store, records, provider):
        _post(client)
        records.only()["created_at"] -= timedelta(days=8)
        ai_settings.reset_cache({"scan_records.retention_days": 7})
        assert _post(client).json()["repeat"] is False

    def test_ledger_failure_still_serves(self, client, store, records, provider):
        _post(client)
        with patch(
            "app.api.scan.ledger.record", AsyncMock(side_effect=RuntimeError("x"))
        ):
            resp = _post(client)
        assert resp.status_code == 200 and resp.json()["repeat"] is True

    def test_logs_counts_only(self, client, store, records, provider, caplog):
        _post(client)
        provider.in_library = {"Dune"}
        with caplog.at_level(logging.INFO, logger="app.ai.scan_records"):
            _post(client)
        line = next(r.getMessage() for r in caplog.records if "event=repeat" in r.msg)
        assert "books=1 in_library=1" in line
        assert str(USER.id) not in caplog.text
        assert "Dune" not in caplog.text


class TestEnrichmentMissing:
    def test_redoes_only_enrichment_with_no_charge(
        self, client, store, records, provider, ledger_rows
    ):
        provider.complete = False
        _post(client)
        before = _snapshot(store)
        provider.complete = True
        resp = _post(client)
        assert resp.status_code == 200
        assert resp.json()["repeat"] is True
        assert provider.identify.await_count == 1  # no AI call
        assert provider.enrich.await_count == 2  # enrichment redone
        assert _snapshot(store) == before  # no charge
        assert records.only()["enrich_done"] is True
        assert [r["status"] for r in ledger_rows] == ["cache_hit"]
        # Now finished: a third upload redoes nothing.
        _post(client)
        assert provider.enrich.await_count == 2

    def test_still_incomplete_is_not_marked_done(
        self, client, store, records, provider
    ):
        provider.complete = False
        _post(client)
        _post(client)
        assert records.only()["enrich_done"] is False
        assert provider.identify.await_count == 1


class TestNotSaved:
    def test_no_match_is_not_saved(self, client, store, records, provider, caplog):
        """An empty ok list can be a genuine {"books": []} or an unparseable
        reply; the adapters cannot tell them apart yet (#572), so it is not
        saved and a re-upload takes the normal path."""
        provider.identify.return_value = ProviderResult([])
        with caplog.at_level(logging.INFO, logger="app.ai.scan_records"):
            assert _post(client).json()["books"] == []
            _post(client)
        assert records.rows == {}
        assert provider.identify.await_count == 2
        assert "event=not_saved" in caplog.text and "reason=no_match" in caplog.text

    @pytest.mark.parametrize("outcome", ["refused", "truncated"])
    def test_refused_or_truncated_not_saved(
        self, client, store, records, provider, outcome
    ):
        provider.identify.return_value = ProviderResult([DUNE], outcome=outcome)
        _post(client)
        _post(client)
        assert records.rows == {}
        assert provider.identify.await_count == 2

    @pytest.mark.parametrize(
        "error",
        [
            ScanUnavailableError("down"),
            ProviderTimeoutError("slow"),
            ValueError("malformed"),
        ],
    )
    def test_errors_not_saved(self, store, records, provider, error):
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        async def _fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = lambda: USER
        app.dependency_overrides[get_db] = _fake_db
        try:
            client = TestClient(app, raise_server_exceptions=False)
            provider.identify.side_effect = error
            assert _post(client).status_code >= 500
        finally:
            app.dependency_overrides.clear()
        assert records.rows == {}
        assert "save_ai_step" not in records.calls

    def test_disabled_neither_looks_up_nor_saves(
        self, client, store, records, provider
    ):
        ai_settings.reset_cache({"scan_records.enabled": False})
        _post(client)
        _post(client)
        assert records.calls == []
        assert provider.identify.await_count == 2


class TestEnhanced:
    def test_standard_record_does_not_answer_an_enhanced_scan(
        self, client, store, records, provider, premium
    ):
        _post(client)
        resp = _post(client, enhanced=True)
        assert resp.json()["repeat"] is False
        assert provider.identify.await_count == 2
        # The enhanced answer replaces the standard one.
        row = records.only()
        assert row["feature"] == "scan.enhanced" and row["tier"] == "premium"

    def test_enhanced_record_answers_both(
        self, client, store, records, provider, premium
    ):
        first = _post(client, enhanced=True)
        before = _snapshot(store)
        again = _post(client, enhanced=True)
        standard = _post(client)
        assert provider.identify.await_count == 1
        assert again.json()["repeat"] is True and standard.json()["repeat"] is True
        assert _snapshot(store) == before
        assert again.json()["enhanced_scans_remaining"] == (
            first.json()["enhanced_scans_remaining"]
        )

    def test_enhanced_repeat_refused_once_not_premium(
        self, client, store, records, provider, ledger_rows, monkeypatch
    ):
        monkeypatch.setattr(
            entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
        )
        _post(client, enhanced=True)
        monkeypatch.setattr(entitlements, "tier_for", AsyncMock(return_value=Tier.FREE))
        before = _snapshot(store)
        resp = _post(client, enhanced=True)
        assert (resp.status_code, resp.json()["detail"]) == (
            403,
            "enhanced_requires_premium",
        )
        assert ledger_rows == []
        assert _snapshot(store) == before
        assert provider.identify.await_count == 1


class TestFailureNeverFailsAScan:
    def test_lookup_raises_takes_the_normal_path(
        self, client, store, records, provider
    ):
        with patch(
            "app.api.scan.scan_records.lookup",
            AsyncMock(side_effect=RuntimeError("db blip")),
        ):
            resp = _post(client)
        assert resp.status_code == 200
        assert resp.json()["books"][0]["title"] == "Dune"
        provider.identify.assert_awaited_once()
        assert store.count() == 1  # the normal path reserved

    def test_ai_save_raises_still_returns(self, client, store, records, provider):
        with patch(
            "app.api.scan.scan_records.save_ai_step",
            AsyncMock(side_effect=RuntimeError("db blip")),
        ):
            resp = _post(client)
        assert resp.status_code == 200
        assert resp.json()["books"][0]["title"] == "Dune"
        assert records.rows == {}

    def test_enrichment_save_raises_still_returns(
        self, client, store, records, provider
    ):
        with patch(
            "app.api.scan.scan_records.save_enrichment_step",
            AsyncMock(side_effect=RuntimeError("db blip")),
        ):
            resp = _post(client)
        assert resp.status_code == 200
        assert records.only()["enrich_done"] is False

    def test_fingerprint_failure_is_the_normal_path(
        self, client, store, records, provider
    ):
        with patch(
            "app.api.scan.scan_records.fingerprint",
            MagicMock(side_effect=ValueError("x")),
        ):
            assert _post(client).status_code == 200
        assert records.calls == []

    def test_failure_logs_carry_no_content(self, client, store, provider, caplog):
        with (
            patch(
                "app.api.scan.scan_records.lookup",
                AsyncMock(side_effect=RuntimeError("SECRET-TITLE")),
            ),
            patch(
                "app.api.scan.scan_records.save_ai_step",
                AsyncMock(side_effect=RuntimeError("SECRET-TITLE")),
            ),
        ):
            assert _post(client).status_code == 200
        assert "scan-record lookup failed" in caplog.text
        assert "scan-record save failed" in caplog.text
        assert "SECRET-TITLE" not in caplog.text
        assert str(USER.id) not in caplog.text


class TestEndpointGuards:
    """Auth, rate limit and input validation still apply before any scan
    record is read, and to repeats."""

    def test_unauthenticated_never_reaches_a_record(self, records, provider):
        resp = TestClient(app).post(
            "/scan", files={"file": ("scan.jpg", io.BytesIO(_JPEG), "image/jpeg")}
        )
        assert resp.status_code == 401
        assert records.calls == []

    @pytest.mark.parametrize(
        "file",
        [
            ("doc.pdf", b"%PDF-1.4", "application/pdf"),
            ("scan.jpg", b"not a jpeg", "image/jpeg"),
        ],
    )
    def test_invalid_upload_rejected_before_lookup(
        self, client, store, records, provider, file
    ):
        name, body, ctype = file
        resp = client.post("/scan", files={"file": (name, io.BytesIO(body), ctype)})
        assert resp.status_code == 415
        assert records.calls == []

    def test_repeats_are_rate_limited(self, client, store, records, provider):
        statuses = [_post(client).status_code for _ in range(11)]
        assert statuses[:10] == [200] * 10
        assert statuses[10] == 429
        assert provider.identify.await_count == 1  # the rest were repeats

    def test_scan_disabled_refuses_repeats_too(self, client, store, records, provider):
        _post(client)
        ai_settings.reset_cache({"scan.standard.enabled": False})
        resp = _post(client)
        assert (resp.status_code, resp.json()["detail"]) == (503, "scan_disabled")
