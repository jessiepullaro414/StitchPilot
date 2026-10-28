"""Drive the stitch editor with real mouse/keyboard events (offscreen)."""
import os
import sys
from pathlib import Path

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pyembroidery as pe  # noqa: E402
from PySide6.QtCore import QEventLoop, QPoint, QPointF, QSettings, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from stitchpilot import gui  # noqa: E402
from stitchpilot.export import export  # noqa: E402

here = Path(__file__).parent
out = here / "out"
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING DIALOG:", a[2] if len(a) > 2 else a))
app = QApplication(sys.argv)
gui.apply_light_theme(app)
w = gui.MainWindow()
w.auto_ai.setChecked(False)
w.resize(1440, 900)
w.show()
v = w.v_st


def wait_idle(timeout=60):
    loop = QEventLoop()
    t = QTimer()
    t.timeout.connect(lambda: (not w.busy and not w.debounce.isActive()) and loop.quit())
    t.start(100)
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()


def vpos(mm) -> QPoint:
    scene = QPointF(*((np.asarray(mm) - v.origin) * v.ppm))
    return v.mapFromScene(scene)


def count():
    return w.design.stitch_count


w.open_path(str(here / "samples" / "badge.png"))
wait_idle()
n0 = count()
w.edit_btn.setChecked(True)
app.processEvents()
v.scale(4, 4)  # zoom in so points are far apart on screen
v.fit = False
pt = v.pts[len(v.pts) // 3]
v.centerOn(QPointF(*((pt - v.origin) * v.ppm)))
app.processEvents()

# click selects one needle point, Delete removes it
QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vpos(pt))
assert len(v.sel) == 1, v.sel
QTest.keyClick(v, Qt.Key.Key_Delete)
print("delete one:", n0, "->", count())
assert count() == n0 - 1 and w.edited

# undo / redo
w.undo()
assert count() == n0
w.redo()
assert count() == n0 - 1
w.undo()

# double-click picks the whole run; delete it
QTest.mouseDClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vpos(pt))
run = len(v.sel)
QTest.keyClick(v, Qt.Key.Key_Delete)
print("delete run of", run, "points:", n0, "->", count())
assert run > 10 and count() == n0 - run
w.undo()

# drag a selected point 2 mm to the right
pt = v.pts[len(v.pts) // 2]
QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vpos(pt))
(bi, row), = v.sel
QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vpos(pt))
QTest.mouseMove(v.viewport(), vpos(pt + (1.0, 0)))
QTest.mouseMove(v.viewport(), vpos(pt + (2.0, 0)))
QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vpos(pt + (2.0, 0)))
moved = w.design.blocks[bi].cmds[row, :2]
print("moved point by", np.round(moved - pt, 2))
assert abs(moved[0] - pt[0] - 2.0) < 0.3

# add tool: sew 9 mm on from the selected point -> 3 new stitches of 3 mm
w.set_tool("add")
QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vpos(moved + (0, 9.0)))
print("added from point:", count() - n0, "stitches")
assert count() - n0 in (3, 4)  # clicks land on whole pixels, so 9 mm may come out as 9.03 mm
# nothing selected: a click starts a new run in the chosen colour
QTest.keyClick(v, Qt.Key.Key_Escape)
before = count()
QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vpos(moved + (5.0, 5.0)))
QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vpos(moved + (11.0, 5.0)))
print("new run:", count() - before, "stitches")
assert count() - before in (3, 4)
w.set_tool("select")

# box select: centred so it's on screen, started from an empty spot (pressing on a point drags it)
v.centerOn(QPointF(*((pt - v.origin) * v.ppm)))
app.processEvents()
corner = min((pt + (dx, dy) for dx in (-3, 3) for dy in (-3, 3)),
             key=lambda c: -np.hypot(*(v.pts - c).T).min())
gap = np.hypot(*(v.pts - corner).T).min()
while v.PICK_PX / (v.transform().m11() * v.ppm) > 0.8 * gap:  # zoomed in enough that the corner isn't on a point
    v.scale(2, 2)  # (the window's layout, and so the starting zoom, differs between Windows and macOS)
v.centerOn(QPointF(*((pt - v.origin) * v.ppm)))
app.processEvents()
a, b = vpos(corner), vpos(2 * pt - corner)
QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
QTest.mouseMove(v.viewport(), b)
QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, b)
print("box selected:", len(v.sel), "points")
assert len(v.sel) > 5
app.processEvents()
w.grab().save(str(out / "gui_edit.png"))

# settings changes re-convert and put the edits back (tests/edit_keep_smoke.py checks where); edits survive export
w.simp.setValue(90)
assert w.debounce.isActive()
wait_idle()
assert w.edited
p = export(w.design, out / "edited.dst")
print("exported edited design:", pe.read(str(p)).count_stitches(), "stitches in file")

# discard -> re-converted from the picture
w.discard_edits()
wait_idle()
print("after discard:", count(), "stitches, edited =", w.edited)
assert not w.edited
w.close()
print("editor ok")
