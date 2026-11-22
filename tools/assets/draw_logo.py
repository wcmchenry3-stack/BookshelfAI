"""The BookshelfAI mark, drawn from code: a chihuahua over an open book, in a ring, with the name on an arc.

The drawing is SVG path data in a 1024 x 1024 box (`dog_shapes`, `book_shapes`, `ring_shapes`,
`wordmark_shapes`). One small Pillow rasteriser turns the same data into PNGs, so the shipped files
are deterministic and need no SVG library. `svg_document` writes the same paths as SVG, the copy a
planned in-app `BookshelfLogo` component (react-native-svg, not built yet: docs/ASSETS.md) will draw.

The mark is two ink layers on a field: cream "add" shapes and navy "erase" shapes, in painting order,
which is what both a flat navy icon and a transparent Android foreground need.

Styles (the audition, docs/ASSETS.md): `OPTIONS` holds every look; `SHIPPED` names the one in use.
  A  line   the dog as the Gemini reference drew it: navy head, cream outline and markings
  B  solid  the dog as a solid cream silhouette with navy markings (reads best very small)
  C  bold   style A with heavier strokes and fewer page lines (survives a 48 px icon)

Usage (from the repo root, inside tools/.venv):
  python tools/assets/draw_logo.py --preview DIR     contact sheet of every style, at real sizes
  python tools/assets/draw_logo.py --svg FILE [-s S] write the logo as SVG
"""

from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass, replace
from pathlib import Path

from logo_glyphs import GLYPHS, UNITS_PER_EM, WORDMARK
from PIL import Image, ImageChops, ImageDraw

# Colours. The logo's own navy and cream, measured from the first (Gemini) logo; the app's UI palette
# is in frontend/theme/tokens.ts and is deliberately separate (a native icon cannot read a theme).
NAVY = (38, 50, 80)  # #263250
CREAM = (239, 233, 212)  # #EFE9D4
SIZE = 1024  # the design box
SS = 4  # supersampling for the rasteriser
CURVE_STEPS = 20

CX = 517  # the book's axis; its right half is the left half mirrored about this


# ---------------------------------------------------------------------------------------------
# Shapes


@dataclass(frozen=True)
class Shape:
    """One path. `ink` is "add" (cream) or "erase" (navy); `fill` paints the inside, `stroke` outlines."""

    d: str
    ink: str = "add"
    fill: bool = False
    stroke: float = 0.0

    def scaled_stroke(self, k: float) -> "Shape":
        return replace(self, stroke=self.stroke * k)


def mirror(d: str, cx: float = CX) -> str:
    """The path mirrored left-right about x = cx (absolute commands only)."""
    out = []
    for cmd, nums in _tokens(d):
        xs = [str(round(2 * cx - v, 2)) if i % 2 == 0 else str(v) for i, v in enumerate(nums)]
        out.append(cmd + " ".join(xs))
    return "".join(out)


