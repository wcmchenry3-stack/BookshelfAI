"""Image normalisation (#529): decode or reject, bomb guard, metadata strip,
orientation, downscale and JPEG output. Fixtures are built in code."""

import io
import logging
import struct
import zlib
from unittest.mock import patch

import pillow_heif
import pytest
from fastapi import HTTPException
from PIL import Image, ImageStat
from PIL.JpegImagePlugin import JpegImageFile

from app.ai.images import (
    JPEG_QUALITY,
    ImageTooLargePixelsError,
    ImageUndecodableError,
    _to_rgb,
    normalise_image,
)
from app.core.file_security import sanitize_image

JPEG_MAGIC = b"\xff\xd8\xff"
LONG_EDGE = 1568
MAX_PIXELS = 40_000_000


def _norm(data: bytes, **kw) -> bytes:
    kw.setdefault("max_long_edge_px", LONG_EDGE)
    kw.setdefault("max_pixels", MAX_PIXELS)
    return normalise_image(data, **kw)


def _encode(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def _open(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _png_with_declared_size(width: int, height: int) -> bytes:
    """A tiny PNG whose IHDR claims ``width`` x ``height`` pixels."""
    png = bytearray(_encode(Image.new("RGB", (1, 1)), "PNG"))
    # Signature (8) + chunk length (4) + b"IHDR" (4) -> IHDR data at offset 16.
    assert png[12:16] == b"IHDR"
    png[16:24] = struct.pack(">II", width, height)
    crc = zlib.crc32(bytes(png[12:29])) & 0xFFFFFFFF
    png[29:33] = struct.pack(">I", crc)
    return bytes(png)


class TestMetadata:
    def test_gps_exif_is_removed(self):
        img = Image.new("RGB", (64, 48), (90, 90, 90))
        exif = Image.Exif()
        exif[0x010F] = "SpyCam"  # Make
        gps = exif.get_ifd(0x8825)
        gps[1] = "N"
        gps[2] = (51.0, 30.0, 0.0)
        gps[3] = "W"
        gps[4] = (0.0, 7.0, 0.0)
        src = _encode(img, "JPEG", exif=exif.tobytes())
        src_exif = _open(src).getexif()
        assert src_exif.get_ifd(0x8825), "fixture must carry GPS"

        out = _open(_norm(src))
        out_exif = out.getexif()
        assert len(out_exif) == 0
        assert not out_exif.get_ifd(0x8825)
        assert b"SpyCam" not in _norm(src)

    def test_icc_xmp_and_comment_are_removed(self):
        img = Image.new("RGB", (64, 48), (90, 90, 90))
        src = _encode(
            img,
            "JPEG",
            icc_profile=b"\x00" * 128 + b"FAKE-ICC",
            comment=b"secret-comment",
            xmp=b"<x:xmpmeta>secret-xmp</x:xmpmeta>",
        )
        out = _norm(src)
        img_out = _open(out)
        assert "icc_profile" not in img_out.info
        assert "comment" not in img_out.info
        assert "xmp" not in img_out.info
        for needle in (b"FAKE-ICC", b"secret-comment", b"secret-xmp"):
            assert needle not in out

    def test_exif_orientation_6_is_output_upright(self):
        # Stored 200x100: left half red, right half blue. Orientation 6 means
        # "rotate 90 degrees clockwise to display", so upright is 100x200 with
        # red on top and blue at the bottom.
        img = Image.new("RGB", (200, 100), (0, 0, 255))
        img.paste((255, 0, 0), (0, 0, 100, 100))
        exif = Image.Exif()
        exif[0x0112] = 6
        src = _encode(img, "JPEG", exif=exif.tobytes(), quality=95)

        out = _open(_norm(src))
        assert out.size == (100, 200)
        top = out.getpixel((50, 30))
        bottom = out.getpixel((50, 170))
        assert top[0] > 200 and top[2] < 60, top
        assert bottom[2] > 200 and bottom[0] < 60, bottom
        assert 0x0112 not in out.getexif()


class TestDownscale:
    @pytest.mark.parametrize("size", [(4000, 3000), (3000, 4000), (2000, 2000)])
    def test_long_edge_is_capped(self, size):
        out = _open(_norm(_encode(Image.new("RGB", size, (1, 2, 3)), "PNG")))
        assert max(out.size) == LONG_EDGE
        # Aspect ratio is kept (within a pixel of rounding).
        assert abs(out.size[0] / out.size[1] - size[0] / size[1]) < 0.01

    def test_small_image_is_not_upscaled(self):
        out = _open(_norm(_encode(Image.new("RGB", (300, 200)), "PNG")))
        assert out.size == (300, 200)

    def test_cap_comes_from_the_argument(self):
        out = _open(
            _norm(_encode(Image.new("RGB", (1000, 500)), "PNG"), max_long_edge_px=400)
        )
        assert out.size == (400, 200)

    def test_large_jpeg_uses_reduced_decode_and_still_caps(self):
        src = _encode(Image.new("RGB", (6000, 4000), (5, 6, 7)), "JPEG")
        real_draft = JpegImageFile.draft
        reduced = []

        def spy(self, mode, size):
            result = real_draft(self, mode, size)
            reduced.append(self.size)  # size after draft picked a scale
            return result

        with patch.object(
            JpegImageFile, "draft", autospec=True, side_effect=spy
        ) as draft:
            out = _open(_norm(src))
        # The first draft() call is normalise_image's own, with the long edge.
        assert draft.call_args_list[0].args[1:] == ("RGB", (LONG_EDGE, LONG_EDGE))
        # libjpeg was asked for a 1/2 scale decode: 6000x4000 -> 3000x2000.
        assert reduced[0] == (3000, 2000)
        assert max(out.size) == LONG_EDGE


class TestOutputFormat:
    @pytest.mark.parametrize("fmt", ["PNG", "WEBP", "JPEG", "HEIF"])
    def test_output_is_jpeg_for_every_input_format(self, fmt):
        src = _encode(Image.new("RGB", (64, 48), (200, 10, 10)), fmt)
        if fmt == "HEIF":
            assert src[4:12] == b"ftypheic"
        out = _norm(src)
        assert out[:3] == JPEG_MAGIC
        decoded = _open(out)
        assert decoded.format == "JPEG"
        assert decoded.mode == "RGB"
        assert decoded.size == (64, 48)

    def test_heic_is_decoded_via_registered_opener(self):
        assert pillow_heif.__version__  # imported and registered by app.ai.images
        src = _encode(Image.new("RGB", (80, 60), (10, 200, 10)), "HEIF")
        px = _open(_norm(src)).getpixel((40, 30))
        assert px[1] > 150 and px[0] < 80

    def test_rgba_png_becomes_rgb_flattened_on_white(self):
        img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))  # fully transparent
        img.paste((255, 0, 0, 255), (0, 0, 20, 40))  # left half opaque red
        out = _open(_norm(_encode(img, "PNG")))
        assert out.mode == "RGB"
        right = out.getpixel((30, 20))
        left = out.getpixel((5, 20))
        assert min(right) > 240, right  # transparent -> white, not black
        assert left[0] > 200 and left[1] < 60, left

    @pytest.mark.parametrize("mode", ["P", "LA", "L", "CMYK"])
    def test_other_modes_become_rgb(self, mode):
        fmt = "JPEG" if mode == "CMYK" else "PNG"
        src = _encode(Image.new(mode, (20, 20)), fmt)
        out = _open(_norm(src))
        assert out.mode == "RGB"

    def test_rgba_thumbnail_runs_before_alpha_flatten(self):
        """The alpha flatten happens at output size, not full decoded size."""
        img = Image.new("RGBA", (4000, 2000), (0, 0, 0, 0))
        img.paste((0, 0, 255, 255), (0, 0, 2000, 2000))  # left half opaque blue
        sizes = []
        real_new = Image.new

        def spy(mode, size, *args, **kw):
            sizes.append((mode, tuple(size)))
            return real_new(mode, size, *args, **kw)

        with patch("app.ai.images.Image.new", side_effect=spy):
            out = _open(_norm(_encode(img, "PNG")))
        # Every RGB canvas (white background, clean copy) is output-sized.
        assert all(max(size) <= LONG_EDGE for _, size in sizes), sizes
        assert out.size == (LONG_EDGE, LONG_EDGE // 2)
        right = out.getpixel((LONG_EDGE * 3 // 4, 100))
        left = out.getpixel((LONG_EDGE // 4, 100))
        assert min(right) > 240, right  # transparent -> white
        assert left[2] > 200 and left[0] < 60, left

    def test_palette_with_transparency_flattens_on_white(self):
        img = Image.new("P", (10, 10), 0)
        img.putpalette([0, 0, 0] * 256)
        src = _encode(img, "PNG", transparency=0)
        out = _open(_norm(src))
        assert min(out.getpixel((5, 5))) > 240

    def test_jpeg_quality_is_85(self):
        assert JPEG_QUALITY == 85
        src = _encode(Image.new("RGB", (10, 10)), "PNG")
        with patch.object(Image.Image, "save", autospec=True) as save:
            save.side_effect = lambda self, fp, **kw: fp.write(b"\xff\xd8\xff")
            _norm(src)
        kwargs = save.call_args.kwargs
        assert kwargs["format"] == "JPEG"
        assert kwargs["quality"] == 85


def _gradient_values(width: int, height: int, top: int) -> list[int]:
    """A left-to-right 0..``top`` gradient, row-major."""
    row = [round(x * top / (width - 1)) for x in range(width)]
    return row * height


def _assert_mid_tone(out: Image.Image) -> None:
    assert out.mode == "RGB"
    mean = ImageStat.Stat(out.convert("L")).mean[0]
    assert 20 < mean < 235, mean
    lo, hi = out.convert("L").getextrema()
    assert lo < 30 and hi > 225, (lo, hi)  # the full range survives
    assert lo != 255  # not all white


class TestHighBitDepthGreyscale:
    """16-bit greyscale is scaled to 8-bit, not clipped to white."""

    def test_sixteen_bit_png_gradient_keeps_its_mid_tones(self):
        img = Image.new("I;16", (512, 64))
        img.putdata(_gradient_values(512, 64, 65535))
        src = _encode(img, "PNG")
        assert _open(src).mode == "I;16"  # what Pillow decodes it as
        _assert_mid_tone(_open(_norm(src)))

    def test_sixteen_bit_png_is_not_clipped_when_downscaled(self):
        img = Image.new("I;16", (3200, 100))
        img.putdata(_gradient_values(3200, 100, 65535))
        out = _open(_norm(_encode(img, "PNG")))
        assert max(out.size) == LONG_EDGE
        _assert_mid_tone(out)

    # Pillow 13 cannot write mode I to PNG (and no allowed decoder yields it),
    # so mode I and the other I;16 byte orders are exercised on the converter.
    @pytest.mark.parametrize(
        ("mode", "top"),
        [
            ("I;16", 65535),
            ("I;16B", 65535),
            ("I;16L", 65535),
            ("I;16N", 65535),
            ("I", 65535),  # 16-bit data held in a 32-bit image
            ("I", 255),  # 8-bit data held in a 32-bit image: kept as-is
            ("I", 1_000_000),  # beyond 16 bits: scaled by its own range
        ],
    )
    def test_int_modes_are_scaled_not_clipped(self, mode, top):
        img = Image.new(mode, (256, 16))
        img.putdata(_gradient_values(256, 16, top))
        _assert_mid_tone(_to_rgb(img))

    def test_signed_int_mode_is_scaled_by_its_range(self):
        img = Image.new("I", (256, 16))
        img.putdata([v - 40_000 for v in _gradient_values(256, 16, 80_000)])
        _assert_mid_tone(_to_rgb(img))

    def test_constant_int_image_does_not_divide_by_zero(self):
        img = Image.new("I", (8, 8), -5)
        assert _to_rgb(img).mode == "RGB"


class TestDecodeOrReject:
    def test_truncated_jpeg_is_undecodable(self):
        src = _encode(Image.new("RGB", (300, 300), (1, 2, 3)), "JPEG")
        with pytest.raises(ImageUndecodableError):
            _norm(src[: len(src) // 2])

    def test_truncated_jpeg_maps_to_415(self):
        src = _encode(Image.new("RGB", (300, 300), (1, 2, 3)), "JPEG")
        with pytest.raises(HTTPException) as exc:
            sanitize_image(src[: len(src) // 2])
        assert exc.value.status_code == 415
        assert exc.value.detail == "image_undecodable"

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"\xff\xd8\xff\xe0" + b"junk" * 50,
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
            b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 40,
            b"\x00\x00\x00\x18ftypheic" + b"\x00" * 100,
        ],
    )
    def test_garbage_with_magic_bytes_is_undecodable(self, data):
        with pytest.raises(ImageUndecodableError):
            _norm(data)

    @pytest.mark.parametrize("fmt", ["GIF", "TIFF", "BMP", "ICO"])
    def test_other_pillow_formats_are_never_decoded(self, fmt):
        """Only JPEG/PNG/WebP/HEIF decoders are reachable from an upload."""
        src = _encode(Image.new("RGB", (16, 16)), fmt)
        with pytest.raises(ImageUndecodableError):
            _norm(src)

    def test_error_codes(self):
        assert ImageUndecodableError.code == "image_undecodable"
        assert ImageTooLargePixelsError.code == "image_too_large_pixels"


class TestDecompressionBomb:
    def test_declared_size_above_cap_is_refused_without_decode(self):
        bomb = _png_with_declared_size(8000, 8000)  # 64 MP, ~70 bytes
        assert len(bomb) < 200
        with (
            patch.object(Image.Image, "load") as base_load,
            patch("PIL.ImageFile.ImageFile.load") as file_load,
            pytest.raises(ImageTooLargePixelsError),
        ):
            _norm(bomb)
        base_load.assert_not_called()
        file_load.assert_not_called()

    def test_size_beyond_pillows_own_bomb_limit_maps_to_too_large(self):
        # 400 MP: Pillow raises its own DecompressionBombError inside open().
        bomb = _png_with_declared_size(20000, 20000)
        with pytest.raises(ImageTooLargePixelsError):
            _norm(bomb)

    def test_bomb_maps_to_413(self):
        with pytest.raises(HTTPException) as exc:
            sanitize_image(_png_with_declared_size(8000, 8000))
        assert exc.value.status_code == 413
        assert exc.value.detail == "image_too_large_pixels"

    def test_cap_defaults_to_setting(self):
        # Patch the settings object file_security actually holds (another test
        # re-imports app modules, so app.core.config.settings may differ).
        from app.core import file_security

        src = _encode(Image.new("RGB", (1001, 1000)), "PNG")
        with (
            patch.object(file_security.settings, "scan_max_image_pixels", 1_000_000),
            pytest.raises(HTTPException) as exc,
        ):
            sanitize_image(src)
        assert exc.value.status_code == 413

    def test_exactly_at_cap_is_allowed(self):
        src = _encode(Image.new("RGB", (1000, 1000)), "PNG")
        assert _norm(src, max_pixels=1_000_000)[:3] == JPEG_MAGIC


class TestLogging:
    def test_logs_numbers_only(self, caplog):
        img = Image.new("RGB", (64, 48))
        exif = Image.Exif()
        exif[0x010F] = "SecretMake"
        src = _encode(img, "JPEG", exif=exif.tobytes())
        with caplog.at_level(logging.DEBUG, logger="app.ai.images"):
            out = _norm(src)
        messages = [r.getMessage() for r in caplog.records if r.name == "app.ai.images"]
        assert messages == [
            f"image normalised: 64x48, {len(src)} bytes -> {len(out)} bytes"
        ]
        assert "SecretMake" not in caplog.text


class TestPixelCapSetting:
    _DB = "postgresql+asyncpg://u:p@localhost/db"

    def test_default_is_40_megapixels(self):
        from app.core.config import Settings

        assert Settings(database_url=self._DB).scan_max_image_pixels == 40_000_000

    @pytest.mark.parametrize("value", [0, 999_999, 100_000_001])
    def test_out_of_bounds_rejected(self, value):
        from pydantic import ValidationError

        from app.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(database_url=self._DB, scan_max_image_pixels=value)

    def test_read_from_env(self, monkeypatch):
        from app.core.config import Settings

        monkeypatch.setenv("SCAN_MAX_IMAGE_PIXELS", "1000000")
        assert Settings(database_url=self._DB).scan_max_image_pixels == 1_000_000
