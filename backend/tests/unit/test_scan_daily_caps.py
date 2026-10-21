"""POST /scan kill switches and daily caps — provider mocked, quota store in memory."""

import io
import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.api import scan as scan_module
from app.core.config import Settings
from app.main import app
from app.models.user import User
from app.services import scan_quota
from app.services.book_identifier import BookCandidate, ScanUnavailableError

USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-0000000005a9"),
    email="caps@example.com",
    enhanced_scan_credits=3,
)
_JPEG = b"\xff\xd8\xff\xe0" + b"x" * 96


def _file():
    return {"file": ("scan.jpg", io.BytesIO(_JPEG), "image/jpeg")}


class FakeQuotaStore:
    """In-memory stand-in for scan_usage_daily with the same semantics."""

    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, date], int] = {}
        self.extra_global = 0

    async def global_scans_today(self, _db, day: date) -> int:
        return self.extra_global + sum(n for (_, d), n in self.rows.items() if d == day)

    async def reserve_scan(self, _db, user_id, day, limit) -> int | None:
        current = self.rows.get((user_id, day), 0)
        if current >= limit:
            return None
        self.rows[(user_id, day)] = current + 1
        return current + 1

    async def refund_scan(self, _db, user_id, day) -> None:
        key = (user_id, day)
        self.rows[key] = max(0, self.rows.get(key, 0) - 1)

    def count(self, user_id=USER.id) -> int:
        return self.rows.get((user_id, scan_quota.utc_today()), 0)


@pytest.fixture
def store():
    s = FakeQuotaStore()
    with (
        patch.object(scan_quota, "global_scans_today", s.global_scans_today),
        patch.object(scan_quota, "reserve_scan", s.reserve_scan),
        patch.object(scan_quota, "refund_scan", s.refund_scan),
    ):
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
    """Mock the vision provider and the downstream lookups."""
    with (
        patch("app.api.scan.ChatGPTVisionIdentifier") as id_cls,
        patch("app.api.scan.EnrichmentService") as enrich_cls,
        patch("app.api.scan.DeduplicationService") as dedup_cls,
    ):
        id_cls.return_value.identify = AsyncMock(return_value=[])
        enrich_cls.return_value.enrich = AsyncMock(return_value=[])
        dedup_cls.return_value.check = AsyncMock(return_value=[])
        yield id_cls


def _set(**values):
    return patch.multiple(scan_module.settings, **values)


class TestKillSwitches:
    def test_scan_disabled_returns_503_before_body_turnstile_or_provider(
        self, client, store, provider
    ):
        with (
            _set(scan_enabled=False, turnstile_required=True),
            patch.object(StarletteUploadFile, "read", new=AsyncMock()) as read,
            patch("app.api.scan._verify_turnstile", new=AsyncMock()) as turnstile,
        ):
            resp = client.post(
                "/scan", files=_file(), data={"cf-turnstile-response": "t"}
            )

        assert resp.status_code == 503
        assert resp.json() == {"detail": "scan_disabled"}
        read.assert_not_awaited()
        turnstile.assert_not_awaited()
        provider.assert_not_called()
        assert store.rows == {}

    def test_read_probe_sees_body_read_when_enabled(self, client, store, provider):
        """Positive control for the read assertions above."""
        with patch.object(
            StarletteUploadFile, "read", new=AsyncMock(return_value=_JPEG)
        ) as read:
            client.post("/scan", files=_file())
        read.assert_awaited()

    def test_scan_disabled_applies_to_enhanced_too(self, client, store, provider):
        with _set(scan_enabled=False):
            resp = client.post("/scan", files=_file(), data={"enhanced": "true"})
        assert resp.json()["detail"] == "scan_disabled"

    def test_enhanced_disabled_rejects_enhanced_scan(self, client, store, provider):
        with (
            _set(scan_enhanced_enabled=False),
            patch.object(StarletteUploadFile, "read", new=AsyncMock()) as read,
        ):
            resp = client.post("/scan", files=_file(), data={"enhanced": "true"})

        assert resp.status_code == 503
        assert resp.json() == {"detail": "enhanced_scan_disabled"}
        read.assert_not_awaited()
        provider.assert_not_called()
        assert store.rows == {}

    def test_enhanced_disabled_leaves_standard_scan_working(
        self, client, store, provider
    ):
        with _set(scan_enhanced_enabled=False):
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 200
        provider.assert_called_once_with(enhanced=False)


class TestPerUserDailyLimit:
    def test_allows_limit_then_429_with_retry_after(self, client, store, provider):
        with _set(scan_daily_limit_per_user=3):
            codes = [client.post("/scan", files=_file()).status_code for _ in range(3)]
            resp = client.post("/scan", files=_file())

        assert codes == [200, 200, 200]
        assert resp.status_code == 429
        assert resp.json() == {"detail": "daily_scan_limit"}
        retry_after = int(resp.headers["Retry-After"])
        assert 1 <= retry_after <= 86_400
        # The rejected request never reached the provider.
        assert provider.return_value.identify.await_count == 3
        assert store.count() == 3

    def test_empty_result_still_counts(self, client, store, provider):
        provider.return_value.identify = AsyncMock(return_value=[])
        resp = client.post("/scan", files=_file())
        assert resp.status_code == 200
        assert resp.json()["books"] == []
        assert store.count() == 1

    def test_enhanced_credit_check_runs_before_reservation(
        self, client, store, provider
    ):
        broke = User(id=uuid.uuid4(), email="broke@example.com")
        broke.enhanced_scan_credits = 0
        from app.auth.dependencies import get_current_user

        app.dependency_overrides[get_current_user] = lambda: broke
        resp = client.post("/scan", files=_file(), data={"enhanced": "true"})
        assert resp.status_code == 402
        assert store.rows == {}


