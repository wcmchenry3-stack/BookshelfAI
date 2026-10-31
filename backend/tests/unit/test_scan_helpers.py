"""Failure-tolerance helpers in app/api/scan.py: each must degrade quietly
(never fail a scan) and say so in the log."""

import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai import breaker, quota, scan_records
from app.ai.entitlements import Tier
from app.ai.gateway import AIResult
from app.api import scan as scan_api
from app.models.ai_usage import STATUS_ERROR
from app.services.scan import settlement as scan_settlement

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_NOW = datetime(2026, 3, 1, tzinfo=UTC)


def _record(*, feature="scan.standard", books=None):
    return scan_records.Record(
        feature=feature,
        tier="free",
        model="m",
        candidates=[],
        books=books,
        created_at=_NOW,
    )


class TestRecordServes:
    def test_no_record_serves_nothing(self):
        assert scan_api._record_serves(None, enhanced=False) is False
        assert scan_api._record_serves(None, enhanced=True) is False

    def test_standard_scan_accepts_any_record(self):
        assert scan_api._record_serves(_record(), enhanced=False) is True
        enhanced = _record(feature=scan_api.ENHANCED_FEATURE)
        assert scan_api._record_serves(enhanced, enhanced=False) is True

    def test_enhanced_scan_never_gets_the_standard_answer(self):
        assert scan_api._record_serves(_record(), enhanced=True) is False

    def test_enhanced_scan_accepts_an_enhanced_record(self):
        enhanced = _record(feature=scan_api.ENHANCED_FEATURE)
        assert scan_api._record_serves(enhanced, enhanced=True) is True


class TestSaveEnrichmentStep:
    async def _save(self, db, **kw):
        args = {
            "fingerprint": "f" * 64,
            "user_id": _USER,
            "ai_created_at": _NOW,
            "books": [],
        }
        args.update(kw)
        return await scan_api._save_enrichment_step(db, **args)

    @pytest.mark.parametrize(
        "override", [{"fingerprint": None}, {"ai_created_at": None}]
    )
    async def test_nothing_to_save_against_returns_false_without_io(self, override):
        db = AsyncMock()
        with patch.object(scan_records, "save_enrichment_step", AsyncMock()) as save:
            assert await self._save(db, **override) is False
        save.assert_not_awaited()
        db.rollback.assert_not_awaited()

    async def test_saved_result_is_passed_through(self):
        with patch.object(
            scan_records, "save_enrichment_step", AsyncMock(return_value=True)
        ):
            assert await self._save(AsyncMock()) is True

    async def test_failure_returns_false_and_recovers_the_session(self, caplog):
        db = AsyncMock()
        boom = AsyncMock(side_effect=RuntimeError("secret detail"))
        with patch.object(scan_records, "save_enrichment_step", boom), caplog.at_level(
            logging.WARNING
        ):
            assert await self._save(db) is False
        db.rollback.assert_awaited_once()
        # Only the exception type is logged.
        assert "RuntimeError" in caplog.text
        assert "secret detail" not in caplog.text


class TestRecoverSession:
    async def test_rolls_back_and_refreshes_the_user(self):
        db, user = AsyncMock(), MagicMock()
        await scan_api._recover_session(db, user)
        db.rollback.assert_awaited_once()
        db.refresh.assert_awaited_once_with(user)

    async def test_without_user_only_rolls_back(self):
        db = AsyncMock()
        await scan_api._recover_session(db)
        db.refresh.assert_not_awaited()

    async def test_never_raises(self, caplog):
        db = AsyncMock()
        db.rollback.side_effect = RuntimeError("conn closed")
        with caplog.at_level(logging.WARNING):
            await scan_api._recover_session(db)
        assert "recovery" in caplog.text


class TestEnhancedRemaining:
    async def test_returns_the_quota_value(self):
        with patch.object(quota, "enhanced_remaining", AsyncMock(return_value=3)):
            assert await scan_api._enhanced_remaining(AsyncMock(), MagicMock()) == 3

    async def test_read_failure_gives_none_and_rolls_back(self):
        db = AsyncMock()
        with patch.object(
            quota, "enhanced_remaining", AsyncMock(side_effect=RuntimeError)
        ):
            assert await scan_api._enhanced_remaining(db, MagicMock()) is None
        db.rollback.assert_awaited_once()

    async def test_rollback_failure_is_also_contained(self):
        db = AsyncMock()
        db.rollback.side_effect = RuntimeError("closed")
        with patch.object(
            quota, "enhanced_remaining", AsyncMock(side_effect=RuntimeError)
        ):
            assert await scan_api._enhanced_remaining(db, MagicMock()) is None


