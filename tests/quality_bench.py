"""Stitch-quality numbers for every sample plus two lettering designs: trims, jumps, density.

    python tests/quality_bench.py [--save before.json] [--compare before.json]

Renders go to tests/out/bench_<name>.png. Used to check that routing and corner changes help.
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from stitchpilot import stitches as st  # noqa: E402
from stitchpilot.analysis import density_grid, dense_share  # noqa: E402
from stitchpilot.digitize import digitize  # noqa: E402
from stitchpilot.imageio import load_image  # noqa: E402
from stitchpilot.render import render  # noqa: E402
from stitchpilot.settings import Settings, TextItem  # noqa: E402

here = Path(__file__).parent
out = here / "out"
out.mkdir(exist_ok=True)

LETTERING = {
    "letters_sans": TextItem("LAZY WAVE", font="Arial", bold=True, height_mm=18),
    "letters_thin": TextItem("MIXTURE", font="Arial", bold=False, height_mm=25),
}


def designs():
    for p in sorted((here / "samples").iterdir()):
        yield p.stem, load_image(p), Settings()
    for name, t in LETTERING.items():
        yield name, None, Settings(texts=[t])


def measure(d) -> dict:
    flags = np.vstack([b.cmds for b in d.active])[:, 2]
    g, _ = density_grid(d)
    stitched = g[g > 0.8]
    return {"colours": len(d.active), "stitches": d.stitch_count, "trims": int((flags == st.TRIM).sum()),
            "jumps": int((flags == st.JUMP).sum()), "dense": round(dense_share(d), 4),
            "p99": round(float(np.percentile(stitched, 99)), 2) if len(stitched) else 0.0}


def main():
    args = sys.argv[1:]
    save = args[args.index("--save") + 1] if "--save" in args else None
    cmp = json.loads(Path(args[args.index("--compare") + 1]).read_text()) if "--compare" in args else {}
    only = [a for a in args if not a.startswith("--") and not a.endswith(".json")]
    res = {}
    for name, rgba, s in designs():
        if only and name not in only:
            continue
        t = time.time()
        d = digitize(rgba, s)
        m = measure(d)
        m["secs"] = round(time.time() - t, 1)
        res[name] = m
        cv2.imwrite(str(out / f"bench_{name}.png"), cv2.cvtColor(render(d, 8), cv2.COLOR_RGB2BGR))
        line = "  ".join(f"{k}={v}" for k, v in m.items())
        if name in cmp:
            b = cmp[name]
            line += f"   | trims {b['trims']}->{m['trims']}  jumps {b['jumps']}->{m['jumps']}  p99 {b['p99']}->{m['p99']}"
        print(f"{name:14s} {line}")
    if save:
        Path(save).write_text(json.dumps(res, indent=1))
    if cmp:
        tb = sum(cmp[n]["trims"] for n in res if n in cmp)
        ta = sum(res[n]["trims"] for n in res if n in cmp)
        print(f"\ntrims: {tb} -> {ta} ({(ta - tb) / max(tb, 1):+.0%})")


if __name__ == "__main__":
    main()
