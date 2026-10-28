"""Usage ledger (#530): the gateway carries usage, /scan records one row per
provider call with the right status, the row is committed with the lease
release, and a ledger failure never changes the scan's outcome. Provider
mocked at the gateway boundary; quota primitives faked in memory."""

import asyncio
import dataclasses
import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.dialects import postgresql
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai import entitlements, ledger, quota
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.gateway import VisionPayload, run_feature
from app.ai.prices import CURRENT_PRICE_VERSION, UnknownPriceError
from app.ai.providers.base import ProviderResult, ProviderTimeoutError
from app.api import scan as scan_module
from app.core.logging import request_id_var
from app.main import app
from app.models.user import User
from app.schemas.book import EnrichedBook
from app.services import token_cleanup
from app.services.book_identifier import BookCandidate, ScanUnavailableError
from tests.unit._quota_fake import FakeQuotaStore

USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-000000000530"),
    email="ledger@example.com",
)
DUNE = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.9)
CALL_ID = uuid.UUID("00000000-0000-0000-0000-00000000ca11")
USAGE = {
    "input_tokens": 900,
    "output_tokens": 120,
    "cache_read_tokens": 256,
    "cache_write_tokens": 0,
}


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (5, 99, 200)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


def _file():
    return {"file": ("scan.jpg", io.BytesIO(_JPEG), "image/jpeg")}


def _params(stmt) -> dict:
    """The bound values of a usage insert."""
    return stmt.compile(dialect=postgresql.dialect()).params


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class TestUsageInsert:
    def test_cost_computed_from_price_table(self):
        stmt = ledger.usage_insert(
            call_id=CALL_ID,
            user_id=USER.id,
            feature="scan.enhanced",
            model="gpt-4o",
            status="ok",
            request_id="rid-1",
            **USAGE,
        )
        params = _params(stmt)
        # 900 * 2.5 + 120 * 10 + 256 * 1.25 = 3,770 micros
        assert params["cost_micros"] == 3_770
        assert params["price_version"] == CURRENT_PRICE_VERSION
        assert params["status"] == "ok"
        assert params["request_id"] == "rid-1"
        assert params["user_id"] == USER.id
        assert params["call_id"] == CALL_ID

    def test_insert_is_idempotent_on_call_id(self):
        sql = str(
            ledger.usage_insert(
                call_id=CALL_ID,
                user_id=None,
                feature="scan.standard",
                model="gpt-4o",
                status="ok",
            ).compile(dialect=postgresql.dialect())
        )
        assert "ON CONFLICT (call_id) DO NOTHING" in sql

    def test_cache_hit_costs_zero(self):
        params = _params(
            ledger.usage_insert(
                call_id=CALL_ID,
                user_id=USER.id,
                feature="scan.standard",
                model="gpt-4o-mini",
                status="cache_hit",
                **USAGE,
            )
        )
        assert (params["status"], params["cost_micros"]) == ("cache_hit", 0)

    def test_cache_hit_with_an_unpriced_model_costs_zero(self):
        params = _params(
            ledger.usage_insert(
                call_id=CALL_ID,
                user_id=USER.id,
                feature="scan.standard",
                model="retired-model-not-in-price-table",
                status="cache_hit",
                **USAGE,
            )
        )
        assert (params["status"], params["cost_micros"]) == ("cache_hit", 0)

    def test_unknown_status_rejected(self):
        with pytest.raises(ValueError):
            ledger.usage_insert(
                call_id=CALL_ID,
                user_id=None,
                feature="scan.standard",
                model="gpt-4o",
                status="weird",
            )

    def test_unknown_price_version_fails_loudly(self):
        with pytest.raises(UnknownPriceError):
            ledger.usage_insert(
                call_id=CALL_ID,
                user_id=None,
                feature="scan.standard",
                model="gpt-4o",
                status="ok",
                price_version="1999-01",
            )

    async def test_record_executes_and_commits(self):
        db = AsyncMock()
        await ledger.record(
            db, user_id=None, feature="scan.standard", model="gpt-4o", status="error"
        )
        db.execute.assert_awaited_once()
        db.commit.assert_awaited_once()
        params = _params(db.execute.await_args.args[0])
        assert isinstance(params["call_id"], uuid.UUID)

    async def test_record_replaces_an_explicit_none_call_id(self):
        db = AsyncMock()
        await ledger.record(
            db,
            user_id=None,
            feature="scan.standard",
            model="gpt-4o",
            status="error",
            call_id=None,
        )
        params = _params(db.execute.await_args.args[0])
        assert isinstance(params["call_id"], uuid.UUID)

    def test_request_id_is_the_servers_or_none(self):
        assert ledger.current_request_id() is None  # outside a request: "-"
        token = request_id_var.set("abc-123")
        try:
            assert ledger.current_request_id() == "abc-123"
        finally:
            request_id_var.reset(token)


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


