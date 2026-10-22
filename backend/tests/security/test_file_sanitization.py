"""
Security tests for image sanitization and ClamAV malware scanning.

pytest -m security -v tests/security/test_file_sanitization.py
"""

import io
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from PIL import Image

from app.ai.providers.base import ProviderResult

# ---------------------------------------------------------------------------
# Helpers — build minimal valid image bytes in memory
# ---------------------------------------------------------------------------


def _make_jpeg(width: int = 10, height: int = 10) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (width, height), color=(255, 0, 0))
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_png(width: int = 10, height: int = 10, mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    img = Image.new(mode, (width, height), color=(0, 255, 0))
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_webp(width: int = 10, height: int = 10) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (width, height), color=(0, 0, 255))
    img.save(buf, format="WEBP")
    return buf.getvalue()


def _jpeg_with_exif() -> bytes:
    """Build a minimal JPEG that includes an EXIF GPS tag."""
    from PIL import Image

    buf = io.BytesIO()
    img = Image.new("RGB", (10, 10), color=(128, 128, 128))
    exif = img.getexif()
    # Tag 0x0112 = Orientation (safe, always present in real cameras)
    exif[0x0112] = 6
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# A04 — Image sanitization (Pillow re-encode, always on)
# ---------------------------------------------------------------------------


@pytest.mark.security
class TestImageSanitization:
    """Pillow re-encode must strip metadata and always produce valid JPEG output."""

    def test_jpeg_sanitized_is_valid_jpeg(self) -> None:
        from app.core.file_security import sanitize_image

        result = sanitize_image(_make_jpeg())
        assert result[:3] == b"\xff\xd8\xff"  # JPEG magic bytes

    def test_png_sanitized_to_jpeg(self) -> None:
        from app.core.file_security import sanitize_image

        png_bytes = _make_png()
        result = sanitize_image(png_bytes)
        # Output must be JPEG regardless of input format
        assert result[:3] == b"\xff\xd8\xff"

    def test_webp_sanitized_to_jpeg(self) -> None:
        from app.core.file_security import sanitize_image

        result = sanitize_image(_make_webp())
        assert result[:3] == b"\xff\xd8\xff"

    def test_rgba_png_converted_without_error(self) -> None:
        """RGBA PNG (has alpha channel) must be downconverted to RGB before JPEG save."""
        from app.core.file_security import sanitize_image

        result = sanitize_image(_make_png(mode="RGBA"))
        assert result[:3] == b"\xff\xd8\xff"

    def test_exif_stripped_after_sanitization(self) -> None:
        """EXIF metadata present in the source image must not appear in the output."""
        from app.core.file_security import sanitize_image

        dirty = _jpeg_with_exif()

        # Confirm EXIF is present in original (APP1 marker = FF E1)
        assert b"\xff\xe1" in dirty

        clean = sanitize_image(dirty)

        # Re-open and confirm no EXIF
        img = Image.open(io.BytesIO(clean))
        exif = img.getexif()
        assert len(exif) == 0, f"Expected no EXIF after sanitization, got: {dict(exif)}"

    def test_undecodable_image_is_rejected_not_passed_through(self) -> None:
        """AC1 (#529): bytes Pillow cannot decode raise 415 image_undecodable.

        Replaces the old raw-bytes fallback test: the original bytes must
        never be returned."""
        from app.core.file_security import sanitize_image

        garbage = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 100

        with pytest.raises(HTTPException) as exc_info:
            sanitize_image(garbage)

        assert exc_info.value.status_code == 415
        assert exc_info.value.detail == "image_undecodable"

    def test_sanitized_image_is_openable_by_pillow(self) -> None:
        """The sanitized output must be a valid image Pillow can fully round-trip."""
        from app.core.file_security import sanitize_image

        result = sanitize_image(_make_jpeg())
        img = Image.open(io.BytesIO(result))
        img.load()
        assert img.format == "JPEG"

    def test_sanitize_reduces_or_equal_size_for_simple_image(self) -> None:
        """A plain colour JPEG should not grow dramatically after sanitization."""
        from app.core.file_security import sanitize_image

        original = _make_jpeg(width=100, height=100)
        result = sanitize_image(original)
        # Allow up to 2× growth (re-encoding complex images can be slightly larger)
        assert len(result) <= len(original) * 2


