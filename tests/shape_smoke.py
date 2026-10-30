"""Drive shape-level editing with real mouse/keyboard events (offscreen), plus AI suggestions (mocked)."""
import json
import math
import sys
from pathlib import Path

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import anthropic  # noqa: E402
import httpx2 as httpx  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QEventLoop, QPoint, QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from stitchpilot import ai, gui  # noqa: E402

here = Path(__file__).parent
out = here / "out"
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING DIALOG:", a[2] if len(a) > 2 else a))
app = QApplication(sys.argv)
gui.apply_light_theme(app)


def window():
    w = gui.MainWindow()
    w.auto_ai.setChecked(False)
    w.resize(1440, 900)
    w.show()
    return w


def wait_idle(w, timeout=90):
    loop = QEventLoop()
    t = QTimer()
    t.timeout.connect(lambda: (not w.busy and not w.debounce.isActive()) and loop.quit())
    t.start(100)
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()


def vpos(v, mm) -> QPoint:
    return v.mapFromScene(QPointF(*((np.asarray(mm) - v.origin) * v.ppm)))


def centre(w, sh):
    return np.array([sh.fx * w.design.width_mm, sh.fy * w.design.height_mm])


def by_colour(w, test):
    return max((sh for sh in w.design.shapes if test(sh.rgb)), key=lambda sh: sh.area_mm2)


RED = lambda c: c[0] > 150 and c[1] < 90  # noqa: E731
BLUE = lambda c: c[2] > 120 and c[0] < 80  # noqa: E731
YELLOW = lambda c: c[0] > 200 and c[1] > 150 and c[2] < 120  # noqa: E731

w = window()
v = w.v_st
w.open_path(str(here / "samples" / "logo.jpg"))
wait_idle(w)
n0 = w.design.stitch_count
print("shapes:", [(gui.KIND_NAMES[sh.kind], round(sh.area_mm2)) for sh in w.design.shapes])

# ---- shape mode, click a shape
w.shape_btn.setChecked(True)
app.processEvents()
assert w.shape_bar.isVisible() and v.shape_mode and not w.edit_bar.isVisible()
assert w.shape_bar.minimumSizeHint().width() <= w.edit_bar.minimumSizeHint().width(), "the bar mustn't widen the window"
assert w.shape_angle.text() == "Auto" and w.shape_density.value() == 100, "nothing picked: neutral values"
star = by_colour(w, RED)
QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vpos(v, centre(w, star)))
assert v.shape is not None and RED(v.shape.rgb), v.shape
print("picked:", w.shape_lbl.text())
assert w.shape_angle.isEnabled() and w.shape_kind.itemText(0) == "Auto (Fill)"

# ---- type a fill direction
w.shape_angle.setValue(90)
w.shape_angle.editingFinished.emit()
wait_idle(w)
star = v.shape
assert star is not None and star.angle == 90 and star.edit is not None, star
assert w.dirty and len(w.shape_edits) == 1
print("typed angle: star now", star.angle, "°")

# ---- drag the arrow handle to about 30°
c, half = v.arrow()
handle = c + half
target = c + np.array([math.cos(math.radians(30)), math.sin(math.radians(30))]) * np.hypot(*half)
QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vpos(v, handle))
QTest.mouseMove(v.viewport(), vpos(v, (handle + target) / 2))
QTest.mouseMove(v.viewport(), vpos(v, target))
QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vpos(v, target))
wait_idle(w)
print("dragged arrow: star now", v.shape.angle, "°")
assert v.shape is not None and abs(v.shape.angle - 30) <= 5, v.shape.angle
assert len(w.shape_edits) == 1, "still one edit for the star"
app.processEvents()
w.grab().save(str(out / "gui_shapes.png"))

# ---- remove with Del, then bring it back; undo works
red_stitches = lambda: sum(b.stitch_count for b in w.design.blocks if RED(b.source_rgb))  # noqa: E731
before = red_stitches()
v.setFocus()
QTest.keyClick(v, Qt.Key.Key_Delete)
wait_idle(w)
print("removed star:", before, "->", red_stitches(), "red stitches; shape", v.shape.kind)
assert v.shape.kind == "skip" and red_stitches() < before * 0.2 and w.shape_remove.isChecked()
w.undo()
wait_idle(w)
assert v.shape.kind == "fill" and red_stitches() == before, "undo brings the star back"
w.redo()
wait_idle(w)
assert v.shape.kind == "skip"
w.shape_remove.click()  # bring it back with the button
wait_idle(w)
assert v.shape.kind == "fill"

