"""StitchPilot Plus end to end, without the internet: the desktop app's real account and AI code talks to the
real AI gateway (gateway/stitchpilot_gateway.py, served locally), which checks allowances with a fake shop
and forwards to a fake Claude and a fake OpenAI. Covers connecting a computer, AI through the gateway,
running out of allowance, refunds when the AI fails, a disconnected computer, and what the gateway refuses."""
import base64
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)

here = Path(__file__).parent
sys.path.insert(0, str(here.parent))
sys.path.insert(0, str(here.parent / "gateway"))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

out = here / "out"
SECRET = "g" * 48


# ===================================================================== fake shop + fake AI providers
class Fake:
    links: dict = {}
    devices: dict = {}  # token -> {"user": email, "revoked": bool}
    usage: dict = {}  # (email, kind) -> used
    limits = {"images": 3, "actions": 3}
    upstream: list = []
    fail_next = 0  # how many upstream calls fail next (the SDK retries once)
    plan = "free"


def picture_b64() -> str:
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    im.paste((255, 120, 180, 255), (40, 40, 216, 216))
    buf = io.BytesIO()
    im.save(buf, "WEBP")
    return base64.b64encode(buf.getvalue()).decode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path == "/api/stitchpilot/me":
            tok = (self.headers.get("authorization") or "")[7:]
            dev = Fake.devices.get(tok)
            if not dev or dev["revoked"]:
                return self._send(401, {"error": "This computer isn't connected to an account."})
            u = lambda k: Fake.usage.get((dev["user"], k), 0)  # noqa: E731
            return self._send(200, {"email": dev["user"], "plan": Fake.plan, "status": None,
                                    "usage": {"period": "free", "images": {"used": u("images"), "limit": Fake.limits["images"]},
                                              "actions": {"used": u("actions"), "limit": Fake.limits["actions"]}},
                                    "plus": {"price": "$7.99/month"}})
        self._send(404, {})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        if path == "/api/stitchpilot/link/start":
            code = f"ABCD-{len(Fake.links):04d}"
            Fake.links[code] = {"secret": "poll-secret", "user": None, "claimed": False, "name": body["deviceName"]}
            return self._send(200, {"code": code, "pollSecret": "poll-secret",
                                    "url": f"http://shop.test/link?code={code}"})
        if path == "/api/stitchpilot/link/poll":
            link = Fake.links.get(body["code"])
            if not link or link["claimed"] or body["pollSecret"] != link["secret"]:
                return self._send(200, {"status": "expired"})
            if not link["user"]:
                return self._send(200, {"status": "pending"})
            link["claimed"] = True
            tok = "spd_" + "x" * 30 + str(len(Fake.devices))
            Fake.devices[tok] = {"user": link["user"], "revoked": False}
            return self._send(200, {"status": "linked", "token": tok})
        if path in ("/api/stitchpilot/usage/claim", "/api/stitchpilot/usage/refund"):
            assert self.headers.get("x-stitchpilot-gateway-secret") == SECRET, "the gateway proves who it is"
            dev = Fake.devices.get(body["token"])
            if not dev or dev["revoked"]:
                return self._send(401, {"error": "not connected"})
            key = (dev["user"], body["kind"])
            if path.endswith("refund"):
                Fake.usage[key] = max(0, Fake.usage.get(key, 0) - body["amount"])
                return self._send(200, {"ok": True})
            used, limit = Fake.usage.get(key, 0), Fake.limits[body["kind"]]
            if used + body["amount"] > limit:
                return self._send(402, {"ok": False, "plan": Fake.plan, "used": used, "limit": limit})
            Fake.usage[key] = used + body["amount"]
            return self._send(200, {"ok": True, "plan": Fake.plan, "used": used + body["amount"], "limit": limit})
        if path in ("/v1/messages", "/v1/images/generations"):
            Fake.upstream.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                  "body": body})
            if Fake.fail_next:
                Fake.fail_next -= 1
                return self._send(529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
            if path == "/v1/messages":
                answer = {"subject": "A pink square", "design_type": "clipart", "background": "transparent",
                          "background_hex": None, "background_inside_is_background": True, "detail": 0.5,
                          "min_width_mm": 30, "suitability": "great", "tips": ["Keep it simple."],
                          "thread_colors": [{"hex": "#ff78b4", "name": "Pink", "role": "main"}]}
                return self._send(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                                        "content": [{"type": "text", "text": json.dumps(answer)}],
                                        "stop_reason": "end_turn", "stop_sequence": None,
                                        "usage": {"input_tokens": 10, "output_tokens": 10}})
            return self._send(200, {"created": 1, "data": [{"b64_json": picture_b64()} for _ in range(body["n"])]})
        self._send(404, {})


