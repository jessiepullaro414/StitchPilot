"""Resize a design by clicking it in the preview and dragging a corner (real mouse events, offscreen)."""
import sys
from pathlib import Path

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from PySide6.QtCore import QEventLoop, QPoint, QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from stitchpilot import gui  # noqa: E402

here = Path(__file__).parent
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING DIALOG:", a[2] if len(a) > 2 else a))
app = QApplication(sys.argv)
gui.apply_light_theme(app)
w = gui.MainWindow()
w.auto_ai.setChecked(False)
w.resize(1440, 900)
w.show()
v = w.v_st
NONE = Qt.KeyboardModifier.NoModifier
LEFT = Qt.MouseButton.LeftButton


def wait_idle(timeout=60):
    loop = QEventLoop()
    t = QTimer()
    t.timeout.connect(lambda: (not w.busy and not w.debounce.isActive()) and loop.quit())
    t.start(100)
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()


def vpos(mm) -> QPoint:
    return v.mapFromScene(QPointF(*((np.asarray(mm, float) - v.origin) * v.ppm)))


def size():
    x0, y0, x1, y1 = w.design.bounds()
    return x1 - x0, y1 - y0


def drag(a, b):
    QTest.mousePress(v.viewport(), LEFT, NONE, vpos(a))
    for t in np.linspace(0, 1, 6)[1:]:
        QTest.mouseMove(v.viewport(), vpos(np.asarray(a) + (np.asarray(b) - a) * t))
    QTest.mouseRelease(v.viewport(), LEFT, NONE, vpos(b))
    app.processEvents()


w.open_path(str(here / "samples" / "badge.png"))
wait_idle()
w0, h0 = size()
print(f"opened: {w0:.1f} x {h0:.1f} mm")
assert w.can_resize() and "Click the design to resize it" in w.stats.text()

# a click on the design picks it; a corner drag before that does nothing but pan
x0, y0, x1, y1 = w.design.bounds()
centre = np.array([(x0 + x1) / 2, (y0 + y1) / 2])
QTest.mouseClick(v.viewport(), LEFT, NONE, vpos(centre))
assert v.picked, "clicking the design should pick it"
assert v._corner_at(np.array([x1, y1])) == 2

# drag the bottom-right corner halfway to the centre: the box follows, then it stitches again at half size
corner = np.array([x1, y1])
QTest.mousePress(v.viewport(), LEFT, NONE, vpos(corner))
QTest.mouseMove(v.viewport(), vpos(centre + (corner - centre) * 0.75))
QTest.mouseMove(v.viewport(), vpos(centre + (corner - centre) * 0.5))
b = v._box()
assert abs((b[2] - b[0]) - w0 / 2) < 1.5, ("live box", b)
v.grab().save(str(here / "out" / "resize_drag.png"))
QTest.mouseRelease(v.viewport(), LEFT, NONE, vpos(centre + (corner - centre) * 0.5))
assert abs(w.size_frac - 0.5) < 0.03, w.size_frac
wait_idle()
w1, h1 = size()
print(f"dragged to half: {w1:.1f} x {h1:.1f} mm (frac {w.size_frac:.2f})")
assert abs(w1 - w0 / 2) < 2 and abs(h1 - h0 / 2) < 2
assert abs(v._box()[2] - v._box()[0] - w1) < 1e-6, "the box shows the new design's real size"
assert v.picked, "it stays picked for another drag"
v.grab().save(str(here / "out" / "resize_done.png"))

# it can't grow past the hoop
x0, y0, x1, y1 = w.design.bounds()
c = np.array([(x0 + x1) / 2, (y0 + y1) / 2])
drag([x1, y1], c + ([x1, y1] - c) * 5)
wait_idle()
print(f"dragged way out: {size()[0]:.1f} mm (frac {w.size_frac:.2f})")
assert w.size_frac == 1.0 and abs(size()[0] - w0) < 1

# ... or shrink below 10 mm across
x0, y0, x1, y1 = w.design.bounds()
c = np.array([(x0 + x1) / 2, (y0 + y1) / 2])
drag([x1, y1], c + ([x1, y1] - c) * 0.01)
wait_idle()
print(f"dragged to nothing: {max(size()):.1f} mm (frac {w.size_frac:.3f})")
assert 9 <= max(size()) <= 12

# a tiny wobble while picking isn't a resize
frac = w.size_frac
x0, y0, x1, y1 = w.design.bounds()
drag([x1, y1], [x1 + 0.01, y1])
assert w.size_frac == frac and not w.debounce.isActive()

# Esc or a click outside drops it
c = np.array([(x0 + x1) / 2, (y0 + y1) / 2])
QTest.keyClick(v, Qt.Key.Key_Escape)
assert not v.picked
QTest.mouseClick(v.viewport(), LEFT, NONE, vpos(c))
assert v.picked
QTest.mouseClick(v.viewport(), LEFT, NONE, vpos(c + [0, 60]))
assert not v.picked

# a smaller design keeps its size in a bigger hoop; it's saved with the project
w.size_frac = 0.5
w.changed()
wait_idle()
before = size()
big = max(gui.HOOPS, key=lambda n: gui.HOOPS[n][0] * gui.HOOPS[n][1])
w.hoop.setCurrentText(big)
wait_idle()
print(f"hoop {big}: {before[0]:.1f} -> {size()[0]:.1f} mm wide")
assert abs(size()[0] - before[0]) < 2
state = w.get_state()
assert abs(state["size_frac"] - w.size_frac) < 1e-9
w.set_state({**state, "size_frac": 0.3})
assert abs(w.size_frac - 0.3) < 1e-9
w.set_state({k: v for k, v in state.items() if k != "size_frac"})  # projects from before resizing
assert w.size_frac == 1.0

# a new picture starts at full size and unpicked; embroidery files can't be resized
w.size_frac = 0.5
v.picked = True
w.open_path(str(here / "samples" / "badge.png"))
wait_idle()
assert w.size_frac == 1.0 and not v.picked
dst = next((here / "out").glob("*.dst"), None)
if dst is not None:
    w.open_path(str(dst))
    app.processEvents()
    assert not w.can_resize()
    QTest.mouseClick(v.viewport(), LEFT, NONE, v.viewport().rect().center())
    assert not v.picked
print("resize ok")