class TestGatewayCarriesUsage:
    async def test_ai_result_has_provider_usage_and_outcome(self):
        with patch("app.ai.gateway.OpenAIVisionProvider") as cls:
            cls.return_value.identify = AsyncMock(
                return_value=ProviderResult([DUNE], outcome="truncated", **USAGE)
            )
            result = await run_feature(
                "scan.standard",
                user=MagicMock(),
                db=MagicMock(),
                payload=VisionPayload(image_bytes=b"img"),
            )
        assert result.candidates == [DUNE]
        assert result.outcome == "truncated"
        assert (
            result.input_tokens,
            result.output_tokens,
            result.cache_read_tokens,
            result.cache_write_tokens,
        ) == (900, 120, 256, 0)

    async def test_deadline_raises_provider_timeout(self):
        async def hung(_image):
            await anyio.sleep(5)

        def fast(spec):
            fast = dataclasses.replace(spec)
            object.__setattr__(fast, "timeout_s", 0.05)
            return fast

        with patch("app.ai.gateway.OpenAIVisionProvider") as cls:
            cls.return_value.identify = hung
            with pytest.raises(ProviderTimeoutError):
                await run_feature(
                    "scan.standard",
                    user=MagicMock(),
                    db=MagicMock(),
                    payload=VisionPayload(image_bytes=b"img"),
                    settings_snapshot=MagicMock(spec_for=fast),
                )


# ---------------------------------------------------------------------------
# Settle: one commit for the ledger row and the lease release
# ---------------------------------------------------------------------------


def _reservation(lease_id=None):
    return quota.Reservation(
        user_id=USER.id,
        feature=quota.SCAN_QUOTA_FEATURE,
        lease_id=lease_id or uuid.uuid4(),
        day=quota.utc_today(),
        tier=MagicMock(),
        allowance=quota.Allowance(5),
    )


class TestSettle:
    async def test_row_executed_before_the_lease_delete_commits(self):
        calls = []
        db = AsyncMock()
        db.execute.side_effect = lambda stmt: calls.append(("execute", stmt))

        async def release_lease(_db, _uid, _lease):
            calls.append(("release_lease", None))
            return True

        row = object()
        with patch.object(quota, "release_lease", release_lease):
            await quota.release(db, _reservation(), row)
        assert calls == [("execute", row), ("release_lease", None)]

    async def test_no_row_only_releases(self):
        db = AsyncMock()
        release_lease = AsyncMock(return_value=True)
        with patch.object(quota, "release_lease", release_lease):
            await quota.release(db, _reservation())
        db.execute.assert_not_awaited()
        release_lease.assert_awaited_once()

    async def test_row_failure_drops_the_row_and_still_releases(self, caplog):
        db = AsyncMock()
        db.execute.side_effect = RuntimeError("row rejected")
        release_lease = AsyncMock(return_value=True)
        with patch.object(quota, "release_lease", release_lease):
            await quota.release(db, _reservation(), object())
        # Two settle attempts failed before release_lease ran; then the
        # lease is released on its own.
        release_lease.assert_awaited_once()
        assert "usage row dropped" in caplog.text
        assert db.rollback.await_count >= 2

    async def test_transient_failure_retries_row_and_release_together(self):
        db = AsyncMock()
        attempts = []

        async def flaky(_db, _uid, _lease):
            attempts.append(db.execute.await_count)
            if len(attempts) == 1:
                raise OSError("blip")
            return True

        row = object()
        with patch.object(quota, "release_lease", flaky):
            await quota.release(db, _reservation(), row)
        # The row was re-executed on the retry, right before the release.
        assert attempts == [1, 2]
        # The SAME statement (so the same call_id) both times: if the first
        # commit had landed, the retry's insert is a no-op.
        assert [c.args[0] for c in db.execute.await_args_list] == [row, row]

    async def test_timed_out_settle_does_not_run_the_fallback(self, caplog):
        """A settle cut off by the bound means a slow database, not a bad
        row: no extra lease-only attempt (the lease expiry is the backstop)."""
        db = AsyncMock()

        async def hang(_stmt):
            await anyio.sleep(5)

        db.execute.side_effect = hang
        release_lease = AsyncMock(return_value=True)
        with (
            patch.object(quota, "release_lease", release_lease),
            patch.object(quota, "CLEANUP_TIMEOUT_SECONDS", 0.05),
            anyio.fail_after(2),
        ):
            await quota.release(db, _reservation(), object())
        release_lease.assert_not_awaited()
        assert db.execute.await_count == 1
        assert "settle timed out" in caplog.text
        assert "usage row dropped" not in caplog.text

    async def test_failed_settle_runs_the_fallback(self):
        db = AsyncMock()
        db.execute.side_effect = RuntimeError("CHECK violated")
        release_lease = AsyncMock(return_value=True)
        with patch.object(quota, "release_lease", release_lease):
            await quota.release(db, _reservation(), object())
        release_lease.assert_awaited_once()

    async def test_never_raises(self):
        db = AsyncMock()
        db.execute.side_effect = RuntimeError("x")
        with patch.object(
            quota, "release_lease", AsyncMock(side_effect=OSError("down"))
        ):
            await quota.release(db, _reservation(), object())