# ---------------------------------------------------------------------------
# A04 — Upload pipeline through POST /scan (#529)
# ---------------------------------------------------------------------------


@pytest.fixture
def scan_client():
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from app.auth.dependencies import get_current_user
    from app.core.database import get_db
    from app.main import app
    from app.models.user import User

    async def _fake_db():
        yield AsyncMock()

    user = User(id="00000000-0000-0000-0000-000000000529", email="u@example.com")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _fake_db
    with (
        patch("app.api.scan._reserve_scan_quota", new=AsyncMock()) as reserve,
        patch("app.api.scan._refund_scan_quota", new=AsyncMock()),
        patch("app.api.scan._release_scan_lease", new=AsyncMock()),
        patch("app.ai.gateway.OpenAIVisionProvider") as provider_cls,
        patch("app.api.scan.EnrichmentService"),
        patch("app.api.scan.DeduplicationService"),
    ):
        provider_cls.return_value.identify = AsyncMock(return_value=ProviderResult([]))
        yield TestClient(app), provider_cls, reserve
    app.dependency_overrides.clear()


@pytest.mark.security
class TestUploadPipeline:
    def test_undecodable_file_never_reaches_provider(self, scan_client) -> None:
        """AC1: valid JPEG magic bytes but no decodable image -> 415, and the
        provider mock is never called (the raw-bytes fallback is gone)."""
        client, provider_cls, reserve = scan_client
        fake = b"\xff\xd8\xff\xe0" + b"not really a jpeg" * 10
        resp = client.post(
            "/scan", files={"file": ("x.jpg", io.BytesIO(fake), "image/jpeg")}
        )
        assert resp.status_code == 415
        assert resp.json() == {"detail": "image_undecodable"}
        provider_cls.assert_not_called()
        provider_cls.return_value.identify.assert_not_called()
        reserve.assert_not_awaited()

    def test_oversize_upload_reads_at_most_cap_plus_one_bytes(
        self, scan_client
    ) -> None:
        """AC6: the handler stops reading as soon as the upload passes the cap."""
        from starlette.datastructures import UploadFile as StarletteUploadFile

        from app.api.scan import MAX_IMAGE_BYTES

        client, provider_cls, reserve = scan_client
        original_read = StarletteUploadFile.read
        requested: list[int] = []
        returned: list[int] = []

        async def spy_read(self, size: int = -1) -> bytes:
            requested.append(size)
            data = await original_read(self, size)
            returned.append(len(data))
            return data

        # Over the 5MB cap but under the 10MB request-size middleware, so the
        # handler (not the middleware) is what stops the read.
        body = _make_jpeg() + b"x" * (MAX_IMAGE_BYTES + 3 * 1024 * 1024)
        with patch.object(StarletteUploadFile, "read", spy_read):
            resp = client.post(
                "/scan", files={"file": ("big.jpg", io.BytesIO(body), "image/jpeg")}
            )

        assert resp.status_code == 413
        assert resp.json() == {"detail": "Image must be 5MB or smaller"}
        # Never an unbounded read, and the total consumed stops at cap + 1.
        assert all(size > 0 for size in requested)
        assert sum(returned) == MAX_IMAGE_BYTES + 1
        assert len(body) > sum(returned)
        provider_cls.assert_not_called()
        reserve.assert_not_awaited()

    def test_polyglot_payload_is_removed_by_reencode(self, scan_client) -> None:
        """A JPEG carrying a script in a comment segment and a zip after EOI is
        re-encoded; neither payload reaches the provider."""
        client, provider_cls, _ = scan_client
        script = b"<script>alert(1)</script>"
        zip_payload = b"PK\x03\x04" + b"evil-archive-entry" * 4
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), (10, 120, 200)).save(
            buf, format="JPEG", comment=script
        )
        polyglot = buf.getvalue() + script + zip_payload
        assert script in polyglot and b"PK\x03\x04" in polyglot

        resp = client.post(
            "/scan", files={"file": ("p.jpg", io.BytesIO(polyglot), "image/jpeg")}
        )

        assert resp.status_code == 200
        sent = provider_cls.return_value.identify.await_args.args[0]
        assert sent[:3] == b"\xff\xd8\xff"
        assert sent != polyglot
        assert script not in sent
        assert b"PK\x03\x04" not in sent
        assert b"evil-archive-entry" not in sent
        assert sent.rstrip(b"\x00").endswith(b"\xff\xd9")