class TestNoMatchField:
    @pytest.mark.parametrize("outcome", [quota.NO_MATCH_FREE, quota.NO_MATCH_COUNTED])
    def test_reports_free_and_counted(self, outcome):
        moved = quota.NoMatchMoved(outcome=outcome, count=1, warn=False)
        assert scan_api._no_match_field(moved) == outcome

    def test_other_values_are_none(self):
        assert scan_api._no_match_field(None) is None
        assert scan_api._no_match_field("free") is None


class TestSettlement:
    @staticmethod
    def _reservation(estimate=900):
        return SimpleNamespace(est_cost_micros=estimate)

    @staticmethod
    def _result(model="m"):
        return AIResult(
            feature="scan.standard",
            provider="p",
            model=model,
            candidates=[],
            tier=Tier.FREE,
            outcome="ok",
            input_tokens=1,
            output_tokens=2,
        )

    def test_answered_call_settles_its_priced_cost(self):
        with patch.object(scan_settlement, "cost_micros", return_value=123):
            s = scan_api._settlement(
                reservation=self._reservation(),
                feature="scan.standard",
                ai=MagicMock(),
                result=self._result(),
            )
        assert s == breaker.Settlement(123, "scan.standard", "m", "ok")

    def test_unpriceable_call_settles_the_estimate(self, caplog):
        with patch.object(
            scan_settlement, "cost_micros", side_effect=KeyError("m")
        ), caplog.at_level(logging.ERROR):
            s = scan_api._settlement(
                reservation=self._reservation(900),
                feature="scan.standard",
                ai=MagicMock(),
                result=self._result(),
            )
        assert s.cost_micros == 900
        assert "settling the estimate" in caplog.text

    def test_failed_call_settles_the_estimate_with_the_resolved_model(self):
        with patch.object(
            scan_settlement, "resolve_spec", return_value=SimpleNamespace(model="m2")
        ):
            s = scan_api._settlement(
                reservation=self._reservation(900),
                feature="scan.standard",
                ai=MagicMock(),
                status="timeout",
            )
        assert s == breaker.Settlement(900, "scan.standard", "m2", "timeout")

    def test_unresolvable_model_is_tagged_unknown_and_status_defaults_to_error(self):
        with patch.object(scan_settlement, "resolve_spec", side_effect=RuntimeError):
            s = scan_api._settlement(
                reservation=self._reservation(900),
                feature="scan.standard",
                ai=MagicMock(),
            )
        assert s == breaker.Settlement(900, "scan.standard", "unknown", STATUS_ERROR)


class TestServeRepeat:
    async def _serve(self, record, *, remaining):
        db = AsyncMock()
        with (
            patch.object(scan_api.ledger, "record", AsyncMock()),
            patch.object(scan_api, "_mark_library", AsyncMock(side_effect=_same)),
            patch.object(
                scan_api.catalog_token, "sign_books", side_effect=lambda b, u: b
            ),
            patch.object(scan_records, "log_event"),
            patch.object(quota, "enhanced_remaining_for", remaining),
        ):
            resp = await scan_api._serve_repeat(
                db,
                user_id=_USER,
                tier=Tier.FREE,
                feature="scan.standard",
                enhanced=False,
                fingerprint="f" * 64,
                record=record,
                ai=MagicMock(),
                attempt=1,
            )
        return resp, db

    async def test_allowance_read_failure_does_not_fail_the_repeat(self):
        resp, db = await self._serve(
            _record(books=[]), remaining=AsyncMock(side_effect=RuntimeError)
        )
        assert resp.repeat is True
        assert resp.enhanced_scans_remaining is None
        db.rollback.assert_awaited()

    async def test_repeat_reports_the_remaining_allowance(self):
        resp, _ = await self._serve(
            _record(books=[]), remaining=AsyncMock(return_value=4)
        )
        assert resp.enhanced_scans_remaining == 4


async def _same(db, user_id, books):
    return books
