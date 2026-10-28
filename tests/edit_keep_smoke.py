"""Hand edits survive settings changes: erase, move and add stitches, then change density, size, rotation and
mirroring; the edits come back in the same places on the re-converted stitches. Also undo/redo afterwards,
projects (edits re-applied after reopening), and older projects whose edits can't be re-applied."""
import sys
from pathlib import Path

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)

import numpy as np  # noqa: E402
import shapely  # noqa: E402
from shapely import affinity  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from stitchpilot import editing, gui, project  # noqa: E402
from stitchpilot import stitches as st  # noqa: E402

here = Path(__file__).parent
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING DIALOG:", a[2] if len(a) > 2 else a))
app = QApplication(sys.argv)
gui.apply_light_theme(app)


def wait_idle(win, timeout=60):
    loop = QEventLoop()
    t = QTimer()
    t.timeout.connect(lambda: (not win.busy and not win.debounce.isActive()) and loop.quit())
    t.start(100)
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()


def inside(win, op, margin=0.0, blocks=None) -> int:
    """How many of the op's thread's stitches are in its area now (more than margin mm inside it).
    After re-converting, fill edges are pulled out a little past a shape's outline: hence the margin."""
    d = win.design
    blocks = blocks if blocks is not None else d.blocks
    bi = editing._block_for(blocks, op.rgb)
    zone = affinity.scale(op.area, d.width_mm, d.height_mm, origin=(0, 0)).buffer(-margin)
    c = blocks[bi].cmds
    pts = c[c[:, 2] == st.STITCH, :2]
    return int(shapely.contains_xy(zone, pts[:, 0], pts[:, 1]).sum()) if not zone.is_empty else 0


def near(win, rgb, xy_frac, mm=0.6) -> bool:
    d = win.design
    c = d.blocks[editing._block_for(d.blocks, rgb)].cmds
    pts = c[c[:, 2] == st.STITCH, :2]
    return bool((np.hypot(*(pts - (xy_frac[0] * d.width_mm, xy_frac[1] * d.height_mm)).T) <= mm).any())


w = gui.MainWindow()
w.auto_ai.setChecked(False)
w.show()
w.open_path(str(here / "samples" / "badge.png"))
wait_idle(w)
d = w.design
n0 = d.stitch_count

# ---- edit: erase a whole run of the biggest colour, nudge a few points, sew a new run of colour 0
big = max(range(len(d.blocks)), key=lambda i: d.blocks[i].area_mm2)
c = d.blocks[big].cmds
mid = np.nonzero(c[:, 2] == st.STITCH)[0][len(c) // 2]
w.v_st.sel = editing.run_of(d.blocks, (big, int(mid)))
w.edit_delete()
erase = w.edit_log[-1][0]
assert erase.kind == "erase" and inside(w, erase) == 0
orig = inside(w, erase, 0.6, blocks=w.undo_stack[-1])
assert orig > 100, orig
w.v_st.sel = {(0, int(r)) for r in np.nonzero(w.design.blocks[0].cmds[:, 2] == st.STITCH)[0][:5]}
w.edit_move(1.5, 0.0)
w.add_color.setCurrentIndex(0)
w.v_st.sel = set()
W, H = w.design.width_mm, w.design.height_mm
w.edit_add((W * 0.03, H * 0.03))
w.edit_add((W * 0.03 + 6, H * 0.03))  # sews on from the new point
rgb0 = w.design.blocks[0].source_rgb
assert len(w.edit_log) == 4 and w.edited
print("edited:", n0, "->", w.design.stitch_count, "stitches,", len(w.edit_log), "edit steps")
start, end = (0.03, 0.03), ((W * 0.03 + 6) / W, 0.03)

# ---- change density: re-converts, edits come back
assert "put back" in w.edit_msg.text()
w.sp_fill.setValue(w.sp_fill.value() + 0.15)
assert w.debounce.isActive(), "settings aren't paused any more"
wait_idle(w)
assert w.edited and len(w.edit_log) == 4
print("after density change: stitches in the erased area", orig, "->", inside(w, erase, 0.6),
      "| the conversion cut it out:", bool(w._baked))
assert w._baked and inside(w, erase, 0.6) <= 3
assert near(w, rgb0, start) and near(w, rgb0, end), "the added run is back"
added_runs = sum(1 for r in w.design.blocks[editing._block_for(w.design.blocks, rgb0)].cmds if r[2] == st.TRIM)

# ---- undo / redo work on the new stitches
w.undo()
w.undo()  # both add steps
assert not near(w, rgb0, start)
w.undo()
w.undo()  # the erase: converts again without it
wait_idle(w)
assert inside(w, erase, 0.6) > 100 and len(w.edit_log) == 0 and len(w.redo_log) == 4
w.redo()
wait_idle(w)
assert inside(w, erase, 0.6) <= 3
w.redo(); w.redo(); w.redo()  # noqa: E702
assert near(w, rgb0, end) and len(w.edit_log) == 4

# ---- bigger: edits are in proportion to the design
w0 = w.design.width_mm
w.w_spin.setValue(w.w_spin.value() * 1.3)
wait_idle(w)
print(f"after resize: {w0:.1f} -> {w.design.width_mm:.1f} mm wide, stitches deep in the erased area: {inside(w, erase, 0.6)}")
assert w.design.width_mm > w0 * 1.1 and inside(w, erase, 0.6) <= 3 and near(w, rgb0, end)

# ---- rotate and mirror: the edits turn with the picture
w._rotate()
wait_idle(w)
erase = w.edit_log[0][0]
assert inside(w, erase, 0.6) <= 3, "erased area rotated along"
f = editing.orientation(90, False)
assert near(w, rgb0, f(*start)) and near(w, rgb0, f(*end)), "added run rotated along"
w.mir_btn.setChecked(True)
wait_idle(w)
erase = w.edit_log[0][0]
assert inside(w, erase, 0.6) <= 3
g = editing.orientation(90, True)
assert near(w, rgb0, g(*start))
print("rotated + mirrored: edits followed")

# ---- projects keep the steps: reopen, change a setting, edits still re-applied
p = _testenv.TMP / "kept-edits.stitchpilot"
w._write_project(p)
w2 = gui.MainWindow()
w2.auto_ai.setChecked(False)
w2.open_project(p)
wait_idle(w2)
assert w2.edited and not w2.edits_fixed and len(w2.edit_log) == 4
assert w2.design.stitch_count == w.design.stitch_count, "reopened exactly as saved"
w2.sp_fill.setValue(w2.sp_fill.value() - 0.1)
wait_idle(w2)
assert inside(w2, w2.edit_log[0][0], 0.6) <= 3 and near(w2, rgb0, g(*end))
print("reopened project re-applies edits:", w2.design.stitch_count, "stitches")

# ---- an older project (edited stitches, no steps): settings stay paused, as before
proj = project.load(p)
state = {k: v for k, v in proj.state.items() if k != "edit_log"}
old = _testenv.TMP / "old-edits.stitchpilot"
project.save(old, state, proj.source_name, proj.source_bytes, proj.blocks, proj.size_mm)
w3 = gui.MainWindow()
w3.auto_ai.setChecked(False)
w3.open_project(old)
wait_idle(w3)
assert w3.edited and w3.edits_fixed and "older" in w3.edit_msg.text()
n3 = w3.design.stitch_count
w3.sp_fill.setValue(w3.sp_fill.value() + 0.1)
assert not w3.debounce.isActive() and w3.design.stitch_count == n3
w3.discard_edits()
wait_idle(w3)
assert not w3.edited and not w3.edits_fixed
for win in (w, w2, w3):
    win.close()
print("edits kept OK")
