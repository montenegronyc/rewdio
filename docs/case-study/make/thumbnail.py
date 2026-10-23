"""Portfolio thumbnails: the desktop waterfall and the phone response chart side by side on cream.

Usage: uv run --with pillow python docs/case-study/make/thumbnail.py docs/img docs/case-study
Writes rewdio-thumbnail-3x2.png (2400x1600) and rewdio-thumbnail-1x1.png (2000x2000).
"""
import sys
from PIL import Image, ImageDraw
img, out = sys.argv[1], sys.argv[2]
CREAM, INK = (243, 238, 226), (0, 0, 0)
H = 1320
desk = Image.open(f"{img}/mac-waterfall.png").convert("RGB").crop((912, 0, 1968, 1350))
phone = Image.open(f"{img}/status.png").convert("RGB").crop((0, 0, 780, 1215))
gap = 120
content_w = desk.width + gap + phone.width

def sheet(W, Hc, scale=1.0, name="x"):
    c = Image.new("RGB", (W, Hc), CREAM)
    d2 = desk.resize((round(desk.width * scale), round(desk.height * scale)), Image.LANCZOS)
    p2 = phone.resize((round(phone.width * scale), round(phone.height * scale)), Image.LANCZOS)
    g = round(gap * scale)
    w = d2.width + g + p2.width
    x0, y0 = (W - w) // 2, (Hc - d2.height) // 2
    c.paste(d2, (x0, y0))
    c.paste(p2, (x0 + d2.width + g, y0))
    lx = x0 + d2.width + g // 2
    ImageDraw.Draw(c).line([(lx, y0), (lx, y0 + d2.height)], fill=INK, width=2)
    c.save(f"{out}/{name}", quality=92, optimize=True)

sheet(2400, 1600, 1.0, "rewdio-thumbnail-3x2.png")
sheet(2000, 2000, 0.92, "rewdio-thumbnail-1x1.png")
