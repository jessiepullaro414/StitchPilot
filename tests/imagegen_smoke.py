"""Create a picture with AI (OpenAI mocked): the request sent, friendly errors, the dialog, and the picture
opening and stitching in the main window."""
import base64
import io
import json
import sys
from pathlib import Path

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx2 as httpx  # noqa: E402
import numpy as np  # noqa: E402
import openai  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from stitchpilot import account, gui, imagegen  # noqa: E402

here = Path(__file__).parent
out = here / "out"
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING DIALOG:", a[2] if len(a) > 2 else a))
app = QApplication(sys.argv)
gui.apply_light_theme(app)


def sticker(colour) -> str:
    """A flat 'AI' picture on a transparent background, as base64 PNG."""
    im = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((60, 60, 452, 452), fill=(40, 40, 40, 255))
    d.ellipse((80, 80, 432, 432), fill=colour + (255,))
    d.ellipse((170, 180, 230, 240), fill=(40, 40, 40, 255))
    d.ellipse((282, 180, 342, 240), fill=(40, 40, 40, 255))
    d.arc((170, 230, 342, 380), 20, 160, fill=(40, 40, 40, 255), width=18)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


seen = []


def client_for(status=200, error=None):
    def handler(request):
        seen.append(json.loads(request.content))
        if error is not None:
            return httpx.Response(status, json={"error": error})
        body = seen[-1]
        cols = [(255, 170, 200), (150, 220, 190), (250, 210, 90), (170, 180, 255)]
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": sticker(cols[i % 4])} for i in range(body["n"])],
                                         "usage": {"input_tokens": 80, "output_tokens": 1500, "total_tokens": 1580}})
    return openai.OpenAI(api_key="sk-test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


# ---- the request: embroidery-friendly prompt and settings
imgs = imagegen.generate("a happy frog", style="Cute sticker", colours=4, count=3, quality="low", client=client_for())
req = seen[-1]
assert len(imgs) == 3 and imgs[0].shape == (512, 512, 4) and imgs[0][0, 0, 3] == 0, "RGBA with a transparent background"
assert req["model"] == imagegen.MODEL and req["n"] == 3 and req["quality"] == "low" and req["size"] == "1024x1024"
assert req["background"] == "transparent" and req["output_format"] == "webp", "webp keeps transparency, smaller"
assert "a happy frog" in req["prompt"] and "at most 4 colours" in req["prompt"] and "no gradients" in req["prompt"]
imagegen.generate("a frog", transparent=False, count=1, size="1536x1024", client=client_for())
assert seen[-1]["background"] == "opaque" and "white background" in seen[-1]["prompt"] and seen[-1]["size"] == "1536x1024"
print("request ok:", req["model"], req["size"], req["quality"], f"n={req['n']}")

# ---- errors people can act on
cases = [(401, {"message": "Incorrect API key", "type": "invalid_request_error", "code": "invalid_api_key"}, "rejected"),
         (400, {"message": "Your request was rejected by the safety system", "type": "image_generation_user_error",
                "code": "moderation_blocked"}, "content rules"),
         (429, {"message": "You exceeded your current quota", "type": "insufficient_quota", "code": "insufficient_quota"},
          "out of credit"),
         (403, {"message": "Your organization must be verified", "type": "invalid_request_error", "code": None}, "verified")]
for status, err, words in cases:
    try:
        imagegen.generate("a frog", client=client_for(status, err))
        raise AssertionError("no error raised")
    except imagegen.ImageGenError as e:
        assert words in str(e), (status, str(e))
try:
    imagegen.generate("   ", client=client_for())
    raise AssertionError("empty idea accepted")
except imagegen.ImageGenError:
    pass
print("errors ok")


def wait_for(cond, timeout=30):
    loop = QEventLoop()
    t = QTimer()
    t.timeout.connect(lambda: cond() and loop.quit())
    t.start(50)
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()


# ---- the main window: connect the account, dialog, picture opens and stitches
w = gui.MainWindow()
w.auto_ai.setChecked(False)
w.resize(1440, 900)
w.show()
w.qs.setValue("ai_pictures_dir", str(_testenv.TMP / "ai pictures"))
assert w.gen_btn.isVisible() and w.gen_btn.isEnabled()
asked = []


def connect(self, reason=""):  # the account window, as if the person connected this computer
    asked.append(reason)
    self._set_token("spd_" + "d" * 40)


gui.MainWindow.show_account = connect
real_openai_client = account.openai_client
account.openai_client = lambda token: (asked.append(token), "client for " + token)[1]
real_generate = imagegen.generate
calls = []


def fake_generate(idea, **kw):
    calls.append((idea, kw))
    return real_generate(idea, **{**kw, "client": client_for()})


def run_dialog(dlg):
    dlg._generate = fake_generate
    dlg.idea.setPlainText("a smiling sloth hugging a coffee cup")
    dlg.colours.setValue(4)
    dlg.count.setValue(3)
    dlg.create()
    wait_for(lambda: not dlg.running)
    app.processEvents()
    dlg.grab().save(str(out / "gui_imagegen.png"))
    assert dlg.grid.count() == 3 and dlg.ok.isEnabled()
    dlg.group.button(1).click()
    return 1


gui.ImageGenDialog.exec = run_dialog
w.create_with_ai()
assert asked[0] and "connect" in asked[0], "asked to connect the account the first time"
assert w._token() == "spd_" + "d" * 40 and calls[-1][1]["client"] == "client for " + w._token()
assert calls[-1][1]["colours"] == 4 and calls[-1][1]["count"] == 3 and calls[-1][1]["quality"] == "low"
assert w.path is not None and w.path.suffix == ".png" and "smiling-sloth" in w.path.name, w.path
assert np.array(Image.open(w.path))[0, 0, 3] == 0, "saved with its transparency"
wait_for(lambda: not w.busy and not w.debounce.isActive() and w.design is not None, 90)
print(f"AI picture: {w.path.name} -> {w.design.stitch_count} stitches, {len(w.design.blocks)} colours")
assert w.design.stitch_count > 1000 and 2 <= len(w.design.blocks) <= 5
w.create_with_ai()
assert len([a for a in asked if "connect" in a]) == 1, "a connected computer isn't asked again"
account.openai_client = real_openai_client
w.grab().save(str(out / "gui_imagegen_result.png"))
w.close()
print("imagegen ok")
