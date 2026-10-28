"""Step 4 "Delight": curved text and monograms, motif / wavy / cross-stitch fills, appliqué cut files and
AI text clean-up (Claude mocked). Saves pictures of each in tests/out."""
import json
from dataclasses import replace
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import anthropic  # noqa: E402
import cv2  # noqa: E402
import httpx2 as httpx  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from stitchpilot import account, ai, gui  # noqa: E402
from stitchpilot import patterns as pat  # noqa: E402
from stitchpilot import stitches as st  # noqa: E402
from stitchpilot.digitize import digitize  # noqa: E402
from stitchpilot.imageio import load_image  # noqa: E402
from stitchpilot.lettering import FRAMES, render_text  # noqa: E402
from stitchpilot.render import render  # noqa: E402
from stitchpilot.settings import Settings, ShapeEdit, TextItem  # noqa: E402

here = Path(__file__).parent
out = here / "out"
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING DIALOG:", a[2] if len(a) > 2 else a))
QMessageBox.information = staticmethod(lambda *a, **k: print("INFO DIALOG:", a[2] if len(a) > 2 else a))
app = QApplication(sys.argv)
gui.apply_light_theme(app)
logo = load_image(here / "samples" / "logo.jpg")


def save(img, name):
    Image.fromarray(np.ascontiguousarray(img[..., :3])).save(out / name)


# ===================================================================== 1. curved text and monograms
flat = render_text(TextItem("Robot Club", height_mm=10), 50)
up = render_text(TextItem("Robot Club", height_mm=10, arc=150), 50)
down = render_text(TextItem("Robot Club", height_mm=10, arc=-150), 50)
assert up.shape[0] > flat.shape[0] * 1.8 and up.shape[1] < flat.shape[1], "an arch is taller and narrower"
# rainbow: the middle letters are higher than the ends; smile: lower
rows = lambda im, x0, x1: np.nonzero(im[:, x0:x1, 3].max(1) > 128)[0].mean()  # noqa: E731
w3 = up.shape[1] // 3
assert rows(up, w3, 2 * w3) < rows(up, 0, w3), "arch: middle above the ends"
assert rows(down, w3, 2 * w3) > rows(down, 0, w3), "smile: middle below the ends"
for frame in FRAMES:
    m = render_text(TextItem("JMP", kind="monogram", frame=frame, height_mm=30), 80)
    if frame != "none":
        assert m.shape[0] > 80 * 1.5, (frame, m.shape)  # the frame goes round the letters
s = Settings(texts=[TextItem("Robot Club", arc=150, height_mm=9, rgb=(220, 60, 120), position="above"),
                    TextItem("JMP", kind="monogram", frame="scallop", height_mm=30, rgb=(30, 30, 90))])
d = digitize(None, s)
kinds = [b.kinds for b in d.blocks]
print(f"monogram + arched name: {d.width_mm:.0f}x{d.height_mm:.0f} mm, {d.stitch_count} stitches, "
      f"{d.trim_count} trims, {kinds}")
assert len(d.blocks) == 2 and all(k.get("satin", 0) >= 3 for k in kinds), "letters and frame sew as satin"
assert d.trim_count < 25 and 60 < d.height_mm < 110
save(render(d, 8), "delight_monogram.png")

# ===================================================================== 2. decorative fills
base = digitize(logo, Settings())
big = sorted(base.shapes, key=lambda sh: -sh.area_mm2)[:3]  # yellow disc, blue ring, red star
results = {}
for combo in (("cross", "waves", "hearts"), ("flowers", "hearts", "stars")):
    edits = [ShapeEdit(sh.rgb, sh.fx, sh.fy, kind="fill", pattern=p) for sh, p in zip(big, combo)]
    d = digitize(logo, Settings(shape_edits=edits))
    used = [(sh.kind, sh.pattern, sh.angle) for sh in sorted(d.shapes, key=lambda sh: -sh.area_mm2)[:3]]
    print(f"{'/'.join(combo)}: {d.stitch_count} stitches, {d.trim_count} trims, {[u[:2] for u in used]}")
    assert [u[1] for u in used] == list(combo), used
    for kind, p, angle in used:
        assert (angle is None) == (p in pat.OPEN), "open fills have no direction arrow"
    assert d.trim_count < 45, d.trim_count
    assert 3000 < d.stitch_count < base.stitch_count * 1.2
    results[combo] = d
    save(render(d, 8), f"delight_fill_{'_'.join(combo)}.png")
# no stitch longer than the machine allows, and the open fills really are open
for d in results.values():
    for b in d.blocks:
        both = (b.cmds[1:, 2] == st.STITCH) & (b.cmds[:-1, 2] == st.STITCH)
        assert np.hypot(*np.diff(b.cmds[:, :2], axis=0)[both].T).max() <= 7.01
