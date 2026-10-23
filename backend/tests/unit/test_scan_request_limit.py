"""The per-user daily /scan request limit (#586), without a database.

The counter's SQL, the setting, the refusal and its place in /scan (after
authentication and the kill switches, before Turnstile, the upload, the
scan-record lookup, the allowance and the provider). The quota primitives
run on the in-memory store (``_quota_fake.FakeQuotaStore``), which keeps
the same semantics; the real-Postgres behaviour (exactness under
concurrency, the CLI, retention) is in ``tests/limits/``.
"""

import io
import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from limits import parse
from PIL import Image
from sqlalchemy.dialects import postgresql

from app.ai import quota, scan_records
from app.ai import settings as ai_settings
from app.ai.providers.base import ProviderResult
from app.api import scan as scan_api
from app.core.config import settings
from app.core.limiter import limiter
from app.main import app
from app.models.user import User
from app.schemas.book import EnrichedBook
from app.services.book_identifier import BookCandidate
from tests.unit._quota_fake import FakeQuotaStore
from tests.unit._scan_records_fake import FakeScanRecords

CODE = "scan_requests_daily_limit"
KEY = "scan.requests_per_user_daily"
USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-00000000586a"),
    email="requests@example.com",
)
OTHER = User(
    id=uuid.UUID("00000000-0000-0000-0000-00000000586b"),
    email="other@example.com",
)
DUNE = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.9)


def _jpeg(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, format="JPEG")
    return buf.getvalue()


