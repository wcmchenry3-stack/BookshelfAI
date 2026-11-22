"""Write every raster of the BookshelfAI mark: app icons, launcher layers, splash and the login logo.

All of it is derived from `draw_logo.py`, so edit the recipe and re-run this; never edit the outputs.

Writes (`frontend/`):
  assets/icon.png                 iOS / store icon, 1024 px, no alpha
  assets/adaptive-icon.png        Android adaptive foreground (transparent, inside the 66 dp safe circle)
  assets/monochrome-icon.png      Android 13 themed-icon layer (alpha only)
  assets/favicon.png              web favicon
  assets/splash.png               splash, the whole mark centred on navy
  ios/.../AppIcon.appiconset/App-Icon-1024x1024@1x.png
  ios/.../SplashScreenLegacy.imageset/image{,@2x,@3x}.png, SplashScreenBackground.colorset
  android/.../mipmap-*/ic_launcher{,_round,_foreground,_monochrome}.webp, drawable-*/splashscreen_logo.png
  android/.../values/colors.xml (iconBackground, splashscreen_background)
  components/brand/logoPaths.ts   the whole mark as path data for the in-app BookshelfLogo component

Icons show the mark without the ring and the wordmark (unreadable at 48 px) in the bold style (C);
the monochrome layer uses the solid silhouette (B); the splash and logo show the whole mark.

Usage: python tools/assets/gen_app_icons.py [--check]    (--check writes nothing and fails on drift)
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path

from draw_logo import CREAM, NAVY, mark_shapes, render, render_mask, ts_module
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
ASSETS = FRONTEND / "assets"
RES = FRONTEND / "android/app/src/main/res"
XCASSETS = FRONTEND / "ios/BookShelfAI/Images.xcassets"

SS = 4
# The dog and book occupy x 210..824, y 165..684 of the design box (centre 517, 425).
CENTRE = (517, 425)
ICON_SIDE = 880  # iOS / legacy: the mark fills about 70 % of the icon
ADAPTIVE_SIDE = 1300  # Android: every opaque pixel within 33 dp of the centre of the 108 dp canvas (the 66 dp
# safe circle). The book's base corners are 383 design units from CENTRE, plus the round stroke ends.
SPLASH_RING_SHARE = 0.58  # ring diameter as a share of the splash width

MIPMAP_FOREGROUND = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}
MIPMAP_LEGACY = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
SPLASH_LOGO = {"mdpi": 288, "hdpi": 432, "xhdpi": 576, "xxhdpi": 864, "xxxhdpi": 1152}
SPLASH_W, SPLASH_H = 1284, 2778


def _view(side: float) -> tuple[float, float, float]:
    return (CENTRE[0] - side / 2, CENTRE[1] - side / 2, side)


def _hex(c: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % c


def icon_art(size: int) -> Image.Image:
    """The mark without ring or wordmark, bold, filling a navy square."""
    return render(size, "C", view=_view(ICON_SIDE), ring=False, text=False)


def adaptive_foreground(size: int) -> Image.Image:
    return render(size, "C", view=_view(ADAPTIVE_SIDE), bg=None, ring=False, text=False)


def monochrome(size: int) -> Image.Image:
    mask = render_mask(size, mark_shapes("B", ring=False, text=False), view=_view(ADAPTIVE_SIDE))
    out = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    out.putalpha(mask)
    return out


def whole_mark(size: int, *, bg=NAVY) -> Image.Image:
    return render(size, "A", bg=bg)


def _circle(size: int) -> Image.Image:
    m = Image.new("L", (size * SS, size * SS), 0)
    ImageDraw.Draw(m).ellipse((0, 0, size * SS - 1, size * SS - 1), fill=255)
    return m.resize((size, size), Image.LANCZOS)


def legacy_square(size: int) -> Image.Image:
    art = icon_art(size * SS).convert("RGBA")
    m = Image.new("L", art.size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, art.width - 1, art.height - 1), radius=int(art.width * 0.22), fill=255)
    art.putalpha(m)
    return art.resize((size, size), Image.LANCZOS)


def legacy_round(size: int) -> Image.Image:
    art = icon_art(size).convert("RGBA")
    art.putalpha(_circle(size))
    return art


def splash_image() -> Image.Image:
    """Navy screen with the whole mark centred (ring diameter `SPLASH_RING_SHARE` of the width)."""
    d = round(SPLASH_W * SPLASH_RING_SHARE)
    side = round(d * 1024 / 1000)  # the ring is 1000 of the 1024 box
    mark = whole_mark(side)
    out = Image.new("RGB", (SPLASH_W, SPLASH_H), NAVY)
    out.paste(mark, ((SPLASH_W - side) // 2, (SPLASH_H - side) // 2))
    return out


def android_splash_logo(size: int) -> Image.Image:
    """The whole mark, transparent around it, ring at 58 % of the canvas (inside Android 12's circle)."""
    side = round(size * 0.58 * 1024 / 1000)
    mark = render(side, "A", bg=None)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.alpha_composite(mark, ((size - side) // 2, (size - side) // 2))
    return out


def _png(img: Image.Image, *, quantize: bool = True) -> bytes:
    if quantize:
        a = img.getchannel("A") if img.mode == "RGBA" else None
        q = img.convert("RGB").quantize(64, dither=Image.NONE)
        if a is not None:
            q = q.convert("RGBA")
            q.putalpha(a)
            img = q
        else:
            img = q
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _webp(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "WEBP", lossless=True, quality=100, method=6)
    return buf.getvalue()


def outputs() -> dict[Path, bytes]:
    """Every generated file and its bytes. The icon, splash and logo are palette PNGs (they are flat art)."""
    out: dict[Path, bytes] = {}
    icon = icon_art(1024)
    out[ASSETS / "icon.png"] = _png(icon)
    out[XCASSETS / "AppIcon.appiconset/App-Icon-1024x1024@1x.png"] = _png(icon)
    out[ASSETS / "adaptive-icon.png"] = _png(adaptive_foreground(1024))
    out[ASSETS / "monochrome-icon.png"] = _png(monochrome(1024))
    out[ASSETS / "favicon.png"] = _png(icon_art(196))
    splash = _png(splash_image())
    out[ASSETS / "splash.png"] = splash
    for name in ("image.png", "image@2x.png", "image@3x.png"):
        out[XCASSETS / "SplashScreenLegacy.imageset" / name] = splash
    for d, px in MIPMAP_FOREGROUND.items():
        out[RES / f"mipmap-{d}/ic_launcher_foreground.webp"] = _webp(adaptive_foreground(px))
        out[RES / f"mipmap-{d}/ic_launcher_monochrome.webp"] = _webp(monochrome(px))
    for d, px in MIPMAP_LEGACY.items():
        out[RES / f"mipmap-{d}/ic_launcher.webp"] = _webp(legacy_square(px))
        out[RES / f"mipmap-{d}/ic_launcher_round.webp"] = _webp(legacy_round(px))
    for d, px in SPLASH_LOGO.items():
        out[RES / f"drawable-{d}/splashscreen_logo.png"] = _png(android_splash_logo(px), quantize=False)
    return out


def source_files() -> dict[Path, bytes]:
    """Generated source (not images): the path data the in-app `BookshelfLogo` draws."""
    return {FRONTEND / "components/brand/logoPaths.ts": ts_module().encode()}


def text_edits() -> dict[Path, str]:
    """Small text files that name the brand colours and the themed layer: derived, so checked too."""
    navy = _hex(NAVY)
    edits: dict[Path, str] = {}

    colors = RES / "values/colors.xml"
    s = colors.read_text()
    s = re.sub(r'(<color name="splashscreen_background">)[^<]*(</color>)', rf"\g<1>{navy}\g<2>", s)
    s = re.sub(r'(<color name="iconBackground">)[^<]*(</color>)', rf"\g<1>{navy}\g<2>", s)
    edits[colors] = s

    for xml in ("ic_launcher.xml", "ic_launcher_round.xml"):
        p = RES / "mipmap-anydpi-v26" / xml
        s = p.read_text()
        if "<monochrome" not in s:
            s = s.replace("</adaptive-icon>", '    <monochrome android:drawable="@mipmap/ic_launcher_monochrome"/>\n</adaptive-icon>')
        edits[p] = s

    sb = FRONTEND / "ios/BookShelfAI/SplashScreen.storyboard"
    s = sb.read_text()
    # The storyboard carries its own copy of the splash colour; keep it in step with the colorset.
    s = re.sub(
        r'(<namedColor name="SplashScreenBackground">\s*<color alpha="1.000" )blue="[^"]*" green="[^"]*" red="[^"]*"',
        lambda m: m.group(1) + 'blue="%s" green="%s" red="%s"' % tuple(f"{v / 255:.14f}" for v in (NAVY[2], NAVY[1], NAVY[0])),
        s,
    )
    edits[sb] = s

    bg = XCASSETS / "SplashScreenBackground.colorset/Contents.json"
    j = json.loads(bg.read_text())
    comps = j["colors"][0]["color"]["components"]
    for key, v in zip(("red", "green", "blue"), NAVY):
        comps[key] = f"{v / 255:.14f}"
    edits[bg] = json.dumps(j, indent=2) + "\n"

    app = FRONTEND / "app.json"
    s = app.read_text()
    # Targeted edits, so the rest of the file keeps its formatting.
    s = re.sub(r'("splash": \{[^}]*?"backgroundColor": )"[^"]*"', rf'\g<1>"{navy}"', s)
    s = re.sub(r'("adaptiveIcon": \{[^}]*?"backgroundColor": )"[^"]*"', rf'\g<1>"{navy}"', s)
    if "monochromeImage" not in s:
        s = s.replace(
            '"foregroundImage": "./assets/adaptive-icon.png",',
            '"foregroundImage": "./assets/adaptive-icon.png",\n        "monochromeImage": "./assets/monochrome-icon.png",',
        )
    edits[app] = s
    return edits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="write nothing; fail if a shipped file differs")
    args = ap.parse_args(argv)
    stale: list[Path] = []
    files: dict[Path, bytes] = outputs()
    files.update(source_files())
    files.update({p: t.encode() for p, t in text_edits().items()})
    for path, data in files.items():
        if path.exists() and path.read_bytes() == data:
            continue
        stale.append(path)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    rel = lambda p: p.relative_to(REPO)  # noqa: E731
    if args.check:
        for p in stale:
            print(f"stale: {rel(p)}")
        return 1 if stale else 0
    print(f"wrote {len(stale)} of {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
