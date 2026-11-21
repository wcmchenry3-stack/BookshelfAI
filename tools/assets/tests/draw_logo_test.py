"""The logo recipe: deterministic pixels, valid SVG, ink inside the ring, every style present."""

import hashlib
import xml.etree.ElementTree as ET

import pytest
from draw_logo import (
    NAVY,
    OPTIONS,
    RING_R,
    SHIPPED,
    SIZE,
    _flatten,
    mark_shapes,
    render,
    render_mask,
    svg_document,
    wordmark_shapes,
)

# sha256 of the raw RGB bytes of render(256, style). Pinned with Pillow 13.0.0 (requirements.txt).
# An intended change to the drawing means updating these in the same commit.
PINNED = {
    "A": "adc68c6d22a3c819a5f5ae4734df765eec2c86b0f3f34ad36de0e58b7cc465c4",
    "B": "f2c8fa250cfa761869e8e80d9f15b5cf04228678f2d0ad6fd153a8e9626e6222",
    "C": "b458f4dd75d55df72f5911924363fcdcacbae7ee05211c04c5f128eae7376303",
}


def _digest(style: str) -> str:
    return hashlib.sha256(render(256, style).tobytes()).hexdigest()


@pytest.mark.parametrize("style", sorted(OPTIONS))
def test_render_is_pinned(style):
    assert _digest(style) == PINNED[style]


def test_shipped_style_exists():
    assert SHIPPED in OPTIONS


def test_render_is_deterministic():
    assert render(128).tobytes() == render(128).tobytes()


@pytest.mark.parametrize("style", sorted(OPTIONS))
def test_all_ink_is_inside_the_ring(style):
    for shape in mark_shapes(style, ring=False):
        for sub in _flatten(shape.d):
            for x, y in sub:
                assert (x - 512) ** 2 + (y - 512) ** 2 < (RING_R - 6) ** 2, (style, shape.d[:40], x, y)


def test_wordmark_has_every_letter():
    assert len(wordmark_shapes()) == len("BookshelfAI")


def test_letters_keep_their_counters():
    """The first 'o' is a ring. Even-odd fill must leave its hole open: the centre is not ink."""
    o = wordmark_shapes()[1]
    assert len(_flatten(o.d)) == 2
    mask = render_mask(SIZE, [o])
    x0, y0, x1, y1 = mask.getbbox()
    assert mask.getpixel(((x0 + x1) // 2, (y0 + y1) // 2)) == 0, "the counter was filled"
    # Some pixel along the middle row is ink: the left stroke of the 'o'.
    assert max(mask.getpixel((x, (y0 + y1) // 2)) for x in range(x0, x0 + (x1 - x0) // 4)) == 255


def test_svg_is_well_formed_and_matches_the_shapes():
    root = ET.fromstring(svg_document())
    ns = "{http://www.w3.org/2000/svg}"
    assert root.attrib["viewBox"] == f"0 0 {SIZE} {SIZE}"
    assert len(root.findall(f"{ns}path")) == len(mark_shapes())


def test_transparent_render_has_no_background():
    img = render(64, bg=None)
    assert img.getpixel((0, 0))[3] == 0
    assert img.getchannel("A").getextrema()[1] == 255


def test_field_is_navy():
    assert render(64).getpixel((2, 2)) == NAVY
