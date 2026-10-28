"""Draw the app icon (pastel embroidery hoop with a stitched heart) as PNG and multi-size ICO."""
import math
from pathlib import Path

from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1]
S = 512
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
# rounded tile with a pink -> lavender gradient
grad = Image.new("RGBA", (S, S))
for y in range(S):
    t = y / S
    grad.paste((int(255 - 40 * t), int(160 - 10 * t), int(205 + 40 * t), 255), (0, y, S, y + 1))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((16, 16, S - 16, S - 16), 120, fill=255)
im.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(im)
# wooden hoop with its screw tab, and the fabric inside
wood = (255, 222, 176, 255)
d.rounded_rectangle((222, 36, 290, 94), 16, fill=wood)
d.ellipse((70, 72, 442, 444), fill=wood)
d.ellipse((100, 102, 412, 414), fill=(255, 251, 253, 255))
# a heart sewn in running stitch
cx, cy, k = 256, 262, 8.6
pts = []
for i in range(161):
    a = i / 160 * 2 * math.pi
    x = 16 * math.sin(a) ** 3
    y = 13 * math.cos(a) - 5 * math.cos(2 * a) - 2 * math.cos(3 * a) - math.cos(4 * a)
    pts.append((cx + k * x, cy - k * y))
pink = (255, 86, 156, 255)
for i in range(0, 160, 8):
    d.line(pts[i:i + 5], fill=pink, width=20, joint="curve")
    for p in (pts[i], pts[min(i + 4, 160)]):
        d.ellipse((p[0] - 9, p[1] - 9, p[0] + 9, p[1] + 9), fill=pink)
# sparkle
for sx, sy, r in ((392, 132, 26), (126, 380, 16)):
    d.polygon([(sx, sy - r), (sx + r * 0.28, sy - r * 0.28), (sx + r, sy), (sx + r * 0.28, sy + r * 0.28),
               (sx, sy + r), (sx - r * 0.28, sy + r * 0.28), (sx - r, sy), (sx - r * 0.28, sy - r * 0.28)],
              fill=(255, 255, 255, 255))
im.save(root / "stitchpilot" / "icon.png")
im.save(Path(__file__).with_name("icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon written")