# ---------------------------------------------------------------------------
# /scan: status mapping
# ---------------------------------------------------------------------------


@pytest.fixture
def store():
    s = FakeQuotaStore(default_user=USER.id)
    with s.installed():
        yield s


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
def provider():
    with (
        patch("app.ai.gateway.OpenAIVisionProvider") as cls,
        patch("app.api.scan.EnrichmentService") as enrich,
        patch("app.api.scan.DeduplicationService") as dedup,
    ):
        cls.return_value.identify = AsyncMock(return_value=ProviderResult([]))
        enrich.return_value.enrich = AsyncMock(return_value=[])
        dedup.return_value.check = AsyncMock(return_value=[])
        yield cls


@pytest.fixture
def rows():
    """Every ledger row /scan builds, and what reached quota.release."""
    built: list[dict] = []
    settled: list[object] = []
    real_insert = ledger.usage_insert
    real_release = quota.release

    def spy_insert(**kwargs):
        stmt = real_insert(**kwargs)
        built.append({**kwargs, "stmt": stmt})
        return stmt

    async def spy_release(db, reservation, ledger_row=None, *args):
        settled.append(ledger_row)
        await real_release(db, reservation, ledger_row, *args)

    with (
        patch.object(ledger, "usage_insert", spy_insert),
        patch.object(quota, "release", spy_release),
    ):
        yield built, settled


def _one(rows):
    built, settled = rows
    assert len(built) == 1, built
    assert settled == [built[0]["stmt"]]  # committed with the lease release
    return built[0]


