"""The web app: starts the real server (with its worker processes) on a spare port and drives every
endpoint the page uses, the public-site limits, and the AI routes against a mocked Claude."""
import io
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

here = Path(__file__).parent
sys.path.insert(0, str(here.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["STITCHPILOT_WORKERS"] = "2"
os.environ["STITCHPILOT_AI_WORKERS"] = "1"


def _inside(x, y, rings):
    c = False
    for r in rings:
        for i in range(len(r)):
            (xi, yi), (xj, yj) = r[i], r[i - 1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                c = not c
    return c


def shape_at(shapes, x, y):
    """What the page does on a click: the smallest shape containing the point."""
    hits = [s for s in shapes if _inside(x, y, s["rings"])]
    return min(hits, key=lambda s: s["area_mm2"]) if hits else None


def main():
    import anthropic
    import httpx2 as httpx
    import uvicorn

    from stitchpilot import ai
    from stitchpilot.web import jobs, server

    port = (lambda s: (s.bind(("127.0.0.1", 0)), s.getsockname()[1], s.close())[1])(socket.socket())
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def call(path, body=None, headers=None, raw=False):
        data, h = None, dict(headers or {})
        if isinstance(body, (dict, list)):
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        elif isinstance(body, bytes):
            data = body
        req = urllib.request.Request(base + path, data=data, headers=h, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=200) as r:
                out = r.read()
                return r.status, (out if raw else json.loads(out)), r.headers
        except urllib.error.HTTPError as e:
            out = e.read()
            try:
                return e.code, json.loads(out), e.headers
            except ValueError:
                return e.code, out, e.headers

    def upload(name, data):
        b = uuid.uuid4().hex
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
        return call("/api/upload", body, {"Content-Type": f"multipart/form-data; boundary={b}"})

    for _ in range(100):  # wait for start-up (it lists the fonts in a worker first)
        try:
            if call("/api/health")[0] == 200:
                break
        except OSError:
            pass
        time.sleep(0.3)

    # ---- the page and its menus
    status, html, headers = call("/", raw=True)
    assert status == 200 and b"StitchPilot" in html and b"/static/app.js" in html
    assert "script-src 'self'" in headers["content-security-policy"] and headers["x-frame-options"] == "DENY"
    for f in ("app.js", "style.css", "icon.png"):
        assert call(f"/static/{f}", raw=True)[0] == 200, f
    _, opt, _ = call("/api/options")
    assert "Arial" in opt["fonts"] or len(opt["fonts"]) > 5, opt["fonts"][:5]
    assert set(opt["patterns"]) >= {"hearts", "cross"} and "circle" in opt["frames"]
    state = dict(opt["defaults"])

    # ---- picture in, stitches out
    t = time.time()
    status, up, _ = upload("logo.jpg", (here / "samples" / "logo.jpg").read_bytes())
    assert status == 200 and not up["project"] and up["width"] == 800, up
    status, r, _ = call("/api/convert", {"image_id": up["image_id"], "state": state})
    assert status == 200, r
    print(f"converted in {time.time() - t:.1f}s: {r['stitches']} stitches, {r['colours']} colours, "
          f"{r['trims']} trims, {r['shapes']} shapes")
    assert r["preview"].startswith("data:image/png;base64,") and r["colours"] == 3 and r["shapes"] >= 3
    assert len(r["shape_list"]) == r["shapes"] and all(sh["rings"] for sh in r["shape_list"])

    # a recolour reuses the digitized design (fast), a size change re-digitizes
    yellow = next(th for th in r["threads"] if th["source"][0] > 200 and th["source"][2] < 120)
    t = time.time()
    _, r2, _ = call("/api/convert", {"image_id": up["image_id"], "state": {**state, "overrides": [[yellow["source"], [120, 200, 120]]]}})
    assert any(th["rgb"] == [120, 200, 120] for th in r2["threads"]) and r2["stitches"] == r["stitches"]
    print(f"recolour: {time.time() - t:.2f}s")

    # ---- click a shape (the star, in the middle): the page picks it from the outlines it was sent
    star = shape_at(r["shape_list"], 47.5, 47.5)
    assert star and star["rgb"][0] > 150 and star["kind"] == "fill", star
    # lettering and appliqué first (they change the layout), then the hearts on the star
    state2 = {**state, "applique": [yellow["source"]],
              "texts": [{"text": "Robot Club", "arc": 140, "height_mm": 9, "rgb": [220, 60, 120], "position": "above"},
                        {"text": "JMP", "kind": "monogram", "frame": "scallop", "height_mm": 20, "rgb": [30, 30, 90],
                         "position": "below"}]}
    _, r3, _ = call("/api/convert", {"image_id": up["image_id"], "state": state2})
    star = max((s for s in r3["shape_list"] if s["rgb"][0] > 150 and s["rgb"][1] < 90), key=lambda s: s["area_mm2"])
    edit = {"rgb": star["rgb"], "fx": star["fx"], "fy": star["fy"], "kind": "fill", "angle": None, "density": 1,
            "order": 0, "pattern": "hearts"}
    state2["shape_edits"] = [edit]
    _, r3, _ = call("/api/convert", {"image_id": up["image_id"], "state": state2})
    print(f"hearts + appliqué + lettering: {r3['stitches']} stitches, {r3['colours']} colours, {r3['stops']} stops")
    assert r3["stops"] == 2 and r3["applique_pieces"] == 1 and r3["colours"] == 5
    # macOS draws fonts a little differently, so a monogram letter right at satin's width limit may be filled
    found = [t for t in r3["threads"] if t["kinds"].get("satin", 0) >= 2]
    assert len(found) >= 2, ("lettering and the monogram frame sew as satin",
                             [(t["rgb"], t["kinds"]) for t in r3["threads"]])
    sh3 = shape_at(r3["shape_list"], *star["centre"])
    assert sh3["pattern"] == "hearts" and sh3["angle"] is None and sh3["edit"]["pattern"] == "hearts", sh3

    # ---- every download (the settings travel with the request: the server keeps no per-visitor state)
    job = {"image_id": up["image_id"], "state": state2, "name": "../../evil name"}
    for kind, extra, magic in (("embroidery", {"ext": "pes"}, b"#PES"), ("embroidery", {"ext": "dst"}, b"LA:"),
                               ("embroidery", {"ext": "exp"}, None), ("cut", {"ext": "svg"}, b"<?xml"),
                               ("cut", {"ext": "dxf"}, b"0"), ("template", {"paper": "A4"}, b"%PDF"),
                               ("preview", {}, b"\x89PNG"), ("project", {}, b"PK")):
        status, data, h = call("/api/download", {**job, "kind": kind, **extra}, raw=True)
        assert status == 200 and len(data) > 200, (kind, extra, status, data[:200])
        assert magic is None or data.startswith(magic), (kind, data[:10])
        assert "/" not in h["content-disposition"] and ".." not in h["content-disposition"], h["content-disposition"]
        if kind == "project":
            project = data
    with zipfile.ZipFile(io.BytesIO(project)) as z:
        doc = json.loads(z.read("project.json"))
        assert doc["state"]["shape_edits"][0]["pattern"] == "hearts" and z.namelist().count("source/logo.jpg") == 1

    # ---- the project opens again, with everything (the desktop app reads the same file)
    status, up2, _ = upload("logo.stitchpilot", project)
    assert status == 200 and up2["project"] and up2["image_id"] == up["image_id"], up2
    assert up2["state"]["texts"][1]["kind"] == "monogram" and up2["state"]["applique"]
    _, r4, _ = call("/api/convert", {"image_id": up2["image_id"], "state": up2["state"]})
    assert r4["stitches"] == r3["stitches"]
    from stitchpilot import project as proj

    p = Path(os.environ.get("TEMP", "/tmp")) / "web_round_trip.stitchpilot"
    p.write_bytes(project)
    assert proj.load(p).state["shape_edits"][0]["pattern"] == "hearts", "the desktop loader reads it"

    # ---- lettering preview, lettering-only designs
    status, png, _ = call("/api/text-preview", {"item": {"text": "AB", "kind": "monogram", "frame": "diamond"}}, raw=True)
    assert status == 200 and png.startswith(b"\x89PNG")
    _, r5, _ = call("/api/convert", {"image_id": None, "state": {**state, "texts": [{"text": "Hello", "arc": -90}]}})
    assert r5["stitches"] > 500 and r5["colours"] == 1

    # ---- what a public site must refuse
    assert upload("evil.png", b"\x00not a picture")[0] == 400
    try:  # refused from its size alone: the server may hang up before the upload has finished sending
        assert upload("big.png", b"0" * (21 * 1024 * 1024))[0] == 413
    except (ConnectionError, urllib.error.URLError):
        pass
    assert call("/api/convert", b"x" * 300_000, {"Content-Type": "application/json"})[0] == 413
    assert call("/api/convert", {"image_id": "nope", "state": state})[1]["error"] == "upload-again"
    assert call("/api/convert", {"image_id": None, "state": state})[0] == 400, "nothing to stitch"
    assert call("/api/download", {**job, "kind": "embroidery", "ext": "exe"})[0] == 400
    assert call("/api/download", {**job, "kind": "embroidery", "image_id": "gone"})[1]["error"] == "upload-again"
    assert call("/api/convert", {"image_id": up["image_id"], "state": {"texts": "nonsense", "shape_edits": [1]}})[0] == 400
    assert call("/api/convert", {"image_id": up["image_id"], "state": {**state, "max_w": 1e9, "max_h": "x"}})[0] == 200
    status, err, _ = call("/api/ai/settings", {"image_id": up["image_id"], "state": state})
    assert status == 400 and "API key" in err["error"]

    # ---- AI with a mocked Claude: shapes (answered in the server process), settings and text (worker jobs,
    # run here directly so the mock applies)
    real = anthropic.Anthropic

    def reply(payload):
        def handler(request):
            body = json.loads(request.content)
            text = payload(body) if callable(payload) else payload
            return httpx.Response(200, json={
                "id": "msg_test", "type": "message", "role": "assistant", "model": ai.MODEL,
                "content": [{"type": "text", "text": json.dumps(text)}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 1000, "output_tokens": 200}})
        client = real(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        return lambda *a, **k: client  # stands in for anthropic.Anthropic(...)

    anthropic.Anthropic = reply({"summary": "Rows along the star.", "suggestions": [
        {"shape": 1, "stitch": "satin", "angle": None, "reason": "A smooth ring."}]})
    status, sug, _ = call("/api/ai/shapes", {"image_id": up["image_id"], "state": state}, {"X-Claude-Key": "sk-test"})
    assert status == 200 and sug["suggestions"][0]["stitch"] == "satin" and "fx" in sug["suggestions"][0], sug
    anthropic.Anthropic = reply({
        "subject": "A star badge", "design_type": "logo", "background": "plain", "background_hex": "#ffffff",
        "background_inside_is_background": True, "detail": 0.6, "min_width_mm": 40, "suitability": "great",
        "thread_colors": [{"hex": "#143c8c", "name": "Blue", "role": "main"}, {"hex": "#fac828", "name": "Gold", "role": "main"},
                          {"hex": "#c81e28", "name": "Red", "role": "main"}], "tips": ["Use a cut-away stabilizer."]})
    jobs.init_worker()
    got = jobs.ai_settings((here / "samples" / "logo.jpg").read_bytes(), "logo.jpg", state, "sk-test")
    assert got["changes"]["colors"] == "ai" and len(got["changes"]["ai_palette"]) == 3 and got["subject"]
    anthropic.Anthropic = reply(lambda body: {"texts": [
        {"text": "STITCH", "left": 290, "top": 662, "right": 520, "bottom": 716, "style": "sans", "bold": True,
         "italic": False, "font_guess": "", "curved": False}]})
    rows = jobs.ai_text((here / "samples" / "logo.jpg").read_bytes(), "logo.jpg", "sk-test")
    anthropic.Anthropic = real
    assert rows[0]["fix"] and rows[0]["score"] > 0.5, rows
    print("AI routes ok:", sug["suggestions"][0]["stitch"], "|", got["subject"], "|", rows[0]["fix"]["font"],
          f"{rows[0]['score']:.0%}")
    _, r6, _ = call("/api/convert", {"image_id": up["image_id"], "state": {**state, "text_fixes": [rows[0]["fix"]]}})
    assert r6["stitches"] > 0

    # ---- rate limit: a visitor hammering the server gets "slow down"
    codes = [call("/api/text-preview", {"item": {"text": ""}}, raw=True)[0] for _ in range(130)]
    assert codes.count(429) >= 5, codes[-10:]
    assert re.match(r"^\d", str(port))
    srv.should_exit = True
    print("web ok")


if __name__ == "__main__":  # the server's worker processes re-import this file
    main()