def _tokens(d: str):
    for cmd, args in re.findall(r"([MLQCZ])([^MLQCZ]*)", d):
        yield cmd, [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", args)]


# --- the ring and the book -------------------------------------------------------------------

RING_R = 500
RING_W = 7


def circle(cx: float, cy: float, r: float) -> str:
    """A circle as four cubic arcs (the usual 0.5523 handle length)."""
    h = r * 0.5523
    return (
        f"M{cx - r:g} {cy:g}C{cx - r:g} {cy - h:g} {cx - h:g} {cy - r:g} {cx:g} {cy - r:g}"
        f"C{cx + h:g} {cy - r:g} {cx + r:g} {cy - h:g} {cx + r:g} {cy:g}"
        f"C{cx + r:g} {cy + h:g} {cx + h:g} {cy + r:g} {cx:g} {cy + r:g}"
        f"C{cx - h:g} {cy + r:g} {cx - r:g} {cy + h:g} {cx - r:g} {cy:g}Z"
    )


def ring_shapes() -> list[Shape]:
    return [Shape(circle(512, 512, RING_R), stroke=RING_W)]


def book_shapes(lines: int = 9) -> list[Shape]:
    left: list[Shape] = []
    # Page stack: the outer edge, then page lines fanning from it into the spine.
    left.append(Shape("M252 563L211 647", stroke=5))
    for i in range(lines):
        t = i / (lines - 1)
        sx, sy = 252 - 40 * t, 563 + 84 * t
        ex, ey = 503, 646 - 4 * (1 - t)
        left.append(
            Shape(f"M{sx:.1f} {sy:.1f}C{sx + 110:.1f} {sy - 4:.1f} {sx + 215:.1f} {sy + 8 + 22 * t:.1f} {ex} {ey:.1f}", stroke=3.4)
        )
    # The turning page: a crescent from the top-left tip into the spine, with its flap and edge.
    left.append(Shape("M294 465C360 484 440 520 488 584C493 600 500 620 503 642C492 628 484 611 474 599C430 546 360 502 294 465Z", fill=True, stroke=2))
    left.append(Shape("M298 468L262 508L252 563", stroke=5))
    left.append(Shape("M264 512C332 522 420 548 487 612", stroke=4.5))
    shapes = left + [Shape(mirror(s.d), s.ink, s.fill, s.stroke) for s in left]
    # The base line and the spine's bulge under the book.
    shapes.append(Shape("M210 655L824 655", stroke=8))
    shapes.append(Shape("M470 654C482 678 500 684 517 684C534 684 552 678 564 654", stroke=5))
    return shapes


# --- the dog ---------------------------------------------------------------------------------

# Closed outline of the head, both ears and the neck: the solid silhouette of style B.
HEAD_SILHOUETTE = (
    "M402 167C430 175 465 205 481 225C482 200 485 185 492 180C500 180 525 225 537 267"
    "C560 262 590 278 620 310C645 345 640 372 650 380L672 388C688 395 695 410 690 425"
    "C686 438 678 447 668 448C640 458 620 462 600 460L572 481L545 550L517 584L500 550"
    "L485 495L480 460C470 430 440 395 430 380L410 340L400 230C397 200 398 180 402 167Z"
)


def dog_shapes(style: str = "line") -> list[Shape]:
    solid = style == "solid"
    s: list[Shape] = []
    if solid:
        s.append(Shape(HEAD_SILHOUETTE, fill=True))
    else:
        # Big ear: outer line from the tip, then down the back of the neck.
        s.append(Shape("M402 167C440 172 470 205 505 271", stroke=5))
        s.append(Shape("M402 167C397 190 398 215 403 250L409 312L412 342C420 362 428 372 430 380C412 420 385 455 362 482", stroke=5))
        # Small ear, then the head top, brow and forehead down to the muzzle.
        s.append(Shape("M481 225C482 200 485 185 492 180C500 180 525 225 537 267", stroke=5))
        s.append(Shape("M505 271C525 264 548 262 560 266C590 272 612 295 627 320C640 345 640 362 642 372", stroke=5))
        # Inner ear, filled cream, with its tufts.
        s.append(Shape("M413 187C440 192 470 225 487 288L480 300L470 296L480 318L462 306L455 332L440 322L425 312L418 300C414 262 410 222 413 187Z", fill=True))
    # Markings. In the solid style these are navy cut-outs; in the line style they are cream.
    ink = "erase" if solid else "add"
    # Inner-ear slits are the reverse in both styles: navy cuts in the cream ear.
    s.append(Shape("M420 217C422 235 424 250 427 265", "erase", stroke=3))
    s.append(Shape("M467 252C474 268 478 282 482 296", "erase", stroke=3))
    # Eye: an almond outline with a bright pupil dot; brow crescent and cheek tick.
    s.append(Shape("M570 377C575 365 580 358 590 356C602 356 611 366 612 377C604 388 594 393 583 391C576 388 572 383 570 377Z", ink, stroke=4))
    s.append(Shape(circle(600, 366, 4.2), "add" if not solid else "erase", fill=True))
    s.append(Shape("M586 351C598 334 614 336 620 346", ink, stroke=7))
    s.append(Shape("M642 343L643 358", ink, stroke=4))
    # Cheek patch, jaw line, muzzle, mouth and nose.
    s.append(Shape("M492 399C505 396 520 392 532 394C544 398 541 412 528 418C512 422 498 416 492 399Z", ink, fill=True))
    s.append(Shape("M475 418C484 440 500 450 520 452C535 452 546 448 552 443", ink, stroke=4))
    if not solid:
        s.append(Shape("M552 443C580 425 610 405 625 392C640 378 660 380 672 388C688 395 695 410 690 425C686 438 678 447 668 448C640 458 620 462 600 460C570 455 560 452 552 443Z", fill=True))
    s.append(Shape("M600 454C625 456 650 452 670 446", "erase", stroke=3.5))
    s.append(Shape("M664 397C664 388 672 384 681 385C691 386 694 394 692 402C689 410 680 414 672 412C666 410 664 404 664 397Z", "erase", fill=True))
    s.append(Shape("M672 404C676 408 682 407 685 402", "add" if not solid else "erase", stroke=2.5))
    # Chest fur: a ragged cream wedge under the chin, with a navy slit.
    s.append(Shape("M480 460L515 472L540 480L572 481L542 497L541 540L545 550L517 584L500 550L496 520L486 497L498 490L480 460Z", ink if not solid else "add", fill=True))
    s.append(Shape("M537 515L542 548", "erase", stroke=3))
    return s


# --- the wordmark ----------------------------------------------------------------------------

TEXT_R = 404  # baseline radius of the arc
TEXT_CX, TEXT_CY = 512, 446  # centre of the arc
TEXT_EM = 100
TEXT_TRACK = 0.015  # extra spacing, in em


def wordmark_shapes() -> list[Shape]:
    """The name on an arc below the book: each outlined letter rotated rigidly onto the circle."""
    k = TEXT_EM / UNITS_PER_EM
    adv = [GLYPHS[c][1] * k + TEXT_TRACK * TEXT_EM for c in WORDMARK]
    total = sum(adv)
    theta = math.pi / 2 + total / TEXT_R / 2  # start at the left, travel clockwise on screen
    shapes: list[Shape] = []
    for ch, a in zip(WORDMARK, adv):
        centre = theta - a / TEXT_R / 2
        px, py = TEXT_CX + TEXT_R * math.cos(centre), TEXT_CY + TEXT_R * math.sin(centre)
        tx, ty = math.sin(centre), -math.cos(centre)  # tangent, in the reading direction
        ux, uy = -math.cos(centre), -math.sin(centre)  # "up" for the glyph: towards the centre
        gx0 = (GLYPHS[ch][1] * k) / 2
        out = []
        for cmd, nums in _tokens(GLYPHS[ch][0]):
            pts = []
            for i in range(0, len(nums), 2):
                gx, gy = nums[i] * k - gx0, nums[i + 1] * k
                pts += [px + gx * tx + gy * ux, py + gx * ty + gy * uy]
            out.append(cmd + " ".join(f"{v:.1f}" for v in pts))
        shapes.append(Shape("".join(out), fill=True))
        theta -= a / TEXT_R
    return shapes


# ---------------------------------------------------------------------------------------------
# Styles


@dataclass(frozen=True)
class Style:
    label: str
    dog: str  # "line" or "solid"
    stroke_k: float  # stroke weight multiplier
    page_lines: int
    note: str


OPTIONS: dict[str, Style] = {
    "A": Style("line", "line", 1.0, 9, "The reference's drawing: navy head, cream outline and markings."),
    "B": Style("solid", "solid", 1.0, 9, "A solid cream dog with navy markings; strongest silhouette."),
    "C": Style("bold", "line", 1.5, 5, "Style A with heavier strokes and fewer page lines, for small sizes."),
}
SHIPPED = "A"


def mark_shapes(style: str = SHIPPED, *, ring: bool = True, text: bool = True) -> list[Shape]:
    st = OPTIONS[style]
    shapes = ring_shapes() if ring else []  # the ring keeps its own weight in every style
    art = book_shapes(st.page_lines) + dog_shapes(st.dog)
    shapes += [s.scaled_stroke(st.stroke_k) for s in art]
    if text:
        shapes += wordmark_shapes()
    return shapes


# ---------------------------------------------------------------------------------------------
# Rasteriser


def _flatten(d: str) -> list[list[tuple[float, float]]]:
    """Subpaths of a path as polylines, with SVG meaning for every command (so SVG viewers agree)."""
    subs: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for cmd, n in _tokens(d):
        if cmd == "M":
            if len(cur) > 1:
                subs.append(cur)
            cur = [(n[0], n[1])]
        elif cmd == "L":
            for i in range(0, len(n), 2):
                cur.append((n[i], n[i + 1]))
        elif cmd == "Q":  # SVG semantics: every four numbers are one (control, end) segment
            for i in range(0, len(n), 4):
                p0 = cur[-1]
                c, to = (n[i], n[i + 1]), (n[i + 2], n[i + 3])
                for s in range(1, CURVE_STEPS + 1):
                    t = s / CURVE_STEPS
                    u = 1 - t
                    cur.append((u * u * p0[0] + 2 * u * t * c[0] + t * t * to[0], u * u * p0[1] + 2 * u * t * c[1] + t * t * to[1]))
        elif cmd == "C":
            for i in range(0, len(n), 6):
                p0 = cur[-1]
                c1, c2, to = (n[i], n[i + 1]), (n[i + 2], n[i + 3]), (n[i + 4], n[i + 5])
                for s in range(1, CURVE_STEPS + 1):
                    t = s / CURVE_STEPS
                    u = 1 - t
                    cur.append(tuple(u**3 * p0[k] + 3 * u * u * t * c1[k] + 3 * u * t * t * c2[k] + t**3 * to[k] for k in (0, 1)))
        elif cmd == "Z":
            if len(cur) > 1:
                cur.append(cur[0])
                subs.append(cur)
            cur = []
    if len(cur) > 1:
        subs.append(cur)
    return subs


def _paint(mask: Image.Image, shape: Shape, k: float, off: tuple[float, float]) -> None:
    """Paint one shape onto a mask (255 = cream, 0 = navy). `k` maps design units to mask pixels."""
    value = 255 if shape.ink == "add" else 0
    subs = _flatten(shape.d)
    pt = lambda p: ((p[0] + off[0]) * k, (p[1] + off[1]) * k)  # noqa: E731
    if shape.fill:
        acc = None  # even-odd: XOR the subpaths so a letter's counter stays open
        for sub in subs:
            if len(sub) < 3:
                continue
            layer = Image.new("L", mask.size, 0)
            ImageDraw.Draw(layer).polygon([pt(p) for p in sub], fill=255)
            acc = layer if acc is None else ImageChops.difference(acc, layer)
        if acc is not None:
            mask.paste(value, mask=acc)
    if shape.stroke:
        w = max(1, round(shape.stroke * k))
        d = ImageDraw.Draw(mask)
        for sub in subs:
            pts = [pt(p) for p in sub]
            d.line(pts, fill=value, width=w, joint="curve")
            for x, y in (pts[0], pts[-1]):  # round caps
                d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=value)