# ---------------------------------------------------------------------------
# A04 — ClamAV scan (optional, env-gated)
# ---------------------------------------------------------------------------


@pytest.mark.security
class TestClamAVScan:
    """ClamAV scanning must be a no-op when disabled and fail-closed when enabled."""

    def test_scan_skipped_when_disabled(self) -> None:
        """With CLAMAV_ENABLED=false, scan_for_malware must return without error
        and without importing or calling clamd."""
        from app.core.file_security import scan_for_malware

        with patch("app.core.file_security.settings") as mock_settings:
            mock_settings.clamav_enabled = False
            # Should complete without raising — no clamd import needed
            scan_for_malware(b"any bytes")

    def test_scan_passes_clean_file(self) -> None:
        """When ClamAV is enabled and reports OK, no exception is raised."""
        from app.core.file_security import scan_for_malware

        mock_cd = MagicMock()
        mock_cd.instream.return_value = {"stream": ("OK", None)}

        with (
            patch("app.core.file_security.settings") as mock_settings,
            patch.dict(
                "sys.modules",
                {
                    "clamd": MagicMock(
                        ClamdNetworkSocket=MagicMock(return_value=mock_cd)
                    )
                },
            ),
        ):
            mock_settings.clamav_enabled = True
            mock_settings.clamav_host = "localhost"
            mock_settings.clamav_port = 3310
            scan_for_malware(_make_jpeg())  # must not raise

    def test_scan_raises_422_on_threat_found(self) -> None:
        """When ClamAV reports FOUND, a 422 HTTPException must be raised."""
        from app.core.file_security import scan_for_malware

        mock_cd = MagicMock()
        mock_cd.instream.return_value = {"stream": ("FOUND", "Eicar-Test-Signature")}

        with (
            patch("app.core.file_security.settings") as mock_settings,
            patch.dict(
                "sys.modules",
                {
                    "clamd": MagicMock(
                        ClamdNetworkSocket=MagicMock(return_value=mock_cd)
                    )
                },
            ),
        ):
            mock_settings.clamav_enabled = True
            mock_settings.clamav_host = "localhost"
            mock_settings.clamav_port = 3310

            with pytest.raises(HTTPException) as exc_info:
                scan_for_malware(b"EICAR test payload")

        assert exc_info.value.status_code == 422
        assert "malware" in exc_info.value.detail.lower()

    def test_scan_raises_503_when_daemon_unreachable(self) -> None:
        """When the ClamAV daemon is down, a 503 HTTPException must be raised
        (fail-closed: do not let uploads through if the scanner is broken)."""
        from app.core.file_security import scan_for_malware

        mock_clamd = MagicMock()
        mock_clamd.ClamdNetworkSocket.side_effect = ConnectionRefusedError(
            "daemon down"
        )

        with (
            patch("app.core.file_security.settings") as mock_settings,
            patch.dict("sys.modules", {"clamd": mock_clamd}),
        ):
            mock_settings.clamav_enabled = True
            mock_settings.clamav_host = "localhost"
            mock_settings.clamav_port = 3310

            with pytest.raises(HTTPException) as exc_info:
                scan_for_malware(_make_jpeg())

        assert exc_info.value.status_code == 503
