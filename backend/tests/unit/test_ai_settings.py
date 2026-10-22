"""ai_settings (#526): cache, fail-closed, stale-on-error, validation, CLI,
overrides reaching run_feature, kill switches. Provider mocked; no real DB."""

import io
import logging
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai import settings as ai_settings
from app.ai.gateway import FeatureDisabledError, VisionPayload, run_feature
from app.ai.registry import FEATURES
from app.core.config import Settings
from app.main import app
from app.models.user import User

USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-000000000526"),
    email="dials@example.com",
    enhanced_scan_credits=3,
)


def _jpeg(size=(32, 24)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def _png(size) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size).save(buf, format="PNG")
    return buf.getvalue()


_JPEG = _jpeg()


def _file(data=_JPEG, name="scan.jpg", ctype="image/jpeg"):
    return {"file": (name, io.BytesIO(data), ctype)}


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    c = Clock()
    with patch.object(ai_settings, "_clock", c):
        yield c


@pytest.fixture
def empty_cache():
    """Start with nothing cached, so the next read goes to the table."""
    ai_settings.reset_cache()
    yield


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
        patch("app.ai.gateway.OpenAIVisionProvider") as provider_cls,
        patch("app.api.scan.EnrichmentService"),
        patch("app.api.scan.DeduplicationService"),
        patch("app.api.scan._reserve_daily_scan", new=AsyncMock()),
        patch("app.api.scan._refund_daily_scan", new=AsyncMock()),
    ):
        provider_cls.return_value.identify = AsyncMock(return_value=[])
        yield provider_cls


def _loader(*results):
    """A patched _load_rows returning/raising each result in turn."""
    return patch.object(ai_settings, "_load_rows", AsyncMock(side_effect=results))


# ---------------------------------------------------------------------------
# Cache TTL
# ---------------------------------------------------------------------------


class TestCacheTTL:
    async def test_reads_once_within_ttl_then_again_at_ttl(self, clock, empty_cache):
        db = MagicMock(name="db")
        first = {"scan.max_image_pixels": 2_000_000}
        second = {"scan.max_image_pixels": 3_000_000}
        with _loader(first, second) as load:
            a = await ai_settings.get_ai_settings(db)
            clock.t += 29.9
            b = await ai_settings.get_ai_settings(db)
            clock.t += 0.1  # exactly 30s after the first read
            c = await ai_settings.get_ai_settings(db)

        assert (a.max_image_pixels, b.max_image_pixels) == (2_000_000, 2_000_000)
        assert c.max_image_pixels == 3_000_000
        assert load.await_count == 2
        # The request's own session is used for the read.
        assert load.await_args_list[0].args == (db,)

    async def test_ttl_comes_from_the_setting(self, clock, empty_cache):
        with (
            patch.object(ai_settings.settings, "ai_settings_cache_ttl_seconds", 5),
            _loader({}, {}, {}) as load,
        ):
            await ai_settings.get_ai_settings(MagicMock())
            clock.t += 4.9
            await ai_settings.get_ai_settings(MagicMock())
            assert load.await_count == 1
            clock.t += 0.1
            await ai_settings.get_ai_settings(MagicMock())
            assert load.await_count == 2

    def test_default_ttl_is_30(self):
        assert Settings(database_url=_DB).ai_settings_cache_ttl_seconds == 30

    @pytest.mark.parametrize("value", [0, 301, -1])
    def test_ttl_out_of_bounds_rejected(self, value):
        with pytest.raises(ValidationError):
            Settings(database_url=_DB, ai_settings_cache_ttl_seconds=value)

    @pytest.mark.parametrize("value", [1, 300])
    def test_ttl_bounds_inclusive(self, value):
        s = Settings(database_url=_DB, ai_settings_cache_ttl_seconds=value)
        assert s.ai_settings_cache_ttl_seconds == value


class TestMaxStaleSetting:
    def test_default_is_300(self):
        assert Settings(database_url=_DB).ai_settings_max_stale_seconds == 300

    def test_read_from_env(self, monkeypatch):
        monkeypatch.setenv("AI_SETTINGS_MAX_STALE_SECONDS", "900")
        assert Settings(database_url=_DB).ai_settings_max_stale_seconds == 900

    @pytest.mark.parametrize("value", [0, -1, 3601])
    def test_out_of_bounds_rejected(self, value):
        with pytest.raises(ValidationError):
            Settings(
                database_url=_DB,
                ai_settings_cache_ttl_seconds=1,
                ai_settings_max_stale_seconds=value,
            )

    @pytest.mark.parametrize("ttl,max_stale", [(1, 1), (30, 30), (300, 3600)])
    def test_bounds_inclusive_and_equal_to_ttl_allowed(self, ttl, max_stale):
        s = Settings(
            database_url=_DB,
            ai_settings_cache_ttl_seconds=ttl,
            ai_settings_max_stale_seconds=max_stale,
        )
        assert s.ai_settings_max_stale_seconds == max_stale

    def test_below_ttl_rejected(self):
        with pytest.raises(ValidationError, match="AI_SETTINGS_MAX_STALE_SECONDS"):
            Settings(
                database_url=_DB,
                ai_settings_cache_ttl_seconds=60,
                ai_settings_max_stale_seconds=59,
            )


