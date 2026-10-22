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


_SIXTEEN_BIT_MODES = frozenset({"I;16", "I;16B", "I;16L", "I;16N"})


def _int_to_l(img: Image.Image) -> Image.Image:
    """Scale a 16-bit or 32-bit greyscale image down to 8-bit ``L``.

    ``convert("L")`` / ``convert("RGB")`` clip these modes at 255 instead of
    scaling, so a 16-bit photo would come out almost all white. ``I;16*``
    covers 0-65535 and maps to 0-255. Mode ``I`` (32-bit signed) is scaled by
    what it holds: values already in 0-255 are kept, values in 0-65535 are
    treated as 16-bit, and anything else is stretched from its own min-max.
    """
    as_i = img if img.mode == "I" else img.convert("I")  # lossless widening
    if img.mode in _SIXTEEN_BIT_MODES:
        lo, scale = 0, 255 / 65535
    else:
        lo, hi = as_i.getextrema()
        if lo >= 0 and hi <= 255:
            lo, scale = 0, 1.0
        elif lo >= 0 and hi <= 65535:
            lo, scale = 0, 255 / 65535
        else:
            scale = 255 / (hi - lo) if hi > lo else 0.0
    # point() on mode I applies v * scale + offset in C; convert("L") then
    # only clips rounding at the ends.
    return as_i.point(lambda v: v * scale - lo * scale).convert("L")


def _to_resizable(img: Image.Image) -> Image.Image:
    """Return ``img`` in a mode that resizes well, without flattening alpha.

    Palette and 1-bit images would be resized with nearest-neighbour, and
    16/32-bit greyscale would be clipped later, so those are converted here.
    Everything else (RGB, RGBA, L, LA, CMYK, ...) is resized as-is; Pillow
    resizes alpha modes premultiplied, so transparent pixels do not bleed.
    """
    if img.mode in _SIXTEEN_BIT_MODES or img.mode == "I":
        return _int_to_l(img)
    if img.mode in ("P", "PA"):
        has_alpha = img.mode == "PA" or "transparency" in img.info
        return img.convert("RGBA" if has_alpha else "RGB")
    if img.mode == "1":
        return img.convert("L")
    return img


def _to_rgb(img: Image.Image) -> Image.Image:
    """Return an RGB copy of ``img``, flattening any alpha onto white."""
    img = _to_resizable(img)
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
    except Image.DecompressionBombError:
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
        # Shrink before flattening alpha, so the RGBA -> RGB copy is made at
        # the output size rather than the full decoded size.
        img = _to_resizable(img)
        img.thumbnail(
            (max_long_edge_px, max_long_edge_px), Image.Resampling.LANCZOS
        )  # shrinks only; never upscales
        img = _to_rgb(img)
        clean = Image.new("RGB", img.size)
        clean.paste(img)

        out = io.BytesIO()
        clean.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        result = out.getvalue()
    except ImageNormalisationError:
        raise
    except Image.DecompressionBombError:
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
