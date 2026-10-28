"""Complexity badge, problem map, compare view, Simplicity slider and 'Make it embroidery-friendly'."""
import os
import sys
from pathlib import Path

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEventLoop, QSettings, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from stitchpilot import gui  # noqa: E402

here = Path(__file__).parent
out = here / "out"
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING DIALOG:", a[2] if len(a) > 2 else a))
app = QApplication(sys.argv)
gui.apply_light_theme(app)
w = gui.MainWindow()
w.auto_ai.setChecked(False)
w.resize(1440, 900)
w.show()


def wait_idle(timeout=90):
    loop = QEventLoop()
    t = QTimer()
    t.timeout.connect(lambda: (not w.busy and not w.debounce.isActive()) and loop.quit())
    t.start(100)
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()


def state(tag):
    s, r, d = w.source_cx, w.result_cx, w.design
    print(f"{tag}: picture={s.level}({s.score}) result={r.level}({r.score}) friendly={r.friendly} "
          f"colours={len(d.blocks)} stitches={d.stitch_count} trims={d.trim_count}")


# a simple logo stays simple
w.open_path(str(here / "samples" / "logo.jpg"))
wait_idle()
state("logo")
assert w.source_cx.level == "Simple" and w.result_cx.friendly

# the busy picture is flagged, problem spots shown
w.open_path(str(here / "samples" / "busy.jpg"))
wait_idle()
state("busy, default")
assert w.source_cx.level in ("Complex", "Very complex")
before = (w.design.stitch_count, w.design.trim_count)
w.show_problems.setChecked(True)
app.processEvents()
w.grab().save(str(out / "gui_simplify_problems.png"))

# slider: more simplicity -> fewer colours/shapes/trims
w.simp.setValue(80)
wait_idle()
state("busy, simplicity 80")
assert w.design.trim_count <= before[1]

# one click
w.simp.setValue(10)
wait_idle()
w.make_friendly()
wait_idle()
wait_idle()
state(f"busy, friendly (slider {w.simp.value()}, colours {w.colors.currentText()})")
assert w.result_cx.friendly
w.show_problems.setChecked(False)
w.compare.setChecked(True)
app.processEvents()
w.grab().save(str(out / "gui_simplify_compare.png"))

# finer controls: remove hair-thin lines
w.simp_more.setChecked(True)
w.thin.setCurrentIndex(w.thin.findData("remove"))
wait_idle()
state("busy, thin lines removed")
w.close()
print("simplify ok")