_DB = "postgresql+asyncpg://u:p@localhost/db"


# ---------------------------------------------------------------------------
# Fail closed / stale on error
# ---------------------------------------------------------------------------


class TestFailClosed:
    async def test_unreadable_with_empty_cache_raises(self, empty_cache):
        with (
            _loader(OSError("db down")),
            pytest.raises(ai_settings.AISettingsUnavailableError),
        ):
            await ai_settings.get_ai_settings(MagicMock())

    def test_scan_returns_503_ai_unavailable_before_upload_is_read(
        self, client, provider, empty_cache
    ):
        with (
            _loader(OSError("db down")),
            patch.object(StarletteUploadFile, "read", new=AsyncMock()) as read,
            patch("app.api.scan._verify_turnstile", new=AsyncMock()) as turnstile,
            patch.object(ai_settings.settings, "turnstile_required", True),
            patch("app.api.scan.sanitize_image") as sanitize,
        ):
            resp = client.post(
                "/scan", files=_file(), data={"cf-turnstile-response": "t"}
            )

        assert resp.status_code == 503
        assert resp.json() == {"detail": "ai_unavailable"}
        read.assert_not_awaited()
        turnstile.assert_not_awaited()
        sanitize.assert_not_called()
        provider.assert_not_called()

    async def test_run_feature_fails_closed_without_calling_provider(
        self, provider, empty_cache
    ):
        with (
            _loader(OSError("db down")),
            pytest.raises(ai_settings.AISettingsUnavailableError),
        ):
            await run_feature(
                "scan.standard",
                user=USER,
                db=MagicMock(),
                payload=VisionPayload(image_bytes=b"x"),
            )
        provider.assert_not_called()


