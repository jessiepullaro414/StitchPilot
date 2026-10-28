"""Checks for lettering, appliqué, borders, jumps-only, rotation, density and reading files back."""
import sys
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stitchpilot import stitches as st  # noqa: E402
from stitchpilot.analysis import dense_share  # noqa: E402
from stitchpilot.digitize import digitize  # noqa: E402
from stitchpilot.export import export, read_design  # noqa: E402
from stitchpilot.imageio import load_image  # noqa: E402
from stitchpilot.render import render  # noqa: E402
from stitchpilot.settings import Settings, TextItem  # noqa: E402

here = Path(__file__).parent
out = here / "out"
out.mkdir(exist_ok=True)
S = Settings()


def save(name, d, **kw):
    cv2.imwrite(str(out / f"feat_{name}.png"), cv2.cvtColor(render(d, 8, **kw), cv2.COLOR_RGB2BGR))


def flags(d):
    return {k: int(sum((b.cmds[:, 2] == v).sum() for b in d.active))
            for k, v in (("stitch", st.STITCH), ("jump", st.JUMP), ("trim", st.TRIM), ("stop", st.STOP))}


# 1. text only (a name design)
d = digitize(None, replace(S, texts=[TextItem("Emma", font="Georgia", height_mm=25, rgb=(200, 40, 90))]))
print("text only:", f"{d.width_mm:.0f}x{d.height_mm:.0f}mm", [b.kinds for b in d.blocks], flags(d))
save("text_only", d)

# 2. picture + text below and above
logo = load_image(here / "samples" / "badge.png")
d = digitize(logo, replace(S, texts=[TextItem("HAPPY", height_mm=10, rgb=(20, 20, 20), position="above"),
                                     TextItem("Robot Club", height_mm=8, rgb=(40, 120, 200))]))
print("picture + text:", f"{d.width_mm:.0f}x{d.height_mm:.0f}mm", len(d.blocks), "colours", flags(d))
save("text_picture", d)

# 3. appliqué for the big blue face, satin patch border
d = digitize(logo, replace(S, applique=[(80, 200, 220)], border="satin", border_rgb=(200, 30, 60), border_offset=1.0))
f = flags(d)
print("appliqué + border:", [b.kinds for b in d.blocks], f)
assert f["stop"] == 2, "appliqué needs two stops"
assert d.blocks[-1].kinds == {"border": 1}
save("applique_border", d)

# 4. jumps only (no trimmer) and rotation
d = digitize(logo, replace(S, trims=False, rotate=90, mirror=True))
f = flags(d)
print("jumps only, rotated:", f"{d.width_mm:.0f}x{d.height_mm:.0f}mm", f)
assert f["trim"] == 0 and d.height_mm > d.width_mm

# 5. density: normal designs should be fine, double-stacked should warn
d = digitize(logo, S)
print("dense share (normal):", f"{dense_share(d):.3f}", d.notes)
dd = digitize(logo, replace(S, fill_spacing=0.2, satin_spacing=0.2))
print("dense share (2x density):", f"{dense_share(dd):.3f}", [n[:40] for n in dd.notes])

# 6. read an embroidery file back for viewing / converting
p = export(d, out / "feat_roundtrip.pes")
r = read_design(p)
print("read back:", len(r.blocks), "colours", r.stitch_count, "stitches", [b.thread for b in r.blocks])
export(r, out / "feat_roundtrip.dst")
save("roundtrip", r, hoop_mm=(100, 100), background=(30, 40, 70))