square = __import__("shapely").box(0, 0, 30, 20)
xs = pat.cross_runs(square, 2.2, None)
assert len(xs) <= 2, f"a plain rectangle is two continuous passes, not {len(xs)}"
hearts = pat.motif_runs(square.buffer(5), "hearts", 5.0, None)
assert len(hearts) <= 3, len(hearts)
waves = pat.wave_runs(square, 0, 0.4, None)
ys = np.concatenate([r[:, 1] for r in waves])
assert ys.min() > -0.5 and ys.max() < 20.5, "waves stay inside the shape"

# ===================================================================== 3. GUI: pattern picker, cut files
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


w.open_path(str(here / "samples" / "logo.jpg"))
wait_idle()
w.shape_btn.setChecked(True)
assert w.shape_bar.minimumSizeHint().width() <= w.edit_bar.minimumSizeHint().width(), "the bar mustn't widen"
star = max((sh for sh in w.design.shapes if sh.rgb[0] > 150 and sh.rgb[1] < 90), key=lambda sh: sh.area_mm2)
w.select_shape(star)
w.shape_kind.setCurrentIndex(w.shape_kind.findData("fill:hearts"))
w.shape_kind.activated.emit(w.shape_kind.currentIndex())
wait_idle()
sh = w.v_st.shape
assert sh is not None and sh.pattern == "hearts" and w.shape_edits[-1].pattern == "hearts", sh
assert w.v_st.arrow() is None and not w.shape_angle.isEnabled(), "no direction for a motif fill"
assert "Hearts" in w.shape_lbl.text()
w.shape_btn.setChecked(False)
# appliqué on the yellow disc, then cut files
yellow = next(i for i, b in enumerate(w.design.blocks) if b.source_rgb[0] > 200 and b.source_rgb[2] < 120)
assert not w.cut_act.isEnabled()
w.set_applique(yellow, True)
wait_idle()
assert w.cut_act.isEnabled() and w.design.stop_count == 2
fabric = next(b.fabric for b in w.design.blocks if b.fabric)
fx = np.vstack(fabric)
want_w, want_h = np.ptp(fx[:, 0]), np.ptp(fx[:, 1])
for ext, flt in (("svg", "SVG"), ("dxf", "DXF")):
    target = _testenv.TMP / f"cut.{ext}"
    QFileDialog.getSaveFileName = staticmethod(
        lambda *a, t=target, f=flt: (str(t.with_suffix("")), next(x for x in a[3].split(";;") if f in x)))
    w.save_cut_file()
    assert target.exists(), f"{ext} not saved (the extension comes from the chosen file type)"
    if ext == "svg":
        root = ET.parse(target).getroot()
        ns = {"s": "http://www.w3.org/2000/svg"}
        paths = root.findall(".//s:g/s:path", ns)
        pts = np.array([[float(v) for v in xy.split(",")] for p in paths for xy in re.findall(r"[-\d.]+,[-\d.]+",
                                                                                              p.get("d"))])
        assert root.get("width").endswith("mm") and len(paths) == len(fabric)
        got_w, got_h = np.ptp(pts[:, 0]), np.ptp(pts[:, 1])
    else:
        lines = target.read_text().splitlines()
        assert lines[-1] == "EOF" and lines.count("POLYLINE") == len(fabric)
        vals = [(lines[i], lines[i + 1]) for i in range(0, len(lines) - 1, 2)]
        verts = [i for i, (c, v) in enumerate(vals) if (c, v) == ("0", "VERTEX")]
        vx = [float(vals[i + 2][1]) for i in verts]
        vy = [float(vals[i + 3][1]) for i in verts]
        got_w, got_h = np.ptp(vx), np.ptp(vy)
    print(f"cut file {ext}: {len(fabric)} piece(s), {got_w:.1f} x {got_h:.1f} mm (fabric {want_w:.1f} x {want_h:.1f})")
    assert abs(got_w - want_w) < 0.05 and abs(got_h - want_h) < 0.05, "real size"