class TestStaleOnError:
    async def test_last_value_used_when_a_later_read_fails(
        self, clock, empty_cache, caplog
    ):
        with _loader({"scan.standard.enabled": False}, OSError("db down")) as load:
            first = await ai_settings.get_ai_settings(MagicMock())
            clock.t += 31  # past the TTL, so the cache is refreshed...
            with caplog.at_level(logging.WARNING, logger="app.ai.settings"):
                second = await ai_settings.get_ai_settings(MagicMock())

        assert load.await_count == 2  # ...and that read failed
        assert first.scan_enabled is False
        assert second.scan_enabled is False  # the stale value, not the env default
        assert "read failed (OSError)" in caplog.text
        assert "db down" not in caplog.text

    async def test_stale_value_used_after_ttl_and_refreshed_on_recovery(
        self, clock, empty_cache
    ):
        rows_a = {"scan.max_image_pixels": 2_000_000}
        rows_b = {"scan.max_image_pixels": 5_000_000}
        with _loader(rows_a, OSError("x"), OSError("x"), rows_b) as load:
            await ai_settings.get_ai_settings(MagicMock())
            clock.t += 200  # past the TTL, within AI_SETTINGS_MAX_STALE_SECONDS
            assert (await ai_settings.get_ai_settings(MagicMock())).max_image_pixels
            stale = await ai_settings.get_ai_settings(MagicMock())
            fresh = await ai_settings.get_ai_settings(MagicMock())
        # Every request retries while the read fails; the first success wins.
        assert load.await_count == 4
        assert stale.max_image_pixels == 2_000_000
        assert fresh.max_image_pixels == 5_000_000

    async def test_stale_value_served_up_to_max_stale_inclusive(
        self, clock, empty_cache
    ):
        rows = {"scan.max_image_pixels": 2_000_000}
        with (
            patch.object(ai_settings.settings, "ai_settings_max_stale_seconds", 120),
            _loader(rows, OSError("x"), OSError("x")) as load,
        ):
            await ai_settings.get_ai_settings(MagicMock())
            clock.t += 119
            a = await ai_settings.get_ai_settings(MagicMock())
            clock.t += 1  # exactly max_stale old
            b = await ai_settings.get_ai_settings(MagicMock())
        assert load.await_count == 3
        assert a.max_image_pixels == b.max_image_pixels == 2_000_000

    async def test_stale_age_is_measured_after_a_slow_failed_read(
        self, clock, empty_cache
    ):
        """A read that hangs before failing must not stretch the stale window."""

        async def slow_failure(_db):
            clock.t += 30  # the failing read itself takes 30s
            raise OSError("pool timeout")

        with (
            patch.object(ai_settings.settings, "ai_settings_max_stale_seconds", 120),
            _loader({"scan.standard.enabled": False}),
        ):
            await ai_settings.get_ai_settings(MagicMock())
        clock.t += 100  # 100s old before the read, 130s old after it
        with (
            patch.object(ai_settings.settings, "ai_settings_max_stale_seconds", 120),
            patch.object(ai_settings, "_load_rows", slow_failure),
            pytest.raises(ai_settings.AISettingsUnavailableError),
        ):
            await ai_settings.get_ai_settings(MagicMock())

    async def test_past_max_stale_fails_closed(self, clock, empty_cache, caplog):
        with (
            patch.object(ai_settings.settings, "ai_settings_max_stale_seconds", 120),
            _loader({"scan.standard.enabled": False}, OSError("db down")),
        ):
            await ai_settings.get_ai_settings(MagicMock())
            clock.t += 120.5
            with (
                caplog.at_level(logging.WARNING, logger="app.ai.settings"),
                pytest.raises(ai_settings.AISettingsUnavailableError),
            ):
                await ai_settings.get_ai_settings(MagicMock())
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "failing closed" in errors[0].getMessage()
        assert "db down" not in caplog.text

    async def test_default_max_stale_is_used(self, clock, empty_cache):
        with _loader({}, OSError("x"), OSError("x")):
            await ai_settings.get_ai_settings(MagicMock())
            clock.t += 300
            await ai_settings.get_ai_settings(MagicMock())
            clock.t += 1
            with pytest.raises(ai_settings.AISettingsUnavailableError):
                await ai_settings.get_ai_settings(MagicMock())

    async def test_recovers_after_failing_closed(self, clock, empty_cache):
        rows_b = {"scan.max_image_pixels": 5_000_000}
        with _loader({}, OSError("x"), rows_b):
            await ai_settings.get_ai_settings(MagicMock())
            clock.t += 1000
            with pytest.raises(ai_settings.AISettingsUnavailableError):
                await ai_settings.get_ai_settings(MagicMock())
            fresh = await ai_settings.get_ai_settings(MagicMock())
        assert fresh.max_image_pixels == 5_000_000

    def test_scan_past_max_stale_returns_503_before_upload_is_read(
        self, client, provider, clock, empty_cache
    ):
        with _loader({}, OSError("db down")):
            assert client.post("/scan", files=_file()).status_code == 200
            provider.reset_mock()
            clock.t += 301
            with (
                patch.object(StarletteUploadFile, "read", new=AsyncMock()) as read,
                patch("app.api.scan.sanitize_image") as sanitize,
            ):
                resp = client.post("/scan", files=_file())
        assert resp.status_code == 503
        assert resp.json() == {"detail": "ai_unavailable"}
        read.assert_not_awaited()
        sanitize.assert_not_called()
        provider.assert_not_called()

    def test_scan_uses_stale_kill_switch_when_read_fails(
        self, client, provider, clock, empty_cache
    ):
        with (
            _loader({"scan.standard.enabled": False}, OSError("db down")),
            patch.object(StarletteUploadFile, "read", new=AsyncMock()) as read,
        ):
            assert client.post("/scan", files=_file()).json() == {
                "detail": "scan_disabled"
            }
            clock.t += 120
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 503
        assert resp.json() == {"detail": "scan_disabled"}
        read.assert_not_awaited()


# ---------------------------------------------------------------------------
# Kill switches read from ai_settings
# ---------------------------------------------------------------------------


