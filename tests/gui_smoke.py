"""Open the GUI offscreen, exercise every feature and save screenshots."""
import sys
from pathlib import Path

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QDesktopServices, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from stitchpilot import gui  # noqa: E402
from stitchpilot.digitize import digitize  # noqa: E402
from stitchpilot.settings import TextItem  # noqa: E402

here = Path(__file__).parent
out = here / "out"
out.mkdir(exist_ok=True)
# no real dialogs in a headless run
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING DIALOG:", a[2] if len(a) > 2 else a))
QMessageBox.information = staticmethod(lambda *a, **k: print("INFO DIALOG:", str(a[2])[:120]))
QDesktopServices.openUrl = staticmethod(lambda *a: True)

app = QApplication(sys.argv)
# simulate Windows dark mode: the app must override it with its own light theme
dark = app.palette()
for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
    dark.setColor(role, QColor("#202020"))
for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
    dark.setColor(role, QColor("#ffffff"))
app.setPalette(dark)
gui.apply_light_theme(app)
w = gui.MainWindow()
w.auto_ai.setChecked(False)
w.resize(1440, 900)
w.show()


def wait_idle(timeout=60):
    loop = QEventLoop()
    t = QTimer()
    t.timeout.connect(lambda: (not w.busy and not w.debounce.isActive()) and loop.quit())
    t.start(100)
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()


def shot(name):
    app.processEvents()
    w.grab().save(str(out / f"gui_{name}.png"))


# picture + settings
w.open_path(str(here / "samples" / "badge.png"))
wait_idle()
assert w.design is not None
print("converted:", w.design.stitch_count, "stitches,", len(w.design.blocks), "colours")
w.hoop.setCurrentIndex(1)
wait_idle()

# lettering + satin patch border
w.texts.append(TextItem("ROBOT CLUB", height_mm=9, rgb=(200, 40, 90)))
w._refresh_texts()
w.border.setCurrentIndex(w.border.findData("satin"))
wait_idle()
print("text + border:", [b.kinds for b in w.design.blocks])
assert any(b.kinds.get("border") for b in w.design.blocks)

# appliqué on the biggest colour, reorder, merge, hide
big = max(range(len(w.design.blocks)), key=lambda i: w.design.blocks[i].area_mm2)
w.set_applique(big, True)
wait_idle()
print("appliqué:", w.design.stop_count, "stops")
assert w.design.stop_count == 2
n_before = len(w.design.blocks)
w.move_block(1, 1)
w.merge_up(2)
print("after merge:", n_before, "->", len(w.design.blocks), "threads")
w.chart.setCurrentIndex(w.chart.findData("Janome"))
shot("features")

# density tab, play
w.tabs.setCurrentWidget(w.v_den)
shot("density")
w.tabs.setCurrentWidget(w.v_st)
w._toggle_play()
for _ in range(15):
    app.processEvents()
    QTimer.singleShot(45, lambda: None)
    loop = QEventLoop()
    QTimer.singleShot(45, loop.quit)
    loop.exec()
print("play position:", w.sim.value())
w._toggle_play()
w.sim.setValue(1000)

# worksheet + save
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(out / "gui_worksheet.pdf"), ""))
w.save_worksheet()
print("worksheet bytes:", (out / "gui_worksheet.pdf").stat().st_size)

# lettering-only design, jumps-only machine
w.new_text_design = w.new_text_design  # (dialog-free path below)
w._reset(None)
w.texts[:] = [TextItem("Emma", font="Georgia", height_mm=20, rgb=(40, 90, 200))]
w._refresh_texts()
w.trims.setChecked(False)
w.border.setCurrentIndex(0)
w.convert()
wait_idle()
print("lettering only:", f"{w.design.width_mm:.0f}x{w.design.height_mm:.0f}mm", w.design.trim_count, "trims")
assert w.design.trim_count == 0
shot("lettering")
w.trims.setChecked(True)

# stitch settings reuse the worked-out picture (and give exactly what a fresh conversion gives); picture
# settings don't
w.open_path(str(here / "samples" / "badge.png"))
wait_idle()
prep0 = w.design.prepared
w.sp_fill.setValue(w.sp_fill.value() + 0.06)
wait_idle()
assert w.design.prepared is prep0, "fill spacing re-worked the picture"
fresh = digitize(w.rgba, w.settings())
assert len(fresh.blocks) == len(w.base.blocks) and all(
    np.array_equal(a.cmds, b.cmds) for a, b in zip(fresh.blocks, w.base.blocks, strict=True)), "reused != fresh"
w.sp_merge.setValue(w.sp_merge.value() + 3)
wait_idle()
assert w.design.prepared is not prep0, "a picture setting must work the picture out again"
print("stitch settings reuse the prepared picture")

# open an existing embroidery file and convert it
w.open_path(str(out / "logo.pes"))
app.processEvents()
print("opened PES:", w.file_mode, len(w.design.blocks), "colours", w.design.stitch_count, "stitches")
assert w.file_mode and w.design.stitch_count > 1000
shot("pes_open")

# batch convert
QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([str(here / "samples" / n) for n in
                                                               ("logo.jpg", "heart.svg", "lineart.png")], ""))
bdir = out / "batch"
bdir.mkdir(exist_ok=True)
QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: str(bdir))
w.batch()
wait_idle(120)
print("batch files:", sorted(p.name for p in bdir.iterdir()))
w.close()
print("all good")