# ---- outline only for the blue ring, denser yellow, sew-first order
ring = by_colour(w, BLUE)
w.select_shape(ring)
w.shape_kind.setCurrentIndex(w.shape_kind.findData("outline"))
w.shape_kind.activated.emit(w.shape_kind.currentIndex())
wait_idle(w)
assert v.shape is not None and v.shape.kind == "outline", v.shape
assert not w.shape_density.isEnabled() and not w.shape_angle.isEnabled()
blue = next(b for b in w.design.blocks if BLUE(b.source_rgb))
assert blue.kinds.get("outline") == 1
yellow = by_colour(w, YELLOW)
w.select_shape(yellow)
ys = sum(b.stitch_count for b in w.design.blocks if YELLOW(b.source_rgb))
w.shape_density.setValue(130)
w.shape_density.editingFinished.emit()
wait_idle(w)
ys2 = sum(b.stitch_count for b in w.design.blocks if YELLOW(b.source_rgb))
print(f"yellow density 130%: {ys} -> {ys2} stitches")
assert ys2 > ys * 1.15
w.shape_order.setCurrentIndex(w.shape_order.findData(-1))
w.shape_order.activated.emit(w.shape_order.currentIndex())
wait_idle(w)
assert v.shape.edit.order == -1 and len(w.shape_edits) == 3

# ---- AI suggestions: request shape, then the dialog applies the ticked ones
seen = {}
reply = {"summary": "The star reads better with vertical rows; the ring should be a satin border.",
         "suggestions": [{"shape": 1, "stitch": "keep", "angle": 90, "reason": "Rows along the star's height."},
                         {"shape": 2, "stitch": "satin", "angle": None, "reason": "A smooth satin ring."},
                         {"shape": 99, "stitch": "fill", "angle": 0, "reason": "out of range: dropped"}]}


def handler(request):
    seen["body"] = json.loads(request.content)
    return httpx.Response(200, json={
        "id": "msg_test", "type": "message", "role": "assistant", "model": ai.MODEL,
        "content": [{"type": "text", "text": json.dumps(reply)}], "stop_reason": "end_turn",
        "stop_sequence": None, "usage": {"input_tokens": 1000, "output_tokens": 200}})


client = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
advice, shapes = ai.suggest_shapes(w.design, w.settings(), client=client)
body = seen["body"]
assert body["model"] == ai.MODEL and body["messages"][0]["content"][0]["type"] == "image"
assert "1:" in body["messages"][0]["content"][1]["text"] and len(advice.suggestions) == 2
print("AI request ok:", len(shapes), "shapes sent,", len(advice.suggestions), "suggestions kept")
# make shape 1 the star and 2 the ring for the reply above, whatever their area order
shapes = [s for s in shapes if RED(s.rgb)][:1] + [s for s in shapes if BLUE(s.rgb)][:1]
gui.SuggestionsDialog.exec = lambda self: (self.list.setCurrentRow(1), 1)[1]  # look at #2, then accept
w._show_suggestions(advice, shapes)
wait_idle(w)
ring = by_colour(w, BLUE)
star = by_colour(w, RED)
print("after AI: ring", ring.kind, "| star angle", star.angle)
assert ring.kind == "satin" and star.angle == 90
w.undo()  # one undo step takes all the AI changes back
wait_idle(w)
assert by_colour(w, BLUE).kind == "outline" and by_colour(w, RED).angle == 30

# ---- projects keep shape edits
state = w.get_state()
p = _testenv.TMP / "shapes.stitchpilot"
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(p), ""))
assert w.save_project(as_new=True)
w2 = window()
w2.open_path(str(p))
wait_idle(w2)
assert w2.get_state()["shape_edits"] == state["shape_edits"]
assert by_colour(w2, BLUE).kind == "outline" and by_colour(w2, RED).angle == 30
assert w2.design.stitch_count == w.design.stitch_count
print("project round trip ok:", len(w2.shape_edits), "shape edits")

# ---- reset everything; the stitch editor and the shape editor are never open together, but shape changes
# can follow stitch edits (the edits are re-applied after converting)
w.reset_all_shapes()
wait_idle(w)
assert not w.shape_edits and w.design.stitch_count == n0, (w.design.stitch_count, n0)
w.edit_btn.setChecked(True)
assert not w.shape_btn.isChecked() and not w.shape_bar.isVisible()
QTest.keyClick(v, Qt.Key.Key_Delete)  # nothing selected: no-op
v.sel = {(0, 5)}
w.edit_delete()
assert w.edited and w.shape_btn.isEnabled(), "shape edits work alongside stitch edits"
w.shape_btn.setChecked(True)
assert w.shape_btn.isChecked() and not w.edit_btn.isChecked() and w.edited
w.edits_fixed = True  # stitch edits from an older project can't be re-applied: shapes wait for them
w.shape_btn.setChecked(False)
w._update_enabled()
assert not w.shape_btn.isEnabled()
print("shapes ok")
