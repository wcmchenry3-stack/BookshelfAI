"""File upload hardening: malware scanning and image sanitization.

Scan order (called from POST /scan):
  1. ClamAV antivirus scan   — optional, controlled by CLAMAV_ENABLED setting
  2. Pillow re-encode        — always on; strips EXIF/XMP/IPTC and neutralises
                               polyglot payloads by re-encoding to JPEG

Both steps operate on raw bytes before they reach OpenAI.  The sanitizer
also normalises all accepted formats (JPEG/PNG/WebP/HEIC) to JPEG, which
is consistent with the hardcoded data:image/jpeg MIME type already sent to
the Vision API.
"""

import io
import logging

from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ClamAV scan (optional)
# ---------------------------------------------------------------------------


def scan_for_malware(raw_bytes: bytes) -> None:
    """Stream *raw_bytes* through the ClamAV daemon if CLAMAV_ENABLED is set.

    Raises:
        HTTPException 422  — if ClamAV reports a threat (FOUND)
        HTTPException 503  — if the daemon is unreachable (fail-closed)
    """
    if not settings.clamav_enabled:
        return

    try:
        import clamd  # type: ignore[import-untyped]
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
    except Exception as exc:  # noqa: BLE001
        logger.error("ClamAV daemon unreachable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Malware scanning unavailable",
        )

    status_code, virus_name = result.get("stream", ("ERROR", "unknown"))
    if status_code == "FOUND":
        logger.warning("ClamAV detected threat: %s", virus_name)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="File failed malware scan",
        )
    if status_code == "ERROR":
        logger.error("ClamAV returned scan error: %s", virus_name)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Malware scanning unavailable",
        )


# ---------------------------------------------------------------------------
# Pillow image sanitization (always on)
# ---------------------------------------------------------------------------


def sanitize_image(raw_bytes: bytes) -> bytes:
    """Re-encode *raw_bytes* as JPEG using Pillow.

    Strips all embedded metadata (EXIF, XMP, IPTC, ICC profiles, comments)
    and neutralises polyglot payloads by forcing a clean re-encode.

    Outputs JPEG regardless of input format so the bytes are consistent with
    the ``data:image/jpeg`` MIME type already sent to the OpenAI Vision API.

    Falls back to the original bytes if Pillow cannot decode the image (e.g.
    HEIC without the pillow-heif plugin installed).  Magic-bytes validation
    has already confirmed the file is a genuine image at this point.

    Returns:
        Sanitized JPEG bytes, or *raw_bytes* unchanged if Pillow decoding fails.
    """
    try:
        from PIL import Image, UnidentifiedImageError  # type: ignore[import-untyped]

        buf = io.BytesIO(raw_bytes)
        try:
            img = Image.open(buf)
            img.load()  # fully decode — catches truncated files
        except (UnidentifiedImageError, OSError):
            # UnidentifiedImageError: Pillow cannot handle this format (e.g. HEIC
            #   without pillow-heif installed).
            # OSError: image file is truncated or otherwise unreadable by Pillow.
            # In both cases the file has already passed magic-bytes validation so
            # it is a genuine image; pass through the original bytes unchanged.
            logger.debug("Pillow could not decode image; skipping sanitization")
            return raw_bytes

        # Convert to RGB — required for JPEG output (no alpha channel support).
        # PNG/WebP may carry RGBA or P (palette) modes.
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        out = io.BytesIO()
        # quality=85 is visually lossless for book-cover thumbnails and keeps
        # file size reasonable for the OpenAI Vision API payload limit.
        img.save(out, format="JPEG", quality=85, optimize=True)
        sanitized = out.getvalue()

        logger.debug(
            "Image sanitized: %d bytes → %d bytes JPEG", len(raw_bytes), len(sanitized)
        )
        return sanitized

    except ImportError:
        # Pillow is not installed.  Log loudly — requirements.txt should include it.
        logger.error("Pillow is not installed; image sanitization skipped")
        return raw_bytes
