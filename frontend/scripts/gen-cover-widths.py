"""Generate theme/coverTextWidths.ts advance table from NotoSerif_600SemiBold.ttf.

Usage (needs `pip install fonttools`), from frontend/:
  python3 scripts/gen-cover-widths.py > theme/coverTextWidths.generated.txt
and paste the array into theme/coverTextWidths.ts. Values are advance widths in
1/1000 em for code points U+0020..U+00FF; 0 means "not in the font".
"""
from fontTools.ttLib import TTFont

PATH = "node_modules/@expo-google-fonts/noto-serif/600SemiBold/NotoSerif_600SemiBold.ttf"
font = TTFont(PATH)
cmap = font.getBestCmap()
upm = font["head"].unitsPerEm
out = []
for cp in range(0x20, 0x100):
    g = cmap.get(cp)
    out.append(round(font["hmtx"][g][0] * 1000 / upm) if g else 0)
print(", ".join(map(str, out)))
