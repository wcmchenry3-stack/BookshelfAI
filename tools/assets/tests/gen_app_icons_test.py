"""The icon, launcher and splash rasters match the recipe, and stay inside the platform safe zones."""

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


def test_ios_storyboard_splash_colour_matches():
    sb = (g.FRONTEND / "ios/BookShelfAI/SplashScreen.storyboard").read_text()
    r, gr, b = (f"{v / 255:.14f}" for v in g.NAVY)
    assert f'blue="{b}" green="{gr}" red="{r}"' in sb


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
    for d, px in g.SPLASH_LOGO.items():
        assert Image.open(g.RES / f"drawable-{d}/splashscreen_logo.png").size == (px, px)


def test_splash_is_navy_with_the_mark_centred():
    img = Image.open(g.ASSETS / "splash.png").convert("RGB")
    assert img.size == (g.SPLASH_W, g.SPLASH_H)
    assert img.getpixel((5, 5)) == g.NAVY
    assert img.getpixel((g.SPLASH_W // 2, 5)) == g.NAVY
    assert "#263250" in (g.RES / "values/colors.xml").read_text()


def test_size_budgets():
    for name in ("icon", "adaptive-icon", "monochrome-icon", "splash"):
        assert (g.ASSETS / f"{name}.png").stat().st_size <= 500 * 1024, name