def _sql(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


class TestSetting:
    def test_default_is_100(self):
        assert ai_settings.AISettings({}).scan_requests_per_user_daily == 100
        assert ai_settings.DEFINITIONS[KEY].default() == 100

    @pytest.mark.parametrize("value", [1, 100, 10_000])
    def test_in_bounds_accepted(self, value):
        assert ai_settings.validate_value(KEY, value) == value

    @pytest.mark.parametrize("value", [0, -1, 10_001, True, 100.0, "100", None])
    def test_out_of_bounds_or_wrong_type_refused(self, value):
        with pytest.raises(ai_settings.AISettingValueError):
            ai_settings.validate_value(KEY, value)

    def test_stored_value_is_used(self):
        ai_settings.reset_cache({KEY: 7})
        try:
            assert ai_settings._cache.rows[KEY] == 7
            assert ai_settings.AISettings({KEY: 7}).scan_requests_per_user_daily == 7
        finally:
            ai_settings.reset_cache()

    def test_invalid_stored_value_falls_back_to_the_default(self):
        with patch.object(ai_settings, "sentry_sdk") as sentry:
            rows = ai_settings._sanitise_rows({KEY: 0})
        assert KEY not in rows
        assert ai_settings.AISettings(rows).scan_requests_per_user_daily == 100
        sentry.capture_message.assert_called_once()

    def test_known_to_the_cli(self):
        assert KEY in ai_settings.KNOWN_KEYS


# ---------------------------------------------------------------------------
# The counter (app/ai/quota.py)
# ---------------------------------------------------------------------------


def _db(returned):
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = returned
    db.execute = AsyncMock(return_value=result)
    return db


class TestCountScanRequest:
    NOW = datetime(2026, 10, 23, 18, 0, 0, tzinfo=UTC)

    async def test_counts_under_the_key_for_the_utc_day(self):
        db = _db(7)
        ai = ai_settings.AISettings({KEY: 9})
        used = await quota.count_scan_request(db, USER.id, ai, now=self.NOW)
        assert used == 7
        sql = _sql(db.execute.await_args.args[0])
        assert "INSERT INTO ai_quota" in sql
        assert "'scan.requests'" in sql
        assert "'day'" in sql
        assert "'2026-10-23'" in sql
        # The exact conditional upsert: + 1 only while under the limit.
        assert "ai_quota.units_used < 9" in sql
        db.commit.assert_awaited_once()

    async def test_at_the_limit_raises_with_retry_after_to_utc_midnight(self):
        db = _db(None)
        ai = ai_settings.AISettings({KEY: 3})
        with pytest.raises(quota.ScanRequestsDailyLimitReached) as exc:
            await quota.count_scan_request(db, USER.id, ai, now=self.NOW)
        assert exc.value.retry_after == 6 * 3600
        assert str(exc.value) == CODE
        assert isinstance(exc.value, quota.QuotaRefusal)
        assert not isinstance(exc.value, quota.DailyLimitReached)

    async def test_uses_the_request_clock(self):
        db = _db(1)
        ai = ai_settings.AISettings({})
        with patch.object(quota, "utc_now", lambda: self.NOW):
            await quota.count_scan_request(db, USER.id, ai)
        assert "'2026-10-23'" in _sql(db.execute.await_args.args[0])

    def test_key_is_not_an_ai_call_key(self):
        assert quota.SCAN_REQUESTS_QUOTA_FEATURE == "scan.requests"
        assert quota.SCAN_REQUESTS_QUOTA_FEATURE in quota.NON_AI_QUOTA_FEATURES
        assert quota.SCAN_QUOTA_FEATURE not in quota.NON_AI_QUOTA_FEATURES
        assert quota.SCAN_NO_MATCH_QUOTA_FEATURE not in quota.NON_AI_QUOTA_FEATURES


class TestCountOnTheStore:
    """The counter on the in-memory store (same semantics as the upsert):
    counts 1..L, then refuses and writes nothing, per user and per day."""

    async def test_counts_up_to_the_limit_then_refuses(self):
        store = FakeQuotaStore()
        ai = ai_settings.AISettings({KEY: 3})
        now = datetime(2026, 10, 23, 12, tzinfo=UTC)
        with store.installed():
            counts = [
                await quota.count_scan_request(None, USER.id, ai, now=now)
                for _ in range(3)
            ]
            for _ in range(5):
                with pytest.raises(quota.ScanRequestsDailyLimitReached):
                    await quota.count_scan_request(None, USER.id, ai, now=now)
            other = await quota.count_scan_request(None, OTHER.id, ai, now=now)
            next_day = now.replace(day=24)
            tomorrow = await quota.count_scan_request(None, USER.id, ai, now=next_day)
        assert counts == [1, 2, 3]
        assert (other, tomorrow) == (1, 1)
        key = (USER.id, quota.SCAN_REQUESTS_QUOTA_FEATURE, "day", date(2026, 10, 23))
        assert store.rows[key] == 3
        assert await store.global_units_today(None, date(2026, 10, 23)) == 0


class TestGlobalCountExcludesRequests:
    def test_filter_leaves_the_request_key_out(self):
        sql = _sql(quota.global_units_filter(date(2026, 10, 23)))
        assert "ai_quota.feature NOT IN ('scan.requests')" in sql
        assert "ai_quota.window_kind = 'day'" in sql

    async def test_global_units_today_uses_the_filter(self):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one.return_value = 4
        db.execute = AsyncMock(return_value=result)
        assert await quota.global_units_today(db, date(2026, 10, 23)) == 4
        sql = _sql(db.execute.await_args.args[0])
        assert "sum(ai_quota.units_used)" in sql
        assert "NOT IN ('scan.requests')" in sql

    async def test_fake_store_mirrors_it(self):
        store = FakeQuotaStore()
        today = date(2026, 10, 23)
        store.rows[(USER.id, quota.SCAN_REQUESTS_QUOTA_FEATURE, "day", today)] = 50
        store.rows[(USER.id, quota.SCAN_QUOTA_FEATURE, "day", today)] = 2
        assert await store.global_units_today(None, today) == 2


# ---------------------------------------------------------------------------
# /scan
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
def as_user():
    current = {"user": USER}
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    async def _fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.dependency_overrides[get_db] = _fake_db
    yield current
    app.dependency_overrides.clear()


@pytest.fixture
def client(as_user):
    return TestClient(app)


@pytest.fixture
def provider():
    with (
        patch("app.ai.gateway.OpenAIVisionProvider") as cls,
        patch("app.api.scan.EnrichmentService") as enrich_cls,
        patch("app.api.scan.DeduplicationService") as dedup_cls,
        patch("app.api.scan.ledger.record", AsyncMock()),
    ):

        async def _enrich(candidates, limit):
            enrich_cls.return_value.last_complete = True
            return [
                EnrichedBook(title=c.title, author=c.author, confidence=c.confidence)
                for c in candidates
            ]

        cls.return_value.identify = AsyncMock(return_value=ProviderResult([DUNE]))
        enrich_cls.return_value.enrich = AsyncMock(side_effect=_enrich)
        dedup_cls.return_value.check = AsyncMock(side_effect=lambda _d, _u, b: b)
        yield cls


@pytest.fixture(autouse=True)
def _settings():
    ai_settings.reset_cache({KEY: 3})
    yield
    ai_settings.reset_cache()


def _post(client, image=None, enhanced=False):
    data = {"enhanced": "true"} if enhanced else {}
    return client.post(
        "/scan",
        files={"file": ("scan.jpg", io.BytesIO(image or _jpeg()), "image/jpeg")},
        data=data,
    )


def _requests(store, user=USER) -> int:
    return store.count(user.id, feature=quota.SCAN_REQUESTS_QUOTA_FEATURE)


class TestScanEndpoint:
    def test_refused_at_the_limit_before_any_other_work(
        self, client, store, records, provider
    ):
        store.rows[
            (USER.id, quota.SCAN_REQUESTS_QUOTA_FEATURE, "day", quota.utc_today())
        ] = 3
        reserve = AsyncMock(side_effect=quota.reserve)
        sanitize = MagicMock(side_effect=scan_api.sanitize_image)
        fingerprint = MagicMock(side_effect=scan_records.fingerprint)
        turnstile = AsyncMock(return_value=True)
        with (
            patch.object(quota, "reserve", reserve),
            patch.object(scan_api, "sanitize_image", sanitize),
            patch.object(scan_records, "fingerprint", fingerprint),
            patch.object(scan_api, "_verify_turnstile", turnstile),
            patch.object(settings, "turnstile_required", True),
        ):
            resp = client.post(
                "/scan",
                files={"file": ("scan.jpg", io.BytesIO(_jpeg()), "image/jpeg")},
                data={"cf-turnstile-response": "tok"},
            )
        assert resp.status_code == 429
        assert resp.json() == {"detail": CODE}
        assert 1 <= int(resp.headers["Retry-After"]) <= 86_400
        assert reserve.await_count == 0
        assert sanitize.call_count == 0
        assert fingerprint.call_count == 0
        assert turnstile.await_count == 0
        assert records.calls == []
        provider.assert_not_called()
        assert store.leases == {}
        assert store.count(USER.id) == 0
        assert _requests(store) == 3

    def test_every_request_counts_until_the_limit(
        self, client, store, records, provider
    ):
        photo = _jpeg((1, 2, 3))
        statuses = [_post(client, photo).status_code for _ in range(4)]
        # New photo, two repeats (free, but counted), then refused.
        assert statuses == [200, 200, 200, 429]
        assert provider.return_value.identify.await_count == 1
        assert _requests(store) == 3
        assert store.count(USER.id) == 1

    def test_refused_for_the_allowance_still_counts(
        self, client, store, records, provider
    ):
        ai_settings.reset_cache({KEY: 3, "quota.free.found_daily": 0})
        resp = _post(client)
        assert resp.json() == {"detail": "daily_scan_limit"}
        assert _requests(store) == 1

    def test_bad_upload_still_counts(self, client, store, records, provider):
        resp = client.post(
            "/scan", files={"file": ("doc.pdf", io.BytesIO(b"%PDF"), "application/pdf")}
        )
        assert resp.status_code == 415
        assert _requests(store) == 1
        assert records.calls == []

    def test_another_users_count_is_unaffected(
        self, client, as_user, store, records, provider
    ):
        for _ in range(3):
            assert _post(client).status_code == 200
        assert _post(client).json() == {"detail": CODE}
        as_user["user"] = OTHER
        assert _post(client).status_code == 200
        assert (_requests(store), _requests(store, OTHER)) == (3, 1)

    def test_global_count_is_unaffected(self, client, store, records, provider):
        _post(client, _jpeg((5, 5, 5)))
        _post(client, _jpeg((5, 5, 5)))  # a repeat: a request, no AI call
        today = quota.utc_today()
        assert _requests(store) == 2
        assert store.count(USER.id) == 1
        assert store.rows[(USER.id, quota.SCAN_QUOTA_FEATURE, "day", today)] == 1

    @pytest.mark.parametrize(
        "rows, enhanced, code",
        [
            ({"scan.standard.enabled": False}, False, "scan_disabled"),
            ({"scan.enhanced.enabled": False}, True, "enhanced_scan_disabled"),
        ],
    )
    def test_kill_switches_come_first_and_are_not_counted(
        self, client, store, records, provider, rows, enhanced, code
    ):
        ai_settings.reset_cache({KEY: 3, **rows})
        resp = _post(client, enhanced=enhanced)
        assert (resp.status_code, resp.json()["detail"]) == (503, code)
        assert _requests(store) == 0

    def test_counted_before_turnstile(self, client, store, records, provider):
        with patch.object(settings, "turnstile_required", True):
            resp = _post(client)
        assert (resp.status_code, resp.json()["detail"]) == (
            400,
            "Missing Turnstile token",
        )
        assert _requests(store) == 1

    def test_settings_change_applies_to_the_next_request(
        self, client, store, records, provider
    ):
        ai_settings.reset_cache({KEY: 1})
        assert _post(client).status_code == 200
        assert _post(client).json() == {"detail": CODE}
        ai_settings.reset_cache({KEY: 2})
        assert _post(client).status_code == 200

    def test_unauthenticated_is_401_and_not_counted(self, store):
        client = TestClient(app)
        resp = _post(client)
        assert resp.status_code == 401
        assert store.rows == {}

    def test_distinct_from_the_per_minute_rate_limit(
        self, client, store, records, provider
    ):
        ai_settings.reset_cache({KEY: 1})
        responses = [_post(client) for _ in range(11)]
        assert [r.status_code for r in responses] == [200] + [429] * 10
        assert all(r.json() == {"detail": CODE} for r in responses[1:10])
        # The 11th in a minute is the per-minute limiter's 429, not ours.
        assert responses[10].json().get("detail") != CODE
        assert _requests(store) == 1


class TestRequestLimitComesFirst:
    """At the request limit every other refusal condition is never reached:
    the request-limit refusal wins (the matrix's ``request_limit_beats_*``
    rows, without a database)."""

    @pytest.fixture(autouse=True)
    def _at_limit(self, store):
        self.today = quota.utc_today()
        store.rows[(USER.id, quota.SCAN_REQUESTS_QUOTA_FEATURE, "day", self.today)] = 3

    def _assert_refused_first(self, resp, store, records, provider):
        assert (resp.status_code, resp.json()) == (429, {"detail": CODE})
        assert records.calls == []
        provider.assert_not_called()
        assert _requests(store) == 3
        assert store.count(USER.id) == 0

    def test_beats_the_daily_allowance(self, client, store, records, provider):
        ai_settings.reset_cache({KEY: 3, "quota.free.found_daily": 0})
        self._assert_refused_first(_post(client), store, records, provider)

    def test_beats_the_global_cap(self, client, store, records, provider):
        ai_settings.reset_cache({KEY: 3, "scan.global_daily_limit": 1})
        store.extra_global = 1
        self._assert_refused_first(_post(client), store, records, provider)

    def test_beats_scan_in_progress(self, client, store, records, provider):
        store.leases[USER.id] = held = uuid.uuid4()
        self._assert_refused_first(_post(client), store, records, provider)
        assert store.leases == {USER.id: held}

    def test_beats_the_spend_ceiling(self, client, store, records, provider):
        ai_settings.reset_cache({KEY: 3, "breaker.free_daily_ceiling_micros": 0})
        self._assert_refused_first(_post(client), store, records, provider)
        assert store.spend == {}

    def test_beats_enhanced_requires_premium(self, client, store, records, provider):
        self._assert_refused_first(
            _post(client, enhanced=True), store, records, provider
        )

    def test_beats_a_repeat(self, client, store, records, provider):
        photo = _jpeg((9, 9, 9))
        store.rows[(USER.id, quota.SCAN_REQUESTS_QUOTA_FEATURE, "day", self.today)] = 2
        assert _post(client, photo).status_code == 200
        assert store.count(USER.id) == 1
        records.calls.clear()
        provider.reset_mock()
        store.rows[(USER.id, quota.SCAN_QUOTA_FEATURE, "day", self.today)] = 0
        self._assert_refused_first(_post(client, photo), store, records, provider)

    def test_retry_after_runs_to_utc_midnight(self, client, store, records, provider):
        noon = datetime.combine(self.today, datetime.min.time(), tzinfo=UTC)
        noon = noon.replace(hour=12)
        with patch.object(quota, "utc_now", lambda: noon):
            resp = _post(client)
        assert resp.headers["Retry-After"] == "43200"

    def test_raising_the_limit_lets_the_user_in(self, client, store, records, provider):
        assert _post(client).json() == {"detail": CODE}
        ai_settings.reset_cache({KEY: 4})
        assert _post(client).status_code == 200
        assert _requests(store) == 4


def test_cli_write_validation_uses_the_same_bounds():
    assert ai_settings.validate_write(KEY, 250, {}) == 250
    for bad in (0, 10_001, "250"):
        with pytest.raises(ai_settings.AISettingValueError):
            ai_settings.validate_write(KEY, bad, {})


def test_scan_keeps_its_rate_limit_decorator():
    """#586 adds a daily limit; the per-minute limiter stays on /scan."""
    route_limits = limiter._route_limits["app.api.scan.scan"]
    assert [str(rl.limit) for rl in route_limits] == [
        str(parse(settings.rate_limit_scan))
    ]


async def test_count_wrapper_maps_the_refusal_to_429():
    from fastapi import HTTPException

    with (
        patch.object(
            quota,
            "count_scan_request",
            AsyncMock(side_effect=quota.ScanRequestsDailyLimitReached(retry_after=77)),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await scan_api._count_scan_request(AsyncMock(), USER, MagicMock())
    assert exc.value.status_code == 429
    assert exc.value.detail == CODE
    assert exc.value.headers == {"Retry-After": "77"}
