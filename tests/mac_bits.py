"""Mac-specific code paths that can be exercised anywhere: Finder/Dock file-open events, trackpad scrolling."""
import sys
from pathlib import Path

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QFileOpenEvent, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from stitchpilot import gui  # noqa: E402

here = Path(__file__).parent
app = gui.App(sys.argv)
gui.apply_light_theme(app)

# a file opened before the window exists is remembered, then opened
QApplication.sendEvent(app, QFileOpenEvent(str(here / "samples" / "heart.svg")))
assert app.pending_file and app.pending_file.endswith("heart.svg")
w = gui.MainWindow()
w.auto_ai.setChecked(False)
app.window = w
w.resize(1200, 800)
w.show()
# ...and once it exists, Finder/Dock opens go straight to it
QApplication.sendEvent(app, QFileOpenEvent(str(here / "samples" / "badge.png")))
assert w.path and w.path.name == "badge.png", w.path
print("file-open events ok:", w.path.name)

v = w.v_orig
v.scale(3, 3)
v.fit = False
app.processEvents()
pos = QPointF(200, 200)


def wheel(pixel, angle, phase, mods=Qt.KeyboardModifier.NoModifier):
    e = QWheelEvent(pos, v.viewport().mapToGlobal(pos), pixel, angle, Qt.MouseButton.NoButton, mods, phase, False)
    QApplication.sendEvent(v.viewport(), e)


m0, s0 = v.transform().m11(), v.verticalScrollBar().value()
wheel(QPoint(0, -40), QPoint(0, -120), Qt.ScrollPhase.ScrollUpdate)  # two-finger trackpad scroll
m1, s1 = v.transform().m11(), v.verticalScrollBar().value()
print(f"trackpad scroll: zoom {m0:.2f}->{m1:.2f}, scroll {s0}->{s1}")
assert abs(m1 - m0) < 1e-6 and s1 != s0, "trackpad scrolling should pan, not zoom"
wheel(QPoint(0, 0), QPoint(0, 120), Qt.ScrollPhase.NoScrollPhase)  # mouse wheel
m2 = v.transform().m11()
print(f"mouse wheel: zoom {m1:.2f}->{m2:.2f}")
assert m2 > m1, "mouse wheel should zoom"
wheel(QPoint(0, 40), QPoint(0, 120), Qt.ScrollPhase.ScrollUpdate, Qt.KeyboardModifier.ControlModifier)
print(f"Ctrl/Cmd + trackpad: zoom {m2:.2f}->{v.transform().m11():.2f}")
assert v.transform().m11() > m2
w.close()
print("mac bits ok")
