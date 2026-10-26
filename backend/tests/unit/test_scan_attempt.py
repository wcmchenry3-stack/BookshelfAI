"""The ``attempt`` form field on POST /scan (#603), database-free: its bounds
and validation (a 422 before anything is counted, reserved or recorded), the
default, that every ledger row the request writes carries it (each provider
outcome and a repeat's ``cache_hit``), that the ``scan_record`` log line
gains ``attempt=N``, and that nothing else in the response or the charge
changes. Provider mocked at the gateway boundary; quota primitives and scan
records faked in memory. Real Postgres behaviour (the column, the
migration) is in tests/integration/test_scan_attempt_db.py."""

import io
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.dialects import postgresql
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai import ledger, scan_records
from app.ai import settings as ai_settings
from app.ai.providers.base import ProviderResult, ProviderTimeoutError
from app.api import scan as scan_module
from app.main import app
from app.models.ai_usage import AIUsage, AIUsageDaily
from app.models.user import User
from app.services.book_identifier import BookCandidate, ScanUnavailableError
from tests.unit._quota_fake import FakeQuotaStore
from tests.unit._scan_records_fake import FakeScanRecords

USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-000000000603"),
    email="attempt@example.com",
)
DUNE = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.9)
USAGE = {
    "input_tokens": 900,
    "output_tokens": 120,
    "cache_read_tokens": 256,
    "cache_write_tokens": 0,
}


