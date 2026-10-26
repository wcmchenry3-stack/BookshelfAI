"""Image normalisation guards that need a crafted Pillow object."""

import io
from unittest.mock import patch

import pytest
from PIL import Image

from app.ai import images
from app.ai.images import ImageTooLargePixelsError, ImageUndecodableError


def _jpeg(size=(40, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(out, format="JPEG")
    return out.getvalue()


def test_one_bit_image_is_widened_to_greyscale_for_resizing():
    bw = Image.new("1", (8, 8), 1)
    out = images._to_resizable(bw)
    assert out.mode == "L"
    assert out.getpixel((0, 0)) == 255


def test_plain_rgb_image_is_left_as_is():
    rgb = Image.new("RGB", (4, 4))
    assert images._to_resizable(rgb) is rgb


def test_header_declaring_no_pixels_is_undecodable():
    class _Empty:
        size = (0, 10)
        format = "JPEG"

        def close(self):
            pass

    with patch.object(images.Image, "open", return_value=_Empty()), pytest.raises(
        ImageUndecodableError, match="no pixels"
    ):
        images.normalise_image(b"x", max_long_edge_px=100, max_pixels=10_000)


def test_decompression_bomb_during_decode_maps_to_too_large():
    with patch.object(
        images.ImageOps,
        "exif_transpose",
        side_effect=Image.DecompressionBombError("bomb"),
    ), pytest.raises(ImageTooLargePixelsError):
        images.normalise_image(_jpeg(), max_long_edge_px=100, max_pixels=10_000)


def test_decompression_bomb_while_opening_maps_to_too_large():
    with patch.object(
        images.Image, "open", side_effect=Image.DecompressionBombError("bomb")
    ), pytest.raises(ImageTooLargePixelsError):
        images.normalise_image(b"x", max_long_edge_px=100, max_pixels=10_000)


def test_unexpected_failure_during_conversion_is_undecodable_not_a_500():
    with patch.object(
        images.ImageOps, "exif_transpose", side_effect=OSError("boom")
    ), pytest.raises(ImageUndecodableError):
        images.normalise_image(_jpeg(), max_long_edge_px=100, max_pixels=10_000)


def test_pixel_cap_is_checked_before_any_decoding():
    with patch.object(images.ImageOps, "exif_transpose") as transpose, pytest.raises(
        ImageTooLargePixelsError
    ):
        images.normalise_image(_jpeg((100, 100)), max_long_edge_px=50, max_pixels=99)
    transpose.assert_not_called()


def test_output_is_downscaled_to_the_long_edge_and_is_a_jpeg():
    out = images.normalise_image(
        _jpeg((400, 200)), max_long_edge_px=100, max_pixels=1_000_000
    )
    with Image.open(io.BytesIO(out)) as img:
        assert img.format == "JPEG"
        assert img.size == (100, 50)
