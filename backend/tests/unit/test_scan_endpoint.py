"""Unit tests for POST /scan — services mocked, no real DB or HTTP."""

import io
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.ai import quota
from app.ai.providers.base import ProviderResult
from app.ai.registry import FEATURES
from app.main import app
from app.models.user import User
from app.schemas.book import EditionPreview, EnrichedBook
from app.services.book_identifier import BookCandidate, ScanUnavailableError

FAKE_USER = User(id="00000000-0000-0000-0000-000000000001", email="test@example.com")

ENRICHED_BOOK = EnrichedBook(
    open_library_work_id="OL45804W",
    google_books_id="gb_dune_001",
    title="Dune",
    author="Frank Herbert",
    confidence=0.97,
    already_in_library=False,
    editions=[EditionPreview(isbn_13="9780441013593")],
)


def _real_jpeg() -> bytes:
    # A real, decodable JPEG: since #529 an undecodable upload is rejected
    # with 415 image_undecodable instead of being passed through.
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (200, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _real_jpeg()


def _image_file(size_bytes: int = 100, content_type: str = "image/jpeg"):
    # A real JPEG, padded after its end marker to the requested size.
    padding = b"x" * max(0, size_bytes - len(_JPEG))
    return ("scan.jpg", io.BytesIO(_JPEG + padding), content_type)


@pytest.fixture(autouse=True)
def _allow_daily_scan_quota():
    """These tests use a fake DB; the daily-cap store is covered separately
    (test_scan_daily_caps.py, integration/test_scan_quota_db.py)."""
    with (
        patch("app.api.scan._reserve_scan_quota", new=AsyncMock()),
        patch("app.api.scan._refund_scan_quota", new=AsyncMock()),
        patch("app.api.scan._release_scan_lease", new=AsyncMock()),
    ):
        yield


@pytest.fixture
def client():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    async def _fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: FAKE_USER
    app.dependency_overrides[get_db] = _fake_db

    yield TestClient(app)

    app.dependency_overrides.clear()


class TestScanEndpoint:
    def test_returns_401_without_auth(self):
        client = TestClient(app)
        resp = client.post("/scan", files={"file": _image_file()})
        assert resp.status_code == 401

    def test_returns_415_for_non_image(self, client):
        resp = client.post(
            "/scan", files={"file": ("doc.pdf", io.BytesIO(b"pdf"), "application/pdf")}
        )
        assert resp.status_code == 415

    def test_returns_413_for_oversized_file(self, client):
        big = 5 * 1024 * 1024 + 1  # 1 byte over 5MB
        resp = client.post("/scan", files={"file": _image_file(size_bytes=big)})
        assert resp.status_code == 413

    def test_returns_503_when_vision_service_unavailable(self, client):
        with patch("app.ai.gateway.OpenAIVisionProvider") as mock_id_cls:
            mock_id_cls.return_value.identify = AsyncMock(
                side_effect=ScanUnavailableError("timeout")
            )
            resp = client.post("/scan", files={"file": _image_file()})

        assert resp.status_code == 503
        assert resp.json()["detail"] == "scan_unavailable"

    def test_returns_empty_list_when_no_candidates(self, client):
        with (
            patch("app.ai.gateway.OpenAIVisionProvider") as mock_id_cls,
            patch("app.api.scan.EnrichmentService"),
            patch("app.api.scan.DeduplicationService"),
        ):
            mock_id_cls.return_value.identify = AsyncMock(
                return_value=ProviderResult([])
            )
            resp = client.post("/scan", files={"file": _image_file()})

        assert resp.status_code == 200
        assert resp.json() == {
            "books": [],
            "enhanced": False,
            # #533: replaces enhanced_scan_credits; None for non-Premium.
            "enhanced_scans_remaining": None,
        }

    def test_returns_enriched_candidates(self, client):
        candidate = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.97)

        with (
            patch("app.ai.gateway.OpenAIVisionProvider") as mock_id_cls,
            patch("app.api.scan.EnrichmentService") as mock_enrich_cls,
            patch("app.api.scan.DeduplicationService") as mock_dedup_cls,
        ):
            mock_id_cls.return_value.identify = AsyncMock(
                return_value=ProviderResult([candidate])
            )
            mock_enrich_cls.return_value.enrich = AsyncMock(
                return_value=[ENRICHED_BOOK]
            )
            mock_dedup_cls.return_value.check = AsyncMock(return_value=[ENRICHED_BOOK])

            resp = client.post("/scan", files={"file": _image_file()})

        assert resp.status_code == 200
        data = resp.json()["books"]
        assert len(data) == 1
        assert data[0]["title"] == "Dune"
        assert data[0]["author"] == "Frank Herbert"
        assert data[0]["open_library_work_id"] == "OL45804W"

    def test_returns_every_book_in_a_multi_book_photo(self, client):
        candidates = [
            BookCandidate(title=f"Book {i}", author="Author", confidence=0.9)
            for i in range(4)
        ]
        books = [
            ENRICHED_BOOK.model_copy(
                update={
                    "title": f"Book {i}",
                    "open_library_work_id": f"OL{i}W",
                    "google_books_id": f"gb_{i}",
                }
            )
            for i in range(4)
        ]

        with (
            patch("app.ai.gateway.OpenAIVisionProvider") as mock_id_cls,
            patch("app.api.scan.EnrichmentService") as mock_enrich_cls,
            patch("app.api.scan.DeduplicationService") as mock_dedup_cls,
        ):
            mock_id_cls.return_value.identify = AsyncMock(
                return_value=ProviderResult(candidates)
            )
            mock_enrich_cls.return_value.enrich = AsyncMock(return_value=books)
            mock_dedup_cls.return_value.check = AsyncMock(
                side_effect=lambda _db, _u, b: b
            )

            resp = client.post("/scan", files={"file": _image_file()})

        assert resp.status_code == 200
        assert [b["title"] for b in resp.json()["books"]] == [
            "Book 0",
            "Book 1",
            "Book 2",
            "Book 3",
        ]
        # Enrichment must not be capped at the single-book limit of 3.
        _, kwargs = mock_enrich_cls.return_value.enrich.call_args
        assert kwargs["limit"] == 15
        mock_id_cls.assert_called_once_with(FEATURES["scan.standard"])

    def test_drops_books_that_enrich_to_the_same_work(self, client):
        dup = ENRICHED_BOOK.model_copy(update={"title": "Dune (Deluxe)"})
        with (
            patch("app.ai.gateway.OpenAIVisionProvider") as mock_id_cls,
            patch("app.api.scan.EnrichmentService") as mock_enrich_cls,
            patch("app.api.scan.DeduplicationService") as mock_dedup_cls,
        ):
            mock_id_cls.return_value.identify = AsyncMock(
                return_value=ProviderResult(
                    [
                        BookCandidate(
                            title="Dune", author="Frank Herbert", confidence=0.9
                        )
                    ]
                )
            )
            mock_enrich_cls.return_value.enrich = AsyncMock(
                return_value=[ENRICHED_BOOK, dup]
            )
            mock_dedup_cls.return_value.check = AsyncMock(
                side_effect=lambda _db, _u, b: b
            )

            resp = client.post("/scan", files={"file": _image_file()})

        assert [b["title"] for b in resp.json()["books"]] == ["Dune"]


