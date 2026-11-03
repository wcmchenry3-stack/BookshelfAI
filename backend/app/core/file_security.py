"""File upload hardening: malware scanning and image sanitization.

Scan order (called from POST /scan, after the magic-byte check):
  1. ClamAV antivirus scan   — optional, controlled by CLAMAV_ENABLED setting
  2. Image normalisation     — always on (app.ai.images): full decode or 415,
                               pixel cap or 413, EXIF orientation, all metadata
                               stripped, long edge capped, re-encoded as JPEG

Both steps operate on the upload before it reaches the AI provider. Every
accepted format (JPEG/PNG/WebP/HEIC) leaves as JPEG, consistent with the
data:image/jpeg MIME type sent to the Vision API.
"""

import io
import logging

from fastapi import HTTPException, status

# Importing app.ai.images registers the HEIC/HEIF opener before any decode.
from app.ai.images import (
    ImageTooLargePixelsError,
    ImageUndecodableError,
    normalise_image,
)
from app.ai.registry import get_feature
from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ClamAV scan (optional)
# ---------------------------------------------------------------------------


def scan_for_malware(raw_bytes: bytes) -> None:
    """Stream *raw_bytes* through the ClamAV daemon if CLAMAV_ENABLED is set.

    Only an explicit ``("OK", ...)`` result passes. Fails closed otherwise.

    Raises:
        HTTPException 422  — if ClamAV reports a threat (FOUND)
        HTTPException 503  — if the daemon is unreachable, reports ERROR, or
                             returns any other status or a missing/malformed
                             result (fail-closed)
    """
    if not settings.clamav_enabled:
        return

    try:
        import clamd  # type: ignore[import-untyped] - clamd ships no type stubs
    except ImportError:
        logger.error("pyclamd is not installed but CLAMAV_ENABLED=true")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Malware scanning unavailable",
        )

    try:
        cd = clamd.ClamdNetworkSocket(
            host=settings.clamav_host,
            port=settings.clamav_port,
            timeout=10,
        )
        result = cd.instream(io.BytesIO(raw_bytes))
    # Any clamd failure (connection, protocol, timeout) means scanning is unavailable.
    except Exception as exc:  # noqa: BLE001 - any clamd failure = 503
        logger.error("ClamAV daemon unreachable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Malware scanning unavailable",
        )

    entry = result.get("stream") if isinstance(result, dict) else None
    if not isinstance(entry, (tuple, list)) or len(entry) != 2:
        logger.error(
            "ClamAV returned a malformed scan result (%s)", type(entry).__name__
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Malware scanning unavailable",
        )

    status_code, virus_name = entry
    if status_code == "OK":
        return
    if status_code == "FOUND":
        logger.warning("ClamAV detected threat: %s", virus_name)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="File failed malware scan",
        )
    # ERROR or any unrecognised status: fail closed, never treat as clean.
    logger.error("ClamAV returned non-OK scan status %r: %s", status_code, virus_name)
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Malware scanning unavailable",
    )


# ---------------------------------------------------------------------------
# Image normalisation (always on)
# ---------------------------------------------------------------------------


def sanitize_image(
    raw_bytes: bytes,
    *,
    max_long_edge_px: int | None = None,
    max_pixels: int | None = None,
) -> bytes:
    """Normalise *raw_bytes* with :func:`app.ai.images.normalise_image`.

    Decodes the image fully (HEIC/HEIF via pillow-heif), applies EXIF
    orientation, strips all metadata, shrinks the long edge to
    ``max_long_edge_px`` and re-encodes as JPEG (quality 85). There is no
    raw-bytes fallback: an image that cannot be decoded is rejected.

    ``max_long_edge_px`` defaults to the ``scan.standard`` registry value and
    ``max_pixels`` to ``SCAN_MAX_IMAGE_PIXELS``. ``/scan`` passes the
    ``scan.max_image_pixels`` value from ``ai_settings``; the env value is only
    the seed and the fallback for callers without a DB session.

    Raises:
        HTTPException 415 ``image_undecodable``    — not a decodable image
        HTTPException 413 ``image_too_large_pixels`` — pixel count above the cap
    """
    if max_long_edge_px is None:
        max_long_edge_px = get_feature("scan.standard").max_image_long_edge_px
    if max_pixels is None:
        max_pixels = settings.scan_max_image_pixels

    try:
        return normalise_image(
            raw_bytes, max_long_edge_px=max_long_edge_px, max_pixels=max_pixels
        )
    except ImageTooLargePixelsError:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=ImageTooLargePixelsError.code,
        ) from None
    except ImageUndecodableError:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=ImageUndecodableError.code,
        ) from None