class TestScanRecordsUsage:
    # A truncated reply holds a book: with none it is malformed (#572).
    @pytest.mark.parametrize(
        ("outcome", "candidates"),
        [("ok", []), ("refused", []), ("truncated", [DUNE])],
    )
    def test_provider_answer_records_its_outcome_and_usage(
        self, client, store, provider, rows, outcome, candidates
    ):
        provider.return_value.identify = AsyncMock(
            return_value=ProviderResult(candidates, outcome=outcome, **USAGE)
        )
        resp = client.post("/scan", files=_file())
        assert resp.status_code == 200
        row = _one(rows)
        assert row["status"] == outcome
        assert row["feature"] == "scan.standard"
        assert row["model"] == "gpt-4o-mini"
        assert row["user_id"] == USER.id
        assert {k: row[k] for k in USAGE} == USAGE
        assert row["request_id"] == resp.headers["X-Request-ID"]
        assert store.leases == {}

    def test_enhanced_scan_records_its_feature_and_model(
        self, client, store, provider, rows
    ):
        # Enhanced scans are Premium-only (#533).
        with patch.object(
            entitlements, "tier_for", AsyncMock(return_value=Tier.PREMIUM)
        ):
            client.post("/scan", files=_file(), data={"enhanced": "true"})
        row = _one(rows)
        assert (row["feature"], row["model"]) == ("scan.enhanced", "gpt-4o")

    def test_model_override_is_recorded(self, client, store, provider, rows):
        ai_settings.reset_cache({"scan.standard.model": "gpt-4o-2024-11-20"})
        client.post("/scan", files=_file())
        assert _one(rows)["model"] == "gpt-4o-2024-11-20"

    def test_gateway_deadline_records_timeout(self, client, store, provider, rows):
        async def hung(_image):
            await anyio.sleep(2)

        def fast_spec(self, spec):
            fast = dataclasses.replace(spec)
            object.__setattr__(fast, "timeout_s", 0.05)
            return fast

        provider.return_value.identify = hung
        with patch.object(ai_settings.AISettings, "spec_for", fast_spec):
            resp = client.post("/scan", files=_file())
        assert resp.json() == {"detail": "scan_unavailable"}
        row = _one(rows)
        assert row["status"] == "timeout"
        assert row["model"] == "gpt-4o-mini"
        params = _params(row["stmt"])
        assert params["input_tokens"] == params["output_tokens"] == 0
        assert params["cost_micros"] == 0
        assert store.count() == 0  # still refunded

    def test_provider_http_timeout_records_timeout(self, client, store, provider, rows):
        provider.return_value.identify = AsyncMock(
            side_effect=ProviderTimeoutError("timed out")
        )
        assert client.post("/scan", files=_file()).status_code == 503
        assert _one(rows)["status"] == "timeout"
        assert store.count() == 0

    @pytest.mark.parametrize(
        "error", [ScanUnavailableError("down"), httpx.ConnectError("refused")]
    )
    def test_unavailable_records_refunded(self, client, store, provider, rows, error):
        provider.return_value.identify = AsyncMock(side_effect=error)
        assert client.post("/scan", files=_file()).status_code == 503
        row = _one(rows)
        assert row["status"] == "refunded"
        params = _params(row["stmt"])
        assert params["input_tokens"] == params["output_tokens"] == 0
        assert params["cost_micros"] == 0
        assert store.count() == 0

    async def test_other_error_records_error_and_keeps_reservation(
        self, store, provider, rows
    ):
        provider.return_value.identify = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            await _call_scan()
        row = _one(rows)
        assert row["status"] == "error"
        assert store.count() == 1

    async def test_cancelled_call_records_an_error_row(self, store, provider, rows):
        """The reservation is kept on cancellation, so the call is recorded
        (as error, 0 tokens) by the shielded settle."""
        provider.return_value.identify = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await _call_scan()
        row = _one(rows)
        assert row["status"] == "error"
        params = _params(row["stmt"])
        assert params["input_tokens"] == params["output_tokens"] == 0
        assert store.count() == 1
        assert store.leases == {}

    async def test_anyio_cancelled_scan_records_an_error_row(
        self, store, provider, rows
    ):
        async def hung(_image):
            await anyio.sleep(5)

        provider.return_value.identify = hung
        with anyio.move_on_after(0.2) as scope:
            await _call_scan()
        assert scope.cancelled_caught
        assert _one(rows)["status"] == "error"
        assert store.leases == {}

    def test_kill_switch_in_the_gateway_records_no_row(
        self, client, store, provider, rows
    ):
        """FeatureDisabledError never reached a provider: refunded, same
        response as before, and no ledger row."""
        from app.ai.registry import FEATURES

        def disabled(name):
            return dataclasses.replace(FEATURES[name], enabled=False)

        with patch("app.ai.gateway.get_feature", disabled):
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 503
        assert resp.json() == {"detail": "scan_unavailable"}
        built, settled = rows
        assert built == []
        assert settled == [None]
        assert store.count() == 0 and store.count(kind="month") == 0
        provider.return_value.identify.assert_not_called()

    def test_each_call_gets_its_own_call_id(self, client, store, provider, rows):
        client.post("/scan", files=_file())
        client.post("/scan", files=_file())
        ids = [r["call_id"] for r in rows[0]]
        assert len(ids) == 2 and len(set(ids)) == 2
        assert all(isinstance(i, uuid.UUID) for i in ids)

    def test_ledger_build_failure_still_returns_the_books(
        self, client, store, provider, rows, caplog
    ):
        book = EnrichedBook(
            open_library_work_id="OL1W",
            title="Dune",
            author="Frank Herbert",
            confidence=0.9,
            already_in_library=False,
        )
        provider.return_value.identify = AsyncMock(return_value=ProviderResult([DUNE]))
        with (
            patch("app.api.scan.EnrichmentService") as enrich,
            patch("app.api.scan.DeduplicationService") as dedup,
            patch.object(
                ledger, "usage_insert", MagicMock(side_effect=UnknownPriceError("x"))
            ),
        ):
            enrich.return_value.enrich = AsyncMock(return_value=[book])
            dedup.return_value.check = AsyncMock(return_value=[book])
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 200
        assert [b["title"] for b in resp.json()["books"]] == ["Dune"]
        assert rows[1] == [None]  # lease released without a row
        assert store.leases == {}
        assert "could not build the usage ledger row" in caplog.text

    def test_ledger_write_failure_still_returns_the_books(
        self, store, provider, rows, caplog
    ):
        """The row is rejected at commit: the scan still succeeds and the
        lease is released on its own."""
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        book = EnrichedBook(
            open_library_work_id="OL1W",
            title="Dune",
            author="Frank Herbert",
            confidence=0.9,
            already_in_library=False,
        )

        async def _db():
            db = AsyncMock()

            async def execute(stmt, *a, **kw):
                if "ai_usage" in str(stmt):
                    raise RuntimeError("ledger insert failed")
                return MagicMock()

            db.execute.side_effect = execute
            yield db

        app.dependency_overrides[get_current_user] = lambda: USER
        app.dependency_overrides[get_db] = _db
        provider.return_value.identify = AsyncMock(return_value=ProviderResult([DUNE]))
        try:
            with (
                patch("app.api.scan.EnrichmentService") as enrich,
                patch("app.api.scan.DeduplicationService") as dedup,
            ):
                enrich.return_value.enrich = AsyncMock(return_value=[book])
                dedup.return_value.check = AsyncMock(return_value=[book])
                resp = TestClient(app).post("/scan", files=_file())
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert [b["title"] for b in resp.json()["books"]] == ["Dune"]
        assert store.leases == {}
        assert "usage row dropped" in caplog.text

    def test_refusals_before_the_provider_record_nothing(
        self, client, store, provider, rows
    ):
        store.leases[USER.id] = uuid.uuid4()  # another scan in flight
        assert client.post("/scan", files=_file()).status_code == 429
        assert rows[0] == []


