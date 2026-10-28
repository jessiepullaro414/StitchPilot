"""End-to-end check: digitize each sample, export every format, read files back."""
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pyembroidery as pe

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stitchpilot.digitize import digitize  # noqa: E402
from stitchpilot.export import FORMATS, export, match_threads  # noqa: E402
from stitchpilot.imageio import load_image  # noqa: E402
from stitchpilot.render import render  # noqa: E402
from stitchpilot.settings import Settings  # noqa: E402

here = Path(__file__).parent
outdir = here / "out"
outdir.mkdir(exist_ok=True)
names = sys.argv[1:] or [p.name for p in sorted((here / "samples").iterdir())]
for name in names:
    t = time.time()
    rgba = load_image(here / "samples" / name)
    d = digitize(rgba, Settings())
    match_threads(d, "Brother", snap=False)
    dt = time.time() - t
    kinds = {}
    for b in d.blocks:
        for k, v in b.kinds.items():
            kinds[k] = kinds.get(k, 0) + v
    print(f"{name}: {d.width_mm:.0f}x{d.height_mm:.0f}mm colours={len(d.blocks)} stitches={d.stitch_count} "
          f"trims={d.trim_count} kinds={kinds} time={dt:.1f}s ~{d.est_minutes():.0f}min")
    for b in d.blocks:
        print(f"   {b.rgb} {b.thread:28s} {b.stitch_count:6d} {b.kinds}")
    lens = []
    for b in d.active:
        c = b.cmds
        ok = (c[1:, 2] == 0) & np.isin(c[:-1, 2], (0, 1))
        lens.append(np.hypot(*(c[1:, :2] - c[:-1, :2]).T)[ok])
    lens = np.concatenate(lens)
    print(f"   stitch length: max={lens.max():.2f}mm  >7mm={int((lens > 7.01).sum())}  <0.2mm={int((lens < 0.2).sum())}")
    assert lens.max() <= 8.0
    for n in d.notes:
        print("   note:", n)
    stem = Path(name).stem
    cv2.imwrite(str(outdir / f"{stem}_stitches.png"), cv2.cvtColor(render(d, 8), cv2.COLOR_RGB2BGR))
    cv2.imwrite(str(outdir / f"{stem}_colors.png"), cv2.cvtColor(d.prepared.color_preview(), cv2.COLOR_RGBA2BGRA))
    for ext in FORMATS.values():
        p = export(d, outdir / f"{stem}.{ext}")
        back = pe.read(str(p))
        assert back is not None and back.count_stitches() > 0, ext
    back = pe.read(str(outdir / f"{stem}.dst"))
    x0, y0, x1, y1 = back.bounds()
    print(f"   dst readback: {back.count_stitches()} stitches, {(x1 - x0) / 10:.1f}x{(y1 - y0) / 10:.1f}mm, "
          f"colour changes={back.count_color_changes()}")
    pe.write_png(back, str(outdir / f"{stem}_dst_readback.png"))