class GatewayHandler(BaseHTTPRequestHandler):
    """Turns HTTP requests into Lambda Function URL events for the real gateway handler."""

    def log_message(self, *a):
        pass

    def _go(self):
        import stitchpilot_gateway as gw

        u = urlparse(self.path)
        n = int(self.headers.get("content-length") or 0)
        event = {"rawPath": u.path, "rawQueryString": u.query, "headers": dict(self.headers),
                 "requestContext": {"http": {"method": self.command}},
                 "body": self.rfile.read(n).decode() if n else None, "isBase64Encoded": False}
        res = gw.lambda_handler(event)
        data = res["body"].encode()
        self.send_response(res["statusCode"])
        for k, v in res["headers"].items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _go


def serve(handler) -> str:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}"


shop = serve(Handler)
os.environ.update(STITCHPILOT_SHOP_URL=shop, ANTHROPIC_BASE_URL=shop, OPENAI_BASE_URL=shop,
                  ANTHROPIC_API_KEY="sk-ant-SERVER", OPENAI_API_KEY="sk-oai-SERVER", STITCHPILOT_GATEWAY_SECRET=SECRET)
gateway = serve(GatewayHandler)

from stitchpilot import account, ai, gui, imagegen  # noqa: E402
from stitchpilot.imageio import load_image  # noqa: E402
from stitchpilot.settings import Settings  # noqa: E402

account.SHOP_URL, account.GATEWAY_URL = shop, gateway
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING DIALOG:", a[2] if len(a) > 2 else a))
app = QApplication(sys.argv)
gui.apply_light_theme(app)


def approve_in_browser(url):
    """What the person does on stitchpilot.jessiescars.com/link: sign in and click Connect."""
    code = parse_qs(urlparse(url.toString() if hasattr(url, "toString") else url).query)["code"][0]
    Fake.links[code]["user"] = "maker@example.com"
    return True


def wait_for(cond, timeout=30):
    loop = QEventLoop()
    t = QTimer()
    t.timeout.connect(lambda: cond() and loop.quit())
    t.start(50)
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()


# ---- connecting a computer, in the account window (the browser step is simulated)
gui.QDesktopServices.openUrl = staticmethod(approve_in_browser)
import stitchpilot.dialogs as dialogs  # noqa: E402

dialogs.QDesktopServices.openUrl = staticmethod(approve_in_browser)
w = gui.MainWindow()
w.auto_ai.setChecked(False)
w.resize(1440, 900)
w.show()
assert w._token() is None
shots = {}


def run_account(dlg):
    shots.setdefault("before", dlg.text.text())
    dlg.poll.setInterval(100)
    dlg.start_link()
    wait_for(lambda: dlg.info is not None)
    app.processEvents()
    dlg.grab().save(str(out / "gui_account.png"))
    shots["after"] = dlg.text.text()
    return 0


dialogs.AccountDialog.exec = run_account
w.open_path(str(here / "samples" / "badge.png"))
wait_for(lambda: not w.busy and not w.debounce.isActive())
w.run_ai()  # not connected yet: the account window comes up first, then the AI runs
assert w._token() and w._token().startswith("spd_"), "connected"
assert "free account" in shots["before"] and "maker@example.com" in shots["after"], shots
wait_for(lambda: not w.ai_running)
call = Fake.upstream[-1]
print("AI through the gateway:", call["path"], "| key upstream:", call["headers"].get("x-api-key"))
assert call["path"].startswith("/v1/messages") and call["headers"]["x-api-key"] == "sk-ant-SERVER", \
    "the gateway uses its own key, never the device token"