class TestKillSwitchesFromTable:
    def test_scan_disabled_row_refuses_before_upload_is_read(
        self, client, provider, empty_cache
    ):
        with (
            _loader({"scan.standard.enabled": False}),
            patch.object(StarletteUploadFile, "read", new=AsyncMock()) as read,
        ):
            resp = client.post("/scan", files=_file(), data={"enhanced": "true"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "scan_disabled"}
        read.assert_not_awaited()
        provider.assert_not_called()

    def test_enhanced_disabled_row_refuses_enhanced_before_upload_is_read(
        self, client, provider, empty_cache
    ):
        with (
            _loader({"scan.enhanced.enabled": False}),
            patch.object(StarletteUploadFile, "read", new=AsyncMock()) as read,
        ):
            resp = client.post("/scan", files=_file(), data={"enhanced": "true"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "enhanced_scan_disabled"}
        read.assert_not_awaited()
        provider.assert_not_called()

    def test_enhanced_disabled_row_leaves_standard_working(
        self, client, provider, empty_cache
    ):
        with _loader({"scan.enhanced.enabled": False}):
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 200
        provider.assert_called_once_with(FEATURES["scan.standard"])

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
    def test_invalid_master_switch_fails_closed(
        self, client, provider, empty_cache, value
    ):
        with (
            _loader({"scan.standard.enabled": value}),
            patch.object(ai_settings.settings, "scan_enabled", True),
            patch.object(StarletteUploadFile, "read", new=AsyncMock()) as read,
        ):
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 503
        assert resp.json() == {"detail": "scan_disabled"}
        read.assert_not_awaited()
        provider.assert_not_called()

    @pytest.mark.parametrize("value", [0, "false", None])
    def test_invalid_enhanced_switch_fails_closed(
        self, client, provider, empty_cache, value
    ):
        with (
            _loader({"scan.enhanced.enabled": value}),
            patch.object(ai_settings.settings, "scan_enhanced_enabled", True),
        ):
            enhanced = client.post("/scan", files=_file(), data={"enhanced": "true"})
        assert enhanced.status_code == 503
        assert enhanced.json() == {"detail": "enhanced_scan_disabled"}
        provider.assert_not_called()

    def test_valid_true_switches_still_enable(self, client, provider, empty_cache):
        with _loader({"scan.standard.enabled": True, "scan.enhanced.enabled": True}):
            resp = client.post("/scan", files=_file(), data={"enhanced": "true"})
        assert resp.status_code == 200
        assert provider.call_args.args[0].name == "scan.enhanced"

    def test_row_wins_over_env(self, client, provider):
        """After seeding the row is the source of truth, not SCAN_ENABLED."""
        ai_settings.reset_cache({"scan.standard.enabled": True})
        with patch.object(ai_settings.settings, "scan_enabled", False):
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 200

    def test_missing_row_falls_back_to_env(self, client, provider):
        ai_settings.reset_cache({})
        with patch.object(ai_settings.settings, "scan_enabled", False):
            resp = client.post("/scan", files=_file())
        assert resp.json() == {"detail": "scan_disabled"}

    async def test_gateway_also_enforces_the_switch(self, provider):
        ai_settings.reset_cache({"scan.enhanced.enabled": False})
        with pytest.raises(FeatureDisabledError):
            await run_feature(
                "scan.enhanced",
                user=USER,
                db=MagicMock(),
                payload=VisionPayload(image_bytes=b"x"),
            )
        provider.assert_not_called()

    def test_master_switch_disables_enhanced_spec(self):
        current = ai_settings.AISettings({"scan.standard.enabled": False})
        assert current.spec_for(FEATURES["scan.enhanced"]).enabled is False
        assert current.feature_enabled("scan.enhanced") is False

    def test_registry_disable_cannot_be_overridden(self):
        from dataclasses import replace

        off = replace(FEATURES["scan.standard"], enabled=False)
        current = ai_settings.AISettings({"scan.standard.enabled": True})
        assert current.spec_for(off).enabled is False


# ---------------------------------------------------------------------------
# Overrides reach run_feature
# ---------------------------------------------------------------------------


class TestOverridesReachRunFeature:
    async def test_model_override_changes_the_model_used(self, provider):
        ai_settings.reset_cache({"scan.standard.model": "gpt-4o-2024-11-20"})
        result = await run_feature(
            "scan.standard",
            user=USER,
            db=MagicMock(),
            payload=VisionPayload(image_bytes=b"x"),
        )
        spec = provider.call_args.args[0]
        assert spec.model == "gpt-4o-2024-11-20"
        assert result.model == "gpt-4o-2024-11-20"
        # Only the model changed; the registry itself is untouched.
        assert spec.max_tokens == FEATURES["scan.standard"].max_tokens
        assert FEATURES["scan.standard"].model == "gpt-4o-mini"

    def test_override_reaches_the_provider_through_scan(self, client, provider):
        ai_settings.reset_cache({"scan.enhanced.model": "gpt-4o-mini"})
        resp = client.post("/scan", files=_file(), data={"enhanced": "true"})
        assert resp.status_code == 200
        assert provider.call_args.args[0].model == "gpt-4o-mini"
        assert provider.call_args.args[0].name == "scan.enhanced"

    async def test_no_override_uses_registry_spec(self, provider):
        await run_feature(
            "scan.enhanced",
            user=USER,
            db=MagicMock(),
            payload=VisionPayload(image_bytes=b"x"),
        )
        provider.assert_called_once_with(FEATURES["scan.enhanced"])


class TestReadOncePerScan:
    def test_cold_cache_scan_reads_settings_once(
        self, client, provider, clock, empty_cache
    ):
        # TTL 0 so the cache cannot hide a second read.
        with (
            patch.object(ai_settings.settings, "ai_settings_cache_ttl_seconds", 0),
            _loader({"scan.standard.model": "gpt-4o"}, {}) as load,
        ):
            resp = client.post("/scan", files=_file())
        assert resp.status_code == 200
        assert load.await_count == 1
        assert provider.call_args.args[0].model == "gpt-4o"

    async def test_run_feature_with_snapshot_skips_the_read(
        self, provider, empty_cache
    ):
        snapshot = ai_settings.AISettings({"scan.standard.model": "gpt-4o"})
        with _loader({}) as load:
            result = await run_feature(
                "scan.standard",
                user=USER,
                db=MagicMock(),
                payload=VisionPayload(image_bytes=b"x"),
                settings_snapshot=snapshot,
            )
        load.assert_not_awaited()
        assert result.model == "gpt-4o"

    async def test_run_feature_snapshot_kill_switch_enforced(self, provider):
        snapshot = ai_settings.AISettings({"scan.enhanced.enabled": False})
        with pytest.raises(FeatureDisabledError):
            await run_feature(
                "scan.enhanced",
                user=USER,
                db=MagicMock(),
                payload=VisionPayload(image_bytes=b"x"),
                settings_snapshot=snapshot,
            )
        provider.assert_not_called()

    async def test_run_feature_without_snapshot_reads(self, provider, empty_cache):
        with _loader({}) as load:
            await run_feature(
                "scan.standard",
                user=USER,
                db=MagicMock(),
                payload=VisionPayload(image_bytes=b"x"),
            )
        load.assert_awaited_once()


class TestMaxImagePixelsFeedsSanitize:
    def test_pixel_cap_from_table(self, client, provider):
        ai_settings.reset_cache({"scan.max_image_pixels": 1_000_000})
        over = client.post(
            "/scan", files=_file(_png((1001, 1000)), "s.png", "image/png")
        )
        at = client.post("/scan", files=_file(_png((1000, 1000)), "s.png", "image/png"))
        assert over.status_code == 413
        assert over.json() == {"detail": "image_too_large_pixels"}
        assert at.status_code == 200


# ---------------------------------------------------------------------------
# Validation: bounds per key, allow-list on write and on read
# ---------------------------------------------------------------------------

_VALID = {
    "scan.standard.enabled": [True, False],
    "scan.enhanced.enabled": [True, False],
    "scan.standard.provider": ["openai"],
    "scan.enhanced.provider": ["openai"],
    "scan.standard.model": ["gpt-4o-mini", "gpt-4o"],
    "scan.enhanced.model": ["gpt-4o", "gpt-4o-2024-08-06"],
    "scan.max_image_pixels": [1_000_000, 40_000_000, 100_000_000],
}
_INVALID = {
    "scan.standard.enabled": ["true", 1, 0, None, "yes"],
    "scan.enhanced.enabled": ["false", 0, None],
    "scan.standard.provider": ["anthropic", "", None, 1, "OPENAI"],
    "scan.enhanced.provider": ["gemini", None],
    "scan.standard.model": ["gpt-5", "", None, 4, "x" * 200, "gpt-4o "],
    "scan.enhanced.model": ["claude-3", None],
    "scan.max_image_pixels": [999_999, 100_000_001, 0, -1, True, 1e7, "4e7", None],
}


def test_every_key_has_bounds_cases():
    assert set(_VALID) == set(_INVALID) == set(ai_settings.KNOWN_KEYS)


def test_initial_key_set():
    assert set(ai_settings.KNOWN_KEYS) == {
        "scan.standard.enabled",
        "scan.enhanced.enabled",
        "scan.standard.provider",
        "scan.enhanced.provider",
        "scan.standard.model",
        "scan.enhanced.model",
        "scan.max_image_pixels",
    }


class TestBounds:
    @pytest.mark.parametrize(
        "key,value", [(k, v) for k, vs in _VALID.items() for v in vs]
    )
    def test_valid(self, key, value):
        assert ai_settings.validate_write(key, value, {}) == value

    @pytest.mark.parametrize(
        "key,value", [(k, v) for k, vs in _INVALID.items() for v in vs]
    )
    def test_invalid_rejected_on_write(self, key, value):
        with pytest.raises(ai_settings.AISettingValueError):
            ai_settings.validate_write(key, value, {})

    @pytest.mark.parametrize(
        "key,value",
        [(k, v) for k, vs in _INVALID.items() for v in vs if not k.endswith("enabled")],
    )
    def test_invalid_ignored_on_read(self, key, value):
        with patch.object(ai_settings.sentry_sdk, "capture_message") as capture:
            ai_settings.reset_cache({key: value})
        current = ai_settings.AISettings(ai_settings._cache.rows)
        assert key not in current.rows
        assert current.get(key) == ai_settings.DEFINITIONS[key].default()
        capture.assert_called_once()
        assert capture.call_args.kwargs == {"level": "warning"}

    @pytest.mark.parametrize(
        "key,value",
        [(k, v) for k, vs in _INVALID.items() for v in vs if k.endswith("enabled")],
    )
    def test_invalid_switch_treated_as_disabled_on_read(self, key, value, caplog):
        with (
            patch.object(ai_settings.sentry_sdk, "capture_message") as capture,
            caplog.at_level(logging.WARNING, logger="app.ai.settings"),
        ):
            ai_settings.reset_cache({key: value})
        assert ai_settings._cache.rows[key] is False
        capture.assert_called_once()
        assert capture.call_args.kwargs == {"level": "warning"}
        assert "treated as disabled" in capture.call_args.args[0]
        assert "treated as disabled" in caplog.text
        # The stored value is not echoed.
        assert repr(value) not in capture.call_args.args[0]

    def test_unknown_key_rejected_on_write(self):
        with pytest.raises(ai_settings.AISettingValueError, match="unknown key"):
            ai_settings.validate_write("scan.premium.enabled", True, {})

    def test_unknown_key_ignored_on_read(self):
        ai_settings.reset_cache({"scan.premium.model": "gpt-5"})
        assert dict(ai_settings._cache.rows) == {}


_TWO_PROVIDERS = {
    "openai": frozenset({"gpt-4o-mini", "gpt-4o"}),
    "other": frozenset({"other-1"}),
}


class TestAllowList:
    async def test_model_outside_allow_list_ignored_on_read_with_warning(
        self, provider, caplog
    ):
        with (
            patch.object(ai_settings.sentry_sdk, "capture_message") as capture,
            caplog.at_level(logging.WARNING, logger="app.ai.settings"),
        ):
            ai_settings.reset_cache({"scan.enhanced.model": "gpt-5-turbo-max"})
        await run_feature(
            "scan.enhanced",
            user=USER,
            db=MagicMock(),
            payload=VisionPayload(image_bytes=b"x"),
        )
        assert provider.call_args.args[0].model == FEATURES["scan.enhanced"].model
        assert "ignoring scan.enhanced.model" in caplog.text
        # The stored value is never echoed into logs or Sentry.
        assert "gpt-5-turbo-max" not in caplog.text
        assert "gpt-5-turbo-max" not in str(capture.call_args)

    def test_model_must_match_its_provider_on_write(self):
        with (
            patch.object(ai_settings, "ALLOWED_MODELS", _TWO_PROVIDERS),
            patch.object(ai_settings, "_ALL_MODELS", frozenset({"other-1", "gpt-4o"})),
        ):
            # other-1 is allow-listed, but not for the registry's provider.
            with pytest.raises(ai_settings.AISettingValueError, match="allow-list"):
                ai_settings.validate_write("scan.standard.model", "other-1", {})
            # Switching provider while the model is an OpenAI one is refused.
            with pytest.raises(ai_settings.AISettingValueError, match="allow-list"):
                ai_settings.validate_write("scan.standard.provider", "other", {})
            # Allowed once the pair matches.
            assert (
                ai_settings.validate_write(
                    "scan.standard.provider",
                    "other",
                    {"scan.standard.model": "other-1"},
                )
                == "other"
            )

    def test_mismatched_pair_ignored_on_read(self):
        with (
            patch.object(ai_settings, "ALLOWED_MODELS", _TWO_PROVIDERS),
            patch.object(ai_settings, "_ALL_MODELS", frozenset({"other-1", "gpt-4o"})),
        ):
            # Model valid for openai but provider says "other": the model is
            # dropped, and the registry default (gpt-4o-mini) is not allowed
            # for "other" either, so the provider falls back too.
            ai_settings.reset_cache(
                {"scan.standard.provider": "other", "scan.standard.model": "gpt-4o"}
            )
            rows = dict(ai_settings._cache.rows)
        assert rows == {}

    def test_spec_for_falls_back_when_registry_rejects(self, caplog):
        current = ai_settings.AISettings({"scan.standard.model": "not-a-model"})
        with caplog.at_level(logging.WARNING, logger="app.ai.settings"):
            spec = current.spec_for(FEATURES["scan.standard"])
        assert spec == FEATURES["scan.standard"]
        assert "rejected by the registry" in caplog.text


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


class TestSeeding:
    def test_seed_values_come_from_env_and_registry(self):
        with (
            patch.object(ai_settings.settings, "scan_enabled", False),
            patch.object(ai_settings.settings, "scan_enhanced_enabled", True),
            patch.object(ai_settings.settings, "scan_max_image_pixels", 2_000_000),
        ):
            values = ai_settings.seed_values()
        assert values == {
            "scan.standard.enabled": False,
            "scan.enhanced.enabled": True,
            "scan.standard.provider": "openai",
            "scan.enhanced.provider": "openai",
            "scan.standard.model": FEATURES["scan.standard"].model,
            "scan.enhanced.model": FEATURES["scan.enhanced"].model,
            "scan.max_image_pixels": 2_000_000,
        }

    async def test_seed_is_insert_if_missing(self):
        db = AsyncMock()
        db.execute.return_value = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = ["k"]
        assert await ai_settings.seed_ai_settings(db) == 1
        sql = str(db.execute.await_args.args[0].compile(dialect=_pg()))
        assert "INSERT INTO ai_settings" in sql
        assert "ON CONFLICT (key) DO NOTHING" in sql
        db.commit.assert_awaited_once()

    async def test_startup_seed_never_raises_when_db_unreachable(self, caplog):
        with (
            patch.object(ai_settings, "_open_session", side_effect=OSError("nope")),
            caplog.at_level(logging.WARNING, logger="app.ai.settings"),
        ):
            await ai_settings.seed_ai_settings_at_startup()
        assert "seeding skipped (OSError)" in caplog.text

    async def test_startup_seed_times_out_instead_of_hanging(self, caplog):
        import asyncio

        async def _hang(*_a, **_k):
            await asyncio.sleep(3600)

        session = AsyncMock()
        session.execute = _hang

        @asynccontextmanager
        async def _session():
            yield session

        with (
            patch.object(ai_settings, "_open_session", _session),
            patch.object(ai_settings, "SEED_TIMEOUT_SECONDS", 0.01),
            caplog.at_level(logging.WARNING, logger="app.ai.settings"),
        ):
            await ai_settings.seed_ai_settings_at_startup()
        assert "seeding skipped (TimeoutError)" in caplog.text

    def test_app_starts_when_seeding_fails(self):
        """TestClient startup (lifespan) with an unreachable DB still works."""
        with (
            patch.object(ai_settings, "_open_session", side_effect=OSError("nope")),
            patch("app.main.cleanup_expired_tokens", new=AsyncMock()),
            TestClient(app) as c,
        ):
            assert c.get("/health").status_code == 200

    def test_lifespan_calls_the_seed(self):
        with (
            patch("app.main.seed_ai_settings_at_startup", new=AsyncMock()) as seed,
            patch("app.main.cleanup_expired_tokens", new=AsyncMock()),
            TestClient(app),
        ):
            pass
        seed.assert_awaited_once()


def _pg():
    from sqlalchemy.dialects import postgresql

    return postgresql.dialect()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class FakeRow:
    def __init__(self, key, value):
        self.key, self.value = key, value
        self.updated_by = "seed"
        from datetime import UTC, datetime

        self.updated_at = datetime(2026, 10, 22, tzinfo=UTC)


@pytest.fixture
def cli_db():
    """A fake session for the CLI; ``stored`` is what read_rows returns."""
    db = AsyncMock()
    db.stored = {}

    @asynccontextmanager
    async def _session():
        yield db

    async def _read_rows(_db):
        return db.stored

    with (
        patch.object(ai_settings, "_open_session", _session),
        patch.object(ai_settings, "read_rows", _read_rows),
        patch.object(ai_settings, "_dispose_engine", new=AsyncMock()),
    ):
        yield db


def _written(db) -> dict:
    stmt = db.execute.await_args.args[0]
    return stmt.compile(dialect=_pg()).params


class TestCLI:
    def test_set_validates_and_writes_value_and_actor(self, cli_db, capsys):
        code = ai_settings.main(
            ["set", "scan.max_image_pixels", "25000000", "--by", "will"]
        )
        assert code == 0
        params = _written(cli_db)
        assert params["key"] == "scan.max_image_pixels"
        assert params["value"] == 25_000_000
        assert params["updated_by"] == "will"
        sql = str(cli_db.execute.await_args.args[0].compile(dialect=_pg()))
        assert "ON CONFLICT (key) DO UPDATE" in sql
        assert "updated_at" in sql
        cli_db.commit.assert_awaited_once()
        assert "25000000" in capsys.readouterr().out

    def test_set_actor_defaults_to_user_then_cli(self, cli_db, monkeypatch):
        monkeypatch.setenv("USER", "ops-person")
        assert ai_settings.main(["set", "scan.standard.enabled", "false"]) == 0
        assert _written(cli_db)["updated_by"] == "ops-person"
        assert _written(cli_db)["value"] is False

        monkeypatch.delenv("USER")
        assert ai_settings.main(["set", "scan.enhanced.model", "gpt-4o"]) == 0
        assert _written(cli_db)["updated_by"] == "cli"
        assert _written(cli_db)["value"] == "gpt-4o"

    @pytest.mark.parametrize(
        "argv",
        [
            ["set", "scan.max_image_pixels", "999999"],
            ["set", "scan.max_image_pixels", "abc"],
            ["set", "scan.standard.enabled", "yes"],
            ["set", "scan.enhanced.model", "gpt-5"],
            ["set", "scan.standard.provider", "anthropic"],
        ],
    )
    def test_set_rejects_bad_value_without_touching_db(self, cli_db, argv, capsys):
        assert ai_settings.main(argv) == 2
        cli_db.execute.assert_not_awaited()
        assert "error:" in capsys.readouterr().err

    def test_set_rejects_unknown_key(self, cli_db):
        with pytest.raises(SystemExit) as exc:
            ai_settings.main(["set", "scan.premium.enabled", "true"])
        assert exc.value.code != 0
        cli_db.execute.assert_not_awaited()

    def test_set_rejects_bad_actor(self, cli_db):
        assert ai_settings.main(["set", "scan.standard.enabled", "true", "--by", " "])
        cli_db.execute.assert_not_awaited()

    def test_db_error_exits_nonzero(self, cli_db, capsys):
        from sqlalchemy.exc import OperationalError

        cli_db.execute.side_effect = OperationalError("x", {}, Exception("down"))
        assert ai_settings.main(["set", "scan.standard.enabled", "true"]) == 1
        assert "could not use the database" in capsys.readouterr().err

    def test_get_and_list(self, cli_db, capsys):
        cli_db.stored = {
            "scan.enhanced.model": FakeRow("scan.enhanced.model", "gpt-4o-mini"),
            "scan.max_image_pixels": FakeRow("scan.max_image_pixels", 5),
        }
        assert ai_settings.main(["get", "scan.enhanced.model"]) == 0
        out = capsys.readouterr().out
        assert 'scan.enhanced.model = "gpt-4o-mini"' in out
        assert "by seed" in out

        assert ai_settings.main(["list"]) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == len(ai_settings.KNOWN_KEYS)
        joined = "\n".join(lines)
        assert "scan.max_image_pixels = 40000000  (stored value is INVALID" in joined
        assert "scan.standard.enabled = true  (no row" in joined

    def test_invalid_switch_described_as_disabled(self, cli_db, capsys):
        cli_db.stored = {
            "scan.enhanced.enabled": FakeRow("scan.enhanced.enabled", "false"),
        }
        assert ai_settings.main(["get", "scan.enhanced.enabled"]) == 0
        out = capsys.readouterr().out
        assert (
            "scan.enhanced.enabled = false  (stored value is INVALID; treated as"
            in (out)
        )

    def test_updated_by_control_characters_are_replaced(self, cli_db, capsys):
        row = FakeRow("scan.enhanced.model", "gpt-4o")
        row.updated_by = "ev\x1b[2Jil\nx"
        cli_db.stored = {"scan.enhanced.model": row}
        assert ai_settings.main(["get", "scan.enhanced.model"]) == 0
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "by ev?[2Jil?x" in out
        assert out.count("\n") == 1

    def test_parse_cli_value(self):
        assert ai_settings.parse_cli_value("true") is True
        assert ai_settings.parse_cli_value("40000000") == 40_000_000
        assert ai_settings.parse_cli_value("gpt-4o") == "gpt-4o"
        assert ai_settings.parse_cli_value('"gpt-4o"') == "gpt-4o"


class TestLoadRowsTransactionChoice:
    """The short-transaction path is only for an empty session: a failed
    outermost rollback would expire every object the session holds."""

    def _session(self, *, in_tx, identity_map, pending=False):
        db = MagicMock()
        db.in_transaction.return_value = in_tx
        db.identity_map = identity_map
        db.new = [object()] if pending else []
        db.dirty = []
        db.deleted = []
        result = MagicMock()
        result.all.return_value = [("scan.standard.enabled", True)]
        db.execute = AsyncMock(return_value=result)
        db.begin = MagicMock(return_value=AsyncMock())
        db.begin_nested = MagicMock(return_value=AsyncMock())
        return db

    async def test_empty_session_uses_a_short_transaction(self):
        db = self._session(in_tx=False, identity_map={})
        await ai_settings._load_rows(db)
        db.begin.assert_called_once()
        db.begin_nested.assert_not_called()

    async def test_session_holding_objects_uses_a_savepoint(self):
        db = self._session(in_tx=False, identity_map={"k": object()})
        await ai_settings._load_rows(db)
        db.begin_nested.assert_called_once()
        db.begin.assert_not_called()

    async def test_open_transaction_uses_a_savepoint(self):
        db = self._session(in_tx=True, identity_map={})
        await ai_settings._load_rows(db)
        db.begin_nested.assert_called_once()
        db.begin.assert_not_called()

    async def test_pending_changes_use_a_savepoint(self):
        db = self._session(in_tx=False, identity_map={}, pending=True)
        await ai_settings._load_rows(db)
        db.begin_nested.assert_called_once()
        db.begin.assert_not_called()
