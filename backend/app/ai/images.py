"""Normalise an uploaded image before it reaches an AI provider.

Every image sent to a provider is decoded in full, turned upright from its
EXIF orientation, stripped of all metadata, shrunk so its long edge fits the
feature's ``max_image_long_edge_px`` and re-encoded as a JPEG at quality 85.
That fixes the per-image token cost and keeps EXIF (GPS and so on), XMP, IPTC,
ICC profiles, comments and any polyglot payload on the server.

Anything Pillow cannot fully decode raises :class:`ImageUndecodableError`; an
image whose declared pixel count is above the cap raises
:class:`ImageTooLargePixelsError` before it is decoded. Callers map these to
HTTP 415 and 413. Nothing else escapes this module.

This module never calls a provider and must not import ``app.ai.providers``.
"""

import io
import logging

import pillow_heif
from PIL import Image, ImageOps

# Registered at import, before the first decode, so HEIC/HEIF photos from
# iPhones are decoded instead of rejected.
pillow_heif.register_heif_opener()

logger = logging.getLogger(__name__)

JPEG_QUALITY = 85

# Only the decoders for the formats /scan accepts are ever tried. Every other
# Pillow plugin (TIFF, GIF, EPS, PDF, MSP, ...) is unreachable from an upload,
# which keeps their parser bugs out of reach. "HEIF" is pillow-heif's opener.
ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP", "HEIF")

_ALPHA_MODES = frozenset({"RGBA", "LA", "PA", "RGBa", "La"})


class ImageNormalisationError(ValueError):
    """Base class for an image that cannot be sent to a provider."""

    code = "image_invalid"


class ImageUndecodableError(ImageNormalisationError):
    """Pillow (with pillow-heif) could not fully decode the image."""

    code = "image_undecodable"


class ImageTooLargePixelsError(ImageNormalisationError):
    """The image's pixel count is above the configured maximum."""

    code = "image_too_large_pixels"


def _to_rgb(img: Image.Image) -> Image.Image:
    """Return an RGB copy of ``img``, flattening any alpha onto white."""
    if img.mode == "P" and "transparency" in img.info:
        img = img.convert("RGBA")
    if img.mode in _ALPHA_MODES:
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    if img.mode == "RGB":
        return img
    return img.convert("RGB")


def normalise_image(data: bytes, *, max_long_edge_px: int, max_pixels: int) -> bytes:
    """Decode ``data`` and return a clean, upright, downscaled JPEG.

    Raises:
        ImageTooLargePixelsError: width x height is above ``max_pixels``.
        ImageUndecodableError: the bytes are not an image Pillow can fully
            decode, or any later step fails.
    """
    try:
        # Image.open only parses the header; the size it reports is the
        # declared size, so the bomb check below runs before any pixel data
        # is decoded. Pillow raises its own DecompressionBombError here for
        # images far above Image.MAX_IMAGE_PIXELS; that is mapped to 413 too.
        img = Image.open(io.BytesIO(data), formats=ALLOWED_FORMATS)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageTooLargePixelsError("image pixel count is above the cap") from None
    except Exception:  # noqa: BLE001 - anything Pillow raises means "not an image"
        raise ImageUndecodableError("image could not be decoded") from None

    try:
        width, height = img.size
        if width <= 0 or height <= 0:
            raise ImageUndecodableError("image has no pixels")
        if width * height > max_pixels:
            raise ImageTooLargePixelsError("image pixel count is above the cap")

        if img.format in ("JPEG", "MPO"):
            # Let libjpeg decode at a reduced scale when the target is much
            # smaller; it still reads the whole file, so truncation is caught.
            img.draft(img.mode, (max_long_edge_px, max_long_edge_px))
        img.load()  # full decode: truncated or corrupt data fails here

        # Orientation first (it lives in EXIF), then build a fresh image so no
        # metadata (EXIF, XMP, IPTC, ICC, comments) is carried into the output.
        img = ImageOps.exif_transpose(img)
        img = _to_rgb(img)
        img.thumbnail(
            (max_long_edge_px, max_long_edge_px), Image.Resampling.LANCZOS
        )  # shrinks only; never upscales
        clean = Image.new("RGB", img.size)
        clean.paste(img)

        out = io.BytesIO()
        clean.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        result = out.getvalue()
    except ImageNormalisationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageTooLargePixelsError("image pixel count is above the cap") from None
    except Exception:  # noqa: BLE001 - any decode/convert failure is a 415
        raise ImageUndecodableError("image could not be decoded") from None
    finally:
        img.close()

    # Numbers only: never the image, a filename or any metadata value.
    logger.info(
        "image normalised: %dx%d, %d bytes -> %d bytes",
        clean.width,
        clean.height,
        len(data),
        len(result),
    )
    return result
