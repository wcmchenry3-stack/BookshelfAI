"""Failure handling in the quota give-back, the breaker totals, the
ai_settings seed/switch helpers and the scan-record codec."""

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai import breaker, quota, scan_records
from app.ai import settings as ai_settings
from app.ai.registry import FeatureSpecError, get_feature
from app.schemas.book import EnrichedBook


def _db():
    db = AsyncMock()
    db.add = MagicMock()
    return db


class TestGiveBack:
    async def test_success_runs_the_op_once(self):
        op = AsyncMock()
        assert await quota._give_back(_db(), "release", op) == quota.GIVE_BACK_DONE
        op.assert_awaited_once()

    async def test_a_failed_first_attempt_is_retried_after_a_rollback(self):
        db = _db()
        op = AsyncMock(side_effect=[RuntimeError("deadlock"), None])
        assert await quota._give_back(db, "release", op) == quota.GIVE_BACK_DONE
        assert op.await_count == 2
        db.rollback.assert_awaited_once()

    async def test_every_attempt_failing_is_reported_not_raised(self, caplog):
        db = _db()
        op = AsyncMock(side_effect=RuntimeError("down"))
        with caplog.at_level(logging.ERROR, logger="app.ai.quota"):
            outcome = await quota._give_back(db, "release", op)
        assert outcome == quota.GIVE_BACK_FAILED
        assert op.await_count == 2
        assert caplog.text.count("release failed") == 2

    async def test_a_non_repeatable_op_is_attempted_once(self):
        op = AsyncMock(side_effect=RuntimeError("ack lost"))
        outcome = await quota._give_back(_db(), "claim", op, attempts=1)
        assert outcome == quota.GIVE_BACK_FAILED
        op.assert_awaited_once()

    async def test_a_failing_rollback_is_swallowed(self, caplog):
        db = _db()
        db.rollback.side_effect = RuntimeError("closed")
        op = AsyncMock(side_effect=[RuntimeError("first"), None])
        with caplog.at_level(logging.ERROR, logger="app.ai.quota"):
            assert await quota._give_back(db, "release", op) == quota.GIVE_BACK_DONE
        assert "rollback after failed release" in caplog.text

    async def test_a_hung_op_times_out_and_the_session_is_rolled_back(
        self, monkeypatch
    ):
        monkeypatch.setattr(quota, "CLEANUP_TIMEOUT_SECONDS", 0.05)
        db = _db()

        async def hang():
            await asyncio.sleep(5)

        assert await quota._give_back(db, "release", hang) == quota.GIVE_BACK_TIMED_OUT
        db.rollback.assert_awaited_once()

    async def test_a_hung_rollback_after_a_timeout_is_also_bounded(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(quota, "CLEANUP_TIMEOUT_SECONDS", 0.05)
        db = _db()

        async def hang(*_a):
            await asyncio.sleep(5)

        db.rollback = hang
        with caplog.at_level(logging.ERROR, logger="app.ai.quota"):
            outcome = await quota._give_back(db, "release", hang)
        assert outcome == quota.GIVE_BACK_TIMED_OUT
        assert "rollback after timed-out release timed out" in caplog.text

    async def test_a_failing_rollback_after_a_timeout_is_logged(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(quota, "CLEANUP_TIMEOUT_SECONDS", 0.05)
        db = _db()
        db.rollback.side_effect = RuntimeError("closed")

        async def hang():
            await asyncio.sleep(5)

        with caplog.at_level(logging.ERROR, logger="app.ai.quota"):
            outcome = await quota._give_back(db, "release", hang)
        assert outcome == quota.GIVE_BACK_TIMED_OUT
        assert "rollback after timed-out release" in caplog.text


class TestQuotaHelpers:
    def test_unknown_unit_flag_is_refused(self):
        with pytest.raises(ValueError, match="unknown unit flag: nope"):
            quota._flag_column("nope")

    def test_known_flags_map_to_columns(self):
        for flag in quota.UNIT_FLAGS:
            assert quota._flag_column(flag) is getattr(quota.AIInflight, flag)

    async def test_alert_without_totals_does_nothing(self):
        db = _db()
        await quota._alert(db, MagicMock(), MagicMock(), None)
        db.execute.assert_not_awaited()
        db.commit.assert_not_awaited()


class TestBreakerTotals:
    @staticmethod
    def _db_with(row):
        result = MagicMock()
        result.one_or_none.return_value = row
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        return db

    async def test_day_without_a_row_is_all_zero(self):
        from datetime import date

        assert await breaker.totals(self._db_with(None), date(2026, 3, 1)) == (0, 0, 0)

    async def test_row_values_are_returned_as_ints(self):
        from datetime import date

        db = self._db_with(("5", 7, 3))
        assert await breaker.totals(db, date(2026, 3, 1)) == (5, 7, 3)


class TestSpecFor:
    def test_feature_outside_the_switchable_set_is_returned_untouched(self):
        spec = MagicMock()
        spec.name = "not.a.scan.feature"
        assert ai_settings.AISettings({}).spec_for(spec) is spec

    def test_provider_model_and_kill_switch_overrides_are_applied(self):
        spec = get_feature("scan.standard")
        rows = {
            "scan.standard.provider": "anthropic",
            "scan.standard.model": "claude-sonnet-5-5",
            "scan.standard.enabled": False,
        }
        out = ai_settings.AISettings(rows).spec_for(spec)
        assert (out.provider, out.model) == ("anthropic", "claude-sonnet-5-5")
        assert out.enabled is False
        # Everything else comes from the registry, untouched.
        assert out.max_tokens == spec.max_tokens

    def test_registry_rejected_model_is_ignored_but_kill_switch_still_applies(self):
        spec = get_feature("scan.standard")
        # An Anthropic model is not allowed on the OpenAI provider.
        rows = {
            "scan.standard.model": "claude-sonnet-5-5",
            "scan.standard.enabled": False,
        }
        out = ai_settings.AISettings(rows).spec_for(spec)
        assert out.model == spec.model
        assert out.provider == spec.provider
        assert out.enabled is False

    def test_defaults_leave_the_registry_spec_unchanged(self):
        spec = get_feature("scan.standard")
        assert ai_settings.AISettings({}).spec_for(spec) == spec


class TestSeedHelpers:
    def test_conflicting_defaults_refuse_to_seed(self):
        with patch.object(
            ai_settings, "_pair_problems", return_value=[("scan.standard", "bad pair")]
        ), pytest.raises(ai_settings.AISettingValueError, match="bad pair"):
            ai_settings.seed_values()

    async def test_startup_seed_logs_the_inserted_count(self, caplog):
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        with (
            patch.object(ai_settings, "_open_session", return_value=session),
            patch.object(ai_settings, "seed_ai_settings", AsyncMock(return_value=4)),
            caplog.at_level(logging.INFO, logger="app.ai.settings"),
        ):
            await ai_settings.seed_ai_settings_at_startup()
        assert "seeded 4 missing key(s)" in caplog.text

    async def test_dispose_engine_disposes_the_shared_engine(self):
        engine = MagicMock()
        engine.dispose = AsyncMock()
        with patch.object(ai_settings.database, "engine", engine):
            await ai_settings._dispose_engine()
        engine.dispose.assert_awaited_once()

    def test_switch_without_a_default_model_is_a_value_error(self):
        with patch.object(
            ai_settings,
            "default_model",
            side_effect=FeatureSpecError("no default model"),
        ), pytest.raises(ai_settings.AISettingValueError, match="no default"):
            ai_settings.validate_switch("scan.standard", "anthropic")

    def test_switch_of_an_unknown_feature_names_the_known_ones(self):
        with pytest.raises(ai_settings.AISettingValueError, match="unknown feature"):
            ai_settings.validate_switch("scan.bogus", "anthropic")


class TestScanRecordCodec:
    def _stored(self, **over):
        item = {
            k: v
            for k, v in scan_records.serialise_books(
                [EnrichedBook(title="Dune", author="Frank Herbert", confidence=0.9)]
            )[0].items()
        }
        item.update(over)
        return [item]

    def test_roundtrip(self):
        books = scan_records.deserialise_books(self._stored())
        assert books[0].title == "Dune"

    def test_a_field_with_the_wrong_type_is_a_bad_book(self):
        with pytest.raises(scan_records.ScanRecordError, match="bad book"):
            scan_records.deserialise_books(self._stored(confidence="high"))

    def test_an_unknown_field_is_a_bad_book(self):
        with pytest.raises(scan_records.ScanRecordError, match="bad book"):
            scan_records.deserialise_books(self._stored(catalog_token="forged"))

    def test_an_old_format_version_is_rejected(self):
        with pytest.raises(scan_records.ScanRecordError, match="old book format"):
            scan_records.deserialise_books(
                self._stored(**{scan_records.BOOK_FORMAT_KEY: 0})
            )

    def test_a_boolean_version_is_not_a_valid_version(self):
        with pytest.raises(scan_records.ScanRecordError, match="old book format"):
            scan_records.deserialise_books(
                self._stored(**{scan_records.BOOK_FORMAT_KEY: True})
            )

    def test_a_non_list_is_rejected(self):
        with pytest.raises(scan_records.ScanRecordError, match="bad books"):
            scan_records.deserialise_books({"title": "x"})


class TestSaveEnrichmentStep:
    async def test_failure_rolls_back_and_reraises(self):
        db = _db()
        db.execute.side_effect = RuntimeError("deadlock")
        with pytest.raises(RuntimeError):
            await scan_records.save_enrichment_step(
                db,
                user_id=MagicMock(),
                fingerprint="f" * 64,
                ai_created_at=datetime(2026, 3, 1, tzinfo=UTC),
                books=[],
            )
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()

    async def test_reports_whether_a_row_was_updated(self):
        for rowcount, expected in ((1, True), (0, False)):
            db = _db()
            db.execute.return_value = MagicMock(rowcount=rowcount)
            saved = await scan_records.save_enrichment_step(
                db,
                user_id=MagicMock(),
                fingerprint="f" * 64,
                ai_created_at=datetime(2026, 3, 1, tzinfo=UTC),
                books=[],
            )
            assert saved is expected