def _plain_user() -> User:
    return User(id="00000000-0000-0000-0000-000000000002", email="u@example.com")


_DUNE = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.9)


class TestEnhancedScan:
    """Wiring of the #533 charge decision in the handler. The quota steps
    themselves (Premium-only refusal, enhanced unit, zero-result record) are
    covered in test_scan_quota_refusals.py, test_enhanced_quota.py and the
    integration tests."""

    @pytest.fixture
    def calls(self):
        """Patch the settle and the remaining-allowance read; record the order
        of enrichment and settle."""
        order: list[str] = []
        release = AsyncMock(side_effect=lambda *a, **k: order.append("settle"))
        with (
            patch("app.api.scan._release_scan_lease", new=release),
            patch(
                "app.api.scan.quota.enhanced_remaining",
                new=AsyncMock(return_value=4),
            ) as remaining,
            patch("app.ai.gateway.OpenAIVisionProvider") as provider_cls,
            patch("app.api.scan.EnrichmentService") as enrich_cls,
            patch("app.api.scan.DeduplicationService") as dedup_cls,
        ):

            async def enrich(candidates, limit):
                order.append("enrich")
                return [ENRICHED_BOOK] if candidates else []

            enrich_cls.return_value.enrich = AsyncMock(side_effect=enrich)
            dedup_cls.return_value.check = AsyncMock(side_effect=lambda _d, _u, b: b)
            yield {
                "order": order,
                "release": release,
                "remaining": remaining,
                "provider_cls": provider_cls,
                "enrich": enrich_cls.return_value.enrich,
            }

    def _post(self, client, enhanced=True):
        data = {"enhanced": "true"} if enhanced else {}
        return client.post("/scan", files={"file": _image_file()}, data=data)

    @staticmethod
    def _zero_result(release):
        args = release.await_args.args
        return args[4] if len(args) > 4 else None

    def test_uses_enhanced_model_and_keeps_the_unit_when_books_found(
        self, client, calls
    ):
        calls["provider_cls"].return_value.identify = AsyncMock(
            return_value=ProviderResult([_DUNE])
        )
        resp = self._post(client)
        assert resp.status_code == 200
        body = resp.json()
        assert body["enhanced"] is True
        assert body["enhanced_scans_remaining"] == 4
        assert "enhanced_scan_credits" not in body
        calls["provider_cls"].assert_called_once_with(FEATURES["scan.enhanced"])
        # Books to hand back: no zero-result decision (the unit is kept).
        assert self._zero_result(calls["release"]) is None
        # Enriched under the lease, before the settle.
        assert calls["order"] == ["enrich", "settle"]

    def test_enhanced_scan_finding_nothing_settles_a_zero_result(self, client, calls):
        calls["provider_cls"].return_value.identify = AsyncMock(
            return_value=ProviderResult([])
        )
        resp = self._post(client)
        assert resp.status_code == 200
        assert resp.json()["books"] == []
        zero = self._zero_result(calls["release"])
        assert isinstance(zero, quota.ZeroResult)
        assert zero.ledger_id is not None  # the usage row's call_id

    def test_enrichment_yielding_nothing_is_a_zero_result(self, client, calls):
        calls["provider_cls"].return_value.identify = AsyncMock(
            return_value=ProviderResult([_DUNE])
        )
        calls["enrich"].side_effect = (
            lambda c, limit: calls["order"].append("enrich") or []
        )
        resp = self._post(client)
        assert resp.json()["books"] == []
        assert isinstance(self._zero_result(calls["release"]), quota.ZeroResult)
        assert calls["order"] == ["enrich", "settle"]

    def test_standard_scan_never_settles_a_zero_result(self, client, calls):
        calls["provider_cls"].return_value.identify = AsyncMock(
            return_value=ProviderResult([])
        )
        resp = self._post(client, enhanced=False)
        assert resp.status_code == 200
        assert self._zero_result(calls["release"]) is None

    def test_standard_scan_enriches_after_the_settle(self, client, calls):
        calls["provider_cls"].return_value.identify = AsyncMock(
            return_value=ProviderResult([_DUNE])
        )
        self._post(client, enhanced=False)
        assert calls["order"] == ["settle", "enrich"]

    def test_provider_error_is_not_a_zero_result(self, client, calls):
        calls["provider_cls"].return_value.identify = AsyncMock(
            side_effect=ScanUnavailableError("down")
        )
        assert self._post(client).status_code == 503
        assert self._zero_result(calls["release"]) is None
        calls["enrich"].assert_not_awaited()

    def test_enrichment_error_is_not_a_zero_result(self, client, calls):
        calls["provider_cls"].return_value.identify = AsyncMock(
            return_value=ProviderResult([_DUNE])
        )
        calls["enrich"].side_effect = RuntimeError("lookup crashed")
        with pytest.raises(RuntimeError):
            self._post(client)
        assert self._zero_result(calls["release"]) is None

    def test_allowance_read_failure_does_not_fail_the_scan(self, client, calls):
        calls["provider_cls"].return_value.identify = AsyncMock(
            return_value=ProviderResult([_DUNE])
        )
        calls["remaining"].side_effect = RuntimeError("db blip")
        resp = self._post(client)
        assert resp.status_code == 200
        assert resp.json()["enhanced_scans_remaining"] is None


