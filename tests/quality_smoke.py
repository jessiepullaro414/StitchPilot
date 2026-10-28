"""Checks for roadmap step 2: hidden travel (fewer trims), mitred satin corners, shape-following fills."""
import math
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from stitchpilot import stitches as st  # noqa: E402
from stitchpilot.analysis import dense_share  # noqa: E402
from stitchpilot.digitize import FILL_ANGLES, digitize, shape_angle  # noqa: E402
from stitchpilot.render import render  # noqa: E402
from stitchpilot.settings import Settings, TextItem  # noqa: E402

out = Path(__file__).parent / "out"
out.mkdir(exist_ok=True)
RED, BLUE = (220, 30, 40), (30, 60, 200)


def trims(block) -> int:
    return int((block.cmds[:, 2] == st.TRIM).sum())


def block_of(d, rgb):
    return min(d.blocks, key=lambda b: sum((x - y) ** 2 for x, y in zip(b.rgb, rgb)))


# ---- corners: the geometry helpers
t = np.linspace(0, 10, 101)
ell = np.vstack([np.column_stack([np.zeros(101), 10 - t]), np.column_stack([t[1:], np.zeros(100)])])
assert len(st.find_corners(ell, False, 6)) == 1, "an L has one corner"
a = np.linspace(0, 2 * math.pi, 200, endpoint=False)
assert st.find_corners(np.column_stack([np.cos(a), np.sin(a)]) * 10, True, 6) == [], "a circle has none"

# ---- corners: bold lettering stays satin, without the dense fans at the points
# (before mitred corners, "LAZY WAVE" had 4.3% dangerously dense area and VWZAM fanned at every point)
for text, limit in (("VWZAMNX", 0.005), ("LAZY WAVE", 0.015)):
    d = digitize(None, Settings(texts=[TextItem(text, font="Arial", bold=True, height_mm=18)]))
    kinds = d.blocks[0].kinds
    share = dense_share(d)
    cv2.imwrite(str(out / f"quality_{text.replace(' ', '_')}.png"), cv2.cvtColor(render(d, 10), cv2.COLOR_RGB2BGR))
    print(f"{text}: {kinds}  dense share {share:.2%}")
    assert kinds.get("fill", 0) == 0, f"letters fell back to fill: {kinds}"
    assert share < limit, f"{text}: corners too dense: {share:.2%}"

# ---- hidden travel: two red squares joined by a blue band that is sewn after them
img = np.full((300, 700, 3), 255, np.uint8)
img[50:250, 50:250] = RED
img[50:250, 450:650] = RED
img[100:200, 250:450] = BLUE
rgba = np.dstack([img, np.full(img.shape[:2], 255, np.uint8)])
s = Settings(max_width_mm=70, max_height_mm=30)
d = digitize(rgba, s)
red, blue = block_of(d, RED), block_of(d, BLUE)
assert d.blocks.index(red) < d.blocks.index(blue), "the big red area is sewn first"
print(f"red under blue: red trims {trims(red)}, covered stitches {int(red.under.sum())}")
assert trims(red) == 1, f"red should reach its second square under the blue band (trims {trims(red)})"
assert red.under is not None and len(red.under) == len(red.cmds) and red.under.any()

# the travel stays inside the band (blue's own stitches mark where it is), a little way in from its edges
bs = blue.cmds[blue.cmds[:, 2] == st.STITCH, :2]
bx0, by0, bx1, by1 = bs[:, 0].min(), bs[:, 1].min(), bs[:, 0].max(), bs[:, 1].max()
travel = red.cmds[red.under & (red.cmds[:, 2] == st.STITCH), :2]
band = travel[(travel[:, 0] > bx0 + 1) & (travel[:, 0] < bx1 - 1)]
assert len(band), "expected travel stitches between the squares"
assert band[:, 1].min() > by0 + 0.2 and band[:, 1].max() < by1 - 0.2, "travel left the blue band"

# if the user sews blue first, or skips it, there is nothing to hide under: red must trim
for kw, why in ((dict(color_order=[BLUE, RED]), "blue first"), (dict(skip_colors=[BLUE]), "blue skipped")):
    d2 = digitize(rgba, Settings(max_width_mm=70, max_height_mm=30, **kw))
    r2 = block_of(d2, RED)
    print(f"{why}: red trims {trims(r2)}")
    assert trims(r2) == 2, f"{why}: red can't hide travel (trims {trims(r2)})"
d2 = digitize(rgba, Settings(max_width_mm=70, max_height_mm=30, color_order=[BLUE, RED]))
assert d2.blocks[0].rgb == block_of(d2, BLUE).rgb, "the user's colour order is used"

# ---- fill direction follows the shape
mask = np.zeros((400, 400), np.uint8)
cv2.ellipse(mask, (200, 200), (170, 50), 30, 0, 360, 1, -1)
assert abs(shape_angle(mask, 0.0) - 30) < 2, shape_angle(mask, 0.0)
disc = np.zeros((400, 400), np.uint8)
cv2.circle(disc, (200, 200), 150, 1, -1)
assert shape_angle(disc, 77.0) == 77.0, "round shapes keep the fallback angle"


def main_angle(block) -> float:
    c = block.cmds
    ok = (c[1:, 2] == st.STITCH) & (c[:-1, 2] == st.STITCH)
    v = np.diff(c[:, :2], axis=0)[ok]
    v = v[np.hypot(*v.T) > 1.5]
    ang = np.degrees(np.arctan2(v[:, 1], v[:, 0])) % 180
    hist, edges = np.histogram(ang, bins=36, range=(0, 180))
    return float(edges[hist.argmax()] + 2.5)


img = np.full((400, 400, 3), 255, np.uint8)
cv2.ellipse(img, (200, 200), (180, 45), 30, 0, 360, RED, -1)
rgba = np.dstack([img, np.full(img.shape[:2], 255, np.uint8)])
follow = main_angle(digitize(rgba, Settings(max_width_mm=60, max_height_mm=60)).blocks[0])
vary = main_angle(digitize(rgba, Settings(max_width_mm=60, max_height_mm=60, fill_follow=False)).blocks[0])
print(f"fill angle: follow {follow:.0f}°, vary {vary:.0f}°")
assert abs(follow - 30) <= 5, follow
assert abs(vary - FILL_ANGLES[0]) <= 5, vary
print("quality ok")