assert "server-side-fallback" in call["headers"].get("anthropic-beta", ""), "the SDK's beta header passes through"
assert w.ai_palette == [(255, 120, 180)], w.ai_palette
assert Fake.usage[("maker@example.com", "actions")] == 1
info = account.me(w._token())
assert info["usage"]["actions"]["used"] == 1 and info["plan"] == "free"

# ---- pictures count per version; a failed AI call is refunded
token = w._token()
imgs = imagegen.generate("a fox", count=2, client=account.openai_client(token))
assert len(imgs) == 2 and imgs[0].shape[2] == 4 and Fake.upstream[-1]["headers"]["authorization"] == "Bearer sk-oai-SERVER"
assert Fake.usage[("maker@example.com", "images")] == 2
Fake.fail_next = 2  # the first try and the SDK's retry
try:
    imagegen.generate("a fox", count=1, client=account.openai_client(token))
    raise AssertionError("the failure should come through")
except imagegen.ImageGenError as e:
    print("upstream failure:", e)
assert Fake.usage[("maker@example.com", "images")] == 2, "refunded"

# ---- out of allowance: the app explains and offers Plus
try:
    imagegen.generate("a fox", count=2, client=account.openai_client(token))
    raise AssertionError("over the allowance")
except account.OutOfAllowance as e:
    print("out of pictures:", e)
    assert "free" in str(e) and e.plan == "free"
assert Fake.usage[("maker@example.com", "images")] == 2, "a refused request costs nothing"
ai.analyse(load_image(here / "samples" / "badge.png"), Settings(), client=account.anthropic_client(token))
ai.analyse(load_image(here / "samples" / "badge.png"), Settings(), client=account.anthropic_client(token))
boxes = []
gui.QMessageBox.exec = lambda self: boxes.append(self.text()) or 0
w.run_ai()
wait_for(lambda: not w.ai_running)
assert boxes and "free AI actions" in boxes[-1] and "Plus" in boxes[-1], boxes
print("GUI offered Plus:", boxes[-1])

# ---- disconnected on the website: the app forgets the token and asks to connect again
Fake.devices[token]["revoked"] = True
asked = []
gui.MainWindow.show_account = lambda self, reason="": asked.append(reason)
w.run_ai()
wait_for(lambda: not w.ai_running)
assert asked and w._token() is None, "forgot the revoked token and asked to connect"

# ---- what the gateway refuses (and nothing is counted)
import stitchpilot_gateway as gw  # noqa: E402

Fake.devices[token]["revoked"] = False
before = dict(Fake.usage)
calls_before = len(Fake.upstream)


def post(path, body, headers):
    ev = {"rawPath": path, "rawQueryString": "", "headers": headers, "requestContext": {"http": {"method": "POST"}},
          "body": json.dumps(body), "isBase64Encoded": False}
    return gw.lambda_handler(ev)


good = {"model": "claude-opus-5-5", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
assert post("/anthropic/v1/messages", {**good, "model": "claude-other"}, {"x-api-key": token})["statusCode"] == 400
assert post("/anthropic/v1/messages", {**good, "max_tokens": 100000}, {"x-api-key": token})["statusCode"] == 400
assert post("/anthropic/v1/messages", {**good, "tools": [{"name": "x"}]}, {"x-api-key": token})["statusCode"] == 400
assert post("/anthropic/v1/messages", good, {"x-api-key": "sk-ant-someone-elses-key"})["statusCode"] == 401
assert post("/anthropic/v1/messages", good, {})["statusCode"] == 401
assert post("/openai/v1/images/generations", {"model": "gpt-image-2.5-flare", "prompt": "x", "n": 9},
            {"authorization": f"Bearer {token}"})["statusCode"] == 400
assert post("/openai/v1/chat/completions", {"model": "gpt-5"}, {"authorization": f"Bearer {token}"})["statusCode"] == 404
assert Fake.usage == before and len(Fake.upstream) == calls_before, "refusals cost nothing and reach no AI"
r = gw.lambda_handler({"rawPath": "/health", "requestContext": {"http": {"method": "GET"}}})
assert r["statusCode"] == 200
for res in (post("/anthropic/v1/messages", good, {"x-api-key": token}),):
    assert "SERVER" not in res["body"] and SECRET not in json.dumps(res), "no key or secret ever comes back"
w.close()
print("account ok")