def render_mask(size: int, shapes: list[Shape], *, view: tuple[float, float, float] = (0, 0, SIZE)) -> Image.Image:
    """The ink as an 'L' mask (255 = cream) of `size` px. `view` is the (x, y, side) window of the
    1024 design box that fills the image; the default shows the whole box."""
    big = size * SS
    mask = Image.new("L", (big, big), 0)
    k = big / view[2]
    for sh in shapes:
        _paint(mask, sh, k, (-view[0], -view[1]))
    return mask.resize((size, size), Image.LANCZOS)


def render(size: int, style: str = SHIPPED, *, view: tuple[float, float, float] = (0, 0, SIZE), bg: tuple[int, int, int] | None = NAVY, **kw) -> Image.Image:
    """The mark on a navy field (`bg`), or on transparency when `bg` is None."""
    mask = render_mask(size, mark_shapes(style, **kw), view=view)
    if bg is None:
        out = Image.new("RGBA", (size, size), CREAM + (0,))
        out.putalpha(mask)
        return out
    field = Image.new("RGB", (size, size), bg)
    return Image.composite(Image.new("RGB", (size, size), CREAM), field, mask)


# ---------------------------------------------------------------------------------------------
# SVG (the shared copy the app's logo component reads) and the contact sheet


def svg_document(style: str = SHIPPED, size: int = 1024) -> str:
    hexa = lambda c: "#%02X%02X%02X" % c  # noqa: E731
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" width="{size}" height="{size}">']
    parts.append(f'<rect width="{SIZE}" height="{SIZE}" fill="{hexa(NAVY)}"/>')
    for s in mark_shapes(style):
        col = hexa(CREAM if s.ink == "add" else NAVY)
        attrs = f'fill="{col}"' if s.fill else 'fill="none"'
        if s.stroke:
            attrs += f' stroke="{col}" stroke-width="{s.stroke:g}" stroke-linecap="round" stroke-linejoin="round"'
        parts.append(f'<path d="{s.d}" {attrs} fill-rule="evenodd"/>')
    parts.append("</svg>")
    return "\n".join(parts)


