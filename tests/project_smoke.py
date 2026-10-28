"""Projects (save/reopen, hand edits, embroidery files, lettering), unsaved-changes prompt, autosave and
crash recovery, machine profiles and the real-size placement template."""
import sys
from pathlib import Path

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton  # noqa: E402

from stitchpilot import gui  # noqa: E402
from stitchpilot.export import export  # noqa: E402
from stitchpilot.settings import TextItem  # noqa: E402
from stitchpilot.template import write_template  # noqa: E402

here = Path(__file__).parent
out = here / "out"
tmp = _testenv.TMP
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING DIALOG:", a[2] if len(a) > 2 else a))
app = QApplication(sys.argv)
gui.apply_light_theme(app)


def window():
    w = gui.MainWindow()
    w.auto_ai.setChecked(False)
    w.resize(1440, 900)
    w.show()
    return w


def wait_idle(w, timeout=60):
    loop = QEventLoop()
    t = QTimer()
    t.timeout.connect(lambda: (not w.busy and not w.debounce.isActive()) and loop.quit())
    t.start(100)
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()


def summary(w):
    d = w.design
    return d.stitch_count, [b.rgb for b in d.blocks], [b.kinds.get("appliqué", 0) for b in d.blocks]


def save_to(w, name):
    p = tmp / name
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(p), ""))
    assert w.save_project(as_new=True)
    return p


# ---------------------------------------------------------------- machine profiles
w = window()
w.machine.setCurrentText("Brother PE800 / PE900 / SE1900 / SE2000 (5×7)")
hoops = [w.hoop.itemText(i) for i in range(w.hoop.count())]
print("PE800:", w.fmt.currentText().split(" –")[0], w.chart.currentData(), w.hoop.currentText(), len(hoops) - 1, "hoops")
assert w.fmt.currentText().startswith("PES") and w.chart.currentData() == "Brother"
assert "100 x 100" in w.hoop.currentText(), "a hoop that still fits the new machine is kept"
assert len(hoops) - 1 == 3 and any("130 x 180" in h for h in hoops)
w.machine.setCurrentText("Janome Memory Craft 500E")
print("500E:", w.fmt.currentText().split(" –")[0], w.chart.currentData(), w.hoop.currentText(),
      f"max {w.w_spin.value():.0f}x{w.h_spin.value():.0f}mm")
assert w.fmt.currentText().startswith("JEF") and "200 x 280" in w.hoop.currentText()
w.machine.setCurrentText("Brother PE800 / PE900 / SE1900 / SE2000 (5×7)")

# ---------------------------------------------------------------- picture project round trip
w.open_path(str(here / "samples" / "badge.png"))
wait_idle(w)
assert not w.dirty, "just opening a picture is not a change"
w.texts.append(TextItem("Robot Club", height_mm=8, rgb=(255, 90, 160)))
w._refresh_texts()
w.border.setCurrentIndex(w.border.findData("satin"))
w.simp.setValue(55)
wait_idle(w)
big = max(range(len(w.design.blocks)), key=lambda i: w.design.blocks[i].area_mm2)
w.set_applique(big, True)
wait_idle(w)
w.overrides[w.design.blocks[1].source_rgb] = (120, 60, 200)
w.refresh_colors()
w.move_block(1, 1)
wait_idle(w)  # a new sewing order re-plans the hidden travel
assert w.dirty and w.windowTitle().endswith("• – StitchPilot") or " •" in w.windowTitle()
before, state_before = summary(w), w.get_state()
p1 = save_to(w, "robot.stitchpilot")
assert not w.dirty and "•" not in w.windowTitle()
print("saved", p1.name, f"{p1.stat().st_size // 1024} KB, title:", w.windowTitle())

w2 = window()
w2.open_path(str(p1))
wait_idle(w2)
after = summary(w2)
print("reopened:", after[0], "stitches,", len(after[1]), "colours", "| same:", after == before)
assert after == before, (before, after)
assert w2.get_state() == state_before
assert not w2.dirty and w2.project_path == p1