def _jpeg(color=(60, 3, 6)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


def _params(stmt) -> dict:
    return stmt.compile(dialect=postgresql.dialect()).params


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def counted():
    """#586's per-request count, stubbed (the fake DB cannot run it) and
    observed: a 422 must never reach it."""
    count = AsyncMock()
    with patch("app.api.scan._count_scan_request", new=count):
        yield count


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
def provider():
    with (
        patch("app.ai.gateway.OpenAIVisionProvider") as cls,
        patch("app.api.scan.EnrichmentService") as enrich,
        patch("app.api.scan.DeduplicationService") as dedup,
    ):

        async def _enrich(candidates, limit):
            enrich.return_value.last_complete = True
            return []

        cls.return_value.identify = AsyncMock(return_value=ProviderResult([]))
        enrich.return_value.enrich = AsyncMock(side_effect=_enrich)
        dedup.return_value.check = AsyncMock(return_value=[])
        yield cls


@pytest.fixture
def rows():
    """Every ledger insert /scan builds (``usage_insert``'s arguments) and
    every row ``ledger.record`` writes (a repeat's ``cache_hit``)."""
    built: list[dict] = []
    recorded: list[dict] = []
    real_insert = ledger.usage_insert

    def spy_insert(**kwargs):
        stmt = real_insert(**kwargs)
        built.append({**kwargs, "stmt": stmt})
        return stmt

    async def record(_db, **fields):
        recorded.append(fields)

    with (
        patch.object(ledger, "usage_insert", spy_insert),
        patch("app.api.scan.ledger.record", record),
    ):
        yield built, recorded


def _post(client, image=_JPEG, **data):
    return client.post(
        "/scan",
        files={"file": ("scan.jpg", io.BytesIO(image), "image/jpeg")},
        data={k: str(v) for k, v in data.items()},
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize("value", ["0", "7", "-1", "x", "1.5", "true"])
    def test_invalid_attempt_is_a_422_with_nothing_taken(
        self, client, counted, store, provider, rows, value
    ):
        resp = _post(client, attempt=value)
        assert resp.status_code == 422
        (error,) = resp.json()["detail"]
        assert error["loc"] == ["body", "attempt"]
        # Rejected before the handler: no #586 request count, no
        # reservation, no provider call, no ledger row.
        counted.assert_not_awaited()
        assert store.rows == {} and store.leases == {}
        provider.return_value.identify.assert_not_called()
        assert rows == ([], [])

    def test_a_bad_attempt_is_counted_like_a_bad_enhanced(
        self, client, counted, store, provider
    ):
        """The #586 count for a 422 matches how other invalid form input
        behaves today: neither reaches the handler, so neither counts."""
        assert _post(client, enhanced="maybe").status_code == 422
        assert _post(client, attempt="9").status_code == 422
        counted.assert_not_awaited()

    @pytest.mark.parametrize("value", [1, 2, 3, 4, 5, 6])
    def test_every_value_in_range_is_accepted_and_recorded(
        self, client, counted, store, provider, rows, value
    ):
        resp = _post(client, attempt=value)
        assert resp.status_code == 200
        counted.assert_awaited_once()
        (row,), _ = rows
        assert row["attempt"] == value
        assert _params(row["stmt"])["attempt"] == value

    def test_missing_means_1(self, client, counted, store, provider, rows):
        assert _post(client).status_code == 200
        (row,), _ = rows
        assert row["attempt"] == 1

    def test_empty_value_means_1(self, client, counted, store, provider, rows):
        """An empty form value is treated as missing, like any optional
        form field."""
        assert _post(client, attempt="").status_code == 200
        (row,), _ = rows
        assert row["attempt"] == 1

    def test_documented_in_the_openapi_schema(self):
        schema = app.openapi()
        bodies = schema["components"]["schemas"]
        (body,) = [
            v
            for k, v in bodies.items()
            if k.startswith("Body_scan") and "attempt" in v.get("properties", {})
        ]
        field = body["properties"]["attempt"]
        assert (field["minimum"], field["maximum"], field["default"]) == (1, 6, 1)
        assert field["type"] == "integer"


# ---------------------------------------------------------------------------
# Every ledger row carries it
# ---------------------------------------------------------------------------


class TestLedgerRows:
    # A truncated reply holds a book: with none it is malformed (#572).
    @pytest.mark.parametrize(
        ("outcome", "candidates", "code"),
        [
            ("ok", [], 200),
            ("ok", [DUNE], 200),
            ("refused", [], 200),
            ("truncated", [DUNE], 200),
            ("malformed", [], 503),
        ],
    )
    def test_answered_call(
        self, client, counted, store, provider, rows, outcome, candidates, code
    ):
        provider.return_value.identify = AsyncMock(
            return_value=ProviderResult(candidates, outcome=outcome, **USAGE)
        )
        assert _post(client, attempt=4).status_code == code
        (row,), recorded = rows
        assert (row["status"], row["attempt"]) == (outcome, 4)
        assert recorded == []

    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (ScanUnavailableError("down"), "refunded"),
            (httpx.ConnectError("refused"), "refunded"),
            (ProviderTimeoutError("slow"), "timeout"),
        ],
    )
    def test_unanswered_call(
        self, client, counted, store, provider, rows, error, status
    ):
        provider.return_value.identify = AsyncMock(side_effect=error)
        assert _post(client, attempt=6).status_code == 503
        (row,), _ = rows
        assert (row["status"], row["attempt"]) == (status, 6)
        assert _params(row["stmt"])["attempt"] == 6

    async def test_error_row(self, counted, store, provider, rows):
        provider.return_value.identify = AsyncMock(side_effect=RuntimeError("boom"))
        upload = StarletteUploadFile(
            file=io.BytesIO(_JPEG),
            filename="scan.jpg",
            headers=Headers({"content-type": "image/jpeg"}),
        )
        with (
            patch.object(scan_module.settings, "turnstile_required", False),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await scan_module.scan.__wrapped__(
                request=MagicMock(),
                file=upload,
                current_user=USER,
                db=AsyncMock(),
                cf_turnstile_response=None,
                enhanced=False,
                attempt=3,
            )
        (row,), _ = rows
        assert (row["status"], row["attempt"]) == ("error", 3)

    def test_repeat_cache_hit_row(self, client, counted, store, records, provider):
        ai_settings.reset_cache({"scan_records.enabled": True})
        provider.return_value.identify = AsyncMock(return_value=ProviderResult([DUNE]))
        recorded: list[dict] = []

        async def record(_db, **fields):
            recorded.append(fields)

        assert _post(client, attempt=1).json()["repeat"] is False
        with patch("app.api.scan.ledger.record", record):
            assert _post(client, attempt=2).json()["repeat"] is True
        (row,) = recorded
        assert (row["status"], row["attempt"]) == ("cache_hit", 2)

    def test_usage_row_helper_passes_it_on_both_paths(self):
        ai = ai_settings.AISettings({})
        call_id = uuid.uuid4()
        no_answer = scan_module._usage_row(
            call_id=call_id,
            user_id=USER.id,
            feature="scan.standard",
            ai=ai,
            status="refunded",
            attempt=5,
        )
        assert _params(no_answer)["attempt"] == 5
        # Required, so a new call site can't silently record attempt 1.
        with pytest.raises(TypeError):
            scan_module._usage_row(  # type: ignore[call-arg]
                call_id=call_id,
                user_id=USER.id,
                feature="scan.standard",
                ai=ai,
                status="refunded",
            )


# ---------------------------------------------------------------------------
# Nothing else changes
# ---------------------------------------------------------------------------


def _charge(store: FakeQuotaStore):
    """What the request took, with the user and day keys dropped."""
    return (
        sorted(store.rows.values()),
        len(store.leases),
        sorted(sum(v) for v in store.spend.values()),
    )


class TestNothingElseChanges:
    @pytest.mark.parametrize(
        "identify",
        [
            AsyncMock(return_value=ProviderResult([DUNE], **USAGE)),
            AsyncMock(return_value=ProviderResult([], **USAGE)),
            AsyncMock(side_effect=ScanUnavailableError("down")),
        ],
        ids=["books", "no_match", "unavailable"],
    )
    def test_same_response_charge_and_cost_for_every_attempt(
        self, client, counted, provider, rows, identify
    ):
        provider.return_value.identify = identify
        seen = []
        for attempt in (None, 1, 6):
            s = FakeQuotaStore(default_user=USER.id)
            with s.installed():
                resp = (
                    _post(client) if attempt is None else _post(client, attempt=attempt)
                )
                seen.append(
                    (
                        resp.status_code,
                        resp.json(),
                        {k: v for k, v in resp.headers.items() if k != "x-request-id"},
                        _charge(s),
                    )
                )
        assert seen[0] == seen[1] == seen[2]
        built, _ = rows
        assert [r["attempt"] for r in built] == [1, 1, 6]
        costs = {_params(r["stmt"])["cost_micros"] for r in built}
        assert len(costs) == 1
        assert counted.await_count == 3


# ---------------------------------------------------------------------------
# Ledger writer and model
# ---------------------------------------------------------------------------


class TestLedgerWriter:
    def test_usage_insert_stores_attempt_without_changing_the_cost(self):
        base = dict(
            call_id=uuid.uuid4(),
            user_id=None,
            feature="scan.standard",
            model="gpt-4o-mini",
            status="ok",
            **USAGE,
        )
        unknown = _params(ledger.usage_insert(**base))
        second = _params(ledger.usage_insert(**base, attempt=2))
        assert unknown["attempt"] is None
        assert second["attempt"] == 2
        assert unknown["cost_micros"] == second["cost_micros"]

    async def test_record_passes_attempt(self):
        db = AsyncMock()
        await ledger.record(
            db,
            user_id=None,
            feature="scan.standard",
            model="gpt-4o-mini",
            status="cache_hit",
            attempt=5,
        )
        assert _params(db.execute.await_args.args[0])["attempt"] == 5

    def test_column_is_nullable_smallint_and_not_rolled_up(self):
        column = AIUsage.__table__.c.attempt
        assert column.nullable is True
        assert column.type.__class__.__name__ == "SmallInteger"
        assert column.server_default is None
        assert "attempt" not in AIUsageDaily.__table__.c


# ---------------------------------------------------------------------------
# The scan_record log line
# ---------------------------------------------------------------------------


class TestLogLine:
    def test_log_event_gains_attempt(self, caplog):
        with caplog.at_level(logging.INFO, logger="app.ai.scan_records"):
            scan_records.log_event("repeat", ai_done=True, enrich_done=True, attempt=3)
            scan_records.log_event("lookup_failed", ai_done=False, enrich_done=False)
        first, second = (r.getMessage() for r in caplog.records)
        assert first == (
            "scan_record event=repeat ai_done=true enrich_done=true books=0 "
            "in_library=0 reason=- attempt=3"
        )
        assert second.endswith("reason=- attempt=-")

    def _lines(self, caplog) -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("scan_record ")
        ]

    def test_saved_and_repeat_lines_carry_the_attempt(
        self, client, counted, store, records, provider, caplog
    ):
        ai_settings.reset_cache({"scan_records.enabled": True})
        provider.return_value.identify = AsyncMock(return_value=ProviderResult([DUNE]))
        with caplog.at_level(logging.INFO, logger="app.ai.scan_records"):
            _post(client, attempt=2)
            _post(client)
        saved, repeat = self._lines(caplog)
        assert saved.startswith("scan_record event=saved ")
        assert saved.endswith(" attempt=2")
        assert repeat.startswith("scan_record event=repeat ")
        assert repeat.endswith(" attempt=1")

    def test_not_saved_line_carries_the_attempt(
        self, client, counted, store, records, provider, caplog
    ):
        ai_settings.reset_cache({"scan_records.enabled": True})
        provider.return_value.identify = AsyncMock(
            return_value=ProviderResult([], outcome="refused")
        )
        with caplog.at_level(logging.INFO, logger="app.ai.scan_records"):
            _post(client, attempt=5)
        (line,) = self._lines(caplog)
        assert "event=not_saved" in line and "reason=refused" in line
        assert line.endswith(" attempt=5")

    def test_failure_lines_carry_the_attempt(
        self, client, counted, store, records, provider, caplog
    ):
        ai_settings.reset_cache({"scan_records.enabled": True})
        provider.return_value.identify = AsyncMock(return_value=ProviderResult([DUNE]))
        with (
            patch(
                "app.api.scan.scan_records.lookup",
                AsyncMock(side_effect=OSError("db")),
            ),
            patch(
                "app.api.scan.scan_records.save_ai_step",
                AsyncMock(side_effect=OSError("db")),
            ),
            caplog.at_level(logging.INFO, logger="app.ai.scan_records"),
        ):
            assert _post(client, attempt=4).status_code == 200
        lookup, save = self._lines(caplog)
        assert "event=lookup_failed" in lookup and lookup.endswith(" attempt=4")
        assert "event=save_failed" in save and save.endswith(" attempt=4")


# ---------------------------------------------------------------------------
# Migration 0014 (database-free; the real cycle is in the integration test)
# ---------------------------------------------------------------------------


def _migration_0014():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0014_ai_usage_attempt.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0014_unit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigration0014:
    def test_follows_0013(self):
        migration = _migration_0014()
        assert (migration.revision, migration.down_revision) == ("0014", "0013")

    def test_upgrade_adds_a_nullable_smallint_with_no_default(self):
        migration = _migration_0014()
        with patch.object(migration, "op") as op:
            migration.upgrade()
        (table, column), _ = op.add_column.call_args
        assert table == "ai_usage"
        assert column.name == "attempt"
        assert column.type.__class__.__name__ == "SmallInteger"
        assert column.nullable is True
        assert column.server_default is None
        assert "lock_timeout" in op.execute.call_args.args[0]
        op.drop_column.assert_not_called()

    def test_downgrade_drops_the_column(self):
        migration = _migration_0014()
        with patch.object(migration, "op") as op:
            migration.downgrade()
        op.drop_column.assert_called_once_with("ai_usage", "attempt")
        op.add_column.assert_not_called()