async def _call_scan(enhanced: bool = False):
    upload = StarletteUploadFile(
        file=io.BytesIO(_JPEG),
        filename="scan.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )
    with patch.object(scan_module.settings, "turnstile_required", False):
        return await scan_module.scan.__wrapped__(
            request=MagicMock(),
            file=upload,
            current_user=USER,
            db=AsyncMock(),
            cf_turnstile_response=None,
            enhanced=enhanced,
        )


# ---------------------------------------------------------------------------
# Daily cleanup task
# ---------------------------------------------------------------------------


class _Stop(Exception):
    pass


class TestCleanupTask:
    async def _run_once(self, **patches):
        """One cleanup run; returns the session the refresh-token delete used."""
        session = AsyncMock()
        # A Result is synchronous (SQLAlchemy): .scalars().all() on it.
        session.execute = AsyncMock(return_value=MagicMock())
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        sleeps = AsyncMock(side_effect=[None, _Stop()])
        with (
            patch.object(token_cleanup, "AsyncSessionLocal", factory),
            patch.object(token_cleanup, "purge_old_quota", AsyncMock(return_value=0)),
            patch.object(token_cleanup, "purge_old_spend", AsyncMock(return_value=0)),
            patch.object(
                token_cleanup, "purge_old_zero_results", AsyncMock(return_value=0)
            ),
            patch.object(
                token_cleanup, "purge_old_scan_records", AsyncMock(return_value=0)
            ),
            patch.object(token_cleanup.asyncio, "sleep", sleeps),
            patch.multiple(token_cleanup, **patches),
            pytest.raises(_Stop),
        ):
            await token_cleanup.cleanup_expired_tokens()
        return session

    async def test_run_rolls_up_the_ledger(self, caplog):
        rollup = AsyncMock(return_value=(7, 2))
        with caplog.at_level("INFO"):
            await self._run_once(rollup_and_purge=rollup)
        rollup.assert_awaited_once()
        assert "rolled up 7 ai_usage row(s)" in caplog.text

    async def test_rollup_failure_does_not_skip_token_cleanup(self, caplog):
        rollup = AsyncMock(side_effect=ValueError("db down"))
        session = await self._run_once(rollup_and_purge=rollup)
        assert "error during cleanup run (ai usage)" in caplog.text
        # The refresh-token purge still ran afterwards.
        session.execute.assert_awaited_once()
        session.commit.assert_awaited_once()