class TestTurnstileProtection:
    """When TURNSTILE_SECRET_KEY is configured, /scan requires a valid Turnstile token."""

    @pytest.fixture
    def client_with_turnstile(self):
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        async def _fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = lambda: FAKE_USER
        app.dependency_overrides[get_db] = _fake_db

        with patch("app.api.scan.settings") as mock_settings:
            mock_settings.rate_limit_scan = "10/minute"
            mock_settings.turnstile_required = True
            mock_settings.turnstile_secret_key = (
                "0x0000000000000000000000000000000000000000"
            )
            mock_settings.clamav_enabled = False
            yield TestClient(app), mock_settings

        app.dependency_overrides.clear()

    def test_returns_400_when_token_missing(self, client_with_turnstile):
        client, _ = client_with_turnstile
        resp = client.post("/scan", files={"file": _image_file()})
        assert resp.status_code == 400
        assert "Turnstile" in resp.json()["detail"]

    def test_returns_403_when_token_invalid(self, client_with_turnstile):
        client, _ = client_with_turnstile
        with patch("app.api.scan._verify_turnstile", new=AsyncMock(return_value=False)):
            resp = client.post(
                "/scan",
                files={"file": _image_file()},
                data={"cf-turnstile-response": "bad-token"},
            )
        assert resp.status_code == 403
        assert "Turnstile" in resp.json()["detail"]

    def test_proceeds_when_token_valid(self, client_with_turnstile):
        client, _ = client_with_turnstile
        with (
            patch("app.api.scan._verify_turnstile", new=AsyncMock(return_value=True)),
            patch("app.ai.gateway.OpenAIVisionProvider") as mock_id_cls,
            patch("app.api.scan.EnrichmentService"),
            patch("app.api.scan.DeduplicationService"),
        ):
            mock_id_cls.return_value.identify = AsyncMock(
                return_value=ProviderResult([])
            )
            resp = client.post(
                "/scan",
                files={"file": _image_file()},
                data={"cf-turnstile-response": "valid-token"},
            )
        assert resp.status_code == 200

    def test_skipped_when_turnstile_disabled(self):
        """When TURNSTILE_REQUIRED=false, the check is skipped entirely."""
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        async def _fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = lambda: FAKE_USER
        app.dependency_overrides[get_db] = _fake_db

        with (
            patch("app.api.scan.settings") as mock_settings,
            patch("app.ai.gateway.OpenAIVisionProvider") as mock_id_cls,
            patch("app.api.scan.EnrichmentService"),
            patch("app.api.scan.DeduplicationService"),
        ):
            mock_settings.rate_limit_scan = "10/minute"
            mock_settings.turnstile_required = False  # explicitly disabled
            mock_settings.clamav_enabled = False
            mock_id_cls.return_value.identify = AsyncMock(
                return_value=ProviderResult([])
            )
            client = TestClient(app)
            resp = client.post("/scan", files={"file": _image_file()})

        app.dependency_overrides.clear()
        assert resp.status_code == 200


