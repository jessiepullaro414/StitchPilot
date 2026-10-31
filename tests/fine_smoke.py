"""Fine detail: small lettering and hairline art sew cleanly (round curves, dots kept, letters whole)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from shapely.geometry import LineString, Polygon  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from stitchpilot import stitches as st  # noqa: E402
from stitchpilot.digitize import digitize  # noqa: E402
from stitchpilot.render import render  # noqa: E402
from stitchpilot.settings import Settings, TextItem  # noqa: E402

out = Path(__file__).parent / "out"
out.mkdir(exist_ok=True)


def text(t, h, font="Arial", bold=False):
    return digitize(None, Settings(texts=[TextItem(t, font=font, bold=bold, height_mm=h)]))


def sewn(d):
    """Union of every sewn stitch (as thread about 0.4 mm wide)."""
    segs = []
    for b in d.active:
        c = b.cmds
        for a, z in zip(c[:-1], c[1:]):
            if a[2] == st.STITCH and z[2] == st.STITCH and np.hypot(*(z[:2] - a[:2])) > 1e-6:
                segs.append(LineString([a[:2], z[:2]]).buffer(0.2))
    return unary_union(segs)


# ---- run stitches follow curves: no stitch strays from a small circle, straight lines keep long stitches
t = np.linspace(0, 2 * np.pi, 400, endpoint=False)
circle = np.column_stack([2 * np.cos(t), 2 * np.sin(t)])
pts = st.follow(circle, 2.0, closed=True)
mid = (pts[:-1] + pts[1:]) / 2
assert np.abs(np.hypot(*mid.T) - 2).max() < 0.06, "run stitches cut a small circle into a polygon"
line = st.follow(np.array([[0.0, 0.0], [10.0, 0.0]]), 2.0)
assert len(line) == 6, f"a straight line should keep 2 mm stitches, got {len(line) - 1}"

# ---- 8 mm regular lettering: every stroke satin (even weight), the dot of the i and the full stop kept
d = text("Hi.", 8)
assert {sh.kind for sh in d.shapes} == {"satin"}, [sh.kind for sh in d.shapes]
assert len(d.shapes) == 4, f"H, i, its dot and the full stop: got {len(d.shapes)} shapes"

# ---- the eye of a small e stays open, and no stitch crosses it
d = text("e", 5)
eyes = [len(p.interiors) for sh in d.shapes for p in st._polys(sh.geom)]
assert max(eyes) >= 1, "the eye of a 5 mm e was filled in"
eye = max((ring for sh in d.shapes for p in st._polys(sh.geom) for ring in p.interiors), key=lambda r: r.length)
eye = Polygon(eye).buffer(-0.25)
covered = sewn(d).intersection(eye).area / max(eye.area, 1e-9)
assert covered < 0.35, f"{covered:.0%} of the e's eye is stitched over"

# ---- curving serif letters are sewn whole (corner cuts used to slice away the far side of a C)
d = text("Ce", 10, font="Times New Roman")
stitched = sewn(d)
for sh in d.shapes:
    share = stitched.intersection(sh.geom).area / sh.geom.area
    assert share > 0.9, f"only {share:.0%} of a {sh.kind} letter is stitched"

# ---- no thread across bare fabric between small letters: those hops are trimmed or hidden
d = text("Fine details matter", 5)
assert {sh.kind for sh in d.shapes} == {"satin"}, "5 mm letters should all be satin, not some thin runs"
letters = unary_union([sh.geom for sh in d.shapes]).buffer(0.3)
for b in d.active:
    c = b.cmds
    for a, z in zip(c[:-1], c[1:]):
        if a[2] == st.STITCH and z[2] == st.STITCH:
            seg = LineString([a[:2], z[:2]])
            if seg.length > 0.5:
                assert seg.difference(letters).length < 0.5, f"a stitch crosses bare fabric at {a[:2]}"

# ---- hairline art: 0.6 mm and wider strokes are satin, hairlines a run; a fill never sews line work
ppmm = 20
img = np.full((30 * ppmm, 50 * ppmm, 3), 255, np.uint8)
for i, w in enumerate((0.3, 0.6, 1.0)):
    y = (5 + 9 * i) * ppmm
    xs = np.linspace(3, 45, 120)
    poly = np.column_stack([xs * ppmm, y + 2 * ppmm * np.sin(xs / 5)]).astype(np.int32)
    cv2.polylines(img, [poly], False, (0, 0, 0), max(1, round(w * ppmm)), cv2.LINE_AA)
rgba = np.dstack([img, np.full(img.shape[:2], 255, np.uint8)])
d = digitize(rgba, Settings(max_width_mm=50, max_height_mm=30))
by_width = sorted((sh.width_mm, sh.kind) for sh in d.shapes)
assert [k for _, k in by_width] == ["run", "satin", "satin"], by_width

cv2.imwrite(str(out / "fine_lines.png"), cv2.cvtColor(render(d, 12), cv2.COLOR_RGB2BGR))
print("fine detail ok")
