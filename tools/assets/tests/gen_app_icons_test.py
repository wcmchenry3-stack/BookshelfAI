"""The icon, launcher and splash rasters match the recipe, and stay inside the platform safe zones."""

import json

import gen_app_icons as g
import math

from PIL import Image


def test_shipped_files_match_the_recipe():
    """The same check as `gen_app_icons.py --check`: nothing under frontend/ has drifted by hand."""
    assert g.main(["--check"]) == 0


def test_ios_icon_is_1024_and_has_no_alpha():
    img = Image.open(g.XCASSETS / "AppIcon.appiconset/App-Icon-1024x1024@1x.png")
    assert img.size == (1024, 1024)
    assert img.mode in ("RGB", "P"), "the App Store rejects an icon with an alpha channel"
    assert Image.open(g.ASSETS / "icon.png").size == (1024, 1024)


def test_ios_icon_is_not_the_expo_placeholder():
    img = Image.open(g.XCASSETS / "AppIcon.appiconset/App-Icon-1024x1024@1x.png").convert("RGB")
    assert img.getpixel((10, 10)) == g.NAVY
    assert img.getpixel((10, 10)) != (37, 99, 235)  # the blue box


def test_adaptive_art_stays_inside_the_safe_circle():
    """Google's safe zone is a 66 dp circle on the 108 dp canvas; circular masks cut at 72 dp across."""
    px = 432  # 4 px per dp
    limit = 33 * px / 108
    for img in (g.adaptive_foreground(px), g.monochrome(px)):
        alpha = img.getchannel("A")
        far = max(
            math.hypot(x - (px - 1) / 2, y - (px - 1) / 2)
            for y in range(px)
            for x in range(px)
            if alpha.getpixel((x, y)) > 40
        )
        assert far <= limit + 1, f"art reaches {far * 108 / px:.1f} dp from centre; safe zone is 33 dp"


def test_adaptive_background_is_navy_and_foreground_transparent():
    fg = Image.open(g.ASSETS / "adaptive-icon.png").convert("RGBA")
    assert fg.getpixel((2, 2))[3] == 0
    assert '"backgroundColor": "%s"' % g._hex(g.NAVY) in (g.FRONTEND / "app.json").read_text()


def test_monochrome_layer_is_declared():
    for name in ("ic_launcher.xml", "ic_launcher_round.xml"):
        assert "ic_launcher_monochrome" in (g.RES / "mipmap-anydpi-v26" / name).read_text()
    assert '"monochromeImage"' in (g.FRONTEND / "app.json").read_text()


def test_every_density_is_written():
    for d, px in g.MIPMAP_FOREGROUND.items():
        assert Image.open(g.RES / f"mipmap-{d}/ic_launcher_foreground.webp").size == (px, px)
        assert Image.open(g.RES / f"mipmap-{d}/ic_launcher_monochrome.webp").size == (px, px)
    for d, px in g.MIPMAP_LEGACY.items():
        assert Image.open(g.RES / f"mipmap-{d}/ic_launcher.webp").size == (px, px)
        assert Image.open(g.RES / f"mipmap-{d}/ic_launcher_round.webp").size == (px, px)


def test_splash_icon_is_the_mark_on_transparent():
    img = Image.open(g.ASSETS / "splash-icon.png").convert("RGBA")
    assert img.size == (g.SPLASH_ICON_SIDE, g.SPLASH_ICON_SIDE)
    assert img.getpixel((2, 2))[3] == 0
    assert img.getchannel("A").getbbox() is not None


def test_splash_colour_is_navy_in_app_json():
    block = json.loads((g.FRONTEND / "app.json").read_text())["expo"]["plugins"]
    cfg = next(p[1] for p in block if isinstance(p, list) and p[0] == "expo-splash-screen")
    assert cfg["backgroundColor"] == g._hex(g.NAVY)
    assert cfg["dark"]["backgroundColor"] == g._hex(g.NAVY)


def test_size_budgets():
    for name in ("icon", "adaptive-icon", "monochrome-icon", "splash-icon"):
        assert (g.ASSETS / f"{name}.png").stat().st_size <= 500 * 1024, name


def test_text_edits_rewrite_the_splash_plugin_colours(monkeypatch):
    """A stale splash colour in app.json is put back by the generator (and so caught by --check)."""
    app = g.FRONTEND / "app.json"
    real = app.read_text()
    stale = real.replace('"backgroundColor": "#263250"', '"backgroundColor": "#000000"')
    assert stale != real
    real_read = type(app).read_text
    monkeypatch.setattr(type(app), "read_text", lambda self, *a, **k: stale if self == app else real_read(self, *a, **k))
    plugin = next(p for p in json.loads(g.text_edits()[app])["expo"]["plugins"] if isinstance(p, list) and p[0] == "expo-splash-screen")
    navy = g._hex(g.NAVY)
    assert plugin[1]["backgroundColor"] == navy
    assert plugin[1]["dark"]["backgroundColor"] == navy