def ts_module(style: str = SHIPPED) -> str:
    """The whole mark as a TypeScript module of path data for the app's `BookshelfLogo` component.

    Colours are not in it: the component paints "add" shapes in the theme's ink colour and "erase"
    shapes in its field colour. Formatted the way Prettier (printWidth 100) would, so lint stays clean.
    """
    lines = [
        "// GENERATED by tools/assets/gen_app_icons.py from tools/assets/draw_logo.py. Do not edit.",
        "// The BookshelfAI mark as SVG path data in a 1024 x 1024 box (the whole mark: ring, dog, book, name).",
        "",
        f"export const LOGO_VIEWBOX = {SIZE};",
        "",
        "export interface LogoShape {",
        "  /** SVG path data (absolute M, L, Q, C, Z). */",
        "  d: string;",
        "  /** `add` is painted in the ink colour, `erase` in the field colour, in order. */",
        "  ink: 'add' | 'erase';",
        "  fill: boolean;",
        "  /** Stroke width in viewBox units, 0 for none. */",
        "  stroke: number;",
        "}",
        "",
        "export const LOGO_SHAPES: readonly LogoShape[] = [",
    ]
    for sh in mark_shapes(style):
        one = f"  {{ d: '{sh.d}', ink: '{sh.ink}', fill: {str(sh.fill).lower()}, stroke: {sh.stroke:g} }},"
        if len(one) <= 100:
            lines.append(one)
        else:
            lines += [
                "  {",
                f"    d: '{sh.d}',",
                f"    ink: '{sh.ink}',",
                f"    fill: {str(sh.fill).lower()},",
                f"    stroke: {sh.stroke:g},",
                "  },",
            ]
    lines += ["];", ""]
    return "\n".join(lines)


