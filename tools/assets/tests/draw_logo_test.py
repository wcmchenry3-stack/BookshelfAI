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
    _tokens,
    mark_shapes,
    render,
    render_mask,
    svg_document,
    wordmark_shapes,
)

# sha256 of the raw RGB bytes of render(256, style). Pinned with Pillow 13.0.0 (requirements.txt).
# An intended change to the drawing means updating these in the same commit.
PINNED = {
    "A": "7a3470c8dce8bd27fe5d2fecc3987beda9f8b0e990968475570a6e62a6228e7a",
    "B": "f4126169cbe836d7b852600e2bea228abd98612bb49bcf87254b93c417b05d50",
    "C": "06e9cd43464470fc5f01062963fe390bc2e41e86ab698adfb99e696303954aba",
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


def test_every_svg_path_is_valid_path_data():
    """SVG needs whole commands: M/L take pairs, Q four numbers, C six, Z none. (TrueType's chained
    quadratics have more than four and render wrong in a real SVG viewer, though not in our rasteriser.)"""
    need = {"M": 2, "L": 2, "Q": 4, "C": 6, "Z": 0}
    for shape in mark_shapes():
        for cmd, nums in _tokens(shape.d):
            assert len(nums) % max(need[cmd], 1) == 0 and (need[cmd] or not nums), (cmd, nums, shape.d[:50])


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