def _png_with_declared_size(width: int, height: int) -> bytes:
    """A tiny PNG whose header claims ``width`` x ``height`` pixels."""
    import struct
    import zlib

    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG")
    png = bytearray(buf.getvalue())
    png[16:24] = struct.pack(">II", width, height)
    png[29:33] = struct.pack(">I", zlib.crc32(bytes(png[12:29])) & 0xFFFFFFFF)
    return bytes(png)


class TestImageNormalisationOnScan:
    """#529: decode-or-reject and the pixel cap run before credits and caps."""

    @pytest.fixture
    def reserve(self):
        with patch("app.api.scan._reserve_scan_quota", new=AsyncMock()) as r:
            yield r

    def test_undecodable_image_returns_415_without_using_a_scan(self, client, reserve):
        fake = b"\xff\xd8\xff\xe0" + b"x" * 200  # JPEG magic, no image
        with patch("app.ai.gateway.OpenAIVisionProvider") as provider_cls:
            resp = client.post(
                "/scan", files={"file": ("s.jpg", io.BytesIO(fake), "image/jpeg")}
            )
        assert resp.status_code == 415
        assert resp.json() == {"detail": "image_undecodable"}
        reserve.assert_not_awaited()
        provider_cls.assert_not_called()

    def test_pixel_bomb_returns_413_without_using_a_scan(self, client, reserve):
        bomb = _png_with_declared_size(9000, 9000)  # 81 MP declared
        with patch("app.ai.gateway.OpenAIVisionProvider") as provider_cls:
            resp = client.post(
                "/scan", files={"file": ("s.png", io.BytesIO(bomb), "image/png")}
            )
        assert resp.status_code == 413
        assert resp.json() == {"detail": "image_too_large_pixels"}
        reserve.assert_not_awaited()
        provider_cls.assert_not_called()

    def test_enhanced_scan_bad_image_does_not_check_credits_first(self, reserve):
        """Normalisation runs before the enhanced entitlement check too (a Free
        user, #533): 415, not 403."""
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        async def _fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = lambda: _plain_user()
        app.dependency_overrides[get_db] = _fake_db
        try:
            resp = TestClient(app).post(
                "/scan",
                files={
                    "file": ("s.jpg", io.BytesIO(b"\xff\xd8\xff\xe0xx"), "image/jpeg")
                },
                data={"enhanced": "true"},
            )
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 415
        reserve.assert_not_awaited()

    def test_provider_receives_normalised_jpeg_not_original(self, client, reserve):
        img = Image.new("RGB", (3000, 1500), (20, 40, 60))
        exif = Image.Exif()
        exif[0x010F] = "OriginalCamera"
        buf = io.BytesIO()
        img.save(buf, format="PNG", exif=exif.tobytes())
        original = buf.getvalue()

        with (
            patch("app.ai.gateway.OpenAIVisionProvider") as provider_cls,
            patch("app.api.scan.EnrichmentService"),
            patch("app.api.scan.DeduplicationService"),
        ):
            provider_cls.return_value.identify = AsyncMock(
                return_value=ProviderResult([])
            )
            resp = client.post(
                "/scan",
                files={"file": ("s.png", io.BytesIO(original), "image/png")},
            )

        assert resp.status_code == 200
        reserve.assert_awaited_once()
        sent = provider_cls.return_value.identify.await_args.args[0]
        assert sent != original
        assert sent[:3] == b"\xff\xd8\xff"
        assert b"OriginalCamera" not in sent
        out = Image.open(io.BytesIO(sent))
        assert out.format == "JPEG"
        assert out.size == (1568, 784)
        assert len(out.getexif()) == 0

    def test_long_edge_follows_the_enhanced_feature_spec(self, reserve):
        """The long-edge cap is read from the registry for the chosen feature."""
        from dataclasses import replace

        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        async def _fake_db():
            yield AsyncMock()

        specs = {
            "scan.standard": FEATURES["scan.standard"],
            "scan.enhanced": replace(
                FEATURES["scan.enhanced"], max_image_long_edge_px=512
            ),
        }
        buf = io.BytesIO()
        Image.new("RGB", (2000, 1000)).save(buf, format="JPEG")
        app.dependency_overrides[get_current_user] = lambda: _plain_user()
        app.dependency_overrides[get_db] = _fake_db
        try:
            with (
                patch("app.api.scan.get_feature", side_effect=specs.__getitem__),
                patch("app.ai.gateway.OpenAIVisionProvider") as provider_cls,
            ):
                provider_cls.return_value.identify = AsyncMock(
                    return_value=ProviderResult([])
                )
                resp = TestClient(app).post(
                    "/scan",
                    files={"file": ("s.jpg", io.BytesIO(buf.getvalue()), "image/jpeg")},
                    data={"enhanced": "true"},
                )
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 200
        sent = provider_cls.return_value.identify.await_args.args[0]
        assert Image.open(io.BytesIO(sent)).size == (512, 256)