# ---------------------------------------------------------------- hand edits survive
w2.edit_btn.setChecked(True)
pt = w2.v_st.pts[len(w2.v_st.pts) // 2]
from stitchpilot import editing  # noqa: E402
w2.v_st.sel = editing.run_of(w2.design.blocks, editing.nearest(w2.design.blocks, pt, 1.0))
w2.edit_delete()
edited = summary(w2)
p2 = save_to(w2, "robot-edited.stitchpilot")
w3 = window()
w3.open_path(str(p2))
wait_idle(w3)
print("edited project:", before[0], "->", edited[0], "| reopened:", summary(w3)[0], "edited flag:", w3.edited)
assert w3.edited and summary(w3) == edited and w3.edit_banner.isVisibleTo(w3)

# ---------------------------------------------------------------- embroidery-file project
pes = export(w.design, tmp / "club.pes")
w4 = window()
w4.open_path(str(pes))
w4.overrides[w4.design.blocks[0].source_rgb] = (10, 200, 120)
w4.refresh_colors()
p3 = save_to(w4, "club-file.stitchpilot")
w5 = window()
w5.open_path(str(p3))
print("embroidery-file project:", w5.file_mode, w5.design.blocks[0].rgb, w5.design.stitch_count, "stitches")
assert w5.file_mode and w5.design.blocks[0].rgb == (10, 200, 120)
assert w5.design.stitch_count == w4.design.stitch_count

# ---------------------------------------------------------------- lettering-only project
w6 = window()
w6._reset(None)
w6.texts[:] = [TextItem("Emma", font="Georgia", height_mm=18, rgb=(40, 90, 200))]
w6._refresh_texts()
w6.convert()
wait_idle(w6)
p4 = save_to(w6, "emma.stitchpilot")
w7 = window()
w7.open_path(str(p4))
wait_idle(w7)
print("lettering project:", [t.text for t in w7.texts], w7.design.stitch_count, "stitches")
assert w7.texts[0].text == "Emma" and w7.design.stitch_count == w6.design.stitch_count

# ---------------------------------------------------------------- unsaved-changes prompt (the real one)


def click_later(label):
    def go():
        box = QApplication.activeModalWidget()
        for b in box.findChildren(QPushButton):
            if b.text().replace("&", "") == label:
                b.click()
                return
    QTimer.singleShot(150, go)


w7.simp.setValue(80)
assert w7.dirty
click_later("Cancel")
assert w7.real_maybe_save() is False
click_later("Don't save")
assert w7.real_maybe_save() is True
print("unsaved-changes prompt: Cancel keeps you here, Don't save lets you go")

# ---------------------------------------------------------------- autosave + crash recovery
wait_idle(w7)
w7._autosave()
auto = gui.autosave_path()
assert auto.exists()
w8 = window()
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
w8.check_recovery()
wait_idle(w8)
print("recovered:", w8.project_path.name if w8.project_path else None, "simplicity", w8.simp.value(),
      "dirty", w8.dirty, "|", w8.design.stitch_count, "stitches")
assert w8.simp.value() == 80 and w8.dirty and w8.project_path == p4
w8.close()  # clean exit removes the autosave
assert not auto.exists()
print("clean close removed the autosave")

# ---------------------------------------------------------------- recent files
recent = w8._recent()
print("recent:", [Path(p).name for p in recent[:4]])
assert any(p.endswith("emma.stitchpilot") for p in recent)

# ---------------------------------------------------------------- placement template
n1 = write_template(str(out / "template_small.pdf"), w.design, w.hoop_mm(), "Robot Club", "Letter")
w.machine.setCurrentText("Any machine (all hoops)")  # the PE800 has no 8x12 hoop
w.hoop.setCurrentText('8" x 12" (200 x 300 mm)')
wait_idle(w)
assert "200 x 300" in w.hoop.currentText()
n2 = write_template(str(out / "template_big.pdf"), w.design, w.hoop_mm(), "Robot Club big", "A4")
print("template pages: small", n1, "| 8x12 hoop on A4:", n2)
assert n1 == 1 and n2 > 1
try:
    from PySide6.QtCore import QSize
    from PySide6.QtPdf import QPdfDocument

    for name in ("template_small", "template_big"):
        doc = QPdfDocument()
        doc.load(str(out / f"{name}.pdf"))
        sz = doc.pagePointSize(0)
        print(f"  {name}: {doc.pageCount()} page(s), {sz.width() / 72 * 25.4:.0f} x {sz.height() / 72 * 25.4:.0f} mm")
        img = doc.render(0, QSize(int(sz.width() * 1.6), int(sz.height() * 1.6)))
        img.save(str(out / f"{name}_p1.png"))
except ImportError:
    pass
print("projects ok")
