"""Cut out subject: the free on-device model (download checked byte for byte), a StitchPilot Plus cut-out kept
with the picture and its project, and the basic cut-out when neither is there. No real model is needed: a tiny
fake stands in for the download and for onnxruntime (set STITCHPILOT_TEST_MODELS to a folder holding the real
isnet-general-use.onnx to run it for real too)."""
import hashlib
import os
import sys
import threading
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)

import numpy as np  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from stitchpilot import cutout, gui, project  # noqa: E402
from stitchpilot.digitize import digitize  # noqa: E402
from stitchpilot.imageio import load_image  # noqa: E402
from stitchpilot.settings import Settings  # noqa: E402

here = Path(__file__).parent
tmp = _testenv.TMP
models = tmp / "models"
os.environ["STITCHPILOT_MODELS"] = str(models)

# ---- download: checked against the expected size and SHA-256; a bad or cancelled download leaves nothing
www = tmp / "www"
www.mkdir()
good = os.urandom(300_000)
(www / "model.onnx").write_bytes(good)
(www / "bad.onnx").write_bytes(good[:-1] + b"x")
class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(www)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
real_model = cutout.MODEL
cutout.MODEL = cutout.Model("test", "model.onnx", f"{base}/bad.onnx", hashlib.sha256(good).hexdigest(), len(good))
try:
    cutout.download()
    raise AssertionError("a file that doesn't match must be refused")
except cutout.CutoutError as e:
    print("refused:", e)
assert not cutout.model_ready() and not list(models.glob("*")), "nothing left behind"
cutout.MODEL = cutout.Model("test", "model.onnx", f"{base}/model.onnx", hashlib.sha256(good).hexdigest(), len(good))
try:
    cutout.download(cancelled=lambda: True)
    raise AssertionError("cancelled")
except cutout.CutoutError:
    pass
assert not cutout.model_ready()
seen = []
cutout.download(lambda d, t: seen.append((d, t)))
assert cutout.model_ready() and seen[-1] == (len(good), len(good))


# ---- on-device: the model's answer becomes the picture's subject
class FakeSession:
    """Says the middle third of the picture is the subject."""

    def get_inputs(self):
        return [type("I", (), {"name": "input"})()]

    def run(self, _outs, feed):
        x = feed["input"]
        assert x.shape == (1, 3, cutout.SIZE, cutout.SIZE) and x.dtype == np.float32
        pred = np.zeros((1, 1, cutout.SIZE, cutout.SIZE), np.float32)
        pred[..., cutout.SIZE // 3: 2 * cutout.SIZE // 3, cutout.SIZE // 3: 2 * cutout.SIZE // 3] = 7.0
        return [pred]


cutout._session = FakeSession()
busy = load_image(here / "samples" / "busy.jpg")
alpha, how = cutout.matte(busy, lambda im: np.ones(im.shape[:2], bool))
assert how == cutout.ON_DEVICE and alpha[400, 400] == 255 and alpha[20, 20] == 0, how
d = digitize(busy, Settings(background="ai"))
ch, cw = d.prepared.source_rgba.shape[:2]
assert ch < 300 and cw < 300, f"only the subject is kept ({cw} x {ch} px of an 800 px picture)"

# ---- the basic cut-out when there's no model (and the cached answer is replaced once the model arrives)
cutout.MODEL = cutout.Model("test", "missing.onnx", base, "0" * 64, 1)
fresh = busy[::-1].copy()
_, how = cutout.matte(fresh, lambda im: np.ones(im.shape[:2], bool))
assert how == cutout.BASIC

# ---- a Plus cut-out is used for that picture (and only that one), whatever the model situation
plus = np.zeros(busy.shape[:2], np.uint8)
plus[100:300, 500:700] = 255
cutout.remember(busy, plus)
assert cutout.matte(busy, None)[1] == cutout.CLOUD
assert cutout.known(busy[:, ::-1].copy()) is None
d = digitize(busy, Settings(background="ai"))
assert d.prepared.source_rgba.shape[:2] == (200, 200), d.prepared.source_rgba.shape

# ---- in the app: the Background row says which cut-out is used and offers the better ones
cutout.MODEL = real_model
app = QApplication.instance() or QApplication(sys.argv)
w = gui.MainWindow()
w.auto_ai.setChecked(False)
w.show()


def wait_for(cond, timeout=60):
    loop = QEventLoop()
    t = QTimer()
    t.timeout.connect(lambda: cond() and loop.quit())
    t.start(50)
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()


w.open_path(str(here / "samples" / "logo.jpg"))
wait_for(lambda: not w.busy and not w.debounce.isActive())
assert not w.cut_row.isVisible()
w.bg.setCurrentIndex(w.bg.findData("ai"))
wait_for(lambda: not w.busy and not w.debounce.isActive())
assert w.cut_row.isVisible() and "basic" in w.cut_lbl.text() and w.cut_dl.isVisible() == (not cutout.model_ready())
# a Plus cut-out arrives (as run_cutout_plus would deliver it): kept, used, and saved with the project
mask = np.zeros(w.rgba.shape[:2], np.uint8)
mask[200:600, 200:600] = 255
w._on_done(w.gen, ("cutout", mask))
wait_for(lambda: not w.busy and not w.debounce.isActive())
assert "Plus" in w.cut_lbl.text() and not w.cut_plus.isEnabled()
assert w.design is not None and w.design.prepared.source_rgba.shape[:2] == (400, 400), "only the cut-out is kept"
# no onnxruntime (Intel Macs): no download offered, the basic cut-out is used and Plus is suggested
real_ok, orig = cutout.runtime_ok, w.rgba
cutout.runtime_ok = lambda: False
w.rgba = orig.copy()
w.rgba[0, 0, 0] ^= 1  # a picture without a cut-out yet
w._update_cut_row()
assert not w.cut_dl.isVisible() and "doesn't run on this computer" in w.cut_lbl.text() and w.cut_plus.isEnabled()
assert cutout.matte(w.rgba[:50, :50].copy(), lambda im: np.ones(im.shape[:2], bool))[1] == cutout.BASIC
cutout.runtime_ok, w.rgba = real_ok, orig
assert cutout.runtime_ok()
w._update_cut_row()
path = tmp / "cut.stitchpilot"
w._write_project(path)
assert "cutout.png" in zipfile.ZipFile(path).namelist()
cutout._cache.clear()
w.open_project(path)
wait_for(lambda: not w.busy and not w.debounce.isActive())
assert cutout.known(w.rgba) is not None and cutout.known(w.rgba)[1] == cutout.CLOUD, "the Plus cut-out came back"
assert "Plus" in w.cut_lbl.text()
assert project.load(path).files["cutout.png"][:8] == b"\x89PNG\r\n\x1a\n"
w.grab().save(str(here / "out" / "gui_cutout.png"))
w.close()

# ---- the real model, when it's around
real = os.environ.get("STITCHPILOT_TEST_MODELS")
if real and (Path(real) / real_model.file).exists():
    os.environ["STITCHPILOT_MODELS"] = real
    cutout._session = None
    a = cutout.on_device(busy)
    share = float((a >= 128).mean())
    assert 0.08 < share < 0.3, f"the real model found {share:.0%} of the busy picture"
    print(f"real ISNet: subject is {share:.0%} of busy.jpg")
print("cut-out ok")
