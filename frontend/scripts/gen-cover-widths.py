"""Print the advance tables for theme/coverTextWidths.ts from NotoSerif_600SemiBold.ttf.

Usage (needs `pip install fonttools`), from frontend/:
  python3 scripts/gen-cover-widths.py
Paste line 1 into BASE_1000 (U+0020..U+017F) and line 2 into QUOTES_1000
(U+2018..U+201D). Values are advance widths in 1/1000 em; 0 means "not in the font".
"""
from fontTools.ttLib import TTFont

PATH = "node_modules/@expo-google-fonts/noto-serif/600SemiBold/NotoSerif_600SemiBold.ttf"
font = TTFont(PATH)
cmap = font.getBestCmap()
upm = font["head"].unitsPerEm


def row(lo, hi):
    out = []
    for cp in range(lo, hi + 1):
        g = cmap.get(cp)
        out.append(round(font["hmtx"][g][0] * 1000 / upm) if g else 0)
    return ", ".join(map(str, out))


print(row(0x20, 0x17F))
print(row(0x2018, 0x201D))