# ===================================================================== 4. AI text clean-up (Claude mocked)
# a small, blurry, badly compressed copy of the logo: the kind of picture whose text stitches badly
small = cv2.resize(logo, (220, 220), interpolation=cv2.INTER_AREA)
ok, buf = cv2.imencode(".jpg", cv2.cvtColor(small[..., :3], cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 30])
blurry = cv2.cvtColor(cv2.imdecode(buf, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
blurry_rgba = np.dstack([blurry, np.full(blurry.shape[:2], 255, np.uint8)])
blurry_png = _testenv.TMP / "blurry_logo.png"
Image.fromarray(blurry).save(blurry_png)
seen = {}


def handler(request):
    body = json.loads(request.content)
    seen["body"] = body
    W, H = (int(v) for v in re.findall(r"(\d+) x (\d+)", body["messages"][0]["content"][1]["text"])[0])
    reply = {"texts": [
        # Claude's boxes are rough: a little loose, a little off centre
        {"text": "STITCH", "left": round(0.355 * W), "top": round(0.825 * H), "right": round(0.655 * W),
         "bottom": round(0.9 * H), "style": "sans", "bold": True, "italic": False, "font_guess": "",
         "curved": False},
        {"text": "EST 2026", "left": 5, "top": 5, "right": 40, "bottom": 12, "style": "serif", "bold": False,
         "italic": False, "font_guess": "", "curved": True}]}
    return httpx.Response(200, json={
        "id": "msg_test", "type": "message", "role": "assistant", "model": ai.MODEL,
        "content": [{"type": "text", "text": json.dumps(reply)}], "stop_reason": "end_turn",
        "stop_sequence": None, "usage": {"input_tokens": 1000, "output_tokens": 200}})


mock = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
found = ai.find_text(blurry_rgba, client=mock)
body = seen["body"]
assert body["model"] == ai.MODEL and body["messages"][0]["content"][0]["type"] == "image"
assert [t.text for t, _ in found] == ["STITCH", "EST 2026"] and all(0 <= v <= 1 for _, b in found for v in b)

# through the GUI: a connected computer (StitchPilot Plus) runs the same request; the mock client
# stands in for the AI service (account_smoke.py tests the real path through the gateway)
real_client = account.anthropic_client
account.anthropic_client = lambda token: mock
w.qs.setValue("cloud_token", "spd_" + "t" * 40)
picked = {}


def accept(dlg):
    picked["rows"] = [(t.text(), f.currentFont().family(), b.isChecked()) for _, t, f, b, _ in dlg.rows]
    return 1


gui.TextFixDialog.exec = accept
w.open_path(str(blurry_png))
wait_idle()
before = w.design
w.clean_text()
wait_idle()
account.anthropic_client = real_client
print("clean-up dialog:", picked.get("rows"))
assert picked["rows"] and picked["rows"][0][0] == "STITCH", "curved text is listed but can't be ticked"
assert len(w.text_fixes) == 1 and w.text_fixes[0].text == "STITCH"
wait_idle()
after = w.design
assert after is not before and after.prepared is not None


def letters(design):
    """The white letters in the ring, which sew as gaps showing the fabric, at a common size."""
    lab = design.prepared.labels
    h, w_ = lab.shape
    crop = (lab == -1)[int(0.8 * h):int(0.91 * h), int(0.3 * w_):int(0.7 * w_)].astype(np.uint8) * 255
    return cv2.resize(crop, (400, 80), interpolation=cv2.INTER_AREA) > 127


# the logo's colours, as AI settings would pick them
s_logo = Settings(palette=[(20, 60, 140), (250, 200, 40), (200, 30, 40)], num_colors=3)
ref = letters(digitize(logo, s_logo))
iou = lambda a, b: (a & b).sum() / max(1, (a | b).sum())  # noqa: E731
d_plain = digitize(blurry_rgba, s_logo)
d_fixed = digitize(blurry_rgba, replace(s_logo, text_fixes=w.text_fixes))
plain_iou, fixed_iou = iou(letters(d_plain), ref), iou(letters(d_fixed), ref)
print(f"letters vs the sharp original: blurry {plain_iou:.0%} -> cleaned {fixed_iou:.0%}")
assert letters(d_fixed).mean() > 0.08 and fixed_iou > plain_iou + 0.1, (plain_iou, fixed_iou)
save(render(d_plain, 10), "delight_text_before.png")
save(render(d_fixed, 10), "delight_text_after.png")

# projects keep text fixes, curved text and patterns
state = w.get_state()
assert state["text_fixes"][0]["text"] == "STITCH"
w.texts.append(TextItem("Hi", arc=90, kind="text"))
p = _testenv.TMP / "delight.stitchpilot"
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(p), ""))
assert w.save_project(as_new=True)
w2 = gui.MainWindow()
w2.open_path(str(p))
w = w2
wait_idle()  # let its conversion finish: exiting with a job still running crashes Python's shutdown
assert [f.text for f in w2.text_fixes] == ["STITCH"] and w2.texts[0].arc == 90
assert w2.fix_text_btn.isEnabled()
w2.close()
print("delight ok")