class TestGlobalDailyLimit:
    def test_global_limit_returns_503_scan_capacity(self, client, store, provider):
        store.extra_global = 50
        with _set(scan_global_daily_limit=50):
            resp = client.post("/scan", files=_file())

        assert resp.status_code == 503
        assert resp.json() == {"detail": "scan_capacity"}
        provider.return_value.identify.assert_not_awaited()
        assert store.count() == 0

    def test_under_global_limit_proceeds(self, client, store, provider):
        store.extra_global = 49
        with _set(scan_global_daily_limit=50):
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 200


class TestRefund:
    def test_provider_unavailable_refunds_and_returns_503(
        self, client, store, provider
    ):
        provider.return_value.identify = AsyncMock(
            side_effect=ScanUnavailableError("timeout")
        )
        resp = client.post("/scan", files=_file())
        assert resp.status_code == 503
        assert resp.json()["detail"] == "scan_unavailable"
        assert store.count() == 0

    def test_unexpected_provider_error_refunds_and_reraises(self, store, provider):
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        async def _fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = lambda: USER
        app.dependency_overrides[get_db] = _fake_db
        provider.return_value.identify = AsyncMock(side_effect=RuntimeError("boom"))
        try:
            resp = TestClient(app, raise_server_exceptions=False).post(
                "/scan", files=_file()
            )
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 500
        assert store.count() == 0

    def test_successful_scan_is_not_refunded(self, client, store, provider):
        provider.return_value.identify = AsyncMock(
            return_value=[BookCandidate(title="Dune", author="F", confidence=0.9)]
        )
        client.post("/scan", files=_file())
        assert store.count() == 1

    def test_refund_failure_does_not_mask_provider_error(self, client, provider):
        provider.return_value.identify = AsyncMock(
            side_effect=ScanUnavailableError("timeout")
        )
        with (
            patch.object(scan_quota, "global_scans_today", AsyncMock(return_value=0)),
            patch.object(scan_quota, "reserve_scan", AsyncMock(return_value=1)),
            patch.object(
                scan_quota, "refund_scan", AsyncMock(side_effect=OSError("db down"))
            ),
        ):
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 503
        assert resp.json()["detail"] == "scan_unavailable"


class TestQuotaHelpers:
    def test_seconds_until_utc_midnight(self):
        now = datetime(2026, 10, 21, 23, 59, 0, tzinfo=UTC)
        assert scan_quota.seconds_until_utc_midnight(now) == 60
        start = datetime(2026, 10, 21, 0, 0, 0, tzinfo=UTC)
        assert scan_quota.seconds_until_utc_midnight(start) == 86_400

    def test_seconds_until_midnight_is_at_least_one(self):
        now = datetime(2026, 10, 21, 23, 59, 59, 999_999, tzinfo=UTC)
        assert scan_quota.seconds_until_utc_midnight(now) == 1

    def test_utc_today_uses_utc(self):
        from datetime import timedelta, timezone

        late_pacific = datetime(
            2026, 10, 21, 20, 0, tzinfo=timezone(timedelta(hours=-7))
        )
        assert scan_quota.utc_today(late_pacific) == date(2026, 10, 22)

    async def test_purge_deletes_in_bounded_batches(self):
        results = [
            MagicMock(rowcount=scan_quota.PURGE_BATCH_SIZE),
            MagicMock(rowcount=7),
        ]
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=results)
        total = await scan_quota.purge_old_scan_usage(db, today=date(2026, 10, 21))
        assert total == scan_quota.PURGE_BATCH_SIZE + 7
        assert db.execute.await_count == 2
        assert "scan_usage_daily" in str(db.execute.call_args[0][0])

    async def test_purge_stops_at_max_batches(self):
        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(rowcount=scan_quota.PURGE_BATCH_SIZE)
        )
        await scan_quota.purge_old_scan_usage(db)
        assert db.execute.await_count == scan_quota.PURGE_MAX_BATCHES


_DB = "postgresql+asyncpg://u:p@localhost/db"


class TestSettingsBounds:
    def test_defaults(self):
        s = Settings(database_url=_DB)
        assert s.scan_enabled is True
        assert s.scan_enhanced_enabled is True
        assert s.scan_daily_limit_per_user == 20
        assert s.scan_global_daily_limit == 2000

    @pytest.mark.parametrize("value", [0, -1, 1001])
    def test_per_user_limit_out_of_bounds_rejected(self, value):
        with pytest.raises(ValidationError):
            Settings(database_url=_DB, scan_daily_limit_per_user=value)

    @pytest.mark.parametrize("value", [0, 1_000_001])
    def test_global_limit_out_of_bounds_rejected(self, value):
        with pytest.raises(ValidationError):
            Settings(database_url=_DB, scan_global_daily_limit=value)

    @pytest.mark.parametrize(
        "field,value",
        [("scan_daily_limit_per_user", 1000), ("scan_global_daily_limit", 1)],
    )
    def test_bounds_inclusive(self, field, value):
        assert getattr(Settings(database_url=_DB, **{field: value}), field) == value

    def test_read_from_env(self, monkeypatch):
        monkeypatch.setenv("SCAN_ENABLED", "false")
        monkeypatch.setenv("SCAN_DAILY_LIMIT_PER_USER", "5")
        s = Settings(database_url=_DB)
        assert s.scan_enabled is False
        assert s.scan_daily_limit_per_user == 5