class TestNormalisationRunsOffTheEventLoop:
    """Normalisation runs in a worker thread, gated by a capacity limiter."""

    @pytest.fixture
    def provider(self):
        with (
            patch("app.ai.gateway.OpenAIVisionProvider") as provider_cls,
            patch("app.api.scan.EnrichmentService"),
            patch("app.api.scan.DeduplicationService"),
        ):
            provider_cls.return_value.identify = AsyncMock(
                return_value=ProviderResult([])
            )
            yield provider_cls

    def test_sanitize_image_goes_through_run_sync_with_the_limiter(
        self, client, provider
    ):
        import anyio.to_thread

        from app.ai import settings as ai_settings
        from app.api import scan as scan_module

        real_run_sync = anyio.to_thread.run_sync
        with patch.object(
            scan_module.anyio.to_thread, "run_sync", wraps=real_run_sync
        ) as run_sync:
            resp = client.post("/scan", files={"file": _image_file()})

        assert resp.status_code == 200
        sanitize_calls = [
            c
            for c in run_sync.call_args_list
            if getattr(c.args[0], "func", None) is scan_module.sanitize_image
        ]
        assert len(sanitize_calls) == 1
        call = sanitize_calls[0]
        assert call.kwargs["limiter"] is scan_module._NORMALISE_LIMITER
        assert call.args[0].args == (_JPEG,)
        # #526: the pixel cap now comes from ai_settings (scan.max_image_pixels);
        # with no row it falls back to SCAN_MAX_IMAGE_PIXELS.
        assert call.args[0].keywords == {
            "max_long_edge_px": FEATURES["scan.standard"].max_image_long_edge_px,
            "max_pixels": ai_settings.settings.scan_max_image_pixels,
        }

    def test_sanitize_image_runs_off_the_event_loop(self, client, provider):
        import asyncio

        from app.api import scan as scan_module

        on_loop = []
        real = scan_module.sanitize_image

        def spy(*args, **kwargs):
            try:
                asyncio.get_running_loop()
                on_loop.append(True)
            except RuntimeError:  # no loop in this thread: a worker thread
                on_loop.append(False)
            return real(*args, **kwargs)

        with patch.object(scan_module, "sanitize_image", side_effect=spy):
            resp = client.post("/scan", files={"file": _image_file()})

        assert resp.status_code == 200
        assert on_loop == [False]

    def test_limiter_size_comes_from_the_setting(self):
        from app.api import scan as scan_module
        from app.core.config import settings

        assert (
            scan_module._NORMALISE_LIMITER.total_tokens
            == settings.scan_normalise_concurrency
        )


class TestNormaliseConcurrencySetting:
    _DB = "postgresql+asyncpg://u:p@localhost/db"

    def test_default_is_2(self):
        from app.core.config import Settings

        assert Settings(database_url=self._DB).scan_normalise_concurrency == 1

    @pytest.mark.parametrize("value", [0, 9])
    def test_out_of_bounds_rejected(self, value):
        from pydantic import ValidationError

        from app.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(database_url=self._DB, scan_normalise_concurrency=value)

    def test_read_from_env(self, monkeypatch):
        from app.core.config import Settings

        monkeypatch.setenv("SCAN_NORMALISE_CONCURRENCY", "4")
        assert Settings(database_url=self._DB).scan_normalise_concurrency == 4