def contact_sheet(out_dir: Path) -> Path:
    """Every option at the sizes it will be seen: 512 (store), 180 (home screen), 48 (small), on dark and light."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cols = list(OPTIONS)
    pad = 24
    sheet = Image.new("RGB", (pad + len(cols) * (512 + pad), pad + 512 + pad + 180 + pad + 48 + pad), (20, 20, 24))
    d = ImageDraw.Draw(sheet)
    for i, key in enumerate(cols):
        x = pad + i * (512 + pad)
        sheet.paste(render(512, key), (x, pad))
        d.text((x + 8, pad + 8), f"{key}: {OPTIONS[key].label}", fill=CREAM)
        y = pad + 512 + pad
        sheet.paste(render(180, key), (x, y))
        light = Image.new("RGB", (180, 180), (246, 244, 239))
        light.paste(render(160, key), (10, 10))
        sheet.paste(light, (x + 200, y))
        small = render(48, key)
        sheet.paste(small, (x, y + 180 + pad))
        sheet.paste(small.resize((24, 24), Image.LANCZOS), (x + 64, y + 180 + pad))
    path = out_dir / "logo-options.png"
    sheet.save(path)
    for key in cols:
        render(1024, key).save(out_dir / f"logo-{key}.png")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preview", metavar="DIR", help="write the contact sheet and one 1024 px render per style")
    ap.add_argument("--svg", metavar="FILE", help="write the shipped style as SVG")
    ap.add_argument("-s", "--style", default=SHIPPED, choices=list(OPTIONS))
    args = ap.parse_args(argv)
    if args.preview:
        print(contact_sheet(Path(args.preview)))
    if args.svg:
        Path(args.svg).write_text(svg_document(args.style))
        print(args.svg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
